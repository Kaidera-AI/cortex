"""CM2 actual lifecycle records bind verified package bytes before host effects."""
import hashlib
import importlib
import json
import os
import time
import uuid

import pytest

from test_cm2_linux_installed_custody import fixture as installed_fixture

INSTALLATION = '11111111-2222-4333-8444-555555555555'
VERSION = '0.2.001-test.20261006.1'
CASES = ['valid', 'schema', 'stage', 'installation', 'namespace', 'source', 'version',
         'inner-version', 'connection', 'port-bool', 'port-protected', 'loaded-missing',
         'loaded-duplicate', 'loaded-foreign', 'extra-field', 'linked', 'hardlink',
         'mode', 'duplicate-json', 'overflow', 'record-replaced', 'inner-replaced',
         'deadline', 'signature-refused']


def fixture(tmp_path, monkeypatch):
    custody, root, package, release, manifest, archive, calls = installed_fixture(tmp_path, monkeypatch)
    module = importlib.import_module('cortex_v2.clients.linux_provisioning')
    release['release_id'] = 'v' + VERSION
    inner_path = package / 'release.json'
    inner = json.loads(inner_path.read_bytes()); inner['version'] = VERSION
    inner_path.write_bytes(json.dumps(inner, sort_keys=True).encode() + b'\n')
    release['payload_manifest_sha256'] = hashlib.sha256(inner_path.read_bytes()).hexdigest()
    manifest.write_text(json.dumps(release)); manifest.chmod(0o600)
    record = {'schema': 'cortex.test-install.v1', 'version': VERSION,
              'source_sha': release['source_revision'], 'installation': INSTALLATION,
              'connection': None, 'port': 18602, 'stage': 'ready',
              'loaded_images': [release['images'][role]['config_id'] for role in custody.ROLES],
              'namespace': 'cortex_v2_package_test_' + uuid.UUID(INSTALLATION).hex,
              'service_label': 'ai.kaidera.cortex.TEST-v2.' + uuid.UUID(INSTALLATION).hex}
    path = root / 'install.json'
    path.write_text(json.dumps(record)); path.chmod(0o600)
    engine_calls = []
    def engine(*, cortex_policy, kos_policy, deadline):
        assert calls == ['signature']
        assert cortex_policy == release['podman'] and kos_policy == {'minimum_version': '6.0.2'}
        assert 0 < deadline - time.monotonic() <= 30
        engine_calls.append(True)
        return {'schema': 'cortex.linux-engine-readiness.v1', 'mode': 'local', 'version': '6.1.3',
                'delegation': {'controllers': ['cpu', 'memory', 'pids']}}
    monkeypatch.setattr(module, 'observe_linux_engine', engine)
    return module, custody, root, package, release, manifest, record, path, calls, engine_calls


@pytest.mark.parametrize('case', CASES)
def test_actual_private_lifecycle_binds_signed_release_before_engine_or_writes(case, tmp_path, monkeypatch):
    module, custody, root, package, release, manifest, record, path, calls, engine_calls = fixture(tmp_path, monkeypatch)
    assert hasattr(module, 'read_linux_installation'), 'concrete lifecycle-to-release binding is absent'
    if case == 'schema': record['schema'] = 'other'
    elif case == 'stage': record['stage'] = 'created'
    elif case == 'installation': record['installation'] = INSTALLATION.upper().replace('11111111', 'not-uuid')
    elif case == 'namespace': record['namespace'] += '_foreign'
    elif case == 'source': record['source_sha'] = '2' * 40
    elif case == 'version': record['version'] = '0.2.001-test.20261006.2'
    elif case == 'inner-version':
        inner = json.loads((package / 'release.json').read_bytes()); inner['version'] = '0.2.001-test.20261006.2'
        (package / 'release.json').write_text(json.dumps(inner))
        release['payload_manifest_sha256'] = hashlib.sha256((package / 'release.json').read_bytes()).hexdigest()
        manifest.write_text(json.dumps(release))
    elif case == 'connection': record['connection'] = 'remote-test'
    elif case == 'port-bool': record['port'] = True
    elif case == 'port-protected': record['port'] = 8501
    elif case == 'loaded-missing': record['loaded_images'].pop()
    elif case == 'loaded-duplicate': record['loaded_images'][1] = record['loaded_images'][0]
    elif case == 'loaded-foreign': record['loaded_images'][0] = 'sha256:' + '0' * 64
    elif case == 'extra-field': record['foreign'] = True
    path.write_text(json.dumps(record))
    if case == 'linked':
        target = root / 'original-install'; path.rename(target); path.symlink_to(target)
    elif case == 'hardlink': os.link(path, root / 'record-link')
    elif case == 'mode': path.chmod(0o644)
    elif case == 'duplicate-json': path.write_text('{"schema":"other",' + json.dumps(record)[1:])
    elif case == 'overflow': path.write_bytes(b'x' * 65537)
    elif case in ('record-replaced', 'inner-replaced', 'deadline'):
        original = module.observe_linux_engine
        def changed(**kwargs):
            value = original(**kwargs)
            if case == 'deadline': time.sleep(.08)
            else:
                selected = path if case == 'record-replaced' else package / 'release.json'
                replacement = root / 'replacement'; replacement.write_bytes(selected.read_bytes())
                replacement.chmod(0o600); replacement.replace(selected)
            return value
        monkeypatch.setattr(module, 'observe_linux_engine', changed)
    elif case == 'signature-refused':
        def refused(*args, **kwargs): raise custody.PrerequisiteRefusal('cortex_release_signature_invalid')
        monkeypatch.setattr(custody, 'verify_signature', refused)
    before = {str(p): p.read_bytes() for p in (manifest, path, package / 'release.json')}
    start = time.monotonic(); deadline = start + (.05 if case == 'deadline' else 5)
    if case == 'valid':
        value = module.read_linux_installation(root, kos_policy={'minimum_version': '6.0.2'}, deadline=deadline)
        assert value['installation_id'] == INSTALLATION and value['host_os'] == 'linux'
        assert value['namespace'] == record['namespace'] and value['origin'] == 'http://127.0.0.1:18602'
        assert value['release'] == release and value['package_root'] == str(package)
        assert value['helper_sha256'] == release['files']['bin/cortex']
        assert value['engine']['version'] == '6.1.3' and engine_calls == [True]
    else:
        with pytest.raises(custody.PrerequisiteRefusal):
            module.read_linux_installation(root, kos_policy={'minimum_version': '6.0.2'}, deadline=deadline)
        assert len(engine_calls) == (1 if case in ('record-replaced', 'inner-replaced', 'deadline') else 0)
    assert time.monotonic() - start < 1
    assert before == {str(p): p.read_bytes() for p in (manifest, path, package / 'release.json')}
