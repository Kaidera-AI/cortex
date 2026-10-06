"""Actual public payload bytes and intercepted migrated catalog; no native effects."""
import asyncio
import copy
import hashlib
import importlib.util
import json

import pytest

CASES = ['valid', 'bad-revision', 'bool-revision', 'missing-source', 'missing-migrations',
         'symlink-source', 'symlink-migration', 'changed-payload', 'oversized-source',
         'migrator-role', 'migrator-super', 'app-role', 'app-bypass', 'migrator-member',
         'schema-owner', 'app-owner', 'absent-relations', 'duplicate-relations',
         'unsorted-relations', 'unknown-relation', 'unknown-field', 'nonbool', 'unforced',
         'direct-grant', 'missing-ledger', 'duplicate-ledger', 'wrong-ledger',
         'extra-migration', 'tx-refusal', 'query-timeout', 'private-error', 'oversized-inventory']
MARKER = 'PUBLIC_BUILD_CATALOG_PRIVATE_EXCEPTION_SENTINEL'


@pytest.mark.asyncio
@pytest.mark.parametrize('case', CASES)
async def test_materialized_catalog_binds_actual_migrated_rows_and_payload_bytes(case, tmp_path, monkeypatch):
    assert importlib.util.find_spec('cortex_v2.build_catalog') is not None, 'actual migrated build-catalog unit is unavailable'
    from cortex_v2 import build_catalog as module
    from cortex_v2.native_prerequisite import NativeRefusal, payload_inventory
    assert hasattr(module, 'materialize_catalog'), 'finite migrated catalog materialization is unavailable'
    source = tmp_path / 'src'; source.mkdir(mode=0o700)
    source_file = source / 'public.py'; source_file.write_bytes(b'PUBLIC_BUILD_PAYLOAD = 1\n')
    migrations = tmp_path / 'migrations'; migrations.mkdir(mode=0o700)
    migration_file = migrations / '0001_core.sql'; migration_file.write_bytes(b'SELECT 1;\n')
    expected_hash = hashlib.sha256(migration_file.read_bytes()).hexdigest()
    revision = '1' * 40
    rows = [{'name': 'cortex_auth.credentials', 'rls': False, 'forced': False, 'app_direct_grant': False, 'app_owns': False},
            {'name': 'cortex_core.records', 'rls': True, 'forced': True, 'app_direct_grant': True, 'app_owns': False}]
    ledger = [{'migration_id': '0001_core.sql', 'checksum_sha256': expected_hash}]
    expected_payload = payload_inventory(source, migrations)['sha256']
    if case == 'bad-revision': revision = 'not-a-frozen-revision'
    if case == 'bool-revision': revision = True
    if case == 'missing-source': source = tmp_path / 'absent-source'
    if case == 'missing-migrations': migrations = tmp_path / 'absent-migrations'
    if case == 'symlink-source':
        original = source_file.with_suffix('.public'); source_file.rename(original); source_file.symlink_to(original)
    if case == 'symlink-migration':
        original = migration_file.with_suffix('.public'); migration_file.rename(original); migration_file.symlink_to(original)
    if case == 'oversized-source': source_file.write_bytes(b'x' * (8 * 1024 * 1024 + 1))
    if case == 'app-owner': rows[1]['app_owns'] = True
    if case == 'absent-relations': rows = []
    if case == 'duplicate-relations': rows.append(copy.deepcopy(rows[1]))
    if case == 'unsorted-relations': rows.reverse()
    if case == 'unknown-relation': rows[1]['name'] = 'public.unexpected'
    if case == 'unknown-field': rows[1]['unexpected'] = True
    if case == 'nonbool': rows[1]['rls'] = 1
    if case == 'unforced': rows[1]['forced'] = False
    if case == 'direct-grant': rows[0]['app_direct_grant'] = True
    if case == 'missing-ledger': ledger = []
    if case == 'duplicate-ledger': ledger.append(copy.deepcopy(ledger[0]))
    if case == 'wrong-ledger': ledger[0]['checksum_sha256'] = 'f' * 64
    if case == 'extra-migration': (migrations / '0002_extra.sql').write_bytes(b'SELECT 2;\n')
    if case == 'oversized-inventory':
        rows = [{'name': 'cortex_core.relation_' + str(i).zfill(4), 'rls': True, 'forced': True,
                 'app_direct_grant': True, 'app_owns': False} for i in range(800)]
    calls = []

    class Transaction:
        async def __aenter__(self):
            calls.append('transaction-enter')
            if case == 'tx-refusal': raise RuntimeError(MARKER)
        async def __aexit__(self, *args): calls.append('transaction-exit')

    class Connection:
        def transaction(self, **kw):
            assert kw == {'isolation': 'repeatable_read', 'readonly': True}
            calls.append('readonly-repeatable-transaction'); return Transaction()
        async def fetchrow(self, query):
            assert 'current_user AS role_name' in query and 'FROM pg_roles' in query
            calls.append('current-role')
            if case == 'private-error': raise RuntimeError(MARKER)
            if case == 'query-timeout': await asyncio.sleep(0.05)
            return {'role_name': 'foreign' if case == 'migrator-role' else 'cortex_v2_migrator',
                    'rolsuper': case == 'migrator-super', 'rolbypassrls': False}
        async def fetchval(self, query):
            calls.append('role-value')
            if 'pg_has_role' in query: return case == 'migrator-member'
            assert 'SELECT count(*)' in query and "owner.rolname = 'cortex_v2_app'" in query
            return int(case == 'app-owner')
        async def fetch(self, query):
            assert query.lstrip().startswith('SELECT'); calls.append('select')
            if 'rolname, rolcanlogin' in query:
                roles = [{'rolname': name, 'rolcanlogin': True, 'rolbypassrls': False, 'rolsuper': False,
                          'rolcreaterole': False, 'rolcreatedb': False} for name in ('cortex_v2_app', 'cortex_v2_migrator')]
                if case == 'app-role': roles.pop(0)
                if case == 'app-bypass': roles[0]['rolbypassrls'] = True
                return roles
            if 'nspname, pg_get_userbyid(nspowner)' in query:
                return [{'nspname': name, 'owner': 'foreign' if case == 'schema-owner' else 'cortex_v2_migrator'}
                        for name in ('cortex_auth', 'cortex_core')]
            if 'migration_id, checksum_sha256' in query: return copy.deepcopy(ledger)
            assert "has_table_privilege('cortex_v2_app'" in query and 'AS app_direct_grant' in query
            assert "c.relkind IN ('r','p')" in query and 'ORDER BY name' in query
            if case == 'changed-payload': source_file.write_bytes(b'PUBLIC_CHANGED_BUILD_PAYLOAD = 2\n')
            return copy.deepcopy(rows)

    monkeypatch.setattr(module, 'COMMAND_TIMEOUT', 0.01)
    if case == 'valid':
        value = await module.materialize_catalog(Connection(), source_revision=revision,
                                                source_root=source, migration_root=migrations)
        assert value == {'schema': 'cortex.rls-inventory.v1', 'source_revision': revision,
                         'api_source_payload_sha256': expected_payload, 'migrations': {'0001_core.sql': expected_hash},
                         'relations': [{k: row[k] for k in ('name', 'rls', 'forced', 'app_direct_grant')} for row in rows]}
        assert calls[0] == 'readonly-repeatable-transaction' and calls[-1] == 'transaction-exit'
        assert calls.count('current-role') == 1
        assert len(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()) <= 65536
    else:
        with pytest.raises(NativeRefusal) as error:
            await module.materialize_catalog(Connection(), source_revision=revision,
                                             source_root=source, migration_root=migrations)
        assert error.value.code in ('cortex_bad_arguments', 'cortex_image_mismatch', 'cortex_health_degraded')
        assert MARKER not in str(error.value)
        if case in ('bad-revision', 'bool-revision', 'missing-source', 'missing-migrations',
                    'symlink-source', 'symlink-migration', 'oversized-source'):
            assert calls == []
