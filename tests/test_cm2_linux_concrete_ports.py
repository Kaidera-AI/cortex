"""Concrete orchestration over actual private stores, journal and publication.

Native runtime/owner/create/HTTP/readiness observations are intercepted models.
"""
from contextlib import contextmanager
import copy
import hmac
import importlib
import json
import os
import secrets
import stat
import time

import pytest

from test_cm2_linux_publication import publication
from test_cm2_linux_engine_observation import POLICY


CASES = ['valid', 'resume', 'owner-refused', 'uncertain-create', 'malformed-response',
    'partial-key-write', 'admission-refused', 'final-runtime', 'store-installation',
    'store-backend', 'lock-busy', 'bad-owner', 'changed-runtime', 'deadline',
    'same-path', 'unsafe-parent', 'linked-output', 'package-output', 'intent-conflict',
    'different-idempotency', 'request-conflict', 'late-record-rotation',
    'unknown-private-error', 'unsupported-adopt', 'unsupported-reissue',
    'unsupported-mac', 'missing-explicit', 'no-console', 'owner-lead-name',
    'connection-journal-overlap']


@pytest.mark.parametrize('case', CASES)
def test_concrete_producer_requires_exact_intent_lock_owner_both_records_and_fresh_runtime(case, tmp_path, monkeypatch):
    host = importlib.import_module('cortex_v2.clients.linux_provisioning')
    assert callable(getattr(host, 'provision_linux', None)), 'concrete Linux producer ports missing'
    assert hasattr(host, 'LinuxProvisionPorts'), 'concrete Linux producer ports missing'
    host, args, response, store, context, proof, members, connection, descriptor, state, calls, _ = publication(tmp_path, monkeypatch)
    tmp_path.chmod(0o700)
    hint = tmp_path / 'install.json'; hint.write_text(json.dumps({'installation': context['installation_id']})); hint.chmod(0o600)
    for label in ('lead', 'console'): store.delete('fresh', label)
    owner = secrets.token_urlsafe(32).encode(); events = []
    journal = descriptor.with_name(descriptor.name + '.delivery.json')
    intent = descriptor.with_name(descriptor.name + '.request.json')
    lock = descriptor.with_name(descriptor.name + '.lock')
    @contextmanager
    def runtime_scope(root, *, kos_policy, deadline):
        assert root == tmp_path and kos_policy == POLICY and 0 < deadline - time.monotonic() <= 30
        events.append('runtime-open')
        yield
        events.append('runtime-final')
        if case == 'final-runtime': raise host.ProvisionRefusal('cortex_image_mismatch')
        if case == 'late-record-rotation':
            store.put('fresh', 'console', secrets.token_urlsafe(32), managed_by='kos', expires_at=response['console']['expires_at'])
    monkeypatch.setattr(host, 'runtime_observation_scope', runtime_scope)
    def command(root, frame, *, kos_policy, deadline):
        events.append(frame['mode'])
        assert root == tmp_path and frame['installation_id'] == context['installation_id']
        assert bool(frame['credential'].encode() == owner) and kos_policy == POLICY
        if frame['mode'] == 'authorize-owner':
            assert not journal.exists() and not intent.exists() or case in ('resume', 'different-idempotency', 'request-conflict', 'intent-conflict')
            if case == 'owner-refused': raise host.ProvisionRefusal('cortex_provisioning_owner_required')
            return {'authorized': True, 'installation_id': context['installation_id']}
        assert frame['mode'] == 'create-project'
        assert host.custody.read_private_json(journal)['state'] == 'command_started'
        assert host.custody.read_private_json(intent)['arguments'] == args
        if case == 'uncertain-create': raise TimeoutError('PRIVATE MODEL ONLY')
        if case == 'unknown-private-error': raise RuntimeError('PRIVATE MODEL ONLY')
        value = copy.deepcopy(response)
        if case == 'malformed-response': value['project_key'] = 'foreign'
        return value
    monkeypatch.setattr(host, 'owned_api_command', command)
    def member(root, request, receipt, role, *, kos_policy, store, deadline):
        events.append('admit-' + role)
        record = store.read('fresh', 'lead' if role == 'lead' else 'console')
        assert record is not None and bool(hmac.compare_digest(record.token, response[role + '_token']))
        if case == 'admission-refused': raise host.ProvisionRefusal('cortex_credential_refused')
        return copy.deepcopy(members[role])
    monkeypatch.setattr(host, 'read_recipient_admission', member)
    def readiness(*a, **kw):
        events.append('readiness'); assert kw['recheck_recipients']() == members
        return copy.deepcopy(proof)
    monkeypatch.setattr(host, 'read_linux_readiness', readiness)
    if case == 'partial-key-write':
        original_put = store.put
        def put(project, name, token, **metadata):
            if name == 'console': raise OSError('PRIVATE MODEL ONLY')
            return original_put(project, name, token, **metadata)
        monkeypatch.setattr(store, 'put', put)
    if case == 'store-installation': store.installation = '20000000-0000-4000-8000-000000000002'
    if case == 'store-backend': store.backend = 'keychain'
    if case == 'changed-runtime': context['installation_id'] = '20000000-0000-4000-8000-000000000002'
    if case == 'bad-owner': owner = b'wrong'
    if case == 'same-path': connection = descriptor
    if case == 'unsafe-parent': tmp_path.chmod(0o755)
    if case == 'linked-output': descriptor.symlink_to(tmp_path / 'foreign')
    if case == 'package-output': connection = tmp_path / 'package' / 'connection.json'
    if case == 'connection-journal-overlap': connection = journal
    if case in ('unsupported-adopt', 'unsupported-reissue'): args['mode'] = case.removeprefix('unsupported-')
    if case == 'unsupported-mac': args['host_os'] = 'macos'
    if case == 'missing-explicit': args['operator_explicit'] = False
    if case == 'no-console': args['create_project']['with_console'] = False
    if case == 'owner-lead-name': args['create_project']['lead_name'] = 'owner'
    def invoke():
        return host.provision_linux(tmp_path, args, owner_token=owner, kos_policy=POLICY,
            connection_file=connection, descriptor_file=descriptor, store=store,
            deadline=time.monotonic() - 1 if case == 'deadline' else time.monotonic() + 10)
    initial = None
    if case in ('resume', 'different-idempotency', 'request-conflict', 'intent-conflict'):
        initial = invoke(); events.clear()
        monkeypatch.setattr(store, 'put', lambda *a, **kw: pytest.fail('receipt-only resume wrote a key'))
        if case == 'different-idempotency': args['idempotency_key'] = 'different-intent'
        if case == 'request-conflict': args['create_project']['display_name'] = 'Different request'
        if case == 'intent-conflict':
            value = host.custody.read_private_json(intent); value['installation_id'] = '20000000-0000-4000-8000-000000000002'
            host.custody.atomic_private_json(intent, value)
    if case == 'lock-busy':
        with host.operation_lock(lock) as acquired:
            assert acquired
            with pytest.raises(host.PrerequisiteRefusal): invoke()
    elif case in ('valid', 'resume'):
        result = invoke()
        assert set(result) == {'status', 'operation_id', 'installation_id', 'principal_id', 'actor_id', 'scope_id',
                               'connection_file', 'descriptor_file', 'expires_at'}
        assert result['status'] == 'READY' and result['actor_id'] == members['console']['actor_id']
        assert events[0] == 'runtime-open' and events[-1] == 'runtime-final'
        assert events.index('authorize-owner') < events.index('admit-lead') < events.index('readiness')
        assert events.count('create-project') == (0 if case == 'resume' else 1)
        assert host.custody.read_private_json(journal)['state'] == 'keys_committed'
        assert initial is None or result == initial
        for path in (intent, journal, connection, descriptor, state):
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
            assert bool(owner not in path.read_bytes())
            for role in ('lead', 'console'): assert bool(response[role + '_token'].encode() not in path.read_bytes())
        assert bool(owner.decode() not in json.dumps(result))
    else:
        with pytest.raises(host.PrerequisiteRefusal) as error: invoke()
        assert 'PRIVATE MODEL ONLY' not in str(error.value)
    if case not in ('valid', 'resume'): assert events.count('create-project') <= 1
    if case in ('uncertain-create', 'malformed-response', 'partial-key-write', 'unknown-private-error'):
        before = events.count('create-project')
        with pytest.raises(host.PrerequisiteRefusal): invoke()
        assert events.count('create-project') == before
        assert not descriptor.exists()
