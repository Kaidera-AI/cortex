"""asyncpg repository helpers for the coordination module.

Transport -> use case -> repository: this module owns every SQL statement
against ``cortex_coord``. Cross-module reads go through published use cases
(``cortex_v2.content.get_content``), never through another owner's private
columns. Conventions mirror W1 exactly: transaction-local context set by
``store.resolve_scopes``, writer-policy revision re-declared inside the same
transaction, typed ``ApiProblem`` errors, audit + outbox rows committed with
the canonical state change, and idempotency receipts via ``receipts.py``.
"""

from __future__ import annotations

import base64
import json
import uuid
from datetime import datetime
from typing import Any

import asyncpg

from ..store import ApiProblem, ScopeContext

# Typed problem catalog: code -> (http status, message). Python validates
# transitions before SQL runs; the database triggers are the concurrency-safe
# second boundary and surface as ``coordination_state_conflict``.
PROBLEMS: dict[str, tuple[int, str]] = {
    "handoff_not_found": (404, "The handoff is unavailable in the selected scope."),
    "task_not_found": (404, "The task is unavailable in the selected scope."),
    "epic_not_found": (404, "The epic is unavailable in the selected scope."),
    "wave_not_found": (404, "The wave is unavailable in the selected scope."),
    "board_not_found": (404, "The board is unavailable in the selected scope."),
    "approval_not_found": (404, "The approval is unavailable in the selected scope."),
    "relay_not_found": (
        404,
        "The relay authorization is unavailable in the selected scope.",
    ),
    "relay_receipt_not_found": (
        404,
        "No dispatched relay receipt is readable; the dispatching scope must "
        "be in your read scopes.",
    ),
    "relay_target_unavailable": (
        404,
        "The relay target scope or actor is unavailable.",
    ),
    "work_product_receipt_not_found": (
        404,
        "The work-product receipt is unavailable in the selected scope.",
    ),
    "unknown_action": (422, "The coordination action is not supported."),
    "invalid_path_parameter": (422, "A path parameter is malformed."),
    "invalid_filter": (422, "A list filter value is not supported."),
    "invalid_cursor": (422, "The pagination cursor is malformed."),
    "invalid_transition": (
        409,
        "The current state does not allow this transition.",
    ),
    "handoff_already_claimed": (
        409,
        "Another claimant holds a live lease on this handoff.",
    ),
    "stale_claim_generation": (
        409,
        "A newer claim generation is active; this claim is fenced out.",
    ),
    "claim_generation_mismatch": (
        409,
        "The expected claim generation does not match the active claim.",
    ),
    "claim_not_active": (409, "The handoff has no active claim."),
    "lease_expired": (
        409,
        "The claim lease expired; reclaim the handoff before publishing.",
    ),
    "renewal_must_extend": (
        409,
        "A lease renewal must extend past the current lease expiry.",
    ),
    "duplicate_handoff": (409, "Another handoff already owns this dedup key."),
    "duplicate_relay_target": (
        409,
        "A relay authorization for this handoff and immutable target already "
        "exists.",
    ),
    "relay_not_authorized": (
        409,
        "The relay authorization is not in the authorized state.",
    ),
    "relay_already_materialized": (
        409,
        "This relay has already been materialized in the target scope.",
    ),
    "relay_same_scope": (
        422,
        "A relay must target a different scope than the source.",
    ),
    "relay_scope_mismatch": (
        409,
        "The relay receipt was dispatched to a different target scope.",
    ),
    "relay_source_not_relayable": (
        409,
        "The source handoff is terminal and cannot be relayed.",
    ),
    "approval_not_pending": (
        409,
        "The approval has already been decided or revoked.",
    ),
    "approval_not_approved": (409, "The approval is not in the approved state."),
    "approval_expired": (409, "The approval has expired."),
    "approval_target_mismatch": (
        422,
        "The relay target does not match the approved immutable target.",
    ),
    "approval_subject_mismatch": (
        422,
        "The approval subject fields do not match the gate kind.",
    ),
    "human_authority_required": (
        403,
        "This action requires a human owner or lead of the scope.",
    ),
    "human_review_required": (
        403,
        "This handoff requires an approved human gate before acceptance.",
    ),
    "not_addressed_to_claimant": (
        403,
        "The handoff is not addressed to this claimant.",
    ),
    "withdraw_not_allowed": (
        403,
        "Only the creator or a human owner/lead may withdraw this handoff.",
    ),
    "addressed_actor_unavailable": (
        422,
        "The addressed actor is not an active member of this scope.",
    ),
    "materialize_not_allowed": (
        403,
        "Only the relay target actor or a human owner/lead of the target "
        "scope may materialize this relay.",
    ),
    "dispatch_not_eligible": (409, "The task is not eligible for dispatch."),
    "dependency_cycle": (422, "The dependency would create a cycle."),
    "duplicate_assignment": (409, "The actor already holds an assignment."),
    "duplicate_board_task": (409, "The task is already on this board."),
    "wave_index_duplicate": (409, "The epic already has a wave with this index."),
    "wave_tasks_not_terminal": (
        409,
        "Every task in the wave must be completed or cancelled first.",
    ),
    "prior_wave_incomplete": (
        409,
        "All earlier waves of the epic must be completed first.",
    ),
    "epic_not_active": (409, "The epic is not active."),
    "task_wave_mismatch": (
        422,
        "The task epic does not match the wave's epic.",
    ),
    "gate_approval_required": (
        422,
        "This wave requires an approved human gate approval to open.",
    ),
    "work_product_not_current": (
        422,
        "A work product must reference content in the current status.",
    ),
    "work_product_scope_mismatch": (
        422,
        "Work-product content must live in the handoff scope.",
    ),
    "writer_policy_denied": (
        403,
        "The active writer policy does not allow this principal to write.",
    ),
    "policy_revision_stale": (
        409,
        "The writer-policy revision changed; retry the command.",
    ),
    "coordination_state_conflict": (
        409,
        "A concurrent coordination change won the race; retry the command.",
    ),
    "content_not_found": (404, "The referenced content is unavailable."),
}

RETRYABLE_PROBLEMS = frozenset({"coordination_state_conflict"})

# Database triggers are the concurrency-safe boundary; Python validates first
# and raises the precise typed problem, so a trigger firing means a race.
SQLSTATE_PROBLEMS = {
    "23514": "coordination_state_conflict",
    "55000": "policy_revision_stale",
}


def problem(code: str) -> ApiProblem:
    status, message = PROBLEMS[code]
    return ApiProblem(
        status, code, message, retryable=code in RETRYABLE_PROBLEMS
    )


def translate_failure(exc: asyncpg.PostgresError) -> ApiProblem | None:
    entry = SQLSTATE_PROBLEMS.get(exc.sqlstate or "")
    if entry is None:
        return None
    return problem(entry)


def path_uuid(path_params: dict[str, Any], name: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(path_params[name]))
    except (KeyError, TypeError, ValueError) as exc:
        raise problem("invalid_path_parameter") from exc


def encode_list_cursor(created_at: datetime, aggregate_id: uuid.UUID) -> str:
    raw = json.dumps(
        {"created_at": created_at.isoformat(), "id": str(aggregate_id)},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_list_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        parsed = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        return datetime.fromisoformat(parsed["created_at"]), uuid.UUID(parsed["id"])
    except Exception as exc:
        raise problem("invalid_cursor") from exc


async def transaction_now(connection: asyncpg.Connection) -> datetime:
    return await connection.fetchval("SELECT now()")


async def declare_policy_revision(
    connection: asyncpg.Connection, context: ScopeContext
) -> int:
    """Mirror of the W1 content convention: re-declare the writer-policy
    revision inside the same transaction; coordination triggers recheck it."""
    policy = await connection.fetchrow(
        "SELECT revision, allowed_roles FROM cortex_auth.writer_policies "
        "WHERE scope_id = $1 ORDER BY revision DESC LIMIT 1",
        context.selected.scope_id,
    )
    revision = policy["revision"] if policy else 0
    if policy is not None:
        role = await connection.fetchval(
            """
            SELECT m.membership_role
              FROM cortex_auth.memberships AS m
              JOIN cortex_auth.actor_bindings AS b ON b.actor_id = m.actor_id
             WHERE m.scope_id = $1
               AND b.principal_id = $2
               AND m.status = 'active'
            """,
            context.selected.scope_id,
            context.principal.principal_id,
        )
        if role is None or role not in policy["allowed_roles"]:
            raise problem("writer_policy_denied")
    await connection.execute(
        "SELECT set_config('cortex.policy_revision', $1, true)", str(revision)
    )
    return revision


async def insert_audit(
    connection: asyncpg.Connection,
    *,
    scope_id: uuid.UUID,
    aggregate_kind: str,
    aggregate_id: uuid.UUID,
    action: str,
    actor_principal_id: uuid.UUID,
    detail: dict[str, Any],
) -> int:
    return await connection.fetchval(
        """
        INSERT INTO cortex_coord.audit_log
            (scope_id, aggregate_kind, aggregate_id, audit_seq, action,
             actor_principal_id, detail)
        VALUES ($1, $2, $3, NULL, $4, $5, $6::jsonb)
        RETURNING audit_seq
        """,
        scope_id,
        aggregate_kind,
        aggregate_id,
        action,
        actor_principal_id,
        json.dumps(detail, ensure_ascii=False, sort_keys=True, default=str),
    )


async def insert_outbox(
    connection: asyncpg.Connection,
    *,
    scope_id: uuid.UUID,
    aggregate_kind: str,
    aggregate_id: uuid.UUID,
    event_type: str,
    payload: dict[str, Any],
    audit_seq: int,
    policy_revision: int,
) -> uuid.UUID:
    event_id = uuid.uuid4()
    body = {
        **payload,
        "aggregate_kind": aggregate_kind,
        "aggregate_version": audit_seq,
        "policy_revision": policy_revision,
    }
    await connection.execute(
        """
        INSERT INTO cortex_coord.outbox_events
            (event_id, scope_id, aggregate_kind, aggregate_id, event_type, payload)
        VALUES ($1, $2, $3, $4, $5, $6::jsonb)
        """,
        event_id,
        scope_id,
        aggregate_kind,
        aggregate_id,
        event_type,
        json.dumps(body, ensure_ascii=False, sort_keys=True, default=str),
    )
    return event_id


async def record_change(
    connection: asyncpg.Connection,
    *,
    scope_id: uuid.UUID,
    aggregate_kind: str,
    aggregate_id: uuid.UUID,
    action: str,
    event_type: str,
    actor_principal_id: uuid.UUID,
    detail: dict[str, Any],
    policy_revision: int,
) -> int:
    """Commit audit + outbox for one state change (same transaction)."""
    audit_seq = await insert_audit(
        connection,
        scope_id=scope_id,
        aggregate_kind=aggregate_kind,
        aggregate_id=aggregate_id,
        action=action,
        actor_principal_id=actor_principal_id,
        detail=detail,
    )
    await insert_outbox(
        connection,
        scope_id=scope_id,
        aggregate_kind=aggregate_kind,
        aggregate_id=aggregate_id,
        event_type=event_type,
        payload=detail,
        audit_seq=audit_seq,
        policy_revision=policy_revision,
    )
    return audit_seq


HANDOFF_COLUMNS = """
    h.scope_id, h.handoff_id, h.dedup_key, h.title, h.brief, h.addressed_role,
    h.addressed_actor_id, h.task_id, h.status, h.claim_generation,
    h.active_lease_expires_at, h.require_human_accept, h.relay_id,
    h.relay_source_scope_id, h.relay_source_handoff_id, h.revision,
    h.created_by_principal, h.created_at, h.updated_at
"""


async def lock_handoff(
    connection: asyncpg.Connection, scope_id: uuid.UUID, handoff_id: uuid.UUID
) -> dict[str, Any]:
    row = await connection.fetchrow(
        f"SELECT {HANDOFF_COLUMNS} FROM cortex_coord.handoffs AS h "
        "WHERE h.scope_id = $1 AND h.handoff_id = $2 FOR UPDATE",
        scope_id,
        handoff_id,
    )
    if row is None:
        raise problem("handoff_not_found")
    return dict(row)


async def load_handoff(
    connection: asyncpg.Connection, scope_id: uuid.UUID, handoff_id: uuid.UUID
) -> dict[str, Any]:
    row = await connection.fetchrow(
        f"SELECT {HANDOFF_COLUMNS} FROM cortex_coord.handoffs AS h "
        "WHERE h.scope_id = $1 AND h.handoff_id = $2",
        scope_id,
        handoff_id,
    )
    if row is None:
        raise problem("handoff_not_found")
    return dict(row)


async def update_handoff_state(
    connection: asyncpg.Connection,
    *,
    scope_id: uuid.UUID,
    handoff_id: uuid.UUID,
    expected_revision: int,
    status: str,
    claim_generation: int | None = None,
    active_lease_expires_at: datetime | None = None,
    clear_lease: bool = False,
) -> None:
    """Optimistic (revision CAS) handoff state write used by every command."""
    if claim_generation is None:
        updated = await connection.execute(
            """
            UPDATE cortex_coord.handoffs
               SET status = $3,
                   active_lease_expires_at = CASE WHEN $4::boolean
                       THEN NULL ELSE COALESCE($5::timestamptz,
                                                active_lease_expires_at) END,
                   revision = revision + 1,
                   updated_at = now()
             WHERE scope_id = $1 AND handoff_id = $2 AND revision = $6
            """,
            scope_id,
            handoff_id,
            status,
            clear_lease,
            active_lease_expires_at,
            expected_revision,
        )
    else:
        updated = await connection.execute(
            """
            UPDATE cortex_coord.handoffs
               SET status = $3,
                   claim_generation = $4,
                   active_lease_expires_at = $5,
                   revision = revision + 1,
                   updated_at = now()
             WHERE scope_id = $1 AND handoff_id = $2 AND revision = $6
            """,
            scope_id,
            handoff_id,
            status,
            claim_generation,
            active_lease_expires_at,
            expected_revision,
        )
    if updated == "UPDATE 0":
        raise problem("coordination_state_conflict")


async def end_active_claim(
    connection: asyncpg.Connection,
    *,
    scope_id: uuid.UUID,
    handoff_id: uuid.UUID,
    claim_generation: int,
    end_reason: str,
) -> None:
    await connection.execute(
        """
        UPDATE cortex_coord.claims
           SET ended_at = now(), end_reason = $3
         WHERE scope_id = $1 AND handoff_id = $2
           AND claim_generation = $4 AND ended_at IS NULL
        """,
        scope_id,
        handoff_id,
        end_reason,
        claim_generation,
    )


async def principal_matches_actor(
    connection: asyncpg.Connection,
    scope_id: uuid.UUID,
    principal_id: uuid.UUID,
    actor_id: uuid.UUID,
) -> bool:
    return await connection.fetchval(
        """
        SELECT EXISTS (
            SELECT 1
              FROM cortex_auth.actor_bindings AS b
              JOIN cortex_auth.memberships AS m
                ON m.scope_id = $1 AND m.actor_id = b.actor_id
             WHERE b.principal_id = $2
               AND b.actor_id = $3
               AND m.status = 'active'
        )
        """,
        scope_id,
        principal_id,
        actor_id,
    )


async def principal_has_role(
    connection: asyncpg.Connection,
    scope_id: uuid.UUID,
    principal_id: uuid.UUID,
    addressed_role: str,
) -> bool:
    return await connection.fetchval(
        """
        SELECT EXISTS (
            SELECT 1
              FROM cortex_auth.memberships AS m
              JOIN cortex_auth.actor_bindings AS b ON b.actor_id = m.actor_id
             WHERE m.scope_id = $1
               AND b.principal_id = $2
               AND m.status = 'active'
               AND (m.membership_role = $3 OR m.responsibility = $3)
        )
        """,
        scope_id,
        principal_id,
        addressed_role,
    )


async def is_human_lead(
    connection: asyncpg.Connection, scope_id: uuid.UUID, principal_id: uuid.UUID
) -> bool:
    return await connection.fetchval(
        """
        SELECT EXISTS (
            SELECT 1
              FROM cortex_auth.memberships AS m
              JOIN cortex_auth.actor_bindings AS b ON b.actor_id = m.actor_id
              JOIN cortex_auth.actors AS a ON a.actor_id = m.actor_id
             WHERE m.scope_id = $1
               AND b.principal_id = $2
               AND m.status = 'active'
               AND a.status = 'active'
               AND a.actor_kind = 'human'
               AND m.membership_role IN ('owner', 'lead')
        )
        """,
        scope_id,
        principal_id,
    )


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def jsonb_dict(value: str | dict[str, Any] | None) -> dict[str, Any]:
    if value is None:
        return {}
    return json.loads(value) if isinstance(value, str) else value


def iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None
