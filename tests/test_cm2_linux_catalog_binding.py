"""Actual signed-inventory/private-record effects; native/runtime/HTTP intercepted."""
import copy
import hashlib
import json
import os
from pathlib import Path
import secrets
import time

import pytest

from test_cm2_linux_delivery_journal import fixture

CASES = ['valid', 'missing', 'unsigned', 'digest', 'source', 'api-payload', 'migrations',
         'duplicate-relations', 'order', 'relation-name', 'unknown-field', 'relation-field',
         'nonbool', 'unforced', 'unsafe-direct-grant', 'symlink', 'mode', 'hardlink',
         'parent-mode', 'file-change', 'runtime-change', 'rotate-console', 'private-exception',
         'false-predicate', 'response-extra', 'scope', 'root', 'roster-change', 'bad-deadline',
         'deadline', 'no-key', 'backend']


@pytest.mark.parametrize('case', CASES)
def test_catalog_reads_only_exact_signed_inventory_and_stable_console_before_return(case, tmp_path, monkeypatch):
    module, journal, journal_path, args, response, store, verify, calls = fixture(tmp_path, monkeypatch)
    assert hasattr(module, 'read_linux_catalog'), 'concrete signed catalog/native readiness binding is absent'
    assert journal.begin() == 'new'
    journal.persist(response, verify=verify)
    release = json.loads((Path(__file__).parent / 'fixtures/cm2-revision4/fixtures/release.linux.json').read_text())
    runtime_root = tmp_path / 'runtime'; runtime_root.mkdir(mode=0o700)
    package = runtime_root / 'package'; package.mkdir(mode=0o700)
    native = package / 'native'; native.mkdir(mode=0o700)
    inventory_file = native / 'rls-inventory.json'
    inventory = {'schema': 'cortex.rls-inventory.v1', 'source_revision': release['source_revision'],
        'api_source_payload_sha256': release['images']['api']['source_payload_sha256'],
        'migrations': {m['name']: m['sha256'] for m in release['migrations']},
        'relations': [{'name': 'cortex_auth.profiles', 'rls': True, 'forced': True, 'app_direct_grant': True},
                      {'name': 'cortex_core.records', 'rls': True, 'forced': True, 'app_direct_grant': True}]}
    if case == 'source': inventory['source_revision'] = 'f' * 40
    if case == 'api-payload': inventory['api_source_payload_sha256'] = 'f' * 64
    if case == 'migrations': inventory['migrations'].pop(next(iter(inventory['migrations'])))
    if case == 'duplicate-relations': inventory['relations'].append(copy.deepcopy(inventory['relations'][0]))
    if case == 'order': inventory['relations'].reverse()
    if case == 'relation-name': inventory['relations'][0]['name'] = 'public.foreign'
    if case == 'unknown-field': inventory['extra'] = True
    if case == 'relation-field': inventory['relations'][0]['app_owns'] = False
    if case == 'nonbool': inventory['relations'][0]['rls'] = 1
    if case == 'unforced': inventory['relations'][0]['forced'] = False
    if case == 'unsafe-direct-grant': inventory['relations'][0]['rls'] = False
    raw = json.dumps(inventory, sort_keys=True, separators=(',', ':')).encode() + b'\n'
    inventory_file.write_bytes(raw); inventory_file.chmod(0o600)
    digest = hashlib.sha256(raw).hexdigest()
    release['files']['native/rls-inventory.json'] = digest
    release['rls_inventory']['sha256'] = digest
    if case == 'unsigned': release['files'].pop('native/rls-inventory.json')
    if case == 'digest': release['rls_inventory']['sha256'] = 'e' * 64
    if case == 'missing': inventory_file.unlink()
    if case == 'symlink':
        actual = native / 'public-actual'; inventory_file.rename(actual); inventory_file.symlink_to(actual)
    if case == 'mode': inventory_file.chmod(0o666)
    if case == 'hardlink': os.link(inventory_file, native / 'public-hardlink')
    if case == 'parent-mode': native.chmod(0o777)
    context = {'installation_id': journal.installation, 'host_os': 'linux',
               'runtime_root': str(runtime_root), 'package_root': str(package),
               'release': copy.deepcopy(release), 'origin': 'http://127.0.0.1:18602',
               'containers': {'api': 'a' * 64}, 'release_manifest_sha256': 'b' * 64}
    current = copy.deepcopy(context); actions = []; reads = []
    def runtime(root, *, kos_policy, deadline):
        assert root == runtime_root and kos_policy == {'minimum_version': '6.0.2'}
        assert deadline > time.monotonic()
        actions.append('runtime')
        return copy.deepcopy(current)
    monkeypatch.setattr(module, 'read_linux_runtime', runtime)
    def rotate():
        store.put('fresh', 'console', secrets.token_urlsafe(32), managed_by='kos',
                  expires_at=response['console']['expires_at'])
    class Transport:
        def __init__(self, origin): assert origin == context['origin']
        def get(self, url, *, headers, timeout, max_bytes):
            if headers.get('Authorization') != 'Bearer ' + response['console_token']:
                raise AssertionError('private Console snapshot changed')
            assert headers['X-Cortex-Scope'] == 'fresh' and 0 < timeout <= 5 and max_bytes == 65536
            index = len(reads) % 3
            suffix = ['/health/ready', '/v1/auth/principal', '/v1/scopes/fresh/roster'][index]
            assert url == context['origin'] + suffix
            reads.append(suffix)
            if case == 'rotate-console' and len(reads) == 1: rotate()
            if index == 0: value = {'status': 'ready'}
            elif index == 1:
                value = {'data': {'installation_id': journal.installation,
                    'principal_id': response['console']['principal_id'], 'scopes': [{
                    'scope_id': response['project_id'], 'primary_alias': 'fresh', 'scope_kind': 'project',
                    'can_read': True, 'can_write': True, 'can_publish': False}]}}
            else:
                value = {'data': {'scope_id': response['project_id'],
                    'roster_revision': 4 if case == 'roster-change' and len(reads) > 3 else 3,
                    'entries': [{'actor_id': '30000000-0000-4000-8000-000000000003',
                    'principal_id': response['console']['principal_id'], 'display_name': 'console',
                    'actor_kind': 'service', 'role': 'member', 'status': 'active'}]}}
            return 200, json.dumps(value).encode()
    monkeypatch.setattr(module, 'LoopbackTransport', Transport)
    predicates = {'migration_checksums_match': True, 'required_rls_enabled_and_forced': True,
                  'app_role_non_superuser_without_bypassrls': True,
                  'app_role_not_migrator_or_table_owner': True, 'database_instance_matches': True}
    database = dict(predicates, project_binding={'scope_id': response['project_id'],
                       'primary_alias': 'fresh', 'primary_root': args['create_project']['repo_root']})
    if case == 'false-predicate': database['required_rls_enabled_and_forced'] = False
    if case == 'response-extra': database['cached_ready'] = True
    if case == 'scope': database['project_binding']['scope_id'] = '90000000-0000-4000-8000-000000000009'
    if case == 'root': database['project_binding']['primary_root'] = str(tmp_path)
    def command(root, frame, *, kos_policy, deadline):
        assert root == runtime_root and kos_policy == {'minimum_version': '6.0.2'} and deadline > time.monotonic()
        expected = {'mode': 'readiness', 'installation_id': journal.installation,
                    'credential': response['console_token'], 'project': 'fresh',
                    'project_root': args['create_project']['repo_root'],
                    'migrations': inventory['migrations'], 'expected_relations': inventory['relations']}
        if frame != expected: raise AssertionError('private readiness frame is not exact')
        actions.append('readiness')
        if case == 'file-change': inventory_file.write_bytes(b'{}\n')
        if case == 'runtime-change': current['containers']['api'] = 'c' * 64
        if case == 'private-exception': raise RuntimeError(response['console_token'])
        if case == 'deadline': time.sleep(.08)
        return copy.deepcopy(database)
    monkeypatch.setattr(module, 'owned_api_command', command)
    if case == 'no-key': monkeypatch.setattr(store, 'read', lambda *a, **kw: None)
    if case == 'backend': store.backend = 'keychain'
    deadline = True if case == 'bad-deadline' else time.monotonic() + (.05 if case == 'deadline' else 5)
    if case == 'valid':
        result = module.read_linux_catalog(runtime_root, args, response,
                     kos_policy={'minimum_version': '6.0.2'}, store=store, deadline=deadline)
        assert result == {'schema': 'cortex.linux-catalog-readiness.v1',
                         'installation_id': journal.installation, 'source_payload_matches': True, **database}
        assert actions.count('readiness') == 1 and len(reads) == 6
        assert 'status' not in result
        public = json.dumps(result)
    else:
        with pytest.raises(module.PrerequisiteRefusal) as error:
            module.read_linux_catalog(runtime_root, args, response,
                      kos_policy={'minimum_version': '6.0.2'}, store=store, deadline=deadline)
        public = str(error.value)
        if case in CASES[1:19] + ['bad-deadline', 'no-key', 'backend']:
            assert 'readiness' not in actions and reads == []
        if case == 'rotate-console': assert 'readiness' not in actions and len(reads) == 1
    if any(response[r + '_token'] in public for r in ('lead', 'console')):
        raise AssertionError('private credential became public')
    assert not (tmp_path / 'connection.json').exists() and not (tmp_path / 'descriptor.json').exists()
    assert json.loads(journal_path.read_text())['state'] == 'keys_committed'
