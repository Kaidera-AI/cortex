"""Retention ownership: versioned policy enactment and ledgered archiving.

Archiving never deletes or rewrites canonical content: it appends ledger
entries recording eligibility, hash and disposition. Restore is a typed
repair that appends the inverse ledger entry.
"""

from __future__ import annotations

import uuid
from typing import Any

import asyncpg

from ..receipts import begin_command, commit_receipt, request_digest
from ..store import ApiProblem, ScopeContext
from .models import RetentionArchiveRequest, RetentionEnactRequest

ARCHIVE_BATCH_LIMIT = 500
OPS_FAILURES: dict[str, tuple[int, str]] = {
    "42501": (403, "owner_authority_required"),
    "P0002": (404, "registry_target_not_found"),
    "23514": (422, "invalid_ops_request"),
    "40001": (409, "policy_revision_conflict"),
}
OPS_FAILURE_MESSAGES = {
    "owner_authority_required": "This operation requires installation owner authority.",
    "registry_target_not_found": "The registry target is unavailable.",
    "invalid_ops_request": "The operations request is invalid.",
    "policy_revision_conflict": "The policy revision moved; recheck and retry.",
}


def _translate(exc: asyncpg.PostgresError) -> ApiProblem | None:
    entry = OPS_FAILURES.get(exc.sqlstate or "")
    if entry is None:
        return None
    status, code = entry
    return ApiProblem(status, code, OPS_FAILURE_MESSAGES[code])


async def enact_retention(
    connection: asyncpg.Connection,
    context: ScopeContext,
    idempotency_key: str,
    payload: RetentionEnactRequest,
    path_params: dict[str, Any],
) -> tuple[int, dict[str, Any], bool]:
    operation = "ops.retention_enact"
    digest = request_digest(
        {
            "operation": operation,
            "scope_id": str(context.selected.scope_id),
            "content_class": payload.content_class,
            "min_age_days": payload.min_age_days,
            "action": payload.action,
            "expected_revision": payload.expected_revision,
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
        revision = await connection.fetchval(
            """
            SELECT cortex_core.enact_retention_policy($1, $2, $3, $4, $5, $6)
            """,
            context.principal.principal_id,
            context.selected.scope_id,
            payload.content_class,
            payload.min_age_days,
            payload.action,
            payload.expected_revision,
        )
    except asyncpg.PostgresError as exc:
        problem = _translate(exc)
        if problem is not None:
            raise problem from exc
        raise
    receipt = {
        "state": "committed",
        "operation": operation,
        "scope_id": str(context.selected.scope_id),
        "content_class": payload.content_class,
        "min_age_days": payload.min_age_days,
        "action": payload.action,
        "policy_revision": revision,
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


async def retention_status(
    connection: asyncpg.Connection,
    context: ScopeContext,
    payload: Any,
    path_params: dict[str, Any],
) -> dict[str, Any]:
    rows = await connection.fetch(
        """
        SELECT DISTINCT ON (content_class)
               content_class, revision, min_age_days, action, enacted_at
          FROM cortex_core.retention_policies
         WHERE scope_id = $1
         ORDER BY content_class, revision DESC
        """,
        context.selected.scope_id,
    )
    return {
        "scope_id": str(context.selected.scope_id),
        "policies": [
            {
                "content_class": row["content_class"],
                "revision": row["revision"],
                "min_age_days": row["min_age_days"],
                "action": row["action"],
                "enacted_at": row["enacted_at"].isoformat(),
            }
            for row in rows
        ],
    }


async def _active_archive_policies(
    connection: asyncpg.Connection, scope_id: uuid.UUID
) -> dict[str, tuple[int, int]]:
    rows = await connection.fetch(
        """
        SELECT DISTINCT ON (content_class)
               content_class, revision, min_age_days
          FROM cortex_core.retention_policies
         WHERE scope_id = $1 AND action = 'archive'
         ORDER BY content_class, revision DESC
        """,
        scope_id,
    )
    return {
        row["content_class"]: (row["revision"], row["min_age_days"]) for row in rows
    }


async def _latest_ledger_actions(
    connection: asyncpg.Connection, scope_id: uuid.UUID
) -> dict[str, str]:
    rows = await connection.fetch(
        """
        SELECT DISTINCT ON (content_id) content_id, action
          FROM cortex_core.retention_ledger
         WHERE scope_id = $1
         ORDER BY content_id, recorded_at DESC, entry_id DESC
        """,
        scope_id,
    )
    return {str(row["content_id"]): row["action"] for row in rows}


async def archive_retention(
    connection: asyncpg.Connection,
    context: ScopeContext,
    idempotency_key: str,
    payload: RetentionArchiveRequest,
    path_params: dict[str, Any],
) -> tuple[int, dict[str, Any], bool]:
    operation = "ops.retention_archive"
    digest = request_digest(
        {
            "operation": operation,
            "scope_id": str(context.selected.scope_id),
            "content_class": payload.content_class,
            "dry_run": payload.dry_run,
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

    policies = await _active_archive_policies(connection, context.selected.scope_id)
    latest_actions = await _latest_ledger_actions(connection, context.selected.scope_id)
    candidates = await connection.fetch(
        """
        SELECT DISTINCT ON (i.content_id)
               i.content_id, i.content_class,
               extract(epoch FROM now() - i.created_at) / 86400 AS age_days,
               r.revision, r.content_hash,
               cortex_core.content_current_status(i.scope_id, i.content_id) AS status
          FROM cortex_core.content_items AS i
          JOIN cortex_core.content_revisions AS r
            ON r.scope_id = i.scope_id AND r.content_id = i.content_id
         WHERE i.scope_id = $1
         ORDER BY i.content_id, r.revision DESC
        """,
        context.selected.scope_id,
    )

    eligible = []
    for row in candidates:
        content_class = row["content_class"]
        policy = policies.get(content_class) or policies.get("*")
        if policy is None:
            continue
        if payload.content_class not in (None, content_class, "*"):
            continue
        policy_revision, min_age_days = policy
        age_days = float(row["age_days"])
        if age_days < min_age_days:
            continue
        if row["status"] == "tombstoned":
            continue
        if latest_actions.get(str(row["content_id"])) == "archive":
            continue
        eligible.append(
            {
                "content_id": str(row["content_id"]),
                "content_class": content_class,
                "revision": row["revision"],
                "content_hash": row["content_hash"].hex(),
                "age_days": round(age_days, 3),
                "policy_revision": policy_revision,
            }
        )
        if len(eligible) >= ARCHIVE_BATCH_LIMIT:
            break
    truncated = len(eligible) >= ARCHIVE_BATCH_LIMIT

    archived_entries: list[dict[str, Any]] = []
    if not payload.dry_run:
        for entry in eligible:
            entry_id = await connection.fetchval(
                """
                INSERT INTO cortex_core.retention_ledger
                    (scope_id, content_id, content_class, content_revision,
                     content_hash, action, policy_revision, disposition,
                     storage_tier, recorded_by)
                VALUES ($1, $2::uuid, $3, $4, decode($5, 'hex'), 'archive', $6,
                        'archived_in_place', 'canonical_in_place', $7)
                RETURNING entry_id
                """,
                context.selected.scope_id,
                entry["content_id"],
                entry["content_class"],
                entry["revision"],
                entry["content_hash"],
                entry["policy_revision"],
                context.principal.principal_id,
            )
            archived_entries.append({**entry, "entry_id": str(entry_id)})

    receipt = {
        "state": "committed",
        "operation": operation,
        "scope_id": str(context.selected.scope_id),
        "dry_run": payload.dry_run,
        "eligible_count": len(eligible),
        "truncated": truncated,
        "storage_tier": "canonical_in_place",
        "entries": archived_entries if not payload.dry_run else eligible,
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
    return 200 if payload.dry_run else 201, receipt, False
