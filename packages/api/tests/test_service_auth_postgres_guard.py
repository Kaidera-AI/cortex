"""Offline regressions for the opt-in real-SQL fixture's qualification guards.

These exercise the actual fixture, replacing only its connection pool. They are
not SQL evidence; the complete sibling suite must also run against PostgreSQL.
"""
import importlib.util
from pathlib import Path

import pytest


spec = importlib.util.spec_from_file_location(
    "cortex_auth_postgres_fixture_guard_target",
    Path(__file__).with_name("test_service_auth_postgres.py"),
)
sql_tests = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sql_tests)

DATABASE = "cortex_auth_test_guard"
DSN = f"postgresql://test:test@127.0.0.1:15432/{DATABASE}"
ROLES = {"cortex_app", "cortex_reader"}


class FakeConnection:
    def __init__(self, *, roles=ROLES, tables=0, auth_schema=False, fail_drop=None):
        self.roles = roles
        self.tables = tables
        self.auth_schema = auth_schema
        self.fail_drop = fail_drop
        self.role_checks = []
        self.executed = []
        self.cleanup_error = RuntimeError("synthetic cleanup failure")

    async def fetchval(self, query, *args):
        if "current_database()" in query:
            return DATABASE
        if "FROM pg_class" in query:
            return self.tables
        if "FROM pg_namespace" in query:
            return self.auth_schema
        if "FROM pg_roles" in query:
            self.role_checks.append(args[0])
            return args[0] in self.roles
        raise AssertionError(f"Unexpected guard query: {query}")

    async def execute(self, query, *args):
        self.executed.append(query)
        if self.fail_drop and query.startswith(self.fail_drop):
            raise self.cleanup_error


class FakePool:
    def __init__(self, conn, *, fail_cleanup_acquire=False):
        self.conn = conn
        self.fail_cleanup_acquire = fail_cleanup_acquire
        self.acquire_count = 0
        self.close_count = 0
        self.cleanup_error = RuntimeError("synthetic cleanup acquisition failure")

    def acquire(self):
        pool = self

        class Acquisition:
            async def __aenter__(self):
                pool.acquire_count += 1
                if pool.fail_cleanup_acquire and pool.acquire_count == 2:
                    raise pool.cleanup_error
                return pool.conn

            async def __aexit__(self, *exc):
                return False

        return Acquisition()

    async def close(self):
        self.close_count += 1


@pytest.fixture(autouse=True)
def no_inherited_qualification_environment(monkeypatch):
    monkeypatch.delenv("CORTEX_SERVICE_AUTH_TEST_DSN", raising=False)
    monkeypatch.delenv("CORTEX_REQUIRE_POSTGRES_TESTS", raising=False)


def supply_pool(monkeypatch, pool):
    async def create_pool(dsn, **kwargs):
        assert dsn == DSN
        assert kwargs == {"min_size": 1, "max_size": 4}
        return pool

    monkeypatch.setenv("CORTEX_SERVICE_AUTH_TEST_DSN", DSN)
    monkeypatch.setattr(sql_tests.asyncpg, "create_pool", create_pool)
    return sql_tests.pg_env.__wrapped__()


def prohibit_pool(monkeypatch):
    async def unexpected_pool(*args, **kwargs):
        pytest.fail("A missing DSN must never select or connect to any database")

    monkeypatch.setattr(sql_tests.asyncpg, "create_pool", unexpected_pool)


@pytest.mark.asyncio
async def test_required_sql_without_dsn_fails_instead_of_skipping(monkeypatch):
    prohibit_pool(monkeypatch)
    monkeypatch.setenv("CORTEX_REQUIRE_POSTGRES_TESTS", "1")
    fixture = sql_tests.pg_env.__wrapped__()
    with pytest.raises(BaseException) as caught:
        await anext(fixture)
    assert isinstance(caught.value, pytest.fail.Exception), type(caught.value)
    assert "CORTEX_SERVICE_AUTH_TEST_DSN" in str(caught.value)


@pytest.mark.asyncio
async def test_developer_opt_in_without_dsn_remains_explicit_skip(monkeypatch):
    prohibit_pool(monkeypatch)
    fixture = sql_tests.pg_env.__wrapped__()
    with pytest.raises(pytest.skip.Exception, match="No explicitly supplied"):
        await anext(fixture)


@pytest.mark.asyncio
@pytest.mark.parametrize("roles", [set(), {"cortex_app"}, {"cortex_reader"}])
async def test_missing_runtime_roles_fail_before_schema_writes(monkeypatch, roles):
    conn = FakeConnection(roles=roles)
    pool = FakePool(conn)
    fixture = supply_pool(monkeypatch, pool)
    try:
        with pytest.raises(pytest.fail.Exception, match="runtime role"):
            await anext(fixture)
    finally:
        await fixture.aclose()
    assert conn.executed == []
    assert pool.close_count == 1


@pytest.mark.asyncio
async def test_both_runtime_roles_checked_and_successful_cleanup_closes_once(monkeypatch):
    conn = FakeConnection()
    pool = FakePool(conn)
    fixture = supply_pool(monkeypatch, pool)
    await anext(fixture)
    await fixture.aclose()
    assert conn.role_checks == ["cortex_app", "cortex_reader"]
    assert [q for q in conn.executed if q.startswith("DROP")] == [
        "DROP SCHEMA IF EXISTS cortex_auth CASCADE",
        "DROP TABLE public.agents, public.cortex_actors, public.cortex_projects",
    ]
    assert pool.close_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["DROP SCHEMA", "DROP TABLE", "acquire"])
async def test_cleanup_failure_is_visible_and_pool_still_closes(monkeypatch, failure):
    conn = FakeConnection(fail_drop=None if failure == "acquire" else failure)
    pool = FakePool(conn, fail_cleanup_acquire=failure == "acquire")
    expected = pool.cleanup_error if failure == "acquire" else conn.cleanup_error
    fixture = supply_pool(monkeypatch, pool)
    await anext(fixture)
    with pytest.raises(RuntimeError) as caught:
        await fixture.aclose()
    assert caught.value is expected
    assert pool.close_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("existing", [{"tables": 1}, {"auth_schema": True}])
async def test_nonempty_database_refused_without_writes_or_drops(monkeypatch, existing):
    conn = FakeConnection(**existing)
    pool = FakePool(conn)
    fixture = supply_pool(monkeypatch, pool)
    with pytest.raises(pytest.fail.Exception, match="Refusing nonempty"):
        await anext(fixture)
    assert conn.executed == []
    assert pool.close_count == 1
