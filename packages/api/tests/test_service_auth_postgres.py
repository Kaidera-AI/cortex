"""Opt-in SQL/asyncpg proof against an EXPLICIT EMPTY disposable database.

Set CORTEX_SERVICE_AUTH_TEST_DSN to a loopback PostgreSQL database whose name is
cortex_auth_test_<suffix>. Set CORTEX_REQUIRE_POSTGRES_TESTS=1 in a qualification
run to fail, not skip, if that explicit DSN is missing. Both cortex_app and
cortex_reader roles must already exist so permission assertions cannot be vacuous.
This never falls back to a runtime/default DSN, never
imports main, never creates a role/database, and refuses any existing user table.
The fixture creates only its three minimal canonical-registry tables (matching
the columns this store consumes) plus cortex_auth; teardown drops precisely those
objects. This is not full-baseline or container/API qualification.
"""
import asyncio
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

import service_auth as auth


REGISTRIES = """
CREATE TABLE public.cortex_projects (
    id uuid PRIMARY KEY, project_key text UNIQUE NOT NULL, status text NOT NULL DEFAULT 'active'
);
CREATE TABLE public.cortex_actors (
    id uuid PRIMARY KEY, project_id uuid NOT NULL REFERENCES public.cortex_projects(id),
    slug text NOT NULL, status text NOT NULL DEFAULT 'active'
);
CREATE TABLE public.agents (
    id uuid PRIMARY KEY, name text NOT NULL, project text NOT NULL,
    project_id uuid REFERENCES public.cortex_projects(id),
    actor_id uuid REFERENCES public.cortex_actors(id), status text NOT NULL DEFAULT 'available'
);
"""


@pytest_asyncio.fixture
async def pg_env():
    dsn = os.environ.get("CORTEX_SERVICE_AUTH_TEST_DSN")
    if not dsn:
        if os.environ.get("CORTEX_REQUIRE_POSTGRES_TESTS") == "1":
            pytest.fail("Required SQL qualification needs CORTEX_SERVICE_AUTH_TEST_DSN; no database was selected")
        pytest.skip("No explicitly supplied empty disposable auth-test database")
    parsed = urlsplit(dsn)
    database = unquote(parsed.path).removeprefix("/")
    if (parsed.scheme not in {"postgres", "postgresql"}
            or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or not re.fullmatch(r"cortex_auth_test_[a-z0-9_]+", database)
            or parsed.query or parsed.fragment):
        pytest.fail("Auth integration requires a loopback cortex_auth_test_<suffix> DSN without query overrides")
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
    owned = False
    try:
        async with pool.acquire() as conn:
            actual_db = await conn.fetchval("SELECT current_database()")
            assert actual_db == database
            tables = await conn.fetchval("""SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                    WHERE n.nspname NOT IN ('pg_catalog','information_schema') AND n.nspname NOT LIKE 'pg_toast%'
                      AND c.relkind IN ('r','p')""")
            auth_schema = await conn.fetchval("SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname='cortex_auth')")
            if tables or auth_schema:
                pytest.fail("Refusing nonempty auth-test database; no objects were changed")
            for role in ("cortex_app", "cortex_reader"):
                if not await conn.fetchval("SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname=$1)", role):
                    pytest.fail(f"Required runtime role {role} is absent; no objects were changed")
            await conn.execute(REGISTRIES)
            owned = True
            sql = (Path(__file__).parents[2] / "data/migrations/2026-09-05-01-service-auth.sql").read_text()
            await conn.execute(sql)
            project_id, actor_id, agent_id = uuid4(), uuid4(), uuid4()
            await conn.execute("INSERT INTO public.cortex_projects(id,project_key) VALUES($1,'notes')", project_id)
            await conn.execute("INSERT INTO public.cortex_actors(id,project_id,slug) VALUES($1,$2,'writer')", actor_id, project_id)
            await conn.execute("INSERT INTO public.agents(id,name,project,project_id,actor_id) VALUES($1,'writer','notes',$2,$3)", agent_id, project_id, actor_id)
        now = [datetime.now(timezone.utc)]
        store = auth.ServiceAuthStore(lambda: pool, clock=lambda: now[0])
        yield store, pool, now, project_id, agent_id
    finally:
        try:
            if owned:
                async with pool.acquire() as conn:
                    # Exact objects created by this fixture, only in the verified
                    # originally empty disposable database. Never truncate shared data.
                    await conn.execute("DROP SCHEMA IF EXISTS cortex_auth CASCADE")
                    await conn.execute("DROP TABLE public.agents, public.cortex_actors, public.cortex_projects")
        finally:
            await pool.close()


async def bootstrap(env):
    store, pool, _, project_id, agent_id = env
    setup = await store.create_setup_grant(authority=auth.LOCAL_OWNER)
    async def resolved(conn):
        assert await conn.fetchval("SELECT id FROM public.agents WHERE id=$1", agent_id) == agent_id
        return project_id, agent_id
    issued = await store.consume_setup_grant(setup.raw_token, authority=auth.LOCAL_OWNER,
                                            resolve_identity=resolved, installation_id="cli-test", scopes=["memory:read", "memory:write"])
    return issued


@pytest.mark.asyncio
async def test_postgres_schema_hash_only_scopes_registry_and_permissions(pg_env):
    store, pool, now, _, _ = pg_env
    issued = await bootstrap(pg_env)
    assert await store.authenticate(issued.raw_token) == issued.context
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM cortex_auth.tokens WHERE id=$1", issued.context.token_id)
        assert len(row["token_digest"]) == 32 and issued.raw_token not in repr(dict(row))
        assert row["expires_at"] - row["issued_at"] == timedelta(days=30)
        assert row["rotate_after"] - row["issued_at"] == timedelta(days=21)
        public_usage = await conn.fetchval("""SELECT EXISTS(SELECT 1 FROM pg_namespace n,
            LATERAL aclexplode(n.nspacl) acl WHERE n.nspname='cortex_auth'
            AND acl.grantee=0 AND acl.privilege_type='USAGE')""")
        assert not public_usage
        for role in ("cortex_app", "cortex_reader"):
            assert not await conn.fetchval("SELECT has_schema_privilege($1,'cortex_auth','USAGE')", role)
        with pytest.raises(asyncpg.RaiseError):
            await conn.execute("UPDATE cortex_auth.principals SET installation_id='changed' WHERE id=$1", issued.context.principal_id)
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute("UPDATE cortex_auth.grants SET scopes=ARRAY['*'] WHERE principal_id=$1", issued.context.principal_id)
    await store.set_grant_scopes(issued.context.principal_id, ["memory:read", "tokens:manage"], authority=auth.LOCAL_OWNER)
    assert (await store.authenticate(issued.raw_token)).scopes == {"memory:read"}
    async with pool.acquire() as conn:
        await conn.execute("UPDATE public.cortex_actors SET status='retired' WHERE id=$1", issued.context.actor_id)
    with pytest.raises(auth.AuthInvalid):
        await store.authenticate(issued.raw_token)


@pytest.mark.asyncio
async def test_postgres_real_bootstrap_race_atomic_callback_and_single_grant(pg_env):
    store, pool, _, project_id, agent_id = pg_env
    setup = await store.create_setup_grant(authority=auth.LOCAL_OWNER)
    calls = []
    async def resolved(conn):
        calls.append(1)
        await conn.execute("SELECT pg_sleep(0.03)")
        return project_id, agent_id
    async def consume():
        return await store.consume_setup_grant(setup.raw_token, authority=auth.LOCAL_OWNER,
                                              resolve_identity=resolved, installation_id="cli-test", scopes=["memory:read"])
    results = await asyncio.gather(consume(), consume(), return_exceptions=True)
    assert sum(isinstance(r, auth.IssuedToken) for r in results) == 1
    assert sum(isinstance(r, auth.AuthInvalid) for r in results) == 1
    assert len(calls) == 1
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM cortex_auth.principals") == 1
        assert await conn.fetchval("SELECT count(*) FROM cortex_auth.tokens") == 1
        assert await conn.fetchval("SELECT count(*) FROM cortex_auth.setup_grants WHERE consumed_at IS NOT NULL") == 1


@pytest.mark.asyncio
async def test_postgres_callback_rollback_preserves_setup_and_registry(pg_env):
    store, pool, _, project_id, agent_id = pg_env
    setup = await store.create_setup_grant(authority=auth.LOCAL_OWNER)
    async def failed(conn):
        await conn.execute("UPDATE public.cortex_projects SET project_key='must-rollback' WHERE id=$1", project_id)
        raise RuntimeError("registration aborted")
    with pytest.raises(RuntimeError):
        await store.consume_setup_grant(setup.raw_token, authority=auth.LOCAL_OWNER,
                                       resolve_identity=failed, installation_id="cli-test", scopes=["memory:read"])
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT project_key FROM public.cortex_projects WHERE id=$1", project_id) == "notes"
        assert await conn.fetchval("SELECT consumed_at FROM cortex_auth.setup_grants WHERE id=$1", setup.grant_id) is None
        assert await conn.fetchval("SELECT count(*) FROM cortex_auth.principals") == 0


@pytest.mark.asyncio
async def test_postgres_rotation_ack_expiry_disable_recovery_restore(pg_env):
    store, pool, now, project_id, agent_id = pg_env
    issued = await bootstrap(pg_env)
    now[0] += timedelta(days=21)
    assert [r["id"] for r in await store.due_rotations(authority=auth.LOCAL_OWNER)] == [issued.context.token_id]
    candidate = await store.rotate(issued.context.token_id, authority=auth.LOCAL_OWNER)
    assert await store.authenticate(candidate.raw_token)
    with pytest.raises(auth.AuthForbidden):
        await store.acknowledge_rotation(candidate.context.token_id, authority=auth.LOCAL_OWNER)
    await store.observe_consumer_use(candidate.context)
    await store.acknowledge_rotation(candidate.context.token_id, authority=auth.LOCAL_OWNER)
    with pytest.raises(auth.AuthInvalid):
        await store.authenticate(issued.raw_token)
    assert await store.invalidate_restored_generations(authority=auth.LOCAL_OWNER) == 2
    with pytest.raises(auth.AuthInvalid):
        await store.authenticate(candidate.raw_token)
    setup = await store.create_setup_grant(authority=auth.LOCAL_OWNER, purpose="recovery")
    async def resolved(conn):
        return project_id, agent_id
    recovered = await store.consume_setup_grant(setup.raw_token, authority=auth.LOCAL_OWNER,
                                               resolve_identity=resolved, installation_id="cli-test", scopes=["memory:read"], purpose="recovery")
    assert recovered.context.generation == 2
    assert await store.authenticate(recovered.raw_token)
    await store.disable_grant(recovered.context.principal_id, authority=auth.LOCAL_OWNER)
    with pytest.raises(auth.AuthInvalid):
        await store.authenticate(recovered.raw_token)
    with pytest.raises(auth.AuthForbidden):
        await store.issue(authority=auth.LOCAL_OWNER, project_id=project_id, agent_id=agent_id,
                          installation_id="cli-test", scopes=["memory:read"], recover=True)
