"""CM2 owned API command binding; runtime/native calls intercepted, private synthetic frames."""
import asyncio
import copy
import hashlib
import importlib
import inspect
import json
import os
from pathlib import Path
import secrets
import sys
import time

import pytest

from test_cm2_linux_native_adapter import native_fixture, product as native_product

INSTALLATION = '10000000-0000-4000-8000-000000000001'
CASES = ['valid', 'payload', 'readiness', 'owner', 'extra-frame', 'credential', 'installation', 'mode',
         'runtime-refused', 'context-change', 'engine-change', 'payload-digest', 'payload-file-digest',
         'payload-extra', 'payload-path', 'payload-migration', 'command-refused', 'command-exception',
         'deadline', 'bad-deadline', 'owner-denied']


def fixture(tmp_path, monkeypatch):
    module = importlib.import_module('cortex_v2.clients.linux_provisioning')
    root = tmp_path / 'runtime'; root.mkdir(mode=0o700)
    release = json.loads((Path(__file__).parent / 'fixtures/cm2-revision4/fixtures/release.linux.json').read_text())
    files = {'src/cortex_v2/native_prerequisite.py': 'a' * 64}
    files.update({'migrations/' + m['name']: m['sha256'] for m in release['migrations']})
    digest = hashlib.sha256(json.dumps(files, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    release['images']['api']['source_payload_sha256'] = digest
    payload = {'files': files, 'sha256': digest}
    context = {'release': release, 'installation_id': INSTALLATION, 'host_os': 'linux',
               'runtime_root': str(root), 'package_root': str(root / 'package'), 'helper_sha256': 'b' * 64,
               'release_manifest_sha256': 'c' * 64, 'namespace': 'cortex_v2_package_test_' + INSTALLATION.replace('-', ''),
               'origin': 'http://127.0.0.1:18602', 'engine': {'version': '6.1.3'},
               'containers': {r: format(i + 1, '064x') for i, r in enumerate(module.custody.ROLES)}, 'network_id': 'd' * 64}
    engine = {'executable': '/usr/bin/podman', 'identity': ('public-fixture',),
              'environment': {'PATH': '/usr/bin:/bin', 'HOME': str(tmp_path), 'XDG_RUNTIME_DIR': str(tmp_path / 'run'), 'LC_ALL': 'C'}}
    args = copy.deepcopy(json.loads((Path(__file__).parent / 'fixtures/cm2-revision4/fixtures/provisioning.json').read_text())['modes']['create-project']['request'])
    args['repo_root'] = str(tmp_path); args['roots'] = [{'path': str(tmp_path), 'kind': 'primary'}]
    frame = {'mode': 'create-project', 'installation_id': INSTALLATION, 'credential': secrets.token_urlsafe(32),
             'idempotency_key': 'explicit-private-idempotency', 'request': args}
    credential = frame['credential']
    actions = []; runtime_calls = []; current = copy.deepcopy(context)
    def runtime(runtime_root, *, kos_policy, deadline):
        assert runtime_root == root and kos_policy == {'minimum_version': '6.0.2'} and deadline > time.monotonic()
        runtime_calls.append(True); actions.append('runtime'); return copy.deepcopy(current)
    monkeypatch.setattr(module, 'read_linux_runtime', runtime, raising=False)
    monkeypatch.setattr(module, '_local_engine_context', lambda: copy.deepcopy(engine))
    response = {'operation_id': '60000000-0000-4000-8000-000000000006',
                'lead_token': secrets.token_urlsafe(32), 'console_token': secrets.token_urlsafe(32)}
    def command(argv, private, *, timeout=5, environment=None):
        assert argv == ['/usr/bin/podman', '--remote=false', 'exec', '--interactive', context['containers']['api'], '/opt/venv/bin/python', '-m', 'cortex_v2.native_prerequisite']
        assert environment == engine['environment'] and 0 < timeout <= 5
        if any(credential in item for item in argv + list(environment.values())):
            raise AssertionError('private input entered argv or environment')
        actions.append(private['mode'])
        if private['mode'] == 'payload':
            assert private == {'mode': 'payload'}; return copy.deepcopy(payload)
        if private != frame: raise AssertionError('private frame mismatch')
        if private['mode'] == 'authorize-owner': return {'authorized': True, 'installation_id': INSTALLATION}
        return copy.deepcopy(response)
    monkeypatch.setattr(module, 'private_json_command', command)
    return module, root, release, context, current, engine, payload, frame, response, actions, runtime_calls, command


@pytest.mark.parametrize('case', CASES)
def test_owned_api_uses_exact_immutable_id_measured_payload_private_frame_and_shared_deadline(case, tmp_path, monkeypatch):
    module, root, release, context, current, engine, payload, frame, response, actions, runtime_calls, original = fixture(tmp_path, monkeypatch)
    assert hasattr(module, 'owned_api_command'), 'concrete owned API payload/private command binding is absent'
    if case == 'payload': frame.clear(); frame.update(mode='payload')
    if case == 'owner':
        frame.pop('request'); frame.pop('idempotency_key'); frame['mode'] = 'authorize-owner'
    if case == 'readiness':
        frame.pop('request'); frame.pop('idempotency_key'); frame.update(mode='readiness', project='kos-primary', project_root=str(tmp_path), migrations={m['name']: m['sha256'] for m in release['migrations']}, expected_relations=[{'name': 'cortex_core.records', 'rls': True, 'forced': True, 'app_direct_grant': True}])
    if case == 'extra-frame': frame['database_url'] = 'prohibited'
    if case == 'credential': frame['credential'] = 'invalid'
    if case == 'installation': frame['installation_id'] = '20000000-0000-4000-8000-000000000002'
    if case == 'mode': frame['mode'] = 'adopt-enrolled'
    if case == 'payload-digest': payload['sha256'] = 'f' * 64
    if case == 'payload-file-digest': next(iter(payload['files'])); payload['files']['src/cortex_v2/native_prerequisite.py'] = 'e' * 64
    if case == 'payload-extra': payload['extra'] = True
    if case == 'payload-path': payload['files']['src/../foreign'] = 'e' * 64
    if case == 'payload-migration': payload['files']['migrations/unlisted.sql'] = 'e' * 64
    if case == 'runtime-refused':
        def refused(*args, **kwargs): raise module.PrerequisiteRefusal('cortex_release_signature_invalid')
        monkeypatch.setattr(module, 'read_linux_runtime', refused)
    def command(argv, private, **kwargs):
        value = original(argv, private, **kwargs)
        if private['mode'] == 'payload':
            if case == 'context-change': current['containers']['api'] = 'e' * 64
            if case == 'engine-change': engine['identity'] = ('changed',)
            if case == 'command-refused': raise module.PrerequisiteRefusal('cortex_podman_unsupported')
            if case == 'command-exception': raise RuntimeError('PUBLIC_OWNED_API_EXCEPTION_TEST')
            if case == 'deadline': time.sleep(.08)
        if case == 'owner-denied' and private['mode'] == 'authorize-owner': value['authorized'] = False
        return value
    if case == 'owner-denied':
        frame.pop('request'); frame.pop('idempotency_key'); frame['mode'] = 'authorize-owner'
    monkeypatch.setattr(module, 'private_json_command', command)
    deadline = time.monotonic() + (.05 if case == 'deadline' else 5)
    if case == 'bad-deadline': deadline = True
    if case in ('valid', 'payload', 'readiness', 'owner'):
        value = module.owned_api_command(root, frame, kos_policy={'minimum_version': '6.0.2'}, deadline=deadline)
        expected = payload if case == 'payload' else {'authorized': True, 'installation_id': INSTALLATION} if case == 'owner' else response
        if value != expected: raise AssertionError('private response mismatch')
        assert actions == (['runtime', 'payload', 'runtime'] if case == 'payload' else ['runtime', 'payload', 'runtime', frame['mode'], 'runtime'])
    else:
        with pytest.raises(module.PrerequisiteRefusal) as error:
            module.owned_api_command(root, frame, kos_policy={'minimum_version': '6.0.2'}, deadline=deadline)
        assert 'PUBLIC_OWNED_API_EXCEPTION_TEST' not in str(error.value)
        private_calls = [x for x in actions if x != 'runtime']
        assert len(private_calls) <= 2
        if case in ('extra-frame', 'credential', 'mode', 'runtime-refused', 'bad-deadline'): assert not private_calls
        if case in ('context-change', 'engine-change', 'command-refused', 'command-exception', 'deadline') or case.startswith('payload-'): assert private_calls == ['payload']
        if case == 'installation': assert not private_calls
    assert not any(x in ('create', 'restart', 'store', 'publish') for x in actions)


@pytest.mark.parametrize('case', ['valid', 'forbidden', 'installation', 'audit-exception'])
def test_separate_native_owner_check_authenticates_readonly_and_discards_audit(case, monkeypatch):
    module = native_product(); db, settings, principal, response, frame, connect = native_fixture(module, monkeypatch)
    frame.pop('request'); frame.pop('idempotency_key'); frame['mode'] = 'authorize-owner'
    from cortex_v2 import identity, store
    if case == 'forbidden':
        async def refused(*args): raise store.ApiProblem(403, 'owner_required', 'PUBLIC_AUDIT_TEST_TEXT')
        monkeypatch.setattr(identity, 'list_privileged_actions', refused)
    if case == 'installation': principal.installation_id = __import__('uuid').UUID('20000000-0000-4000-8000-000000000002')
    if case == 'audit-exception':
        async def broken(*args): raise RuntimeError('PUBLIC_AUDIT_TEST_TEXT')
        monkeypatch.setattr(identity, 'list_privileged_actions', broken)
    if case == 'valid':
        result = asyncio.run(module.execute_private(frame, settings=settings, connect=connect))
        assert result == {'authorized': True, 'installation_id': INSTALLATION}
        assert db.actions == ['connect', ('transaction', {'readonly': True}), 'authenticate', 'owner-authorization', 'transaction-exit', 'close']
    else:
        with pytest.raises(module.NativeRefusal) as error: asyncio.run(module.execute_private(frame, settings=settings, connect=connect))
        assert error.value.code == {'forbidden': 'cortex_provisioning_owner_required', 'installation': 'cortex_instance_mismatch', 'audit-exception': 'cortex_health_unavailable'}[case]
        assert 'PUBLIC_AUDIT_TEST_TEXT' not in str(error.value) and 'existing-create-service' not in db.actions
    assert db.closed


@pytest.mark.parametrize('case', ['valid', 'wrong', 'extra'])
def test_actual_private_process_accepts_only_fresh_sterile_engine_environment(case, tmp_path, monkeypatch):
    module = importlib.import_module('cortex_v2.clients.linux_provisioning')
    assert 'environment' in inspect.signature(module.private_json_command).parameters, 'explicit verified native engine environment port is absent'
    environment = {'PATH': '/usr/bin:/bin', 'HOME': str(tmp_path), 'XDG_RUNTIME_DIR': str(tmp_path / 'run'), 'LC_ALL': 'C'}
    context = {'executable': '/usr/bin/podman', 'identity': ('public-fixture',), 'environment': dict(environment)}
    monkeypatch.setattr(module, '_local_engine_context', lambda: context)
    monkeypatch.setenv('CORTEX_PRIVATE_TEST_POISON', secrets.token_urlsafe(32))
    if case == 'wrong': environment['XDG_RUNTIME_DIR'] += '-foreign'
    if case == 'extra': environment['CORTEX_PRIVATE_TEST_POISON'] = 'prohibited'
    program = 'import json,os,sys;sys.stdin.buffer.read();print(json.dumps({"valid": "XDG_RUNTIME_DIR" in os.environ and not any(k.startswith("CORTEX_") for k in os.environ)}))'
    argv = [sys.executable, '-c', program]
    if case == 'valid':
        assert module.private_json_command(argv, {'mode': 'public-environment-probe'}, timeout=1, environment=environment) == {'valid': True}
    else:
        with pytest.raises(module.PrerequisiteRefusal): module.private_json_command(argv, {'mode': 'public-environment-probe'}, timeout=1, environment=environment)
