"""Remeasure OCI closure and deterministic layer metadata without extraction."""
import io
import _imp
import importlib.util
import marshal
import posixpath
import re
import tarfile
from datetime import datetime, timezone
from types import CodeType
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


def tar_members(raw, limit, *, metadata=None, layer_policy=None):
    result, inventory, seen, total, end = {}, [], set(), 0, 0
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode='r:') as archive:
            for member in archive:
                path = relative_path(member.name.rstrip('/') if member.isdir() else member.name)
                require(path not in seen and len(seen) < 50000, 'tar_duplicate_or_count')
                seen.add(path)
                require(not member.issparse() and not member.pax_headers and
                        (member.isfile() or member.isdir() or member.issym()),
                        'tar_nonregular_or_extended')
                require(member.size <= limit and total + member.size <= limit,
                        'tar_byte_bound')
                require(not member.uname and not member.gname and (member.issym() or not member.linkname),
                        'tar_identity_or_alias')
                total += member.size
                end = max(end, member.offset_data + ((member.size + 511)//512)*512)
                if member.isfile():
                    stream = archive.extractfile(member)
                    require(stream is not None, 'tar_missing_member')
                    body = stream.read(member.size + 1)
                    require(len(body) == member.size, 'tar_truncated')
                    result[path] = body
                elif member.isdir():
                    require(member.size == 0 and (not metadata or layer_policy['allow_directories']), 'layer_directory_policy')
                    body = b''
                else:
                    require(metadata is not None and layer_policy['symlinks'].get(path) == member.linkname and member.size == 0,
                            'layer_symlink_policy')
                    destination = posixpath.normpath(posixpath.join(posixpath.dirname(path),member.linkname))
                    relative_path(destination)
                    body = member.linkname.encode('utf-8')
                    result[path] = body
                if metadata:
                    require(member.uid == metadata['uid'] and member.gid == metadata['gid'] and
                            member.mtime == metadata['mtime_epoch'] and member.mode in ((511,) if member.issym() else (493,) if member.isdir() else (420,493)),
                            'layer_metadata_policy')
                record=dict(path=path, bytes=len(body), sha256=sha256(body),mode=member.mode,
                            uid=member.uid,gid=member.gid,mtime=member.mtime)
                if member.isdir():record['kind']='directory'
                if member.issym():record.update(kind='symlink',link_target=member.linkname)
                inventory.append(record)
        require(len(raw) % 512 == 0 and len(raw) - end >= 1024 and not any(raw[end:]), 'tar_unlisted_trailing_bytes')
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


def inspect_oci(raw_archive, output_records, output_bytes, oci, value, *, module_policy=None):
    limit = value['resources']['disk_bytes']
    module_policy = module_policy or {'oci_config':{},'history':[], 'layer_policy':{'allow_replacements':False,'allow_directories':False,'symlinks':{}}}
    members, outer_inventory = tar_members(raw_archive, limit)
    require(all(row.get('kind') != 'directory' or row['path'] in ('blobs','blobs/sha256') for row in outer_inventory), 'oci_unknown_directory')
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
    require(set(config) <= {'architecture','os','rootfs','config','created','history'}, 'oci_unknown_config_metadata')
    epoch = datetime.fromtimestamp(value['determinism']['epoch'],timezone.utc).isoformat(timespec='seconds').replace('+00:00','Z')
    history = config.get('history',[])
    require(('created' not in config or config['created'] == epoch) and isinstance(history,list) and
            all(isinstance(row,dict) and row.get('created') == epoch for row in history), 'oci_clock_policy')
    require([{k:v for k,v in row.items() if k != 'created'} for row in history] == module_policy['history'], 'oci_history_policy')
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
    files, contents = {}, {}
    for key in layers:
        bodies, inventory = tar_members(blobs[key], limit, metadata=value['determinism']['file_metadata'],layer_policy=module_policy['layer_policy'])
        for record in inventory:
            path = record['path']
            # This closed policy denies generated accounts/logs/caches and aliases.
            require(not path.startswith(('var/log/', 'var/cache/', 'tmp/', 'root/', 'home/')) and
                    path not in ('etc/passwd', 'etc/group', 'etc/shadow', 'etc/gshadow') and
                    not path.endswith('.pyo') and ('/__pycache__/' not in '/' + path or path.endswith('.pyc')),
                    'generated_account_log_cache')
            require(path not in files or (module_policy['layer_policy']['allow_replacements'] and
                    record.get('kind','regular') == files[path].get('kind','regular')), 'layer_overwrite_or_whiteout')
            require(not any(p.startswith('.wh.') for p in path.split('/')), 'layer_whiteout')
            files[path] = record
        contents.update(bodies)
    for path, record in files.items():
        if record.get('kind') == 'symlink':
            destination=posixpath.normpath(posixpath.join(posixpath.dirname(path),record['link_target']))
            require(destination in files and files[destination].get('kind','regular') == 'regular', 'layer_symlink_target')
        elif path.endswith('.pyc'):
            _check_pyc(path,contents[path],contents,value)
    require(config.get('config') == module_policy['oci_config'], 'unreviewed_runtime_config_or_labels')
    inventory = [files[path] for path in sorted(files)]
    return tuple(sorted(closure)), inventory


def _check_pyc(path, raw, contents, value):
    policy=value['determinism']['python']
    require(policy['applicable'] and len(raw) <= 1024*1024 and len(raw) >= 16 and
            int.from_bytes(raw[4:8],'little') == 3, 'python_checked_hash_policy')
    magic={'3.12':bytes.fromhex('cb0d0d0a'),'3.13':bytes.fromhex('f30d0d0a'),'3.14':bytes.fromhex('2b0e0d0a')}
    version='.'.join(value['builder_requirements']['toolchain']['version'].split('.')[:2])
    require(raw[:4] == magic.get(version) and raw[:4] == importlib.util.MAGIC_NUMBER, 'python_verifier_compiler_unqualified')
    if '/__pycache__/' in path:
        prefix,name=path.rsplit('/__pycache__/',1)
        suffix='.cpython-'+version.replace('.','')+('.opt-'+str(policy['optimization']) if policy['optimization'] else '')+'.pyc'
        require(name.endswith(suffix), 'python_cache_name_policy')
        source=prefix+'/'+name[:-len(suffix)]+'.py'
    else:
        source=path[:-1]
    require(source in contents and raw[8:16] == _imp.source_hash(int.from_bytes(raw[:4],'little'),contents[source]), 'python_source_hash_policy')
    filename='/'+source
    require(filename.startswith(policy['final_root'].rstrip('/')+'/'), 'python_final_root_policy')
    try:
        stream=io.BytesIO(raw[16:]);code=marshal.load(stream)
        require(not stream.read(1) and isinstance(code,CodeType), 'python_bytecode_shape')
        def filenames(current):
            require(current.co_filename == filename, 'python_final_filename_policy')
            for constant in current.co_consts:
                if isinstance(constant,CodeType):filenames(constant)
        filenames(code)
        expected=compile(contents[source],filename,'exec',dont_inherit=True,optimize=policy['optimization'])
        require(code == expected, 'python_compilation_policy')
    except (EOFError, ValueError, TypeError, SyntaxError, RecursionError) as error:
        if isinstance(error,ReceiptRefusal):raise
        raise ReceiptRefusal('python_bytecode_shape') from error
