"""Migration and release status: honest applied-versus-expected reporting."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import asyncpg

from .. import __version__
from ..config import active_profile
from ..store import ScopeContext

MIGRATION_DIRECTORY = Path(__file__).resolve().parents[3] / "migrations"


async def run_migration_status(
    connection: asyncpg.Connection,
    context: ScopeContext,
    payload: Any,
    path_params: dict[str, Any],
) -> dict[str, Any]:
    ledger = {
        row["migration_id"]: row
        for row in await connection.fetch(
            """
            SELECT migration_id, checksum_sha256, applied_at
              FROM cortex_core.schema_migrations
            """
        )
    }
    profile = active_profile()
    expected = []
    for migration_id in profile.migrations:
        row = ledger.get(migration_id)
        migration_path = MIGRATION_DIRECTORY / migration_id
        expected_checksum = (
            hashlib.sha256(migration_path.read_text().encode("utf-8")).hexdigest()
            if migration_path.is_file()
            else None
        )
        if row is None:
            state = "missing"
        elif expected_checksum is None:
            state = "checksum_unverified"
        elif row["checksum_sha256"] == expected_checksum:
            state = "applied"
        else:
            state = "checksum_mismatch"
        expected.append(
            {
                "migration_id": migration_id,
                "state": state,
                "applied_at": row["applied_at"].isoformat() if row else None,
            }
        )
    return {
        "instance_id": profile.instance_id,
        "expected_order": list(profile.migrations),
        "migrations": expected,
        "unexpected_applied": sorted(set(ledger) - set(profile.migrations)),
    }


async def run_release_status(
    connection: asyncpg.Connection,
    context: ScopeContext,
    payload: Any,
    path_params: dict[str, Any],
) -> dict[str, Any]:
    profile = active_profile()
    applied = [
        row["migration_id"]
        for row in await connection.fetch(
            """
            SELECT migration_id FROM cortex_core.schema_migrations
             ORDER BY migration_id
            """
        )
    ]
    return {
        "product_version": __version__,
        "api_version": "v1",
        "instance_id": profile.instance_id,
        "deployment_class": profile.deployment_class,
        "contract_version": profile.contract_version,
        "schema_applied": applied,
        "schema_expected": list(profile.migrations),
        "operation_modules": list(profile.operation_modules),
        "release_class": "local-candidate-evidence",
    }
