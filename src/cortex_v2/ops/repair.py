"""Typed repairs: bounded, precondition-checked, ledgered — never raw SQL.

Outbox redelivery is deliberately absent: 0007's feed contract enforces
exactly-once delivery marking on both outbox tables, and redelivery flows
through the feed dispatcher/cursor operations instead of an ops repair.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import asyncpg

from ..receipts import begin_command, commit_receipt, request_digest
from ..store import ApiProblem, ScopeContext
from .models import RepairRequest


def _detail_json(detail: dict[str, Any]) -> str:
    return json.dumps(detail, ensure_ascii=False, sort_keys=True)


async def _restore_retention(
    connection: asyncpg.Connection,
    context: ScopeContext,
    target: uuid.UUID,
) -> tuple[str, dict[str, Any]]:
    entry = await connection.fetchrow(
        """
        SELECT entry_id, content_id, content_class, content_revision, content_hash,
               action, policy_revision
          FROM cortex_core.retention_ledger
         WHERE entry_id = $1 AND scope_id = $2
        """,
        target,
        context.selected.scope_id,
    )
    if entry is None:
        return "not_found", {}
    latest = await connection.fetchrow(
        """
        SELECT action
          FROM cortex_core.retention_ledger
         WHERE scope_id = $1 AND content_id = $2
         ORDER BY recorded_at DESC, entry_id DESC
         LIMIT 1
        """,
        context.selected.scope_id,
        entry["content_id"],
    )
    if latest is None or latest["action"] != "archive":
        return "precondition_failed", {"reason": "content_is_not_archived"}
    await connection.execute(
        """
        INSERT INTO cortex_core.retention_ledger
            (scope_id, content_id, content_class, content_revision, content_hash,
             action, policy_revision, disposition, storage_tier, recorded_by)
        VALUES ($1, $2, $3, $4, $5, 'restore', $6, 'restored',
                'canonical_in_place', $7)
        """,
        context.selected.scope_id,
        entry["content_id"],
        entry["content_class"],
        entry["content_revision"],
        entry["content_hash"],
        entry["policy_revision"],
        context.principal.principal_id,
    )
    return "applied", {"content_id": str(entry["content_id"])}


async def run_repair(
    connection: asyncpg.Connection,
    context: ScopeContext,
    idempotency_key: str,
    payload: RepairRequest,
    path_params: dict[str, Any],
) -> tuple[int, dict[str, Any], bool]:
    operation = "ops.repair"
    digest = request_digest(
        {
            "operation": operation,
            "scope_id": str(context.selected.scope_id),
            "repair_kind": payload.repair_kind,
            "target_reference": payload.target_reference,
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

    try:
        target = uuid.UUID(payload.target_reference)
    except ValueError as exc:
        raise ApiProblem(
            422, "invalid_target", "The repair target must be a UUID reference."
        ) from exc

    outcome, detail = await _restore_retention(connection, context, target)

    repair_id = await connection.fetchval(
        """
        INSERT INTO cortex_core.repairs
            (repair_kind, scope_id, target_reference, outcome, detail, requested_by)
        VALUES ($1, $2, $3, $4, $5::jsonb, $6)
        RETURNING repair_id
        """,
        payload.repair_kind,
        context.selected.scope_id,
        payload.target_reference,
        outcome,
        _detail_json(detail),
        context.principal.principal_id,
    )
    receipt = {
        "state": "committed",
        "operation": operation,
        "repair_id": str(repair_id),
        "repair_kind": payload.repair_kind,
        "target_reference": payload.target_reference,
        "outcome": outcome,
        "detail": detail,
        "scope_id": str(context.selected.scope_id),
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
    return 200, receipt, False
