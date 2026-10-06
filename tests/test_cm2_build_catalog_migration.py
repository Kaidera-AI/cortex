"""Owned migration connection and public bytes only; no native connection effects."""
import asyncio
import hashlib
import inspect
import json
from types import SimpleNamespace

import pytest

CASES = ['applied', 'replayed', 'ordinary', 'revision', 'bool-revision', 'not-ci',
         'darwin', 'arm64', 'production', 'foreign-instance', 'role-refusal',
         'fixture-refusal', 'query-refusal', 'catalog-refusal', 'close-refusal']
MARKER = 'PUBLIC_MIGRATION_CATALOG_EXCEPTION_SENTINEL'


@pytest.mark.parametrize('case', CASES)
def test_native_catalog_uses_same_verified_migrator_and_refuses_unsafe_invocation(case, tmp_path, monkeypatch, capsys):
    from cortex_v2 import migrate, build_catalog
    from cortex_v2.native_prerequisite import NativeRefusal
    assert 'build_catalog_source' in inspect.signature(migrate.apply).parameters, 'native CI catalog migration caller is missing'
    directory = tmp_path / 'migrations'; directory.mkdir(mode=0o700)
    sql = b'SELECT 1;\n'; (directory / '0001_core.sql').write_bytes(sql)
    checksum = hashlib.sha256(sql).hexdigest()
    profile = SimpleNamespace(instance_id='cortex_v2_package_test', migrations=('0001_core.sql',), database_host='db')
    if case == 'production': profile.instance_id = migrate.PRODUCTION_INSTANCE
    if case == 'foreign-instance': profile.instance_id = 'foreign'
    revision = None if case == 'ordinary' else ('invalid' if case == 'revision' else True if case == 'bool-revision' else '1' * 40)
    monkeypatch.setattr(migrate, 'active_profile', lambda: profile)
    monkeypatch.setattr(migrate, 'MIGRATION_DIRECTORY', directory)
    monkeypatch.setenv('GITHUB_ACTIONS', 'false' if case == 'not-ci' else 'true')
    monkeypatch.setattr(migrate.platform, 'system', lambda: 'Darwin' if case == 'darwin' else 'Linux')
    monkeypatch.setattr(migrate.platform, 'machine', lambda: 'aarch64' if case == 'arm64' else 'x86_64')
    events = []
    monkeypatch.setattr(migrate, '_sandbox_fixture', lambda value: events.append('fixture-read') or {})
    monkeypatch.setattr(migrate, '_migrator_url', lambda host: events.append('connection-input') or 'PUBLIC_INTERCEPTED_CONNECTION')

    class Transaction:
        async def __aenter__(self): events.append('transaction')
        async def __aexit__(self, *args): events.append('transaction-exit')

    class Connection:
        async def execute(self, query, *args):
            events.append('execute')
            if case == 'query-refusal': raise RuntimeError(MARKER)
        async def fetchval(self, query, *args):
            assert 'checksum_sha256' in query and args == ('0001_core.sql',)
            return checksum if case == 'replayed' else None
        def transaction(self): return Transaction()
        async def close(self):
            events.append('close')
            if case == 'close-refusal': raise RuntimeError(MARKER)

    connection = Connection()
    async def connect(url, *, command_timeout):
        assert url == 'PUBLIC_INTERCEPTED_CONNECTION' and command_timeout == 60
        events.append('connect'); return connection
    async def roles(value):
        assert value is connection; events.append('roles')
        if case == 'role-refusal': raise RuntimeError(MARKER)
    async def fixture(value, body):
        assert value is connection and body == {}; events.append('fixture')
        if case == 'fixture-refusal': raise RuntimeError(MARKER)
    catalog = {'schema': 'cortex.rls-inventory.v1', 'source_revision': '1' * 40,
               'api_source_payload_sha256': '2' * 64, 'migrations': {'0001_core.sql': checksum},
               'relations': [{'name': 'cortex_core.records', 'rls': True, 'forced': True, 'app_direct_grant': True}]}
    async def materialize(value, *, source_revision):
        assert value is connection and source_revision == '1' * 40 and events[-1] == 'fixture'
        events.append('catalog')
        if case == 'catalog-refusal': raise RuntimeError(MARKER)
        return catalog
    monkeypatch.setattr(migrate.asyncpg, 'connect', connect)
    monkeypatch.setattr(migrate, '_assert_role_contract', roles)
    monkeypatch.setattr(migrate, '_seed_sandbox', fixture)
    monkeypatch.setattr(build_catalog, 'materialize_catalog', materialize)
    if case in ('applied', 'replayed', 'ordinary'):
        asyncio.run(migrate.apply(build_catalog_source=revision))
        value = json.loads(capsys.readouterr().out)
        expected = {'instance': profile.instance_id, 'migrations': [{'migration': '0001_core.sql', 'checksum': checksum}],
                    'status': 'verified' if case == 'replayed' else 'applied', 'fixture': 'verified'}
        if case != 'ordinary': expected['build_catalog'] = catalog
        assert value == expected and events.count('connect') == events.count('close') == 1
        assert events.count('catalog') == (0 if case == 'ordinary' else 1)
        assert events[-1] == 'close'
    else:
        with pytest.raises(NativeRefusal) as error:
            asyncio.run(migrate.apply(build_catalog_source=revision))
        assert error.value.code in ('cortex_bad_arguments', 'cortex_health_degraded', 'cortex_image_mismatch')
        assert MARKER not in str(error.value)
        assert capsys.readouterr().out == ''
        if case in CASES[3:10]: assert events == []
        else: assert events.count('connect') == events.count('close') == 1 and events[-1] == 'close'


@pytest.mark.parametrize('case', ['catalog', 'ordinary', 'unknown-option'])
def test_migration_cli_forwards_only_explicit_catalog_source(case, monkeypatch):
    from cortex_v2 import migrate
    assert 'argv' in inspect.signature(migrate.main).parameters, 'explicit native CI catalog CLI is missing'
    calls = []
    async def apply(*, build_catalog_source=None): calls.append(build_catalog_source)
    monkeypatch.setattr(migrate, 'apply', apply)
    if case == 'unknown-option':
        with pytest.raises(SystemExit) as error: migrate.main(['--unexpected-catalog', '1' * 40])
        assert error.value.code == 2 and calls == []
    else:
        migrate.main(['--linux-ci-build-catalog-source', '1' * 40] if case == 'catalog' else [])
        assert calls == ['1' * 40 if case == 'catalog' else None]
