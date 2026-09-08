#!/usr/bin/env python3
"""Finite standalone migration owner; imports the canonical API apply engine.

Fresh volumes adopt the dump's proven prefix without executing historical SQL.
The remaining files are applied or reconciled by main.apply_schema_migrations.
The receipt reports those two categories separately; a rerun applies zero files.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

import asyncpg
import main as cortex
from db_tls import connection_kwargs

from baseline import (MIGRATIONS_TABLE, RECEIPT_TABLE, SAFE_ID,
                      inventory_sha256, load_manifest, validate_inventory)


MIGRATION_LOCK = "cortex:standalone-schema-migrations:v1"


async def schema_status(conn) -> dict:
    """Read the actual ledger identity even when inspecting a newer schema for rollback.

This deliberately neither creates a missing ledger nor reconciles it with this
payload's files. Compatibility belongs to the caller's signed payload manifest.
The apply path still rejects every unknown or changed ledger row.
    """
    rows = await conn.fetch(
        "SELECT migration_id, checksum_sha256 FROM cortex_schema_migrations ORDER BY migration_id"
    )
    inventory = [(str(row["migration_id"]), str(row["checksum_sha256"])) for row in rows]
    import re
    if any(not SAFE_ID.fullmatch(name) or not re.fullmatch(r"[0-9a-f]{64}", digest)
           for name, digest in inventory):
        raise RuntimeError("invalid standalone ledger identity")
    if len({name for name, _ in inventory}) != len(inventory):
        raise RuntimeError("duplicate standalone ledger identity")
    return {"schema_revision": inventory_sha256(inventory), "migration_count": len(inventory)}


def validate_applied(applied: dict[str, str], sources: dict[str, str],
                     baseline: list[tuple[str, str]]) -> None:
    for migration_id, checksum in applied.items():
        if migration_id not in sources:
            raise RuntimeError(f"unknown standalone migration ledger row: {migration_id}")
        if checksum != sources[migration_id]:
            raise RuntimeError(f"standalone migration checksum mismatch: {migration_id}")
    present = set(applied).intersection(dict(baseline))
    if present and present != set(dict(baseline)):
        raise RuntimeError("standalone migration ledger has partial baseline adoption")
    if applied and not present:
        raise RuntimeError("standalone migration ledger is nonempty before baseline adoption")


async def adopt_baseline(conn, *, migration_dir: Path | None = None,
                         manifest: dict | None = None) -> int:
    manifest = load_manifest() if manifest is None else manifest
    source_rows = cortex.schema_migration_files(migration_dir)
    by_id = {row["id"]: row for row in source_rows}
    checksums = {name: row["checksum_sha256"] for name, row in by_id.items()}
    expected = validate_inventory(manifest, checksums)
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", MIGRATION_LOCK)
        if not await conn.fetchval(
            "SELECT to_regclass($1) IS NOT NULL AND to_regclass($2) IS NOT NULL",
            f"public.{MIGRATIONS_TABLE}", f"public.{RECEIPT_TABLE}",
        ):
            raise RuntimeError("standalone database has no proven bootstrap baseline receipt")
        receipt = await conn.fetchrow(
            f"SELECT schema_version, source_revision, cutoff_exclusive, migration_count, "
            f"inventory_sha256, schema_source_sha256 FROM {RECEIPT_TABLE} WHERE singleton IS TRUE"
        )
        if receipt is None or dict(receipt) != manifest:
            raise RuntimeError("standalone bootstrap receipt does not match the pinned manifest")
        rows = await conn.fetch(
            f"SELECT migration_id, checksum_sha256 FROM {MIGRATIONS_TABLE} ORDER BY migration_id"
        )
        actual = [(str(row["migration_id"]), str(row["checksum_sha256"])) for row in rows]
        if actual != expected:
            raise RuntimeError("standalone bootstrap receipt inventory is incomplete or different")
        await cortex.ensure_schema_migrations_table(conn)
        ledger = await conn.fetch("SELECT migration_id, checksum_sha256 FROM cortex_schema_migrations")
        applied = {str(row["migration_id"]): str(row["checksum_sha256"]) for row in ledger}
        validate_applied(applied, checksums, expected)
        if applied:
            return 0
        for migration_id, checksum in expected:
            await conn.execute(
                """INSERT INTO cortex_schema_migrations
                   (migration_id, checksum_sha256, source_path, applied_by,
                    statement_status, surface_version)
                   VALUES ($1, $2, $3, $4, $5, $6)""",
                migration_id, checksum, by_id[migration_id]["path"],
                "cortex-standalone-baseline", "BASELINE ADOPTED; SQL NOT REPLAYED",
                cortex.CORTEX_SURFACE_VERSION,
            )
        return len(expected)


async def converge_roles(conn) -> None:
    """Remove password auth and keep the app login subject to row-level security."""
    await conn.execute("ALTER ROLE postgres PASSWORD NULL")
    await conn.execute(
        "REVOKE INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER "
        "ON cortex_schema_migrations FROM cortex_app"
    )
    await conn.execute(
        "ALTER ROLE cortex_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
        "NOREPLICATION NOBYPASSRLS PASSWORD NULL"
    )
    await conn.execute("""DO $roles$ BEGIN
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cortex_reader') THEN
            ALTER ROLE cortex_reader NOLOGIN PASSWORD NULL;
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cortex_app_test') THEN
            ALTER ROLE cortex_app_test NOLOGIN PASSWORD NULL;
        END IF;
    END $roles$;""")


async def migrate_connection(conn, *, migration_dir: Path | None = None,
                             manifest: dict | None = None) -> dict:
    # Serialize the whole finite lifecycle, including nontransactional engine paths.
    await conn.execute("SELECT pg_advisory_lock(hashtext($1))", MIGRATION_LOCK)
    adopted = await adopt_baseline(conn, migration_dir=migration_dir, manifest=manifest)
    await converge_roles(conn)
    result = await cortex.apply_schema_migrations(
        conn, dry_run=False, migration_dir=migration_dir,
        applied_by="cortex-standalone-migrator",
    )
    await converge_roles(conn)
    plan = await cortex.schema_migration_plan(conn, migration_dir=migration_dir)
    unresolved = [row["id"] for row in plan["migrations"]
                  if row["status"] not in {"applied", "superseded"}]
    if unresolved:
        raise RuntimeError("standalone migration plan remains unresolved: " + ", ".join(unresolved))
    # A scoped, empty installation project makes read-only readiness probes
    # exercise real search/handoff paths without borrowing another tenant.
    await conn.execute("""INSERT INTO cortex_projects
        (project_key, display_name, repo_root, repo_type, metadata)
        VALUES ('cortex-standalone-canary', 'Cortex installation health',
                '/installation-health', 'repo', '{"purpose":"installation-health"}'::jsonb)
        ON CONFLICT (project_key) DO NOTHING""")
    return {
        **await schema_status(conn),
        "baseline_adopted": adopted,
        "historical_replayed": 0,
        "applied": int(result["applied_count"]),
        "tail_receipted": int(result["applied_count"]),
        "tail_actions": [{"id": row["id"], "action": row["action"]}
                         for row in result["results"]
                         if row["action"] != "skip_applied"],
    }


async def run(*, status_only: bool = False) -> None:
    if os.environ.get("CORTEX_DB_AUTH") != "certificate":
        raise RuntimeError("standalone migrator requires CORTEX_DB_AUTH=certificate")
    dsn = os.environ.get("CORTEX_PG_DSN_ADMIN", "").strip()
    if not dsn:
        raise RuntimeError("CORTEX_PG_DSN_ADMIN is required by the standalone migrator")
    conn = await asyncpg.connect(dsn, **connection_kwargs(dsn, "admin"))
    try:
        receipt = await schema_status(conn) if status_only else await migrate_connection(conn)
    finally:
        await conn.close()  # Also releases the session-level migration advisory lock.
    print(json.dumps(receipt, sort_keys=True), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status", action="store_true", help="read actual ledger digest without applying")
    args = parser.parse_args()
    asyncio.run(run(status_only=args.status))
