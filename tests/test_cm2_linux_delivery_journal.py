"""Actual private KeyStore/lock effects; receipt-only replay carries no token."""
import copy
import importlib
import json
import os
from pathlib import Path
import secrets
import stat

import pytest

INSTALLATION = '10000000-0000-4000-8000-000000000001'


def fixture(tmp_path, monkeypatch):
    module = importlib.import_module('cortex_v2.clients.linux_provisioning')
    assert hasattr(module, 'DeliveryJournal'), 'accepted private delivery journal is absent'
    keys = importlib.import_module('cortex_v2.clients.key_store')
    monkeypatch.setattr(keys, '_is_linux', lambda: True)
    root = tmp_path / 'project'; root.mkdir(mode=0o700)
    body = {'source_project': None, 'project_key': 'fresh', 'display_name': 'Fresh project',
            'lead_name': 'lead', 'lead_responsibility': 'delivery', 'lead_key_manager': 'kos',
            'with_console': True, 'parent_project_key': None, 'repo_root': str(root),
            'roots': [{'path': str(root), 'kind': 'primary'}]}
    args = {'mode': 'create-project', 'host_os': 'linux', 'create_project': body,
            'operator_explicit': True, 'manager_explicit': True, 'idempotency_key': 'explicit-fixture-key'}
    response = {'operation_id': '60000000-0000-4000-8000-000000000006',
                'project_id': '40000000-0000-4000-8000-000000000004', 'project_key': 'fresh',
                'delivery_state': 'issued_once',
                'lead': {'principal_id': '70000000-0000-4000-8000-000000000007',
                         'manager': 'kos', 'expires_at': '2027-10-06T05:00:00Z'},
                'console': {'principal_id': '20000000-0000-4000-8000-000000000002',
                            'manager': 'kos', 'expires_at': '2027-10-06T05:00:00Z'},
                'lead_token': secrets.token_urlsafe(32), 'console_token': secrets.token_urlsafe(32)}
    store = keys.KeyStore(INSTALLATION, root=tmp_path / 'keys', backend='file')
    path = tmp_path / 'delivery.json'
    journal = module.DeliveryJournal(path, args, INSTALLATION, store)
    calls = []
    def verify(name, record, principal):
        calls.append(name)
        assert bool(record.token == response[name + '_token'])
        assert principal == response[name]['principal_id']
        return True
    return module, journal, path, args, response, store, verify, calls


def replay(response):
    value = copy.deepcopy(response)
    value.pop('lead_token'); value.pop('console_token')
    value['delivery_state'] = 'reissue_required'
    return value


def test_actual_both_recipient_writes_and_nonsecret_committed_journal(tmp_path, monkeypatch):
    module, journal, path, args, response, store, verify, calls = fixture(tmp_path, monkeypatch)
    assert journal.begin() == 'new'
    assert json.loads(path.read_text())['state'] == 'command_started'
    journal.persist(response, verify=verify)
    assert calls == ['lead', 'console']
    for name in ('lead', 'console'):
        record = store.read('fresh', name)
        assert bool(record.token == response[name + '_token'])
        assert record.metadata.managed_by == 'kos'
        assert record.metadata.expires_at == response[name]['expires_at']
    raw = path.read_bytes(); saved = json.loads(raw)
    assert saved['state'] == 'keys_committed' and saved['operation_id'] == response['operation_id']
    assert saved['project_id'] == response['project_id'] and saved['installation_id'] == INSTALLATION
    assert set(saved) == {'schema', 'installation_id', 'request_sha256', 'operation_id',
                          'project_id', 'project_key', 'lead', 'console', 'state'}
    for name in ('lead', 'console'):
        assert bool(response[name + '_token'].encode() not in raw)
        assert set(saved[name]) == {'principal_id', 'manager', 'expires_at'}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert journal.complete(replay(response), verify=verify) is True
    assert calls == ['lead', 'console', 'lead', 'console']


def test_receipt_only_resume_reads_existing_keys_without_writes(tmp_path, monkeypatch):
    module, journal, path, args, response, store, verify, calls = fixture(tmp_path, monkeypatch)
    journal.begin(); journal.persist(response, verify=verify)
    before = {p: p.read_bytes() for p in (tmp_path / 'keys').rglob('*.key')}
    resumed = module.DeliveryJournal(path, args, INSTALLATION, store)
    assert resumed.begin() == 'resume'
    monkeypatch.setattr(store, 'put', lambda *a, **kw: pytest.fail('receipt replay wrote a key'))
    assert resumed.complete(replay(response), verify=verify) is True
    assert before == {p: p.read_bytes() for p in before}
    assert calls == ['lead', 'console', 'lead', 'console']


@pytest.mark.parametrize('crash', ['command-started', 'lead-only', 'both-unverified'])
def test_uncertain_or_partial_delivery_never_automatically_retries(tmp_path, monkeypatch, crash):
    module, journal, path, args, response, store, verify, calls = fixture(tmp_path, monkeypatch)
    journal.begin()
    if crash != 'command-started':
        original = store.put
        def put(project, name, token, **metadata):
            if crash == 'lead-only' and name == 'console': raise OSError('private fixture error')
            original(project, name, token, **metadata)
        monkeypatch.setattr(store, 'put', put)
        with pytest.raises(module.ProvisionRefusal) as error:
            journal.persist(response, verify=lambda *a: False)
        assert error.value.code == 'cortex_provisioning_reissue_required'
    raw = path.read_bytes()
    resumed = module.DeliveryJournal(path, args, INSTALLATION, store)
    with pytest.raises(module.ProvisionRefusal) as error: resumed.begin()
    assert error.value.code == 'cortex_provisioning_reissue_required'
    assert path.read_bytes() == raw and calls == []
    for name in ('lead', 'console'): assert bool(response[name + '_token'].encode() not in raw)


@pytest.mark.parametrize('change', ['missing-lead', 'missing-console', 'lead-manager',
    'console-expiry', 'lead-identity', 'operation', 'project', 'installation', 'request', 'verifier-refusal'])
def test_receipt_only_replay_requires_exact_binding_and_both_authenticated_recipients(tmp_path, monkeypatch, change):
    module, journal, path, args, response, store, verify, calls = fixture(tmp_path, monkeypatch)
    journal.begin(); journal.persist(response, verify=verify)
    receipt = replay(response)
    if change.startswith('missing-'): store.delete('fresh', change.removeprefix('missing-'))
    elif change == 'lead-manager': store.put('fresh', 'lead', response['lead_token'], managed_by='user', expires_at=response['lead']['expires_at'])
    elif change == 'console-expiry': store.put('fresh', 'console', response['console_token'], managed_by='kos', expires_at='2027-10-07T05:00:00Z')
    elif change == 'lead-identity': receipt['lead']['principal_id'] = '80000000-0000-4000-8000-000000000008'
    elif change == 'operation': receipt['operation_id'] = '80000000-0000-4000-8000-000000000008'
    elif change == 'project': receipt['project_id'] = '80000000-0000-4000-8000-000000000008'
    elif change == 'installation': journal = module.DeliveryJournal(path, args, '80000000-0000-4000-8000-000000000008', store)
    elif change == 'request':
        args = copy.deepcopy(args); args['create_project']['display_name'] = 'Different request'
        journal = module.DeliveryJournal(path, args, INSTALLATION, store)
    elif change == 'verifier-refusal': verify = lambda *a: False
    try: result = journal.complete(receipt, verify=verify)
    except module.ProvisionRefusal as error: result = error.code
    assert result in (False, 'cortex_provisioning_reissue_required', 'cortex_provisioning_conflict')


def test_private_error_and_extra_nested_token_are_never_retained_in_journal(tmp_path, monkeypatch):
    module, journal, path, args, response, store, verify, calls = fixture(tmp_path, monkeypatch)
    private = secrets.token_urlsafe(32)
    response['lead']['unused_private_field'] = private
    journal.begin()
    def refuse(*a): raise OSError(private)
    with pytest.raises(module.ProvisionRefusal) as error: journal.persist(response, verify=refuse)
    assert bool(private not in str(error.value)) and bool(private.encode() not in path.read_bytes())
    assert set(json.loads(path.read_text())['lead']) == {'principal_id', 'manager', 'expires_at'}


def test_receipt_only_has_no_plaintext_delivery_path(tmp_path, monkeypatch):
    module, journal, path, args, response, store, verify, calls = fixture(tmp_path, monkeypatch)
    journal.begin()
    with pytest.raises(module.ProvisionRefusal) as error: journal.persist(replay(response), verify=verify)
    assert error.value.code == 'cortex_provisioning_reissue_required'
    assert calls == [] and not (tmp_path / 'keys').exists()


def lock_product():
    module = importlib.import_module('cortex_v2.clients.linux_provisioning')
    assert hasattr(module, 'operation_lock'), 'accepted private nonblocking operation lock is absent'
    return module


def test_actual_lock_contention_and_release_keeps_same_owned_inode(tmp_path):
    module = lock_product(); path = tmp_path / 'operation.lock'
    with module.operation_lock(path) as first:
        assert first is True
        identity = (path.stat().st_dev, path.stat().st_ino)
        with module.operation_lock(path) as second: assert second is False
    with module.operation_lock(path) as third: assert third is True
    assert identity == (path.stat().st_dev, path.stat().st_ino)
    assert path.read_bytes() == b'' and stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize('unsafe', ['symlink', 'hardlink', 'mode', 'directory', 'parent-mode', 'content'])
def test_unsafe_lock_custody_refuses_before_entering(tmp_path, unsafe):
    module = lock_product(); path = tmp_path / 'operation.lock'; sentinel = tmp_path / 'sentinel'
    sentinel.write_bytes(b'untouched'); sentinel.chmod(0o600)
    if unsafe == 'symlink': path.symlink_to(sentinel)
    elif unsafe == 'hardlink': os.link(sentinel, path)
    elif unsafe == 'directory': path.mkdir(mode=0o700)
    elif unsafe == 'parent-mode': tmp_path.chmod(0o755)
    else:
        path.write_bytes(b'not-empty' if unsafe == 'content' else b''); path.chmod(0o644)
        if unsafe == 'content': path.chmod(0o600)
    entered = []
    with pytest.raises(module.ProvisionRefusal):
        with module.operation_lock(path): entered.append(True)
    assert entered == [] and sentinel.read_bytes() == b'untouched'


def test_lock_path_replacement_is_detected_before_successful_exit(tmp_path):
    module = lock_product(); path = tmp_path / 'operation.lock'
    with pytest.raises(module.ProvisionRefusal):
        with module.operation_lock(path) as acquired:
            assert acquired is True
            path.unlink(); path.write_bytes(b''); path.chmod(0o600)
