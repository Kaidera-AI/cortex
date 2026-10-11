"""Shared test helpers for the Cortex API test suite.

E006 Inc04 (Roster-as-Data) made the writer guard registry-driven: every write/
handoff endpoint now calls the async ``load_roster_policy(project)`` resolver,
which issues two reads —

  1. ``SELECT metadata FROM cortex_projects WHERE project_key=$1 ...``  (admin pool)
  2. ``SELECT lower(name), capabilities->>'writer_scope', role FROM agents a ...``
     and ``... FROM roles ...``                                         (scoped pool)

Tests that drive a guard-protected endpoint through a hand-rolled fake connection
need to answer those reads. The helpers below provide the seeded-kaidera-os answers
(work writers {kai,ren}; system writers {beat,migration,system}) so a fake Conn
can delegate to them in its ``fetchrow``/``fetch`` dispatch before falling back to
its own ``AssertionError``. This keeps the registry shape defined in exactly one
place and behaviour-preserving for the existing endpoint tests.

The dedicated guard tests (test_kaidera_os_writer_guard / test_agent_name_guard /
test_roster_as_data) deliberately DO NOT use these — they exercise the real
resolver against their own fixtures to prove the data path.
"""

import ipaddress
import re
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest_asyncio

import os
from pathlib import Path

# Source CORTEX_ADMIN_TOKEN from local-cortex/.env — the SAME file the cortex-api container reads
# (compose `env_file:`). Without it, admin-guarded endpoints 403 for BOTH the in-process guard
# (module-level ADMIN_TOKEN, read at import) AND the live integration tests (http_client → :8501,
# whose token must match the running container). We read the dev .env (no hardcoded secret),
# exactly as the container does — this is what made all 19 admin-token tests fail.
if not os.environ.get("CORTEX_ADMIN_TOKEN"):
    _env = Path(__file__).resolve().parents[3] / "local-cortex" / ".env"
    if _env.exists():
        for _line in _env.read_text().splitlines():
            if _line.startswith("CORTEX_ADMIN_TOKEN="):
                os.environ["CORTEX_ADMIN_TOKEN"] = _line.split("=", 1)[1].strip()
                break

# Seeded kaidera-os cortex_projects.metadata — mirrors the Step-1 backfill verbatim
# in the shape the resolver reads. Computed -> work={kai,ren}, system={beat,migration,system}.
SEEDED_kaidera_os_METADATA = {
    "enforce_writer_roster": True,
    "roster_policy": {
        "enforce_writer_roster": True,
        "roster_schema_version": "1",
        "default_writer_scope": "work",
        "system_event_writers": ["beat", "migration", "system"],
        "beat_may_create_handoff": True,
        "handoff_targets": "writers",
        "suggest_cutoff": 0.6,
    },
}

_SEEDED_kaidera_os_AGENTS = [
    {"n": "kai", "scope": "work", "role": "full-stack-developer"},
    {"n": "ren", "scope": "work", "role": "full-stack-developer"},
]


class _NotARosterRead(Exception):
    """Raised when a SQL string is not one of the resolver's registry reads."""


def roster_fetchrow(sql, args):
    """Answer the resolver's cortex_projects.metadata read; else signal _NotARosterRead.

    Usage inside a fake Conn.fetchrow, just before its own AssertionError fallback:

        try:
            return roster_fetchrow(sql, args)
        except _NotARosterRead:
            pass
        raise AssertionError(...)
    """
    if "SELECT metadata FROM cortex_projects" in sql:
        return {"metadata": SEEDED_kaidera_os_METADATA}
    raise _NotARosterRead


def roster_fetch(sql, args):
    """Answer the resolver's agents/roles reads; else signal _NotARosterRead."""
    if "writer_scope" in sql and "FROM agents a" in sql:
        project = args[0] if args else None
        return list(_SEEDED_kaidera_os_AGENTS) if project == "kaidera-os" else []
    if "FROM roles" in sql and "default_capabilities" in sql:
        return []
    raise _NotARosterRead


# This fixture is requested only by the two RLS suites. The marker belongs to
# their disposable provisioner; test setup deliberately cannot create it.


class OwnedRlsRefusal(RuntimeError):
    pass


_RLS_TABLES = (
    "decisions", "cortex_projects", "cortex_project_paths",
    "harness_artifacts", "memory_sync_events", "profile_bundles",
)


def _owned_rls_settings():
    dsn = os.environ.get("CORTEX_OWNED_RLS_DSN", "").strip()
    if not dsn:
        raise OwnedRlsRefusal("owned RLS setup requires an explicit DSN opt-in")
    if dsn != os.environ.get("CORTEX_TEST_PG_DSN", "").strip():
        raise OwnedRlsRefusal("owned RLS DSN must match the test connection DSN")
    try:
        parsed = urlsplit(dsn)
        host = parsed.hostname or ""
        port = parsed.port or 5432
    except ValueError:
        raise OwnedRlsRefusal("owned RLS DSN endpoint is invalid") from None
    if parsed.scheme not in {"postgres", "postgresql"} or not parsed.path.strip("/"):
        raise OwnedRlsRefusal("owned RLS DSN must name a PostgreSQL database")
    if port in {5499, 5500, 8501}:
        raise OwnedRlsRefusal("owned RLS setup refuses a live service port")
    try:
        local = host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        local = False
    if not local:
        raise OwnedRlsRefusal("owned RLS target must use pod-local TCP loopback")
    if {"host", "hostaddr", "port", "dbname"}.intersection(
        parse_qs(parsed.query, keep_blank_values=True)
    ):
        raise OwnedRlsRefusal("owned RLS DSN route overrides are forbidden")
    run_id = os.environ.get("CORTEX_OWNED_RLS_RUN_ID", "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", run_id):
        raise OwnedRlsRefusal("owned RLS setup requires a provisioner run ID")
    project = os.environ.get("CORTEX_TEST_PROJECT", "").strip()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,79}", project):
        raise OwnedRlsRefusal("owned RLS setup requires a selected test project")
    return {"dsn": dsn, "run_id": run_id, "project": project}


async def _require_owned_rls_marker(conn, run_id):
    marker = await conn.fetchrow(
        "SELECT c.relkind::text AS relkind FROM pg_class c WHERE "
        "c.oid=to_regclass('cortex_test_fixture.owned_rls')"
    )
    if not marker or marker["relkind"] != "r":
        raise OwnedRlsRefusal("owned RLS target is unmarked; setup refused before mutation")
    row = await conn.fetchrow(
        "SELECT run_id FROM cortex_test_fixture.owned_rls "
        "WHERE run_id=$1 AND database_name=current_database()",
        run_id,
    )
    if not row:
        raise OwnedRlsRefusal("owned RLS provisioner marker does not match the run ID")


async def _grant_owned_rls_test_role(conn, state):
    role = await conn.fetchrow(
        "SELECT rolsuper,rolbypassrls,rolcanlogin FROM pg_roles "
        "WHERE rolname='cortex_app_test'"
    )
    if role and (role["rolsuper"] or role["rolbypassrls"] or role["rolcanlogin"]):
        raise OwnedRlsRefusal("owned RLS test role must be non-superuser, non-bypass and NOLOGIN")
    if await conn.fetchval(
        "SELECT EXISTS(SELECT 1 FROM pg_class WHERE "
        "relnamespace='public'::regnamespace AND "
        "relname=ANY($1::text[]) AND pg_get_userbyid(relowner)='cortex_app_test')",
        list(_RLS_TABLES),
    ):
        raise OwnedRlsRefusal("owned RLS test role must not own the protected tables")
    state["created_role"] = role is None
    if role is None:
        await conn.execute("CREATE ROLE cortex_app_test NOLOGIN NOSUPERUSER NOBYPASSRLS")
    state["added_usage"] = not await conn.fetchval(
        "SELECT has_schema_privilege('cortex_app_test','public','USAGE')"
    )
    if state["added_usage"]:
        await conn.execute("GRANT USAGE ON SCHEMA public TO cortex_app_test")
    state["added_select"] = []
    for table in _RLS_TABLES:
        if not await conn.fetchval(
            "SELECT has_table_privilege('cortex_app_test',$1,'SELECT')", "public." + table
        ):
            await conn.execute(f"GRANT SELECT ON public.{table} TO cortex_app_test")
            state["added_select"].append(table)


async def _seed_owned_rls_projects_and_rows(conn, state):
    own = state["project"]
    foreign = "rls-foreign-" + uuid4().hex[:16]
    if await conn.fetchval("SELECT EXISTS(SELECT 1 FROM cortex_projects WHERE project_key=$1)", own):
        raise OwnedRlsRefusal("selected RLS test project already exists; fixture will not reuse it")
    state["projects"] = []
    for project in [own, foreign]:
        root = "/owned-rls/" + state["run_id"] + "/" + project
        pid = await conn.fetchval(
            "INSERT INTO cortex_projects(project_key,display_name,repo_root,repo_type) "
            "VALUES($1,$1,$2,'repo') RETURNING id", project, root,
        )
        state["projects"].append({"key": project, "id": pid, "root": root})
        await conn.execute(
            "INSERT INTO decisions(project,summary,project_id) VALUES($1,'owned RLS fixture',$2)",
            project, pid,
        )
        await conn.execute(
            "INSERT INTO harness_artifacts(harness,path,generated_from_hash,last_compiled_at,status,project_id) "
            "VALUES('owned-rls',$1,$2,now(),'current',$3)", root, "f" * 64, pid,
        )
        await conn.execute(
            "INSERT INTO memory_sync_events(source,target,direction,content_hash,conflict_policy,result,project_id) "
            "VALUES('owned-rls','owned-rls','to-cortex',$1,'owned','accepted',$2)", "f" * 64, pid,
        )
        await conn.execute(
            "INSERT INTO profile_bundles(agent_name,version,content_hash,source_paths,rendered_markdown,rendered_json,project_id) "
            "VALUES('owned-rls',1,$1,'[]'::jsonb,'owned RLS fixture','{}'::jsonb,$2)", "f" * 64, pid,
        )


async def _seed_owned_rls_project_paths(conn, state):
    for project in state["projects"]:
        await conn.execute(
            "INSERT INTO cortex_project_paths(project_key,root_path,path_kind) VALUES($1,$2,'primary')",
            project["key"], project["root"],
        )


async def _prepare_owned_rls_environment():
    state = _owned_rls_settings()
    import asyncpg
    conn = await asyncpg.connect(state["dsn"])
    try:
        # No BEGIN, DDL, grants or data writes may precede this marker check.
        await _require_owned_rls_marker(conn, state["run_id"])
        async with conn.transaction():
            await _grant_owned_rls_test_role(conn, state)
            await _seed_owned_rls_projects_and_rows(conn, state)
            await _seed_owned_rls_project_paths(conn, state)
    finally:
        await conn.close()
    return state


async def _cleanup_owned_rls_environment(state):
    import asyncpg
    conn = await asyncpg.connect(state["dsn"])
    try:
        await _require_owned_rls_marker(conn, state["run_id"])
        async with conn.transaction():
            ids = [p["id"] for p in state["projects"]]
            keys = [p["key"] for p in state["projects"]]
            for table in ["harness_artifacts", "memory_sync_events", "profile_bundles"]:
                await conn.execute(f"DELETE FROM public.{table} WHERE project_id=ANY($1::uuid[])", ids)
            await conn.execute("DELETE FROM decisions WHERE project_id=ANY($1::uuid[])", ids)
            await conn.execute("DELETE FROM cortex_project_paths WHERE project_key=ANY($1::text[])", keys)
            await conn.execute("DELETE FROM cortex_projects WHERE id=ANY($1::uuid[])", ids)
            for table in state["added_select"]:
                await conn.execute(f"REVOKE SELECT ON public.{table} FROM cortex_app_test")
            if state["added_usage"]:
                await conn.execute("REVOKE USAGE ON SCHEMA public FROM cortex_app_test")
            if state["created_role"]:
                await conn.execute("DROP ROLE cortex_app_test")
    finally:
        await conn.close()


@pytest_asyncio.fixture(scope="module")
async def owned_rls_environment():
    state = await _prepare_owned_rls_environment()
    try:
        yield state
    finally:
        await _cleanup_owned_rls_environment(state)
