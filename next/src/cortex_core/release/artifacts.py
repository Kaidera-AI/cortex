"""Remeasure OCI closure and deterministic layer metadata without extraction."""
import io
import re
import tarfile
from .codec import canonical_bytes, decode_json, relative_path, sha256
from .contracts import ReceiptRefusal

INDEX = 'application/vnd.oci.image.index.v1+json'
MANIFEST = 'application/vnd.oci.image.manifest.v1+json'
CONFIG = 'application/vnd.oci.image.config.v1+json'
LAYER = 'application/vnd.oci.image.layer.v1.tar'


def require(condition, reason):
    if not condition:
        raise ReceiptRefusal(reason)


def measure(record, reader, limit):
    relative_path(record['path'])
    require(record['bytes'] <= limit, 'artifact_policy_bound')
    try:
        raw = reader(record['path'])
    except (OSError, KeyError, ValueError) as error:
        raise ReceiptRefusal('artifact_unavailable') from error
    require(type(raw) is bytes and len(raw) == record['bytes'] and sha256(raw) == record['sha256'], 'artifact_bytes_mismatch')
    return raw


def tar_members(raw, limit, *, metadata=None):
    result, inventory = {}, []
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode='r:') as archive:
            for member in archive:
                relative_path(member.name)
                require(member.name not in result and len(result) < 50000, 'tar_duplicate_or_count')
                require(member.isfile() and not member.issparse() and not member.pax_headers,
                        'tar_nonregular_or_extended')
                require(member.size <= limit and sum(len(v) for v in result.values()) + member.size <= limit,
                        'tar_byte_bound')
                require(not member.uname and not member.gname and not member.linkname,
                        'tar_identity_or_alias')
                stream = archive.extractfile(member)
                require(stream is not None, 'tar_missing_member')
                body = stream.read(member.size + 1)
                require(len(body) == member.size, 'tar_truncated')
                result[member.name] = body
                if metadata:
                    require(member.uid == metadata['uid'] and member.gid == metadata['gid'] and
                            member.mtime == metadata['mtime_epoch'] and member.mode in (420, 493),
                            'layer_metadata_policy')
                inventory.append(dict(path=member.name, bytes=len(body), sha256=sha256(body),
                                      mode=member.mode, uid=member.uid, gid=member.gid, mtime=member.mtime))
        require(bool(result), 'empty_tar')
        return result, sorted(inventory, key=lambda r: r['path'])
    except (tarfile.TarError, EOFError, OSError) as error:
        raise ReceiptRefusal('invalid_uncompressed_tar') from error


def _descriptor(desc, media, blobs):
    require(isinstance(desc, dict) and desc.get('mediaType') == media and
            isinstance(desc.get('digest'), str) and re.fullmatch('sha256:[0-9a-f]{64}', desc['digest']) and
            type(desc.get('size')) is int, 'oci_descriptor_shape')
    key = desc['digest'][7:]
    require(key in blobs and len(blobs[key]) == desc['size'] and sha256(blobs[key]) == key,
            'oci_descriptor_closure')
    return key


def inspect_oci(raw_archive, output_records, output_bytes, oci, value):
    limit = value['resources']['disk_bytes']
    members, _ = tar_members(raw_archive, limit)
    require('oci-layout' in members and 'index.json' in members, 'oci_layout_missing')
    require(decode_json(members['oci-layout'], 1024, canonical=False) == {'imageLayoutVersion': '1.0.0'},
            'oci_layout_version')
    blob_paths = {path for path in members if path not in ('oci-layout', 'index.json')}
    require(all(re.fullmatch(r'blobs/sha256/[0-9a-f]{64}', path) for path in blob_paths), 'oci_unknown_member')
    blobs = {path[13:]: members[path] for path in blob_paths}
    require(all(sha256(body) == key for key, body in blobs.items()), 'oci_blob_name_hash')
    index = decode_json(members['index.json'], 8 * 1024 * 1024, canonical=False)
    require(isinstance(index, dict) and type(index.get('schemaVersion')) is int and index['schemaVersion'] == 2 and
            index.get('mediaType') == INDEX and isinstance(index.get('manifests'), list) and len(index['manifests']) == 1,
            'oci_index_shape')
    platform = value['target']['oci_platform']
    desc = index['manifests'][0]
    require(desc.get('platform') == {'os': 'linux', 'architecture': platform.split('/')[1]}, 'oci_platform')
    manifest_key = _descriptor(desc, MANIFEST, blobs)
    manifest = decode_json(blobs[manifest_key], 8 * 1024 * 1024, canonical=False)
    require(isinstance(manifest, dict) and type(manifest.get('schemaVersion')) is int and manifest['schemaVersion'] == 2 and
            manifest.get('mediaType') == MANIFEST and isinstance(manifest.get('layers'), list) and
            1 <= len(manifest['layers']) <= 50000, 'oci_manifest_shape')
    config_key = _descriptor(manifest.get('config'), CONFIG, blobs)
    config = decode_json(blobs[config_key], 8 * 1024 * 1024, canonical=False)
    require(isinstance(config, dict) and config.get('os') == 'linux' and
            config.get('architecture') == platform.split('/')[1], 'oci_config_platform')
    layers = [_descriptor(desc, LAYER, blobs) for desc in manifest['layers']]
    require(len(layers) == len(set(layers)), 'oci_duplicate_layer')
    require(config.get('rootfs') == {'type': 'layers', 'diff_ids': ['sha256:' + key for key in layers]},
            'oci_diff_ids')
    closure = set(layers + [manifest_key, config_key])
    require(set(blobs) == closure and oci == {'index': sha256(members['index.json']), 'manifest': manifest_key,
            'config': config_key, 'blobs': sorted(closure), 'platform': platform}, 'oci_declared_closure')
    roles = {r['role']: r for r in output_records}
    expected = {'index': (INDEX, members['index.json']), 'manifest': (MANIFEST, blobs[manifest_key]),
                'config': (CONFIG, blobs[config_key]), **{'blob.' + key: ('application/octet-stream', blobs[key]) for key in layers}}
    require(set(roles) == set(expected) | {'archive', 'sbom', 'provenance'}, 'output_role_closure')
    for role, (media, body) in expected.items():
        record = roles[role]
        require(record['media_type'] == media and output_bytes[record['path']] == body, 'oci_inventory_body')
    files = {}
    for key in layers:
        contents, inventory = tar_members(blobs[key], limit, metadata=value['determinism']['file_metadata'])
        for record in inventory:
            path = record['path']
            # This closed policy denies generated accounts/logs/caches and aliases.
            require(not path.startswith(('var/log/', 'var/cache/', 'tmp/', 'root/', 'home/')) and
                    path not in ('etc/passwd', 'etc/group', 'etc/shadow', 'etc/gshadow') and
                    '/__pycache__/' not in '/' + path and not path.endswith(('.pyc', '.pyo')),
                    'generated_account_log_cache')
            require(path not in files, 'layer_overwrite_or_whiteout')
            require(not any(p.startswith('.wh.') for p in path.split('/')), 'layer_whiteout')
            files[path] = record
    require(config.get('config') == {}, 'unreviewed_runtime_config_or_labels')
    inventory = [files[path] for path in sorted(files)]
    return tuple(sorted(closure)), inventory
