"""R02-compatible inert producer: actual OCI bytes, fixture qualification only."""
import hashlib
import io
import tarfile
from fixture import build_input, canonical, digest

CLASSES = ['accounts', 'cache', 'clock', 'files', 'input', 'labels', 'logs',
           'python', 'runtime', 'serialization', 'xattrs-acls']
PHASES = ['input', 'parser', 'build', 'export', 'audit', 'cleanup']


def tar(members):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w', format=tarfile.USTAR_FORMAT) as archive:
        for path, raw in sorted(members.items()):
            member = tarfile.TarInfo(path)
            member.size = len(raw)
            member.mode = 420
            member.uid = member.gid = 1001
            member.mtime = 0
            archive.addfile(member, io.BytesIO(raw))
    return output.getvalue()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def receipt_fixture():
    value, inputs = build_input()
    outputs, evidence = {}, {}
    def artifact(storage, role, path, raw, media='application/json'):
        storage[path] = raw
        return dict(role=role, path=path, media_type=media, bytes=len(raw), sha256=sha(raw))
    layer = tar({'app/module.py': b'pass\n'})
    config = canonical({'architecture': 'amd64', 'os': 'linux', 'config': {},
                        'rootfs': {'type': 'layers', 'diff_ids': ['sha256:' + sha(layer)]}})
    def descriptor(media, raw):
        return {'mediaType': media, 'digest': 'sha256:' + sha(raw), 'size': len(raw)}
    manifest = canonical({'schemaVersion': 2, 'mediaType': 'application/vnd.oci.image.manifest.v1+json',
                          'config': descriptor('application/vnd.oci.image.config.v1+json', config),
                          'layers': [descriptor('application/vnd.oci.image.layer.v1.tar', layer)]})
    index = canonical({'schemaVersion': 2, 'mediaType': 'application/vnd.oci.image.index.v1+json',
                       'manifests': [dict(descriptor('application/vnd.oci.image.manifest.v1+json', manifest),
                                          platform={'os': 'linux', 'architecture': 'amd64'})]})
    blobs = {sha(raw): raw for raw in (manifest, config, layer)}
    archive = tar({'oci-layout': canonical({'imageLayoutVersion': '1.0.0'}), 'index.json': index,
                   **{'blobs/sha256/' + key: raw for key, raw in blobs.items()}})
    layer_inventory = [{'path': 'app/module.py', 'bytes': 5, 'sha256': sha(b'pass\n'),
                        'mode': 420, 'uid': 1001, 'gid': 1001, 'mtime': 0}]
    builds, reports, all_evidence = {}, {}, []
    source_sha = digest(value['source'])
    recipe_sha = digest(value['recipe'])
    dependency_sha = digest(value['dependencies'])
    for slot in ('A', 'B'):
        identity = 'fixture-' + slot
        observed = {'qualification_scope': 'fixture', 'os': 'linux', 'architecture': 'amd64',
                    'kernel': 'fixture-kernel', 'uid': 1001, 'rootless': True,
                    'toolchain': value['builder_requirements']['toolchain'],
                    'engine': value['builder_requirements']['engine'],
                    **{key: identity + '-' + key for key in ('build_id', 'execution_id', 'store_id', 'runroot_id', 'container_id', 'source_id')},
                    'fresh_store': True, 'mutable_shared': False}
        observed['build_id'] = identity
        operations = []
        for phase in PHASES:
            op = {'phase': phase, 'argv': ['fixture', phase], 'environment_names': ['PYTHONHASHSEED'],
                  'environment_policy_sha256': 'e' * 64, 'status': 'exited', 'exit_code': 0,
                  'started_at_unix_ns': PHASES.index(phase) * 1000,
                  'finished_at_unix_ns': (PHASES.index(phase) + 1) * 1000}
            raw_record = dict(op, build_id=identity, qualification_scope='fixture')
            record = artifact(evidence, slot + '.operation.' + phase,
                              'evidence/' + slot + '/' + phase + '.json', canonical(raw_record))
            operations.append(dict(op, evidence=record))
            all_evidence.append(record)
        build_outputs = [artifact(outputs, 'archive', slot + '/image.tar', archive, 'application/x-tar'),
                         artifact(outputs, 'index', slot + '/index.json', index, 'application/vnd.oci.image.index.v1+json'),
                         artifact(outputs, 'manifest', slot + '/manifest.json', manifest, 'application/vnd.oci.image.manifest.v1+json'),
                         artifact(outputs, 'config', slot + '/config.json', config, 'application/vnd.oci.image.config.v1+json'),
                         artifact(outputs, 'blob.' + sha(layer), slot + '/layer.tar', layer, 'application/octet-stream')]
        provenance = {'build_id': identity, 'input_sha256': digest(value), 'output_sha256': sha(archive),
                      'source_sha256': source_sha, 'recipe_sha256': recipe_sha, 'dependency_sha256': dependency_sha,
                      'policy_sha256': 'e' * 64, 'observed_builder': observed, 'qualification_scope': 'fixture'}
        provenance_record = artifact(outputs, 'provenance', slot + '/provenance.json', canonical(provenance))
        build_outputs += [provenance_record, artifact(outputs, 'sbom', slot + '/sbom.json',
                              canonical({'output_sha256': sha(archive), 'inventory_sha256': digest(layer_inventory)}))]
        build_reports = {}
        for category in CLASSES:
            record = artifact(evidence, slot + '.audit.' + category, 'evidence/' + slot + '/audit-' + category + '.json',
                              canonical({'build_id': identity, 'class': category, 'output_sha256': sha(archive),
                                         'inventory_sha256': digest(layer_inventory), 'findings': []}))
            build_reports[category] = record
            all_evidence.append(record)
        reports[slot] = build_reports
        custody_record = artifact(evidence, slot + '.custody', 'evidence/' + slot + '/custody.json',
                                   canonical({'build_id': identity, 'outputs': sorted(build_outputs, key=lambda r: r['path']), 'complete': True}))
        cleanup_record = artifact(evidence, slot + '.cleanup', 'evidence/' + slot + '/cleanup.json',
                                   canonical({'build_id': identity, 'removed_resources': [observed[k] for k in ('execution_id', 'store_id', 'runroot_id', 'container_id', 'source_id')], 'remaining_owned_resources': [], 'complete': True}))
        all_evidence += [custody_record, cleanup_record]
        builds[slot] = {'build_id': identity, 'observed_builder': observed, 'source_sha256': source_sha,
                        'recipe_sha256': recipe_sha, 'dependency_sha256': dependency_sha,
                        'started_at_unix_ns': 0, 'finished_at_unix_ns': 6000, 'operations': operations,
                        'outputs': sorted(build_outputs, key=lambda r: r['path']),
                        'oci': {'index': sha(index), 'manifest': sha(manifest), 'config': sha(config),
                                'blobs': sorted(blobs), 'platform': 'linux/amd64'},
                        'provenance': provenance_record, 'custody': custody_record, 'cleanup': cleanup_record}
    cleanup = artifact(evidence, 'cleanup', 'evidence/cleanup.json', canonical({'build_ids': [builds[s]['build_id'] for s in ('A', 'B')], 'complete': True, 'remaining_owned_resources': []}))
    all_evidence.append(cleanup)
    custody = artifact(evidence, 'custody', 'evidence/custody.json', canonical({'complete': True, 'partial': False, 'missing_roles': [], 'outputs': sorted([r for s in ('A', 'B') for r in builds[s]['outputs']], key=lambda r:r['path'])}))
    all_evidence.append(custody)
    all_evidence.sort(key=lambda r:r['path'])
    index_record = artifact(evidence, 'evidence-index', 'evidence/index.json', canonical({'inventory': all_evidence}))
    receipt = {'schema': 'cortex.build-receipt.v1', 'format_version': 1, 'input_sha256': digest(value),
               'target': value['target'], 'qualification_scope': 'fixture', 'result': 'PASS', 'reason': 'complete',
               'builds': builds, 'comparison': {'raw_archives_equal': True, 'raw_archive_sha256': sha(archive), 'raw_archive_bytes': len(archive), 'oci_closure_equal': True, 'semantic_audit_equal': True},
               'audit': {'policy_id': 'fixture-policy', 'policy_sha256': 'e' * 64, 'required_classes': CLASSES, 'reports': reports, 'findings': []},
               'evidence': {'inventory': all_evidence, 'index': index_record},
               'custody': {'complete': True, 'partial': False, 'missing_roles': [], 'receipt': custody},
               'cleanup': {'complete': True, 'receipt': cleanup, 'remaining_owned_resources': []}}
    policy = {'input_sha256': digest(value), 'policy_sha256': 'e' * 64, 'qualification_scope': 'fixture', 'required_classes': CLASSES}
    return value, inputs, receipt, outputs, evidence, policy
