"""Lossless backup manifests and verification for the candidate's own data.

A backup manifest records the applied schema ledger, the caller's RLS scope
coverage and a per-table row count plus a bounded, deterministic, ordered fold
of per-row digests. Digest computation streams rows through a server cursor and
folds fixed-size hashes incrementally, so no aggregate materializes the table
in one allocation. Verification compares coverage first: when the verifying
caller's RLS coverage differs from the manifest's, digests are incomparable and
the state is the typed `coverage_differs` — never a phantom `drifted`. A
physical pg_dump/restore drill is an operator-level candidate exercise recorded
separately, not an API path.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterable
from typing import Any

import asyncpg

from ..receipts import begin_command, commit_receipt, request_digest
from ..store import ApiProblem, ScopeContext

BACKUP_TABLES: tuple[str, ...] = (
    "cortex_core.schema_migrations",
    "cortex_core.scopes",
    "cortex_core.scope_aliases",
    "cortex_auth.scope_grants",
    "cortex_core.memory_records",
    "cortex_core.lexical_documents",
    "cortex_core.outbox_events",
    "cortex_core.idempotency_receipts",
    "cortex_core.source_connectors",
    "cortex_core.content_items",
    "cortex_core.content_revisions",
    "cortex_core.content_status_log",
    "cortex_core.content_lexical_documents",
    "cortex_core.content_outbox_events",
    # command_receipts is excluded with backup_manifests: both are
    # self-referential (the backup command writes its own receipt after the
    # digests are folded), so including them would manufacture phantom drift.
    "cortex_core.retention_policies",
    "cortex_core.retention_ledger",
    "cortex_core.repairs",
)

ROW_HASH_SQL = (
    "SELECT encode(sha256(convert_to(row_to_json(t)::text, 'UTF8')), 'hex') "
    "AS row_hash FROM {table} AS t ORDER BY 1"
)


def fold_row_digest(row_hashes: Iterable[str]) -> tuple[int, str]:
    """Deterministic bounded fold: order-sensitive, O(1) memory per step."""
    digest = hashlib.sha256()
    count = 0
    for row_hash in row_hashes:
        digest.update(row_hash.encode("ascii"))
        count += 1
    return count, digest.hexdigest()


async def _table_digest(connection: asyncpg.Connection, table: str) -> dict[str, Any]:
    # Table names come only from the fixed BACKUP_TABLES allowlist.
    query = ROW_HASH_SQL.format(table=table)
    digest = hashlib.sha256()
    count = 0
    async for row in connection.cursor(query):
        digest.update(row["row_hash"].encode("ascii"))
        count += 1
    return {"row_count": count, "digest": digest.hexdigest()}


async def _digests(connection: asyncpg.Connection) -> dict[str, dict[str, Any]]:
    return {table: await _table_digest(connection, table) for table in BACKUP_TABLES}


async def create_backup(
    connection: asyncpg.Connection,
    context: ScopeContext,
    idempotency_key: str,
    payload: Any,
    path_params: dict[str, Any],
) -> tuple[int, dict[str, Any], bool]:
    operation = "ops.backup_create"
    digest = request_digest(
        {
            "operation": operation,
            "scope_id": str(context.selected.scope_id),
            "read_scopes": sorted(
                str(scope.scope_id) for scope in context.read_scopes
            ),
        }
    )
    previous, replayed = await begin_command(
        connection,
        principal_id=context.principal.principal_id,
        operation=operation,
        idempotency_key=idempotency_key,
        digest=digest,
        scope_id=context.selected.scope_id,
    )
    if replayed and previous is not None:
        return 200, previous, True

    schema_rows = await connection.fetch(
        """
        SELECT migration_id, checksum_sha256, applied_at
          FROM cortex_core.schema_migrations
         ORDER BY migration_id
        """
    )
    schema_snapshot = [
        {
            "migration_id": row["migration_id"],
            "checksum_sha256": row["checksum_sha256"],
            "applied_at": row["applied_at"].isoformat(),
        }
        for row in schema_rows
    ]
    table_digests = await _digests(connection)
    scope_coverage = [str(scope.scope_id) for scope in context.read_scopes]
    manifest_id = await connection.fetchval(
        """
        INSERT INTO cortex_core.backup_manifests
            (installation_id, created_by, schema_snapshot, scope_coverage,
             table_digests)
        VALUES ($1, $2, $3::jsonb, $4::uuid[], $5::jsonb)
        RETURNING manifest_id
        """,
        context.principal.installation_id,
        context.principal.principal_id,
        json.dumps(schema_snapshot, sort_keys=True),
        scope_coverage,
        json.dumps(table_digests, sort_keys=True),
    )
    receipt = {
        "state": "committed",
        "operation": operation,
        "manifest_id": str(manifest_id),
        "tables": len(table_digests),
        "total_rows": sum(entry["row_count"] for entry in table_digests.values()),
        "scope_coverage": scope_coverage,
        "schema_snapshot": schema_snapshot,
        "row_visibility": "rls-scoped-caller-view",
        "digest_method": "ordered-sha256-row-fold-v2",
    }
    await commit_receipt(
        connection,
        principal_id=context.principal.principal_id,
        operation=operation,
        idempotency_key=idempotency_key,
        digest=digest,
        receipt_kind="committed",
        receipt=receipt,
        scope_id=context.selected.scope_id,
    )
    return 201, receipt, False


async def verify_backup(
    connection: asyncpg.Connection,
    context: ScopeContext,
    payload: Any,
    path_params: dict[str, Any],
) -> dict[str, Any]:
    try:
        manifest_id = uuid.UUID(str(path_params["manifest_id"]))
    except (KeyError, ValueError) as exc:
        raise ApiProblem(
            422, "invalid_target", "The manifest reference must be a UUID."
        ) from exc
    manifest = await connection.fetchrow(
        """
        SELECT manifest_id, created_at, schema_snapshot, scope_coverage,
               table_digests
          FROM cortex_core.backup_manifests
         WHERE manifest_id = $1
        """,
        manifest_id,
    )
    if manifest is None:
        raise ApiProblem(
            404, "manifest_not_found", "The backup manifest is unavailable."
        )

    stored_coverage = sorted(str(scope) for scope in manifest["scope_coverage"])
    caller_coverage = sorted(
        str(scope.scope_id) for scope in context.read_scopes
    )
    if stored_coverage != caller_coverage:
        # Digests were computed under the creator's RLS view; a different
        # coverage sees different row sets, so no drift claim is possible.
        return {
            "state": "coverage_differs",
            "manifest_id": str(manifest["manifest_id"]),
            "created_at": manifest["created_at"].isoformat(),
            "coverage_same": False,
            "manifest_coverage": stored_coverage,
            "caller_coverage": caller_coverage,
            "detail": (
                "digests are incomparable across different RLS coverage; "
                "verify from a session with the manifest's scope coverage"
            ),
            "row_visibility": "rls-scoped-caller-view",
        }

    stored = (
        json.loads(manifest["table_digests"])
        if isinstance(manifest["table_digests"], str)
        else manifest["table_digests"]
    )
    current = await _digests(connection)
    tables: dict[str, Any] = {}
    drifted: list[str] = []
    for table in BACKUP_TABLES:
        stored_entry = stored.get(table, {"row_count": None, "digest": None})
        current_entry = current[table]
        match = (
            stored_entry.get("digest") == current_entry["digest"]
            and stored_entry.get("row_count") == current_entry["row_count"]
        )
        tables[table] = {
            "manifest_rows": stored_entry.get("row_count"),
            "current_rows": current_entry["row_count"],
            "digest_match": match,
        }
        if not match:
            drifted.append(table)
    return {
        "state": "verified" if not drifted else "drifted",
        "manifest_id": str(manifest["manifest_id"]),
        "created_at": manifest["created_at"].isoformat(),
        "coverage_same": True,
        "drifted_tables": drifted,
        "tables": tables,
        "row_visibility": "rls-scoped-caller-view",
        "digest_method": "ordered-sha256-row-fold-v2",
    }
