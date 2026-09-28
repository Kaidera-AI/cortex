"""Disposable PG18 test: v1 schema fragments copied from production v1 DDL.

Requires an isolated cx-conv-* PostgreSQL container with the v2 roles and
extensions, and CORTEX_V2_CONVERTER_SYNTHETIC_TEST=1. No live DSN is accepted.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import uuid
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import asyncpg
import pytest

from cortex_v2 import migrate
from cortex_v2.config import INSTANCE_PROFILES, KAI_TEST_INSTANCE
from cortex_v2.converter import ensure_ledger_schema

pytestmark = pytest.mark.skipif(
    os.environ.get("CORTEX_V2_CONVERTER_SYNTHETIC_TEST") != "1",
    reason="requires the isolated synthetic converter database",
)

HOST = "127.0.0.1"
TARGET_URL = f"postgresql://cortex_v2_migrator@{HOST}:5432/cortex_v2"
SOURCE_URL = f"postgresql://postgres@{HOST}:5432/legacy_restore"
SOURCE_PROJECT_ID = uuid.UUID("fb9fa792-5fd0-4a39-8e27-0eb4de7e844d")
SOURCE_OWNER_ID = uuid.UUID("cad15a18-b3a9-49c9-8130-2d81e9d21d0c")
SOURCE_INSTALLATION_ID = "synthetic-source-installation"


# Relevant statements from the real v1 public.decisions/handoffs CREATE TABLE DDL.
V1_TABLES = (
    """CREATE TABLE public.decisions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    sprint_id uuid, agent_id uuid, summary text NOT NULL, rationale text,
    outcome text, category text, files_affected text[], tags text[],
    embedding public.vector(768), created_at timestamptz DEFAULT now(),
    superseded_by uuid, agent_name text, project text, invalidated_at timestamptz,
    metadata jsonb DEFAULT '{}'::jsonb, times_selected integer DEFAULT 0,
    times_applied integer DEFAULT 0, times_completed integer DEFAULT 0,
    times_fallback integer DEFAULT 0, quality_score real, parent_decision_id uuid,
    generation integer DEFAULT 0, supersession_summary text,
    search_vector tsvector, parent_goal_id text, project_id uuid, actor_id uuid,
    compacted boolean DEFAULT false NOT NULL
) WITH (autovacuum_vacuum_scale_factor='0.02',
        autovacuum_analyze_scale_factor='0.01')""",
    """CREATE TABLE public.handoffs (
    id uuid DEFAULT gen_random_uuid() NOT NULL, project text NOT NULL,
    from_agent text NOT NULL, from_role text, to_role text NOT NULL,
    priority text DEFAULT 'medium'::text, sprint_id uuid, branch text,
    summary text NOT NULL, files_changed text[], verification text,
    next_steps text, context text, acceptance jsonb DEFAULT '{}'::jsonb NOT NULL,
    evidence jsonb DEFAULT '{}'::jsonb NOT NULL,
    retry jsonb DEFAULT '{}'::jsonb NOT NULL,
    retry_count integer DEFAULT 0 NOT NULL,
    escalation jsonb DEFAULT '{}'::jsonb NOT NULL,
    status text DEFAULT 'pending'::text, claimed_by text, claimed_at timestamptz,
    completed_at timestamptz, created_at timestamptz DEFAULT now(),
    invalidated_at timestamptz, terminal_reason text, to_agent text,
    parent_goal_id text, project_id uuid, from_actor_id uuid, to_actor_id uuid,
    claimed_by_actor_id uuid,
    CONSTRAINT handoffs_priority_check CHECK
        (priority = ANY (ARRAY['low','medium','high','urgent'])),
    CONSTRAINT handoffs_status_check CHECK
        (status = ANY (ARRAY['pending','claimed','completed','released',
                             'abandoned','failed','archived']))
)""",
)
V1_PROVENANCE_TABLES = (
    "CREATE TABLE public.cortex_projects "
    "(id uuid PRIMARY KEY, project_key text NOT NULL)",
    "CREATE TABLE cortex_auth.principals "
    "(id uuid PRIMARY KEY, project_id uuid NOT NULL, "
    "installation_id text NOT NULL, disabled_at timestamptz)",
    "CREATE TABLE cortex_auth.grants "
    "(principal_id uuid PRIMARY KEY, scopes text[] NOT NULL, "
    "disabled_at timestamptz)",
)



def _fixture() -> dict[str, object]:
    prefix = uuid.UUID("cf1b36d5-683d-43b1-a55d-9772f6af4a07")
    uuid_fields = (*migrate.W1_FIXTURE_UUID_FIELDS,)
    fixture: dict[str, object] = {
        "instance_id": KAI_TEST_INSTANCE,
        "credential_generation": 1,
        "recovery_generation": 1,
        "project_alias": "fixture-project",
        "shared_alias": "fixture-shared",
        "local_alias": "fixture-local",
        "ungranted_alias": "fixture-ungranted",
    }
    fixture.update({field: str(uuid.uuid5(prefix, field)) for field in uuid_fields})
    pepper = b"synthetic-test-only-pepper-32-bytes"
    for name in ("owner", "worker", "recovery"):
        token = f"synthetic-{name}-fixture-never-production"
        fixture[f"{name}_hash" if name == "recovery" else f"{name}_credential_hash"] = (
            hmac.new(pepper, token.encode(), hashlib.sha256).hexdigest()
        )
    return fixture


async def prepare_synthetic_databases() -> dict[str, object]:
    """Run the actual profile migration path, not a hand-built target schema."""
    fixture = _fixture()
    secrets_dir = Path("/run/secrets")
    credentials = (("fixture.json", json.dumps(fixture)), ("db-url", TARGET_URL))
    for name, content in credentials:
        secret = secrets_dir / name
        secret.write_text(content)
        secret.chmod(0o600)
    with patch.dict(os.environ, {
        "CORTEX_V2_SANDBOX_INSTANCE": KAI_TEST_INSTANCE,
        "CORTEX_V2_FIXTURE_FILE": "/run/secrets/fixture.json",
        "CORTEX_V2_MIGRATOR_DATABASE_URL_FILE": "/run/secrets/db-url",
    }), patch.object(
        migrate, "active_profile",
        return_value=replace(INSTANCE_PROFILES[KAI_TEST_INSTANCE], database_host=HOST),
    ):
        await migrate.apply()
    source = await asyncpg.connect(SOURCE_URL)
    try:
        await source.execute("CREATE SCHEMA IF NOT EXISTS cortex_auth")
        for name, ddl in zip(
            ("public.cortex_projects", "cortex_auth.principals",
             "cortex_auth.grants"),
            V1_PROVENANCE_TABLES, strict=True,
        ):
            if await source.fetchval("SELECT to_regclass($1)", name) is None:
                await source.execute(ddl)
        await source.execute(
            "INSERT INTO public.cortex_projects(id, project_key) "
            "VALUES ($1,'fixture-project') ON CONFLICT (id) DO NOTHING",
            SOURCE_PROJECT_ID,
        )
        await source.execute(
            "INSERT INTO cortex_auth.principals"
            "(id,project_id,installation_id) VALUES ($1,$2,$3) "
            "ON CONFLICT (id) DO NOTHING",
            SOURCE_OWNER_ID, SOURCE_PROJECT_ID, SOURCE_INSTALLATION_ID,
        )
        await source.execute(
            "INSERT INTO cortex_auth.grants(principal_id,scopes) "
            "VALUES ($1,ARRAY['instance:admin']::text[]) "
            "ON CONFLICT (principal_id) DO NOTHING",
            SOURCE_OWNER_ID,
        )
        await source.execute("CREATE EXTENSION IF NOT EXISTS vector SCHEMA public")
        for name, ddl in zip(("decisions", "handoffs"), V1_TABLES, strict=True):
            exists = await source.fetchval("SELECT to_regclass($1)", f"public.{name}")
            if exists is None:
                await source.execute(ddl)
            has_primary_key = await source.fetchval(
                """SELECT EXISTS (
                     SELECT 1 FROM pg_index
                      WHERE indrelid = to_regclass($1) AND indisprimary
                   )""",
                f"public.{name}",
            )
            if not has_primary_key:
                await source.execute(
                    f"ALTER TABLE ONLY public.{name} ADD CONSTRAINT "
                    f"{name}_pkey PRIMARY KEY (id)"
                )
    finally:
        await source.close()
    return fixture


def test_converter_ledger_enforces_one_outcome_per_physical_v1_row() -> None:
    async def check() -> None:
        fixture = await prepare_synthetic_databases()
        source = await asyncpg.connect(SOURCE_URL)
        source_transaction = source.transaction()
        await source_transaction.start()
        connection = await asyncpg.connect(TARGET_URL)
        try:
            await ensure_ledger_schema(connection)
            transaction = connection.transaction()
            await transaction.start()
            try:
                run_id = uuid.uuid4()
                source_id = await source.fetchval(
                    """INSERT INTO public.handoffs
                       (project, from_agent, to_role, summary)
                       VALUES ('fixture-project', 'synthetic-writer',
                               'implementer', 'Synthetic relay') RETURNING id"""
                )
                await connection.execute(
                    """INSERT INTO cortex_conversion.runs
                       (run_id, snapshot_sha256, source_schema_hash,
                        target_schema_hash, converter_revision, policy_sha256,
                        policy_version, installation_id)
                       VALUES ($1,$2,$2,$2,'synthetic-revision',$2,
                               'synthetic', $3)""",
                    run_id, "a" * 64, uuid.UUID(str(fixture["installation_id"])),
                )
                await connection.execute(
                    """INSERT INTO cortex_conversion.outcomes
                       (run_id, source_schema, source_table, source_pk,
                        source_hash, source_scope, target_relation,
                        target_scope_id, target_id, outcome)
                       VALUES ($1,'public','handoffs',$2,$3,'fixture-project',
                               'cortex_coord.handoffs',$4,$5,'migrated')""",
                    run_id, str(source_id), b"x" * 32,
                    uuid.UUID(str(fixture["project_scope_id"])), source_id,
                )
                with pytest.raises(asyncpg.UniqueViolationError):
                    async with connection.transaction():
                        await connection.execute(
                            """INSERT INTO cortex_conversion.outcomes
                               (run_id, source_schema, source_table, source_pk,
                                source_hash, source_scope, target_relation,
                                target_scope_id, target_id, outcome)
                               VALUES ($1,'public','handoffs',$2,$3,
                                      'fixture-project','cortex_coord.handoffs',
                                      $4,$5,'migrated')""",
                            run_id, str(source_id), b"y" * 32,
                            uuid.UUID(str(fixture["project_scope_id"])), source_id,
                        )
                assert await connection.fetchval(
                    "SELECT count(*) FROM cortex_conversion.outcomes WHERE run_id=$1",
                    run_id,
                ) == 1
            finally:
                await transaction.rollback()
        finally:
            await connection.close()
            await source_transaction.rollback()
            await source.close()

    asyncio.run(check())
