"""Phase C RLS audit-tables extension — contract tests (Task #79 closure).

Companion to test_phase_c_rls.py. Verifies the audit extension migrations
apply RLS to 5 additional tables that store project-scoped data via
project_key or project_id columns:

  project_key tables (direct match):
    - cortex_projects, cortex_project_paths

  project_id tables (subquery against cortex_projects):
    - harness_artifacts, memory_sync_events, profile_bundles

The unset-project case is a stronger assertion than the original migration
because the project_id policy uses a subquery that returns NULL when
cortex.project is unset; NULL never matches via `=`, so unset session sees
zero rows on id tables (defensive default).
"""

from __future__ import annotations

import os
import pytest
import pytest_asyncio

import asyncpg


PG_DSN = os.environ.get("CORTEX_TEST_PG_DSN", "").strip()
TEST_PROJECT = os.environ.get("CORTEX_TEST_PROJECT", "").strip()

pytestmark = pytest.mark.skipif(
    not PG_DSN or not TEST_PROJECT,
    reason="CORTEX_TEST_PG_DSN and CORTEX_TEST_PROJECT are required for direct RLS tests",
)


PROJECT_KEY_TABLES = {"cortex_projects", "cortex_project_paths"}
PROJECT_ID_TABLES = {"harness_artifacts", "memory_sync_events", "profile_bundles"}
EXTENSION_TABLES = PROJECT_KEY_TABLES | PROJECT_ID_TABLES


@pytest_asyncio.fixture
async def conn():
    c = await asyncpg.connect(PG_DSN)
    yield c
    await c.close()


@pytest.mark.asyncio
async def test_audit_tables_rls_enabled(conn):
    """All 5 extension tables have RLS enabled."""
    rows = await conn.fetch("""
        SELECT relname FROM pg_class c
        JOIN pg_namespace n ON c.relnamespace = n.oid
        WHERE n.nspname = 'public' AND c.relkind = 'r' AND relrowsecurity = TRUE
    """)
    enabled = {r["relname"] for r in rows}
    missing = EXTENSION_TABLES - enabled
    assert not missing, f"RLS not enabled on extension tables: {sorted(missing)}"


@pytest.mark.asyncio
async def test_audit_tables_have_isolation_policies(conn):
    """Each extension table has its canonical <table>_project_isolation policy."""
    rows = await conn.fetch("""
        SELECT tablename, policyname FROM pg_policies
        WHERE schemaname = 'public'
          AND policyname LIKE '%_project_isolation'
    """)
    by_table = {r["tablename"]: r["policyname"] for r in rows}
    for tbl in EXTENSION_TABLES:
        assert tbl in by_table, f"No project_isolation policy on {tbl}"
        assert by_table[tbl] == f"{tbl}_project_isolation", (
            f"Wrong policy name on {tbl}: {by_table[tbl]}"
        )


@pytest.mark.asyncio
async def test_project_key_isolation_under_non_superuser(conn):
    """project_key tables filter to the explicitly selected test project.

    The regression must not depend on any product or customer project default.
    It verifies the selected project sees exactly its own project row/path and
    an unset session sees zero rows.
    """
    # Ensure test role exists (idempotent)
    role_exists = await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cortex_app_test')"
    )
    if not role_exists:
        await conn.execute("CREATE ROLE cortex_app_test NOLOGIN")
        await conn.execute("GRANT USAGE ON SCHEMA public TO cortex_app_test")
        await conn.execute("GRANT SELECT ON ALL TABLES IN SCHEMA public TO cortex_app_test")

    # Baseline as superuser — current project must exist and have a path.
    project_exists = await conn.fetchval(
        "SELECT COUNT(*) FROM cortex_projects WHERE project_key = $1", TEST_PROJECT
    )
    project_paths_baseline = await conn.fetchval(
        "SELECT COUNT(*) FROM cortex_project_paths WHERE project_key = $1", TEST_PROJECT
    )
    assert project_exists == 1, f"test requires cortex_projects row for {TEST_PROJECT}"
    assert project_paths_baseline >= 1, f"test requires >=1 path for {TEST_PROJECT}"

    await conn.execute("SET ROLE cortex_app_test")
    try:
        await conn.execute("SELECT set_config('cortex.project', $1, false)", TEST_PROJECT)
        project_rows = await conn.fetchval("SELECT COUNT(*) FROM cortex_projects")
        project_paths = await conn.fetchval("SELECT COUNT(*) FROM cortex_project_paths")

        # Unset
        await conn.execute("SELECT set_config('cortex.project', '', false)")
        unset_projects = await conn.fetchval("SELECT COUNT(*) FROM cortex_projects")
    finally:
        await conn.execute("RESET ROLE")
        await conn.execute("SELECT set_config('cortex.project', '', false)")

    assert project_rows == 1, (
        f"{TEST_PROJECT} session must see exactly 1 cortex_projects row; got {project_rows}"
    )
    assert project_paths >= 1, (
        f"{TEST_PROJECT} session should see at least 1 path; got {project_paths}"
    )
    assert unset_projects == 0, (
        f"unset session must see 0 cortex_projects rows (defensive default); "
        f"got {unset_projects}"
    )


@pytest.mark.asyncio
async def test_project_id_isolation_via_subquery(conn):
    """project_id tables filter via the selected project subquery.

    The policy uses cortex.project to resolve the project's UUID. Unset sessions
    resolve to NULL and match nothing.
    """
    role_exists = await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cortex_app_test')"
    )
    if not role_exists:
        await conn.execute("CREATE ROLE cortex_app_test NOLOGIN")
        await conn.execute("GRANT USAGE ON SCHEMA public TO cortex_app_test")
        await conn.execute("GRANT SELECT ON ALL TABLES IN SCHEMA public TO cortex_app_test")

    # Baseline counts as superuser
    superuser_artifacts = await conn.fetchval("SELECT COUNT(*) FROM harness_artifacts")
    superuser_sync = await conn.fetchval("SELECT COUNT(*) FROM memory_sync_events")
    superuser_bundles = await conn.fetchval("SELECT COUNT(*) FROM profile_bundles")

    await conn.execute("SET ROLE cortex_app_test")
    try:
        await conn.execute("SELECT set_config('cortex.project', $1, false)", TEST_PROJECT)
        project_artifacts = await conn.fetchval(
            "SELECT COUNT(*) FROM harness_artifacts"
        )
        project_sync = await conn.fetchval("SELECT COUNT(*) FROM memory_sync_events")
        project_bundles = await conn.fetchval("SELECT COUNT(*) FROM profile_bundles")

        # Unset session
        await conn.execute("SELECT set_config('cortex.project', '', false)")
        unset_artifacts = await conn.fetchval("SELECT COUNT(*) FROM harness_artifacts")
        unset_sync = await conn.fetchval("SELECT COUNT(*) FROM memory_sync_events")
        unset_bundles = await conn.fetchval("SELECT COUNT(*) FROM profile_bundles")
    finally:
        await conn.execute("RESET ROLE")
        await conn.execute("SELECT set_config('cortex.project', '', false)")

    # Current-project session sees only its own project_id rows; <= superuser baseline.
    assert project_artifacts <= superuser_artifacts, (
        f"{TEST_PROJECT} harness_artifacts ({project_artifacts}) "
        f"must be <= superuser baseline ({superuser_artifacts})"
    )
    assert project_sync <= superuser_sync
    assert project_bundles <= superuser_bundles

    # Unset session sees 0 (NULL subquery never matches)
    assert unset_artifacts == 0, (
            f"unset session must see 0 harness_artifacts (NULL subquery never matches); "
        f"got {unset_artifacts}"
    )
    assert unset_sync == 0, (
        f"unset session must see 0 memory_sync_events; got {unset_sync}"
    )
    assert unset_bundles == 0, (
        f"unset session must see 0 profile_bundles; got {unset_bundles}"
    )


@pytest.mark.asyncio
async def test_global_tables_remain_unrestricted(conn):
    """Tables with no project column are NOT covered by RLS.

    cortex_meta, retention_config, amad_loop_passes are legitimately global
    (no project column) — Phase C extension intentionally skips them.
    """
    rows = await conn.fetch("""
        SELECT tablename FROM pg_tables
        WHERE schemaname = 'public'
          AND tablename IN ('cortex_meta', 'retention_config', 'amad_loop_passes')
          AND rowsecurity = TRUE
    """)
    rls_on_global = {r["tablename"] for r in rows}
    assert not rls_on_global, (
        f"Global tables incorrectly have RLS enabled: {sorted(rls_on_global)} — "
        f"these have no project column and should be unrestricted"
    )
