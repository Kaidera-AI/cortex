"""Finite native application adapter; private synthetic DB boundaries only."""
import asyncio
import copy
import hashlib
import importlib
import json
import secrets
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

HOME = Path(__file__).parent / 'fixtures/cm2-revision4'
INSTALLATION = '10000000-0000-4000-8000-000000000001'


def product():
    path = Path(__file__).parents[1] / 'src/cortex_v2/native_prerequisite.py'
    assert path.is_file(), 'accepted finite owned-image application adapter is absent'
    return importlib.import_module('cortex_v2.native_prerequisite')


class PrivateConnection:
    def __init__(self): self.actions = []; self.closed = False
    @asynccontextmanager
    async def transaction(self, **kwargs):
        self.actions.append(('transaction', kwargs))
        try: yield
        finally: self.actions.append('transaction-exit')
    async def close(self): self.closed = True; self.actions.append('close')


def native_fixture(module, monkeypatch):
    connection = PrivateConnection(); owner = secrets.token_urlsafe(32)
    settings = SimpleNamespace(database_url='PRIVATE_SYNTHETIC_DSN', token_pepper=b'PRIVATE_SYNTHETIC_PEPPER',
                               instance_id='cortex_v2_package_test', installation_id=None)
    principal = SimpleNamespace(principal_id=uuid.UUID('70000000-0000-4000-8000-000000000007'),
                                installation_id=uuid.UUID(INSTALLATION))
    response = {'operation_id': '60000000-0000-4000-8000-000000000006', 'project_key': 'kos-primary',
                'project_id': '40000000-0000-4000-8000-000000000004',
                'lead_token': secrets.token_urlsafe(32), 'console_token': secrets.token_urlsafe(32)}
    async def connect(dsn, **kwargs):
        assert dsn == settings.database_url and 0 < kwargs['timeout'] <= 5
        connection.actions.append('connect'); return connection
    async def authenticate(db, digest, **kwargs):
        assert db is connection and digest != owner.encode()
        connection.actions.append('authenticate'); return principal
    async def owner_probe(db, caller, limit):
        assert caller is principal and limit == 1
        connection.actions.append('owner-authorization'); return [{'detail': 'private audit body'}]
    async def create(db, caller, pepper, body, key):
        assert caller is principal and pepper == settings.token_pepper and key == 'explicit-key'
        assert body.source_project is None and body.with_console is True
        connection.actions.append('existing-create-service'); return 201, response, False
    from cortex_v2 import identity, store
    monkeypatch.setattr(store, 'authenticate', authenticate)
    monkeypatch.setattr(identity, 'list_privileged_actions', owner_probe)
    monkeypatch.setattr(identity, 'create_project', create)
    body = copy.deepcopy(json.loads((HOME / 'fixtures/provisioning.json').read_text())['modes']['create-project']['request'])
    frame = {'mode': 'create-project', 'installation_id': INSTALLATION, 'credential': owner,
             'idempotency_key': 'explicit-key', 'request': body}
    return connection, settings, principal, response, frame, connect


def test_finite_creation_uses_native_auth_owner_check_and_existing_transaction(monkeypatch):
    p = product(); db, settings, principal, response, frame, connect = native_fixture(p, monkeypatch)
    value = asyncio.run(p.execute_private(frame, settings=settings, connect=connect))
    assert value == response and db.closed
    assert db.actions == ['connect', ('transaction', {'readonly': False}), 'authenticate',
                          'owner-authorization', 'existing-create-service', 'transaction-exit', 'close']
    # Only private once-issued output may contain recipient tokens; no audit/DB input returns.
    assert settings.database_url not in json.dumps(value) and 'private audit body' not in json.dumps(value)


def test_actual_owner_refusal_precedes_existing_create_service(monkeypatch):
    p = product(); db, settings, principal, response, frame, connect = native_fixture(p, monkeypatch)
    from cortex_v2 import identity
    from cortex_v2.store import ApiProblem
    async def refuse(*args): raise ApiProblem(403, 'owner_required', 'private synthetic audit message')
    monkeypatch.setattr(identity, 'list_privileged_actions', refuse)
    with pytest.raises(p.NativeRefusal) as exc: asyncio.run(p.execute_private(frame, settings=settings, connect=connect))
    assert exc.value.code == 'cortex_provisioning_owner_required'
    assert 'existing-create-service' not in db.actions and db.closed
    assert frame['credential'] not in str(exc.value) and 'private synthetic' not in str(exc.value)


def test_database_identity_mismatch_cannot_issue(monkeypatch):
    p = product(); db, settings, principal, response, frame, connect = native_fixture(p, monkeypatch)
    principal.installation_id = uuid.UUID('50000000-0000-4000-8000-000000000005')
    with pytest.raises(p.NativeRefusal) as exc: asyncio.run(p.execute_private(frame, settings=settings, connect=connect))
    assert exc.value.code == 'cortex_instance_mismatch'
    assert 'existing-create-service' not in db.actions and db.closed


@pytest.mark.parametrize('case', ['adopt', 'reissue', 'unknown', 'extra-dsn', 'missing-credential',
    'invalid-credential', 'wrong-uuid', 'implicit-source', 'missing-console'])
def test_invalid_private_command_cannot_connect(case, monkeypatch):
    p = product(); db, settings, principal, response, frame, connect = native_fixture(p, monkeypatch)
    if case in ('adopt', 'reissue', 'unknown'): frame['mode'] = case
    elif case == 'extra-dsn': frame['database_url'] = 'prohibited'
    elif case == 'missing-credential': frame.pop('credential')
    elif case == 'invalid-credential': frame['credential'] = 'invalid\nsecret'
    elif case == 'wrong-uuid': frame['installation_id'] = 'not-a-uuid'
    elif case == 'implicit-source': frame['request']['source_project'] = 'existing'
    else: frame['request']['with_console'] = False
    with pytest.raises(p.NativeRefusal): asyncio.run(p.execute_private(frame, settings=settings, connect=connect))
    assert db.actions == [] and not db.closed


def test_payload_inventory_hashes_actual_all_source_and_migrations(tmp_path):
    p = product(); src = tmp_path / 'src'; migrations = tmp_path / 'migrations'
    src.mkdir(); migrations.mkdir(); (src / 'actual.py').write_bytes(b'actual source')
    (src / 'data.json').write_bytes(b'actual data'); (migrations / '0001.sql').write_bytes(b'actual SQL')
    cache = src / '__pycache__'; cache.mkdir(); (cache / 'ignored.pyc').write_bytes(b'generated cache')
    expected = {'src/actual.py': hashlib.sha256(b'actual source').hexdigest(),
                'src/data.json': hashlib.sha256(b'actual data').hexdigest(),
                'migrations/0001.sql': hashlib.sha256(b'actual SQL').hexdigest()}
    result = p.payload_inventory(src, migrations)
    assert result['files'] == expected
    assert result['sha256'] == hashlib.sha256(json.dumps(expected, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    (src / 'actual.py').write_bytes(b'changed despite unchanged source label')
    assert p.payload_inventory(src, migrations)['sha256'] != result['sha256']


def test_linked_payload_source_refuses(tmp_path):
    p = product(); src = tmp_path / 'src'; migrations = tmp_path / 'migrations'; src.mkdir(); migrations.mkdir()
    target = tmp_path / 'outside'; target.write_bytes(b'outside'); (src / 'linked.py').symlink_to(target)
    with pytest.raises(p.NativeRefusal): p.payload_inventory(src, migrations)


class CatalogConnection:
    def __init__(self):
        self.queries = []
        self.role = {'role_name': 'cortex_v2_app', 'rolsuper': False, 'rolbypassrls': False, 'migrator_member': False}
        self.relations = [{'name': 'cortex_core.records', 'rls': True, 'forced': True, 'app_owns': False, 'app_direct_grant': True},
                          {'name': 'cortex_auth.credentials', 'rls': False, 'forced': False, 'app_owns': False, 'app_direct_grant': False}]
        self.ledger = [{'migration_id': '0001_core.sql', 'checksum_sha256': 'a' * 64}]
        self.project = {'scope_id': '40000000-0000-4000-8000-000000000004', 'primary_alias': 'kos-primary', 'primary_root': '/physical/project'}
    async def fetchrow(self, sql, *args):
        self.queries.append(sql)
        return self.role if 'pg_roles' in sql else self.project
    async def fetch(self, sql, *args):
        self.queries.append(sql)
        return self.ledger if 'schema_migrations' in sql else self.relations


@pytest.mark.parametrize('case', ['valid', 'superuser', 'bypass', 'migrator', 'wrong-role', 'app-owner',
    'rls-disabled', 'rls-unforced', 'function-mediated-grant', 'missing-relation', 'extra-relation',
    'migration-mismatch', 'migration-missing', 'migration-extra', 'wrong-project', 'wrong-root'])
def test_live_native_catalog_measures_every_required_security_property(case, monkeypatch):
    p = product(); db = CatalogConnection()
    expected_relations = [{k: row[k] for k in ('name', 'rls', 'forced', 'app_direct_grant')} for row in db.relations]
    if case == 'superuser': db.role['rolsuper'] = True
    elif case == 'bypass': db.role['rolbypassrls'] = True
    elif case == 'migrator': db.role['migrator_member'] = True
    elif case == 'wrong-role': db.role['role_name'] = 'other'
    elif case == 'app-owner': db.relations[0]['app_owns'] = True
    elif case == 'rls-disabled': db.relations[0]['rls'] = False
    elif case == 'rls-unforced': db.relations[0]['forced'] = False
    elif case == 'function-mediated-grant': db.relations[1]['app_direct_grant'] = True
    elif case == 'missing-relation': db.relations.pop()
    elif case == 'extra-relation': db.relations.append({'name': 'cortex_core.unexpected', 'rls': False, 'forced': False, 'app_owns': False, 'app_direct_grant': True})
    elif case == 'migration-mismatch': db.ledger[0]['checksum_sha256'] = 'b' * 64
    elif case == 'migration-missing': db.ledger.clear()
    elif case == 'migration-extra': db.ledger.append({'migration_id': 'extra.sql', 'checksum_sha256': 'b' * 64})
    elif case == 'wrong-project': db.project['primary_alias'] = 'foreign'
    elif case == 'wrong-root': db.project['primary_root'] = '/foreign'
    principal = SimpleNamespace(principal_id=uuid.uuid4(), installation_id=uuid.UUID(INSTALLATION))
    args = dict(project='kos-primary', project_root='/physical/project', migrations={'0001_core.sql': 'a' * 64},
                expected_relations=expected_relations)
    if case == 'valid':
        value = asyncio.run(p.read_native_database(db, principal, **args))
        assert value['migration_checksums_match'] is True and value['required_rls_enabled_and_forced'] is True
        assert value['app_role_non_superuser_without_bypassrls'] is True
        assert value['app_role_not_migrator_or_table_owner'] is True
        assert value['project_binding'] == db.project
    else:
        with pytest.raises(p.NativeRefusal): asyncio.run(p.read_native_database(db, principal, **args))
    assert db.queries and all(sql.lstrip().upper().startswith('SELECT') for sql in db.queries)
    assert any('pg_roles' in q for q in db.queries)
