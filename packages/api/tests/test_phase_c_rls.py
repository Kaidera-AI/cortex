"""Phase C RLS defense-in-depth — contract tests (handoff 1f5746f2 Phase C).

Verifies:
1. RLS is enabled on every table with a `project` column.
2. The canonical `<table>_project_isolation` policy exists on each.
3. `acquire_scoped(project)` actually SETs `cortex.project` on the connection.
4. As a non-superuser role, RLS isolates rows by project.

These tests are **postgres-superuser-aware**: cortex-api connects as `postgres`
which BYPASSES RLS by default. The tests use a `cortex_app_test` non-superuser
role to verify enforcement, mirroring the future cortex_app cutover.
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


# Tables that should have RLS enabled per Phase C migration
EXPECTED_RLS_TABLES = {
    "agent_diaries", "agent_profiles", "agent_sessions", "agents",
    "archive_decisions", "archive_events", "archive_handoffs", "archive_lessons",
    "archive_messages", "artifact_edges", "artifacts", "captured_patterns",
    "cortex_audit_log", "cortex_entities", "cortex_relationships", "decisions",
    "execution_analyses", "handoffs", "knowledge", "lessons", "messages",
    "pattern_metrics", "session_sources", "sprints", "tasks", "team_events",
}


@pytest_asyncio.fixture
async def conn():
    c = await asyncpg.connect(PG_DSN)
    yield c
    await c.close()


@pytest.mark.asyncio
async def test_rls_enabled_on_all_project_tables(conn):
    """Every table with a project column has RLS enabled."""
    rows = await conn.fetch("""
        SELECT relname FROM pg_class c
        JOIN pg_namespace n ON c.relnamespace = n.oid
        WHERE n.nspname = 'public' AND c.relkind = 'r' AND relrowsecurity = TRUE
    """)
    enabled = {r["relname"] for r in rows}
    missing = EXPECTED_RLS_TABLES - enabled
    assert not missing, f"RLS not enabled on: {sorted(missing)}"


@pytest.mark.asyncio
async def test_isolation_policy_exists_on_every_table(conn):
    """Each RLS-enabled table has the canonical <table>_project_isolation policy."""
    rows = await conn.fetch("""
        SELECT tablename, policyname FROM pg_policies
        WHERE schemaname = 'public'
          AND policyname LIKE '%_project_isolation'
    """)
    by_table = {r["tablename"]: r["policyname"] for r in rows}
    for tbl in EXPECTED_RLS_TABLES:
        assert tbl in by_table, f"No project_isolation policy on {tbl}"
        assert by_table[tbl] == f"{tbl}_project_isolation", (
            f"Wrong policy name on {tbl}: {by_table[tbl]}"
        )


@pytest.mark.asyncio
async def test_acquire_scoped_sets_cortex_project(conn):
    """The acquire_scoped helper pattern sets cortex.project session-context.

    Reproduces what acquire_scoped() does in main.py and verifies the
    set_config call actually changes the value visible to subsequent queries.
    """
    # Fresh state
    await conn.execute("SELECT set_config('cortex.project', '', false)")
    val = await conn.fetchval("SELECT current_setting('cortex.project', TRUE)")
    assert val == "", f"expected empty, got {val!r}"

    # Set it like acquire_scoped would
    await conn.execute("SELECT set_config('cortex.project', $1, false)", TEST_PROJECT)
    val = await conn.fetchval("SELECT current_setting('cortex.project', TRUE)")
    assert val == TEST_PROJECT

    # Reset
    await conn.execute("SELECT set_config('cortex.project', '', false)")
    val = await conn.fetchval("SELECT current_setting('cortex.project', TRUE)")
    assert val == ""


@pytest.mark.asyncio
async def test_rls_isolates_under_non_superuser_role(conn):
    """Under a non-superuser role, RLS limits visible rows by project.

    This is the test that proves Phase C actually defends against the
    cross-project leak class-of-bug. As `postgres` we'd see all rows; as
    `cortex_app_test` we see only the project we SET cortex.project to.
    """
    # Ensure the test role exists
    role_exists = await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cortex_app_test')"
    )
    if not role_exists:
        await conn.execute("CREATE ROLE cortex_app_test NOLOGIN")
        await conn.execute("GRANT USAGE ON SCHEMA public TO cortex_app_test")
        await conn.execute("GRANT SELECT ON ALL TABLES IN SCHEMA public TO cortex_app_test")

    # Baseline as superuser — see all decisions
    superuser_count = await conn.fetchval("SELECT COUNT(*) FROM decisions")

    # Switch to non-superuser; no cortex.project — should see only _global rows
    await conn.execute("SET ROLE cortex_app_test")
    try:
        await conn.execute("SELECT set_config('cortex.project', '', false)")
        no_scope_count = await conn.fetchval("SELECT COUNT(*) FROM decisions")

        # With a project scope set, see that project's rows plus _global.
        await conn.execute(
            "SELECT set_config('cortex.project', $1, false)", TEST_PROJECT
        )
        project_count = await conn.fetchval("SELECT COUNT(*) FROM decisions")

        # With cortex.project=nonexistent — see only _global rows (zero)
        await conn.execute(
            "SELECT set_config('cortex.project', 'nonexistent_project_xyz', false)"
        )
        nonexistent_count = await conn.fetchval("SELECT COUNT(*) FROM decisions")
    finally:
        await conn.execute("RESET ROLE")
        await conn.execute("SELECT set_config('cortex.project', '', false)")

    # Assertions:
    #   - no scope: at most _global rows visible (small or zero)
    #   - test project: more rows than no-scope (the selected project has decisions)
    #   - test project < superuser: superuser sees all projects
    #   - nonexistent: only _global rows (matches no-scope OR is zero)
    assert project_count < superuser_count, (
        f"{TEST_PROJECT} rows ({project_count}) should be < superuser ({superuser_count}) — "
        f"if equal, RLS isn't filtering"
    )
    assert project_count > no_scope_count, (
        f"{TEST_PROJECT} ({project_count}) should reveal more than no-scope ({no_scope_count})"
    )
    assert nonexistent_count == no_scope_count, (
        f"nonexistent project ({nonexistent_count}) should equal no-scope ({no_scope_count}) — "
        f"both should see only _global rows"
    )
