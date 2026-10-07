"""Read-only challenge binding over actual private state and recipient records."""
from contextlib import contextmanager
import copy
import hashlib
import hmac
import importlib
import json
import os
from pathlib import Path
import secrets
import time

import pytest

from test_cm2_linux_publication import publication
from test_cm2_linux_engine_observation import POLICY


CASES = ['valid', 'nonce-short', 'nonce-upper', 'nonce-whitespace', 'descriptor-symlink',
    'descriptor-public', 'descriptor-extra', 'descriptor-schema', 'descriptor-owner',
    'descriptor-package', 'descriptor-release-path', 'descriptor-reader', 'descriptor-policy',
    'descriptor-origin', 'connection-principal', 'connection-actor', 'connection-root',
    'state-missing', 'state-extra', 'state-installation', 'state-token', 'state-request',
    'intent-missing', 'intent-invalid', 'journal-pending', 'lock-missing', 'lock-busy',
    'admission-refused', 'final-runtime', 'descriptor-restore', 'state-restore',
    'deadline', 'wrong-executable', 'readiness-not-ready', 'late-key-rotation']


@pytest.mark.parametrize('case', CASES)
def test_challenge_reads_only_existing_exact_state_and_never_provisions_or_repairs(case, tmp_path, monkeypatch):
    host = importlib.import_module('cortex_v2.clients.linux_provisioning')
    assert callable(getattr(host, 'read_linux_prerequisite_proof', None)), 'read-only Linux challenge reader missing'
    host, args, response, store, context, proof, members, connection, descriptor, state, _, publish = publication(tmp_path, monkeypatch)
    tmp_path.chmod(0o700)
    helper = tmp_path / 'package/bin/cortex'; helper.parent.mkdir(mode=0o700, parents=True)
    helper.parent.parent.chmod(0o700); helper.write_bytes(b'PUBLIC signed helper model'); helper.chmod(0o700)
    digest = hashlib.sha256(helper.read_bytes()).hexdigest()
    context['helper_sha256'] = context['release']['files']['bin/cortex'] = proof['helper_sha256'] = digest
    publish()
    journal = descriptor.with_name(descriptor.name + '.delivery.json')
    journal.write_bytes((tmp_path / 'delivery.json').read_bytes()); journal.chmod(0o600)
    intent = descriptor.with_name(descriptor.name + '.request.json')
    host.custody.atomic_private_json(intent, {'schema': 'cortex.provision-intent.v1',
        'installation_id': context['installation_id'], 'arguments': args})
    lock = descriptor.with_name(descriptor.name + '.lock'); lock.touch(mode=0o600)
    nonce = '0123456789abcdef0123456789abcdef'; events = []
    original_put = store.put
    @contextmanager
    def runtime_scope(root, *, kos_policy, deadline):
        assert root == tmp_path and kos_policy == POLICY and 0 < deadline - time.monotonic() <= 30
        events.append('runtime-open'); yield; events.append('runtime-final')
        if case == 'final-runtime': raise host.ProvisionRefusal('cortex_image_mismatch')
        if case == 'late-key-rotation':
            original_put('fresh', 'console', secrets.token_urlsafe(32), managed_by='kos', expires_at=response['console']['expires_at'])
    monkeypatch.setattr(host, 'runtime_observation_scope', runtime_scope)
    def member(root, request, receipt, role, *, kos_policy, store, deadline):
        events.append('admit-' + role)
        assert 'lead_token' not in receipt and 'console_token' not in receipt and receipt['delivery_state'] == 'reissue_required'
        record = store.read('fresh', 'lead' if role == 'lead' else 'console')
        assert record is not None and bool(hmac.compare_digest(record.token, response[role + '_token']))
        if case == 'admission-refused': raise host.ProvisionRefusal('cortex_credential_refused')
        return copy.deepcopy(members[role])
    monkeypatch.setattr(host, 'read_recipient_admission', member)
    changed = []
    def readiness(*a, **kw):
        events.append('readiness'); assert kw['recheck_recipients']() == members
        if case in ('descriptor-restore', 'state-restore') and not changed:
            path = descriptor if case == 'descriptor-restore' else state
            raw = path.read_bytes(); before = path.stat(); path.write_bytes(b'{"PUBLIC":"changed"}')
            path.write_bytes(raw); os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns)); changed.append(True)
        value = copy.deepcopy(proof)
        if case == 'readiness-not-ready': value['status'] = 'not-ready'
        return value
    monkeypatch.setattr(host, 'read_linux_readiness', readiness)
    for name in ('owned_api_command', 'publish_linux_prerequisite'):
        monkeypatch.setattr(host, name, lambda *a, **kw: pytest.fail('challenge attempted provisioning'))
    monkeypatch.setattr(host.DeliveryJournal, 'begin', lambda *a: pytest.fail('challenge began a delivery'))
    monkeypatch.setattr(host.custody, 'atomic_private_json', lambda *a, **kw: pytest.fail('challenge wrote private state'))
    monkeypatch.setattr(store, 'put', lambda *a, **kw: pytest.fail('challenge wrote a key'))
    selected = {'descriptor': descriptor, 'connection': connection, 'state': state, 'intent': intent, 'journal': journal}
    def change(path, fn):
        value = host.custody.read_private_json(path); fn(value); path.write_text(json.dumps(value)); path.chmod(0o600)
    if case == 'nonce-short': nonce = '0' * 31
    if case == 'nonce-upper': nonce = 'A' * 32
    if case == 'nonce-whitespace': nonce += '\n'
    if case == 'descriptor-symlink': target = tmp_path / 'saved'; descriptor.rename(target); descriptor.symlink_to(target)
    if case == 'descriptor-public': descriptor.chmod(0o644)
    changes = {'descriptor-extra': ('descriptor', 'cached', True), 'descriptor-schema': ('descriptor', 'schema', 'foreign'),
        'descriptor-owner': ('descriptor', 'owner_uid', os.getuid() + 1), 'descriptor-package': ('descriptor', 'package_root', str(tmp_path)),
        'descriptor-release-path': ('descriptor', 'release_manifest', str(state)), 'descriptor-reader': ('descriptor', 'member_reader_archive_sha256', '0' * 64),
        'descriptor-origin': ('descriptor', 'api_origin', 'http://127.0.0.1:18603'), 'connection-principal': ('connection', 'principal_id', response['lead']['principal_id']),
        'connection-actor': ('connection', 'actor_id', members['lead']['actor_id']), 'connection-root': ('connection', 'project_root', str(tmp_path)),
        'state-extra': ('state', 'cached', True), 'state-installation': ('state', 'installation_id', '20000000-0000-4000-8000-000000000002'),
        'journal-pending': ('journal', 'state', 'delivery_pending')}
    if case in changes:
        which, key, value = changes[case]; change(selected[which], lambda obj: obj.update({key: value}))
    if case == 'descriptor-policy': change(descriptor, lambda obj: obj['podman'].update(policy_sha256='0' * 64))
    if case == 'state-token': change(state, lambda obj: obj['receipt'].update(console_token=response['console_token']))
    if case == 'state-request': change(state, lambda obj: obj['create_project'].update(display_name='Different request'))
    if case == 'intent-invalid': change(intent, lambda obj: obj['arguments'].update(idempotency_key=''))
    if case == 'state-missing': state.unlink()
    if case == 'intent-missing': intent.unlink()
    if case == 'lock-missing': lock.unlink()
    before = {p: (p.read_bytes(), host.custody._file_identity(p.stat())) for p in [*selected.values(), lock] if p.exists() and not p.is_symlink()}
    def invoke():
        return host.read_linux_prerequisite_proof(descriptor, nonce, store=store,
            executable=tmp_path / 'wrong-helper' if case == 'wrong-executable' else helper,
            deadline=time.monotonic() - 1 if case == 'deadline' else time.monotonic() + 10)
    if case == 'lock-busy':
        with host.operation_lock(lock) as acquired:
            assert acquired
            with pytest.raises(host.PrerequisiteRefusal): invoke()
    elif case == 'valid':
        value = invoke(); frozen = json.loads((Path(__file__).parent / 'fixtures/cm2-revision4/fixtures/local-proof.linux.json').read_text())
        assert set(value) == set(frozen) and value['schema'] == 'cortex.prerequisite-proof.v2' and value['nonce'] == nonce
        assert value['descriptor_sha256'] == hashlib.sha256(descriptor.read_bytes()).hexdigest()
        assert value['helper_sha256'] == digest and value['project_binding']['scope_id'] == response['project_id']
        assert events[0] == 'runtime-open' and events[-1] == 'runtime-final' and events.count('admit-lead') >= 2
        for role in ('lead', 'console'): assert bool(response[role + '_token'] not in json.dumps(value))
    else:
        with pytest.raises(host.PrerequisiteRefusal): invoke()
    if case not in ('descriptor-restore', 'state-restore'):
        assert bool(before == {p: (p.read_bytes(), host.custody._file_identity(p.stat())) for p in before})
