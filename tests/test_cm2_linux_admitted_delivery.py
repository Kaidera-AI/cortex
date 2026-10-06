"""Actual private delivery effects with intercepted signed runtime and HTTP."""
import copy
import json
import secrets
import time

import pytest

from test_cm2_linux_delivery_journal import fixture, replay

CASES = ['valid', 'rotate-lead', 'rotate-console', 'last-console-rotation',
         'runtime-change', 'roster-revision', 'journal-refusal', 'journal-request',
         'bad-deadline', 'deadline', 'http-exception', 'body-refusal', 'after-close']


@pytest.mark.parametrize('mode', ['issued', 'resume'])
@pytest.mark.parametrize('case', CASES)
def test_journal_and_both_authenticated_recipients_hold_one_private_snapshot(mode, case, tmp_path, monkeypatch):
    module, journal, path, args, issued, store, verify, old_calls = fixture(tmp_path, monkeypatch)
    assert hasattr(module, 'admitted_recipients'), 'concrete journal/recipient binding is absent'
    assert journal.begin() == 'new'
    if mode == 'resume':
        journal.persist(issued, verify=verify)
        journal = module.DeliveryJournal(path, args, journal.installation, store)
        assert journal.begin() == 'resume'
    response = issued if mode == 'issued' else replay(issued)
    original_put = store.put; writes = []; reads = []
    def put(project, name, token, **metadata):
        writes.append(name)
        original_put(project, name, token, **metadata)
    monkeypatch.setattr(store, 'put', put)
    context = {'installation_id': journal.installation, 'host_os': 'linux',
               'origin': 'http://127.0.0.1:18602', 'containers': {'api': 'a' * 64},
               'release_manifest_sha256': 'b' * 64}
    current = copy.deepcopy(context)
    def runtime(root, *, kos_policy, deadline):
        assert root == tmp_path and kos_policy == {'minimum_version': '6.0.2'}
        assert deadline > time.monotonic()
        return copy.deepcopy(current)
    monkeypatch.setattr(module, 'read_linux_runtime', runtime)
    def rotate(role):
        label = args['create_project']['lead_name'] if role == 'lead' else 'console'
        original_put('fresh', label, secrets.token_urlsafe(32),
                     managed_by=issued[role]['manager'], expires_at=issued[role]['expires_at'])
    class Transport:
        def __init__(self, origin): assert origin == context['origin']
        def get(self, url, *, headers, timeout, max_bytes):
            role = next((r for r in ('lead', 'console')
                         if headers.get('Authorization') == 'Bearer ' + issued[r + '_token']), None)
            assert role is not None and headers['X-Cortex-Scope'] == 'fresh'
            assert 0 < timeout <= 5 and max_bytes == 65536
            label = args['create_project']['lead_name'] if role == 'lead' else 'console'
            index = len(reads) % 3
            suffix = ['/health/ready', '/v1/auth/principal', '/v1/scopes/fresh/roster'][index]
            assert url == context['origin'] + suffix
            reads.append((role, suffix))
            if len(reads) == 1:
                if case.startswith('rotate-'): rotate(case.removeprefix('rotate-'))
                if case == 'runtime-change': current['containers']['api'] = 'c' * 64
                if case == 'deadline': time.sleep(.08)
                if case == 'http-exception': raise RuntimeError(issued['lead_token'])
            if case == 'last-console-rotation' and len(reads) == 6: rotate('console')
            if index == 0: value = {'status': 'ready'}
            elif index == 1:
                value = {'data': {'installation_id': journal.installation,
                         'principal_id': issued[role]['principal_id'], 'scopes': [{
                         'scope_id': issued['project_id'], 'primary_alias': 'fresh',
                         'scope_kind': 'project', 'can_read': True, 'can_write': True,
                         'can_publish': role == 'lead'}]}}
            else:
                value = {'data': {'scope_id': issued['project_id'],
                         'roster_revision': 4 if case == 'roster-revision' and role == 'console' else 3,
                         'entries': [{'actor_id': '30000000-0000-4000-8000-00000000000' + ('3' if role == 'lead' else '4'),
                         'principal_id': issued[role]['principal_id'], 'display_name': label,
                         'actor_kind': 'agent' if role == 'lead' else 'service',
                         'role': 'lead' if role == 'lead' else 'member', 'status': 'active'}]}}
            return 200, json.dumps(value).encode()
    monkeypatch.setattr(module, 'LoopbackTransport', Transport)
    if case == 'journal-request': journal.body = dict(journal.body, display_name='different')
    if case == 'journal-refusal':
        if mode == 'issued':
            def refuse(*a, **kw): raise OSError(issued['console_token'])
            monkeypatch.setattr(journal, 'persist', refuse)
        else: monkeypatch.setattr(journal, 'complete', lambda *a, **kw: False)
    deadline = True if case == 'bad-deadline' else time.monotonic() + (.05 if case == 'deadline' else 5)
    captured = []; public = ''
    def operation():
        with module.admitted_recipients(tmp_path, args, response,
                kos_policy={'minimum_version': '6.0.2'}, journal=journal, deadline=deadline) as check:
            captured.append(check)
            if case == 'body-refusal': raise module.ProvisionRefusal('cortex_health_degraded')
            members = check()
            assert set(members) == {'lead', 'console'}
            assert members['lead']['roster_revision'] == members['console']['roster_revision'] == 3
            assert members['lead']['can_publish'] is True and members['console']['can_publish'] is False
            assert members['lead']['principal_id'] == issued['lead']['principal_id']
            assert members['console']['principal_id'] == issued['console']['principal_id']
            return json.dumps(members)
    if case in ('valid', 'after-close'):
        public = operation()
        assert writes == (['lead', 'console'] if mode == 'issued' else [])
        assert len(reads) == 12 and json.loads(path.read_text())['state'] == 'keys_committed'
        before = len(reads)
        with pytest.raises(module.PrerequisiteRefusal): captured[0]()
        assert len(reads) == before
    else:
        with pytest.raises(module.PrerequisiteRefusal) as error: operation()
        public = str(error.value)
        if case == 'body-refusal': assert error.value.code == 'cortex_health_degraded'
        if case in ('rotate-lead', 'rotate-console', 'runtime-change', 'deadline', 'http-exception'):
            assert len(reads) == 1
        if case == 'last-console-rotation': assert len(reads) == 6
        if case in ('journal-request', 'bad-deadline', 'journal-refusal'):
            assert reads == [] and writes == []
        if case != 'body-refusal' and mode == 'issued':
            assert json.loads(path.read_text())['state'] != 'keys_committed'
    if any(issued[r + '_token'] in public or issued[r + '_token'].encode() in path.read_bytes()
           for r in ('lead', 'console')):
        raise AssertionError('private delivery material became public')
    assert not (tmp_path / 'connection.json').exists() and not (tmp_path / 'descriptor.json').exists()
