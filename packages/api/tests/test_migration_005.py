"""Migration 005 idempotency test.

Tests that 005_skills_rules_bindings.sql:
  1. Applies cleanly to a fresh DB.
  2. Applies a second time without error (idempotent).
  3. Creates the three expected tables with their UNIQUE constraints.

This test is skipped automatically when ``psycopg2`` or a running Postgres is
unavailable, so it does not block the normal (no-DB) test run.  The scratch DB
is expected to be provided via the env var ``CORTEX_TEST_PG_DSN`` — typically
``postgresql://postgres:x@localhost:55999/postgres`` started by the caller.

In CI (or when the env var is absent) the test is skipped gracefully.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

MIGRATION_PATH = (
    Path(__file__).resolve().parents[3]
    / "local-cortex"
    / "migrations"
    / "005_skills_rules_bindings.sql"
)
TEST_DSN = os.environ.get("CORTEX_TEST_PG_DSN", "")


def _psycopg2():
    """Return psycopg2 module or None if not installed."""
    try:
        import psycopg2  # type: ignore

        return psycopg2
    except ImportError:
        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _apply_migration(conn) -> None:
    sql = MIGRATION_PATH.read_text(encoding="utf-8")
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()


def _table_exists(conn, table: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT EXISTS("
            "  SELECT 1 FROM information_schema.tables"
            "  WHERE table_schema='public' AND table_name=%s"
            ")",
            (table,),
        )
        return bool(cur.fetchone()[0])


def _unique_constraint_exists(conn, constraint_name: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT EXISTS("
            "  SELECT 1 FROM information_schema.table_constraints"
            "  WHERE constraint_type='UNIQUE' AND constraint_name=%s"
            ")",
            (constraint_name,),
        )
        return bool(cur.fetchone()[0])


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not TEST_DSN, reason="CORTEX_TEST_PG_DSN not set — scratch DB not available")
def test_migration_005_applies_cleanly():
    pg = _psycopg2()
    if pg is None:
        pytest.skip("psycopg2 not installed")

    conn = pg.connect(TEST_DSN)
    try:
        _apply_migration(conn)
        assert _table_exists(conn, "agent_skills"), "agent_skills table not created"
        assert _table_exists(conn, "agent_skill_bindings"), "agent_skill_bindings table not created"
        assert _table_exists(conn, "rules"), "rules table not created"
    finally:
        conn.rollback()
        conn.close()


@pytest.mark.skipif(not TEST_DSN, reason="CORTEX_TEST_PG_DSN not set — scratch DB not available")
def test_migration_005_is_idempotent():
    pg = _psycopg2()
    if pg is None:
        pytest.skip("psycopg2 not installed")

    conn = pg.connect(TEST_DSN)
    try:
        # Apply twice — must not raise
        _apply_migration(conn)
        _apply_migration(conn)
        assert _table_exists(conn, "agent_skills")
        assert _table_exists(conn, "agent_skill_bindings")
        assert _table_exists(conn, "rules")
    finally:
        conn.rollback()
        conn.close()


@pytest.mark.skipif(not TEST_DSN, reason="CORTEX_TEST_PG_DSN not set — scratch DB not available")
def test_migration_005_unique_constraints_exist():
    pg = _psycopg2()
    if pg is None:
        pytest.skip("psycopg2 not installed")

    conn = pg.connect(TEST_DSN)
    try:
        _apply_migration(conn)
        assert _unique_constraint_exists(conn, "agent_skills_project_skill_slug_version_key"), \
            "UNIQUE constraint on agent_skills not found"
        # PostgreSQL truncates constraint names at 63 bytes — the bindings table
        # name is long enough to trigger truncation.
        assert _unique_constraint_exists(conn, "agent_skill_bindings_project_subject_kind_subject_skill_slu_key"), \
            "UNIQUE constraint on agent_skill_bindings not found"
        assert _unique_constraint_exists(conn, "rules_project_rule_slug_version_key"), \
            "UNIQUE constraint on rules not found"
    finally:
        conn.rollback()
        conn.close()
