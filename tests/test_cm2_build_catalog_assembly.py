"""Actual public artifact bytes; source assembly only, no native qualification."""
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import stat
import tarfile

import pytest
from test_cm2_build_catalog_rehearsal import fixture, load

ROOT = Path(__file__).resolve().parents[1]
SOURCE = '1' * 40
VERSION = '0.2.001-test.20261006.1'
MESSAGE = 'native build catalog missing or mismatched'
CASES = ['valid', 'missing', 'replay', 'digest', 'source', 'payload', 'ledger', 'nonbool',
         'schema', 'roles', 'image-id', 'target', 'version', 'status', 'scope', 'extra',
         'podman', 'network', 'initial-status', 'replay-status', 'smoke', 'restart',
         'smoke-source', 'smoke-version', 'smoke-checks', 'smoke-uuid', 'smoke-epoch',
         'cleanup', 'cleanup-id', 'cleanup-namespace', 'cleanup-source', 'cleanup-version',
         'cleanup-missing', 'cleanup-extra', 'cleanup-duplicate', 'cleanup-foreign',
         'cleanup-retained', 'cleanup-root', 'epoch', 'bool-source']
ASSEMBLY_CASES = ['linux', 'mac', 'missing-catalog', 'bad-replay', 'duplicate-json',
                  'linked-json', 'fifo-json', 'oversized-json', 'absent-role', 'archive-path',
                  'source-label', 'version-label', 'class-label', 'layer', 'existing']
OCI_CASES = ['valid', 'missing', 'source', 'version', 'class', 'architecture', 'layer']


def required(monkeypatch):
    module = load('build-candidate.py', monkeypatch)
    assert callable(getattr(module, 'linux_catalog_bytes', None)), 'Linux assembly catalog binding missing'
    return module


def observation(tmp_path, monkeypatch):
    module = required(monkeypatch)
    source, migrations, initial = fixture(tmp_path)
    installer = load('install_candidate.py', monkeypatch)
    installation = '10000000-0000-4000-8000-000000000001'
    record = {'installation': installation}
    entries = {role: {'config_id': 'sha256:' + str(index) * 64}
               for index, role in enumerate(installer.ROLES, 1)}
    smoke = {'status': 'PASS', 'scope': 'TEST package smoke only', 'source_sha': SOURCE,
             'version': VERSION, 'checks': ['readiness', 'unauthenticated refusal', 'synthetic write/read'],
             'record_id': '20000000-0000-4000-8000-000000000002', 'epoch': 1}
    replay = copy.deepcopy(initial); replay['status'] = 'verified'
    raw = json.dumps(initial['build_catalog'], sort_keys=True, separators=(',', ':')).encode() + b'\n'
    value = {'scope': 'native Linux amd64 CI; not installed product qualification',
             'target': 'linux-x86_64', 'source_sha': SOURCE, 'version': VERSION, 'podman': '6.1.3',
             'image_ids': {role: entry['config_id'] for role, entry in entries.items()},
             'migration': initial, 'migration_replay': replay, 'build_catalog': initial['build_catalog'],
             'build_catalog_sha256': hashlib.sha256(raw).hexdigest(),
             'internal_network_loopback_publish': 'PASS', 'smoke': smoke,
             'restart_smoke': copy.deepcopy(smoke), 'status': 'PASS', 'epoch': 1,
             'cleanup': {'status': 'erased', 'installation': installation,
                         'namespace': installer.namespace(record), 'source_sha': SOURCE, 'version': VERSION,
                         'removed': [{'kind': kind, 'name': name} for kind, name in installer.names(record)],
                         'retained_shared_images': [], 'root': '/home/runner/.cortex/test/ci-rehearsal-' + installation}}
    monkeypatch.setattr(module, 'ROOT', tmp_path)
    return module, entries, value, raw


@pytest.mark.parametrize('case', CASES)
def test_linux_assembly_binds_complete_public_rehearsal_to_actual_payload(case, tmp_path, monkeypatch):
    module, entries, value, raw = observation(tmp_path, monkeypatch)
    revision = SOURCE
    if case == 'missing': value.pop('build_catalog')
    if case == 'replay': value['migration_replay']['build_catalog']['relations'][1]['app_direct_grant'] = False
    if case == 'digest': value['build_catalog_sha256'] = 'f' * 64
    if case == 'source': value['build_catalog']['source_revision'] = 'f' * 40
    if case == 'payload': value['build_catalog']['api_source_payload_sha256'] = 'f' * 64
    if case == 'ledger': value['migration']['migrations'][0]['checksum'] = 'f' * 64
    if case == 'nonbool': value['build_catalog']['relations'][1]['rls'] = 1
    if case == 'schema': value['build_catalog']['schema'] = 'foreign'
    if case == 'roles': entries.pop('graph')
    if case == 'image-id': value['image_ids']['api'] = 'sha256:' + 'f' * 64
    if case == 'target': value['target'] = 'macos-arm64'
    if case == 'version': value['version'] = 'foreign'
    if case == 'status': value['status'] = 'cached'
    if case == 'scope': value['scope'] = 'production'
    if case == 'extra': value['unexpected'] = True
    if case == 'podman': value['podman'] = '5.8.2'
    if case == 'network': value['internal_network_loopback_publish'] = 'FAIL'
    if case == 'initial-status': value['migration']['status'] = 'verified'
    if case == 'replay-status': value['migration_replay']['status'] = 'applied'
    if case == 'smoke': value['smoke']['status'] = 'FAIL'
    if case == 'restart': value.pop('restart_smoke')
    if case == 'smoke-source': value['smoke']['source_sha'] = 'f' * 40
    if case == 'smoke-version': value['restart_smoke']['version'] = 'foreign'
    if case == 'smoke-checks': value['restart_smoke']['checks'].pop()
    if case == 'smoke-uuid': value['smoke']['record_id'] = 'invalid'
    if case == 'smoke-epoch': value['smoke']['epoch'] = True
    cleanup = value['cleanup']
    if case == 'cleanup': cleanup['status'] = 'retained'
    if case == 'cleanup-id': cleanup['installation'] = 'invalid'
    if case == 'cleanup-namespace': cleanup['namespace'] = 'foreign'
    if case == 'cleanup-source': cleanup['source_sha'] = 'f' * 40
    if case == 'cleanup-version': cleanup['version'] = 'foreign'
    if case == 'cleanup-missing': cleanup['removed'].pop()
    if case == 'cleanup-extra': cleanup['removed'].append({'kind': 'container', 'name': 'foreign'})
    if case == 'cleanup-duplicate': cleanup['removed'].append(copy.deepcopy(cleanup['removed'][0]))
    if case == 'cleanup-foreign': cleanup['removed'][0]['name'] = 'foreign'
    if case == 'cleanup-retained': cleanup['retained_shared_images'] = ['sha256:' + 'f' * 64]
    if case == 'cleanup-root': cleanup['root'] = 'relative'
    if case == 'epoch': value['epoch'] = True
    if case == 'bool-source': revision = True
    call = lambda: module.linux_catalog_bytes(value, entries, revision, VERSION)
    if case == 'valid': assert call() == raw
    else:
        with pytest.raises(RuntimeError) as error: call()
        assert str(error.value) == MESSAGE


def image(path, *, role, architecture='amd64', defect=None):
    labels = {'org.opencontainers.image.revision': SOURCE, 'org.opencontainers.image.version': VERSION,
              'com.kaidera.deployment-class': 'TEST'}
    if defect == 'missing': labels = {}
    if defect == 'source': labels['org.opencontainers.image.revision'] = 'f' * 40
    if defect == 'version': labels['org.opencontainers.image.version'] = 'foreign'
    if defect == 'class': labels['com.kaidera.deployment-class'] = 'PRODUCTION'
    config = json.dumps({'os': 'linux', 'architecture': architecture, 'config': {'Labels': labels},
                         'public_role': role}).encode()
    layer = ('PUBLIC LAYER ' + role).encode(); checksum = lambda raw: 'sha256:' + hashlib.sha256(raw).hexdigest()
    cid = checksum(config); lid = checksum(layer)
    manifest = json.dumps({'config': {'digest': cid}, 'layers': [{'digest': lid, 'size': len(layer)}]}).encode()
    mid = checksum(manifest)
    files = {'index.json': json.dumps({'manifests': [{'digest': mid}]}).encode(),
             'blobs/sha256/' + mid[7:]: manifest, 'blobs/sha256/' + cid[7:]: config,
             'blobs/sha256/' + lid[7:]: b'BROKEN' if defect == 'layer' else layer}
    with tarfile.open(path, 'w') as stream:
        for name, raw in files.items():
            member = tarfile.TarInfo(name); member.size = len(raw); stream.addfile(member, io.BytesIO(raw))
    return {'manifest_digest': mid, 'config_id': cid, 'os': 'linux', 'architecture': architecture,
            'layers': [lid], 'archive': 'images/' + role + '.oci.tar'}


@pytest.mark.parametrize('case', OCI_CASES)
def test_linux_actual_oci_config_labels_and_layers_bind_frozen_source(case, tmp_path, monkeypatch):
    module = required(monkeypatch); archive = tmp_path / 'public.oci.tar'
    entry = image(archive, role='api', architecture='arm64' if case == 'architecture' else 'amd64', defect=case)
    call = lambda: module.oci_identity(archive, architecture='amd64', source_sha=SOURCE, version=VERSION)
    if case == 'valid': assert call() == {key: value for key, value in entry.items() if key != 'archive'}
    else:
        with pytest.raises(RuntimeError): call()


@pytest.mark.parametrize('case', ASSEMBLY_CASES)
def test_actual_package_assembly_preflights_catalog_and_writes_exact_owned_inventory(case, tmp_path, monkeypatch):
    module, entries, value, raw = observation(tmp_path, monkeypatch)
    architecture = 'arm64' if case == 'mac' else 'amd64'
    images = tmp_path / 'image-stage'; images.mkdir(); (images / 'images').mkdir(); (images / 'sbom').mkdir()
    for role in entries:
        entries[role] = image(images / 'images' / (role + '.oci.tar'), role=role, architecture=architecture,
            defect={'source-label': 'source', 'version-label': 'version', 'class-label': 'class'}.get(case, case) if role == 'api' else None)
    value['image_ids'] = {r: entry['config_id'] for r, entry in entries.items()}
    target = 'macos-arm64' if case == 'mac' else 'linux-x86_64'; value['target'] = target
    if case == 'missing-catalog': value.pop('build_catalog')
    if case == 'bad-replay': value['migration_replay']['build_catalog']['relations'][1]['app_direct_grant'] = False
    if case == 'absent-role': entries.pop('graph'); value['image_ids'].pop('graph')
    if case == 'archive-path': entries['api']['archive'] = '../outside'
    inventory = {'source_sha': SOURCE, 'version': VERSION, 'target': target, 'images': entries, 'builder': {'public': True}}
    (images / 'image-inventory.json').write_text(json.dumps(inventory))
    receipt = images / 'rehearsal-receipt.json'; receipt.write_text(json.dumps(value))
    if case == 'duplicate-json': receipt.write_text('{"status":"PASS","status":"PASS"}')
    if case == 'linked-json':
        receipt.rename(images / 'actual.json'); receipt.symlink_to(images / 'actual.json')
    if case == 'fifo-json': receipt.unlink(); os.mkfifo(receipt, 0o600)
    if case == 'oversized-json': receipt.write_bytes(b' ' * 1048577)
    host = tmp_path / 'host-stage'; host.mkdir(); (host / 'bin').mkdir(); (host / 'licenses').mkdir()
    programs = {}
    for name in ['cortex-test', 'cortex-agent']:
        binary = host / 'bin' / name; binary.write_bytes(('PUBLIC HOST ' + name).encode())
        programs[name] = {'sha256': hashlib.sha256(binary.read_bytes()).hexdigest(), 'architecture': 'x86_64',
                          'help': 'PASS', 'maximum_glibc': '2.35', 'embedded_elf_count': 1}
    for name in ['cortex-projects', '_cortex_api.sh']:
        path = tmp_path / 'scripts/agent-shims' / name; path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / 'scripts/agent-shims' / name, path); shutil.copyfile(path, host / 'bin' / name)
    for name in ['host.spdx.json', 'host-archive-inventory.txt', 'agent-archive-inventory.txt']:
        (host / name).write_text('PUBLIC\n')
    native = {'source_sha': SOURCE, 'version': VERSION, 'target': target, 'maximum_glibc': '2.35', 'programs': programs}
    (host / 'host-inventory.json').write_text(json.dumps(native))
    for name in ['LICENSE', 'scripts/release/requirements-linux-host-build.txt', 'scripts/release/requirements-host-build.txt',
                 'docs/install/linux-test-candidate.md', 'docs/install/macos-test-candidate.md']:
        path = tmp_path / name; path.parent.mkdir(parents=True, exist_ok=True); shutil.copyfile(ROOT / name, path)
    shutil.copytree(ROOT / 'deploy/release/licenses', tmp_path / 'deploy/release/licenses')
    calls = []; monkeypatch.setattr(module, 'source_identity', lambda sha: calls.append(sha))
    if case == 'mac':
        monkeypatch.setattr(module, 'linux_catalog_bytes', lambda *args: pytest.fail('Mac called Linux catalog binder'))
    output = tmp_path / 'output'
    if case == 'existing': output.mkdir(); (output / 'retained').write_bytes(b'PUBLIC RETAINED')
    call = lambda: module.assemble(output, images, host, SOURCE, VERSION, target=target)
    if case in ('linux', 'mac'):
        call(); package = output / ('cortex-' + VERSION + '-' + target)
        manifest = json.loads((package / 'release.json').read_text())
        assert manifest['signature'] == 'unsigned TEST; not a signed customer release'
        if case == 'linux':
            path = package / 'native/rls-inventory.json'
            assert path.read_bytes() == raw
            assert stat.S_IMODE(path.stat().st_mode) == 0o600 and path.stat().st_uid == os.getuid()
            assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700 and not path.parent.is_symlink()
            assert manifest['files']['native/rls-inventory.json'] == hashlib.sha256(raw).hexdigest()
            assert manifest['images']['api']['source_payload_sha256'] == value['build_catalog']['api_source_payload_sha256']
            assert hashlib.sha256(raw).hexdigest() + '  native/rls-inventory.json\n' in (package / 'SHA256SUMS').read_text()
            with tarfile.open(output / (package.name + '.tar.gz')) as stream:
                member = stream.getmember(package.name + '/native/rls-inventory.json')
                assert member.mode == 0o600 and stream.extractfile(member).read() == raw
                assert stream.getmember(package.name + '/native').mode == 0o700
        else:
            assert not (package / 'native').exists()
            assert 'source_payload_sha256' not in manifest['images']['api']
    else:
        with pytest.raises(RuntimeError): call()
        if case == 'existing': assert sorted(p.name for p in output.iterdir()) == ['retained']
        else: assert not output.exists()
    assert calls == [SOURCE]
