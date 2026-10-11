"""Owned RLS fixture guard contracts; the native refusal uses an unmarked lab DB."""

import os

import pytest

import conftest as rls_setup


def _opt_in(monkeypatch, dsn="postgresql://postgres@127.0.0.1:55431/rls_test"):
    monkeypatch.setenv("CORTEX_OWNED_RLS_DSN", dsn)
    monkeypatch.setenv("CORTEX_TEST_PG_DSN", dsn)
    monkeypatch.setenv("CORTEX_OWNED_RLS_RUN_ID", "owned-rls-guard-test")
    monkeypatch.setenv("CORTEX_TEST_PROJECT", "owned-rls-project")


def test_owned_rls_requires_explicit_dsn_opt_in(monkeypatch):
    _opt_in(monkeypatch)
    monkeypatch.delenv("CORTEX_OWNED_RLS_DSN")
    with pytest.raises(RuntimeError, match="explicit"):
        rls_setup._owned_rls_settings()


@pytest.mark.parametrize("port", [5499, 5500, 8501])
def test_owned_rls_refuses_live_ports(monkeypatch, port):
    _opt_in(monkeypatch, f"postgresql://postgres@127.0.0.1:{port}/rls_test")
    with pytest.raises(RuntimeError, match="port"):
        rls_setup._owned_rls_settings()


@pytest.mark.parametrize("host", ["192.0.2.7", "database.example", "127.0.0.1,192.0.2.7"])
def test_owned_rls_refuses_nonlocal_targets(monkeypatch, host):
    _opt_in(monkeypatch, f"postgresql://postgres@{host}:55431/rls_test")
    with pytest.raises(RuntimeError, match="loopback"):
        rls_setup._owned_rls_settings()


def test_owned_rls_refuses_dsn_mismatch(monkeypatch):
    _opt_in(monkeypatch)
    monkeypatch.setenv("CORTEX_TEST_PG_DSN", "postgresql://postgres@127.0.0.1:55432/other")
    with pytest.raises(RuntimeError, match="match"):
        rls_setup._owned_rls_settings()


def test_owned_rls_refuses_route_overrides(monkeypatch):
    _opt_in(monkeypatch, "postgresql://postgres@127.0.0.1:55431/rls_test?host=database.example")
    with pytest.raises(RuntimeError, match="override"):
        rls_setup._owned_rls_settings()


@pytest.mark.parametrize("key", ["host", "hostaddr", "port", "dbname"])
def test_owned_rls_refuses_blank_route_overrides(monkeypatch, key):
    _opt_in(monkeypatch, f"postgresql://postgres@127.0.0.1:55431/rls_test?{key}=")
    with pytest.raises(RuntimeError, match="override"):
        rls_setup._owned_rls_settings()


def test_owned_rls_requires_run_id(monkeypatch):
    _opt_in(monkeypatch)
    monkeypatch.delenv("CORTEX_OWNED_RLS_RUN_ID")
    with pytest.raises(RuntimeError, match="run ID"):
        rls_setup._owned_rls_settings()


async def _database_snapshot(conn):
    tables = await conn.fetch(
        "SELECT relname FROM pg_class WHERE relnamespace='public'::regnamespace "
        "AND relkind='r' ORDER BY relname"
    )
    counts = {}
    for row in tables:
        quoted = '"' + row["relname"].replace('"', '""') + '"'
        counts[row["relname"]] = await conn.fetchval(f"SELECT count(*) FROM public.{quoted}")
    grants = [tuple(row) for row in await conn.fetch(
        "SELECT grantee,table_name,privilege_type,is_grantable "
        "FROM information_schema.role_table_grants WHERE table_schema='public' "
        "ORDER BY grantee,table_name,privilege_type,is_grantable"
    )]
    schema_acl = await conn.fetchval("SELECT nspacl::text FROM pg_namespace WHERE nspname='public'")
    roles = [tuple(row) for row in await conn.fetch(
        "SELECT rolname,rolsuper,rolbypassrls,rolcanlogin FROM pg_roles ORDER BY rolname"
    )]
    return {"row_counts": counts, "grant_count": len(grants), "grants": grants,
            "schema_acl": schema_acl, "roles": roles}


@pytest.mark.asyncio
async def test_unmarked_owned_database_refusal_has_zero_writes(monkeypatch, request):
    dsn = os.environ.get("CORTEX_UNMARKED_RLS_TEST_DSN", "").strip()
    if not dsn:
        if os.environ.get("CORTEX_REQUIRE_OWNED_RLS_TESTS") == "1":
            pytest.fail("owned unmarked PostgreSQL fixture is required")
        pytest.skip("owned unmarked PostgreSQL fixture not provided")
    _opt_in(monkeypatch, dsn)
    settings = rls_setup._owned_rls_settings()
    import asyncpg
    conn = await asyncpg.connect(settings["dsn"])
    try:
        assert await conn.fetchval("SELECT to_regclass('cortex_test_fixture.owned_rls')") is None
        before = await _database_snapshot(conn)
        with pytest.raises(RuntimeError, match="unmarked"):
            await rls_setup._prepare_owned_rls_environment()
        after = await _database_snapshot(conn)
        request.node.user_properties.extend([("unmarked_before", before), ("unmarked_after", after)])
        assert before == after, "unmarked target changed rows, grants, roles or schema ACL"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_mismatched_marker_run_id_has_zero_writes(monkeypatch, request):
    dsn = os.environ.get("CORTEX_MARKED_RLS_TEST_DSN", "").strip()
    if not dsn:
        if os.environ.get("CORTEX_REQUIRE_OWNED_RLS_TESTS") == "1":
            pytest.fail("owned marked PostgreSQL fixture is required")
        pytest.skip("owned marked PostgreSQL fixture not provided")
    _opt_in(monkeypatch, dsn)
    settings = rls_setup._owned_rls_settings()
    import asyncpg
    conn = await asyncpg.connect(settings["dsn"])
    try:
        assert await conn.fetchval("SELECT to_regclass('cortex_test_fixture.owned_rls')") is not None
        assert not await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM cortex_test_fixture.owned_rls WHERE run_id=$1)",
            settings["run_id"],
        )
        before = await _database_snapshot(conn)
        with pytest.raises(RuntimeError, match="run ID"):
            await rls_setup._prepare_owned_rls_environment()
        after = await _database_snapshot(conn)
        request.node.user_properties.extend([("wrong_run_before", before), ("wrong_run_after", after)])
        assert before == after, "wrong marker run ID changed rows, grants, roles or schema ACL"
    finally:
        await conn.close()
