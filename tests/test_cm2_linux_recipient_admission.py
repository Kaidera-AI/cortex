"""Actual private fixture records plus intercepted recipient HTTP/runtime admission."""
import copy
import importlib
import json
import secrets
import time

import pytest

from test_cm2_linux_provision import fresh_arguments, fixture_ports, product
from cortex_v2.clients.key_store import KeyStore

INSTALLATION = '10000000-0000-4000-8000-000000000001'
ACTOR = '30000000-0000-4000-8000-000000000003'
CASES = ['valid', 'missing', 'expired', 'manager', 'store-installation', 'principal', 'scope',
         'extra-grant', 'capability', 'actor', 'status', 'duplicate', 'rotation', 'runtime-change',
         'http-exception', 'http-status', 'deadline', 'bad-deadline']


@pytest.mark.parametrize('role', ['lead', 'console'])
@pytest.mark.parametrize('case', CASES)
def test_exact_recipient_record_principal_scope_actor_and_stable_runtime_before_publication(role, case, tmp_path, monkeypatch):
    module = importlib.import_module('cortex_v2.clients.linux_provisioning')
    assert hasattr(module, 'read_recipient_admission'), 'concrete two-recipient authentication is absent'
    args = fresh_arguments(tmp_path); io = fixture_ports(product(), tmp_path)
    response = io.response; receipt = response[role]
    label = args['create_project']['lead_name'] if role == 'lead' else 'console'
    token = response[role + '_token']
    store = KeyStore(INSTALLATION, root=tmp_path / 'keys', backend='file')
    if case != 'missing':
        store.put(response['project_key'], label, token,
                  managed_by='openkai' if case == 'manager' else receipt['manager'],
                  expires_at='2000-01-01T00:00:00Z' if case == 'expired' else receipt['expires_at'])
    if case == 'store-installation': store.installation = '20000000-0000-4000-8000-000000000002'
    context = {'installation_id': INSTALLATION, 'host_os': 'linux', 'origin': 'http://127.0.0.1:18602',
               'containers': {'api': 'a' * 64}, 'release_manifest_sha256': 'b' * 64}
    current = copy.deepcopy(context); actions = []
    def runtime(root, *, kos_policy, deadline):
        assert root == tmp_path and kos_policy == {'minimum_version': '6.0.2'} and deadline > time.monotonic()
        actions.append('runtime'); return copy.deepcopy(current)
    monkeypatch.setattr(module, 'read_linux_runtime', runtime)
    principal = {'data': {'installation_id': INSTALLATION, 'principal_id': receipt['principal_id'],
                  'scopes': [{'scope_id': response['project_id'], 'primary_alias': response['project_key'],
                              'scope_kind': 'project', 'can_read': True, 'can_write': True,
                              'can_publish': role == 'lead'}]}}
    entry = {'actor_id': ACTOR, 'principal_id': receipt['principal_id'], 'display_name': label,
             'actor_kind': 'agent' if role == 'lead' else 'service', 'role': 'lead' if role == 'lead' else 'member',
             'status': 'active', 'responsibility': None}
    roster = {'data': {'scope_id': response['project_id'], 'roster_revision': 3, 'entries': [entry]}}
    if case == 'principal': principal['data']['principal_id'] = '90000000-0000-4000-8000-000000000009'
    if case == 'scope': principal['data']['scopes'][0]['scope_id'] = '90000000-0000-4000-8000-000000000009'
    if case == 'extra-grant': principal['data']['scopes'].append(copy.deepcopy(principal['data']['scopes'][0]))
    if case == 'capability': principal['data']['scopes'][0]['can_publish'] = role != 'lead'
    if case == 'actor': entry['actor_id'] = 'invalid'
    if case == 'status': entry['status'] = 'inactive'
    if case == 'duplicate': roster['data']['entries'].append(copy.deepcopy(entry))
    class Transport:
        def __init__(self, origin): assert origin == context['origin']
        def get(self, url, *, headers, timeout, max_bytes):
            if headers != {'Authorization': 'Bearer ' + token, 'X-Cortex-Scope': response['project_key']}:
                raise AssertionError('private recipient header binding changed')
            assert 0 < timeout <= 5 and max_bytes == 65536
            paths = ['/health/ready', '/v1/auth/principal', '/v1/scopes/kos-primary/roster']
            reads = len([a for a in actions if a.startswith('http:')])
            assert url == context['origin'] + paths[reads]
            actions.append('http:' + paths[reads])
            if case == 'http-exception': raise RuntimeError('PUBLIC_RECIPIENT_EXCEPTION_TEST')
            if case == 'runtime-change': current['containers']['api'] = 'c' * 64
            if case == 'rotation':
                store.put(response['project_key'], label, secrets.token_urlsafe(32),
                          managed_by=receipt['manager'], expires_at=receipt['expires_at'])
            if case == 'deadline': time.sleep(.08)
            value = {'status': 'ready'} if reads == 0 else principal if reads == 1 else roster
            return (403 if case == 'http-status' else 200), json.dumps(value).encode()
    monkeypatch.setattr(module, 'LoopbackTransport', Transport)
    deadline = True if case == 'bad-deadline' else time.monotonic() + (.05 if case == 'deadline' else 5)
    def admission():
        return module.read_recipient_admission(tmp_path, args, response, role,
            kos_policy={'minimum_version': '6.0.2'}, store=store, deadline=deadline)
    if case == 'valid':
        member = admission()
        assert member['principal_id'] == receipt['principal_id'] and member['actor_id'] == ACTOR
        assert member['scope_id'] == response['project_id'] and member['member_name'] == label
        assert member['project_root'] == args['create_project']['repo_root'] and member['can_publish'] is (role == 'lead')
        assert len([a for a in actions if a.startswith('http:')]) == 3 and actions.count('runtime') == 4
        public = json.dumps(member)
    else:
        with pytest.raises(module.PrerequisiteRefusal) as error: admission()
        public = str(error.value)
        assert 'PUBLIC_RECIPIENT_EXCEPTION_TEST' not in public
        calls = [a for a in actions if a.startswith('http:')]
        if case in ('missing', 'expired', 'manager', 'store-installation', 'bad-deadline'): assert not calls
        if case in ('rotation', 'runtime-change', 'http-exception', 'http-status', 'deadline'): assert len(calls) == 1
    if any(secret in public for secret in (token, response['lead_token'], response['console_token'])):
        raise AssertionError('private recipient material became public')
    assert not any(a in ('write', 'publish', 'create', 'restart') for a in actions)
