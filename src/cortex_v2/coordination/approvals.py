"""Approvals and human gates (R07-R09).

An approval is an immutable-input gate: its subject and relay targets are
fixed at request time and can never be re-pointed. Only a human owner/lead
of the scope may decide or revoke one. Consumers (handoff acceptance, wave
opening, relay dispatch) lock and re-check the approval inside their own
transaction, so consume/revoke ordering and the state change share one
transaction.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import asyncpg

from ..store import ScopeContext
from . import repository
from .commands import begin, begin_digest, committed_receipt, finish
from .models import DecideApprovalRequest, NoteRequest, RequestApprovalRequest
from .repository import problem

GATE_SUBJECT_COLUMNS = {
    "handoff_accept": ("subject_handoff_id",),
    "wave": ("subject_wave_id",),
    "relay": (
        "relay_source_handoff_id",
        "relay_target_scope_id",
        "relay_target_actor_id",
    ),
}
ALL_SUBJECT_COLUMNS = (
    "subject_handoff_id",
    "subject_wave_id",
    "relay_source_handoff_id",
    "relay_target_scope_id",
    "relay_target_actor_id",
)

APPROVAL_COLUMNS = """
    a.scope_id, a.approval_id, a.gate_kind, a.subject_handoff_id,
    a.subject_wave_id, a.relay_source_handoff_id, a.relay_target_scope_id,
    a.relay_target_actor_id, a.requested_by_principal, a.requested_at,
    a.decision, a.decided_by_principal, a.decided_at, a.expires_at, a.note,
    a.detail
"""


def _approval_view(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "approval_id": str(row["approval_id"]),
        "scope_id": str(row["scope_id"]),
        "gate_kind": row["gate_kind"],
        "subject_handoff_id": (
            str(row["subject_handoff_id"]) if row["subject_handoff_id"] else None
        ),
        "subject_wave_id": (
            str(row["subject_wave_id"]) if row["subject_wave_id"] else None
        ),
        "relay": (
            None
            if row["relay_source_handoff_id"] is None
            else {
                "source_handoff_id": str(row["relay_source_handoff_id"]),
                "target_scope_id": str(row["relay_target_scope_id"]),
                "target_actor_id": str(row["relay_target_actor_id"]),
            }
        ),
        "decision": row["decision"],
        "requested_by_principal": str(row["requested_by_principal"]),
        "requested_at": row["requested_at"].isoformat(),
        "decided_by_principal": (
            str(row["decided_by_principal"]) if row["decided_by_principal"] else None
        ),
        "decided_at": repository.iso(row["decided_at"]),
        "expires_at": repository.iso(row["expires_at"]),
        "note": row["note"],
        "detail": repository.jsonb_dict(row["detail"]),
    }


async def load_approval_for_gate(
    connection: asyncpg.Connection,
    *,
    scope_id: uuid.UUID,
    approval_id: uuid.UUID,
    gate_kind: str,
    subject_handoff_id: uuid.UUID | None = None,
    subject_wave_id: uuid.UUID | None = None,
    relay_source_handoff_id: uuid.UUID | None = None,
    relay_target_scope_id: uuid.UUID | None = None,
    relay_target_actor_id: uuid.UUID | None = None,
    lock: bool = True,
) -> dict[str, Any]:
    """Lock and validate an approval for a consuming gate transition."""
    row = await connection.fetchrow(
        f"SELECT {APPROVAL_COLUMNS} FROM cortex_coord.approvals AS a "
        "WHERE a.scope_id = $1 AND a.approval_id = $2"
        + (" FOR UPDATE" if lock else ""),
        scope_id,
        approval_id,
    )
    if row is None:
        raise problem("approval_not_found")
    if row["decision"] != "approved":
        raise problem("approval_not_approved")
    if row["gate_kind"] != gate_kind:
        raise problem("approval_subject_mismatch")

    expected = {
        "subject_handoff_id": subject_handoff_id,
        "subject_wave_id": subject_wave_id,
        "relay_source_handoff_id": relay_source_handoff_id,
        "relay_target_scope_id": relay_target_scope_id,
        "relay_target_actor_id": relay_target_actor_id,
    }
    for column, value in expected.items():
        if value is not None and row[column] != value:
            raise problem("approval_target_mismatch")

    now = await repository.transaction_now(connection)
    if row["expires_at"] is not None and row["expires_at"] <= now:
        raise problem("approval_expired")
    return dict(row)


async def request_approval(
    connection: asyncpg.Connection,
    context: ScopeContext,
    payload: RequestApprovalRequest,
    idempotency_key: str,
) -> tuple[int, dict[str, Any], bool]:
    operation = "coordination.approval.request"
    digest = begin_digest(operation, context, {"request": payload.model_dump(mode="json")})
    previous, replayed = await begin(
        connection, context, operation, idempotency_key, digest
    )
    if replayed and previous is not None:
        return 200, previous, True

    policy_revision = await repository.declare_policy_revision(connection, context)
    required = GATE_SUBJECT_COLUMNS[payload.gate_kind]
    provided = {
        column: getattr(payload, column) for column in ALL_SUBJECT_COLUMNS
    }
    for column, value in provided.items():
        if column in required and value is None:
            raise problem("approval_subject_mismatch")
        if column not in required and value is not None:
            raise problem("approval_subject_mismatch")

    scope_id = context.selected.scope_id
    if payload.gate_kind in ("handoff_accept", "relay"):
        await repository.load_handoff(
            connection, scope_id, payload.relay_source_handoff_id
            if payload.gate_kind == "relay"
            else payload.subject_handoff_id
        )
    if payload.gate_kind == "wave":
        wave = await connection.fetchrow(
            "SELECT wave_id FROM cortex_coord.waves "
            "WHERE scope_id = $1 AND wave_id = $2",
            scope_id,
            payload.subject_wave_id,
        )
        if wave is None:
            raise problem("wave_not_found")
    if payload.gate_kind == "relay":
        target_scope = await connection.fetchrow(
            "SELECT scope_id FROM cortex_core.scopes "
            "WHERE scope_id = $1 AND is_active",
            payload.relay_target_scope_id,
        )
        if target_scope is None:
            raise problem("relay_target_unavailable")
        member = await connection.fetchval(
            "SELECT EXISTS (SELECT 1 FROM cortex_auth.memberships "
            "WHERE scope_id = $1 AND actor_id = $2 AND status = 'active')",
            payload.relay_target_scope_id,
            payload.relay_target_actor_id,
        )
        if not member:
            raise problem("relay_target_unavailable")

    approval_id = uuid.uuid4()
    now = await repository.transaction_now(connection)
    expires_at = (
        now + timedelta(seconds=payload.expires_in_seconds)
        if payload.expires_in_seconds
        else None
    )
    requested_at = await connection.fetchval(
        """
        INSERT INTO cortex_coord.approvals
            (scope_id, approval_id, gate_kind, subject_handoff_id,
             subject_wave_id, relay_source_handoff_id, relay_target_scope_id,
             relay_target_actor_id, requested_by_principal, requested_at,
             expires_at, detail)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12::jsonb)
        RETURNING requested_at
        """,
        scope_id,
        approval_id,
        payload.gate_kind,
        payload.subject_handoff_id,
        payload.subject_wave_id,
        payload.relay_source_handoff_id,
        payload.relay_target_scope_id,
        payload.relay_target_actor_id,
        context.principal.principal_id,
        now,
        expires_at,
        repository.json_dumps(payload.detail),
    )
    await repository.record_change(
        connection,
        scope_id=scope_id,
        aggregate_kind="approval",
        aggregate_id=approval_id,
        action="approval.requested",
        event_type="coordination.approval.requested",
        actor_principal_id=context.principal.principal_id,
        detail={
            "gate_kind": payload.gate_kind,
            "subject_handoff_id": _opt_str(payload.subject_handoff_id),
            "subject_wave_id": _opt_str(payload.subject_wave_id),
            "relay_source_handoff_id": _opt_str(payload.relay_source_handoff_id),
            "relay_target_scope_id": _opt_str(payload.relay_target_scope_id),
            "relay_target_actor_id": _opt_str(payload.relay_target_actor_id),
        },
        policy_revision=policy_revision,
    )
    receipt = committed_receipt(
        operation,
        context,
        policy_revision,
        "approval",
        approval_id,
        gate_kind=payload.gate_kind,
        decision="pending",
        requested_at=requested_at.isoformat(),
        expires_at=repository.iso(expires_at),
        subject_handoff_id=_opt_str(payload.subject_handoff_id),
        subject_wave_id=_opt_str(payload.subject_wave_id),
        relay_source_handoff_id=_opt_str(payload.relay_source_handoff_id),
        relay_target_scope_id=_opt_str(payload.relay_target_scope_id),
        relay_target_actor_id=_opt_str(payload.relay_target_actor_id),
    )
    return await finish(
        connection, context, operation, idempotency_key, digest, receipt, 201
    )


def _opt_str(value: uuid.UUID | None) -> str | None:
    return str(value) if value is not None else None


async def _decide_or_revoke(
    connection: asyncpg.Connection,
    context: ScopeContext,
    approval_id: uuid.UUID,
    payload: DecideApprovalRequest | NoteRequest,
    idempotency_key: str,
    *,
    operation: str,
    new_decision: str,
    required_decision: str,
    conflict_code: str,
    action: str,
    event_type: str,
) -> tuple[int, dict[str, Any], bool]:
    digest = begin_digest(
        operation,
        context,
        {
            "approval_id": str(approval_id),
            "request": payload.model_dump(mode="json"),
        },
    )
    previous, replayed = await begin(
        connection, context, operation, idempotency_key, digest
    )
    if replayed and previous is not None:
        return 200, previous, True

    policy_revision = await repository.declare_policy_revision(connection, context)
    row = await connection.fetchrow(
        f"SELECT {APPROVAL_COLUMNS} FROM cortex_coord.approvals AS a "
        "WHERE a.scope_id = $1 AND a.approval_id = $2 FOR UPDATE",
        context.selected.scope_id,
        approval_id,
    )
    if row is None:
        raise problem("approval_not_found")
    if row["decision"] != required_decision:
        raise problem(conflict_code)
    if not (
        await repository.is_human_lead(
            connection, context.selected.scope_id, context.principal.principal_id
        )
    ):
        raise problem("human_authority_required")
    if new_decision != "revoked":
        now = await repository.transaction_now(connection)
        if row["expires_at"] is not None and row["expires_at"] <= now:
            raise problem("approval_expired")

    note = payload.note
    updated = await connection.execute(
        """
        UPDATE cortex_coord.approvals
           SET decision = $3, decided_by_principal = $4, decided_at = now(),
               note = COALESCE($5, note)
         WHERE scope_id = $1 AND approval_id = $2 AND decision = $6
        """,
        context.selected.scope_id,
        approval_id,
        new_decision,
        context.principal.principal_id,
        note,
        required_decision,
    )
    if updated == "UPDATE 0":
        raise problem("coordination_state_conflict")

    await repository.record_change(
        connection,
        scope_id=context.selected.scope_id,
        aggregate_kind="approval",
        aggregate_id=approval_id,
        action=action,
        event_type=event_type,
        actor_principal_id=context.principal.principal_id,
        detail={
            "decision": new_decision,
            "prior_decision": required_decision,
            "gate_kind": row["gate_kind"],
            "note": note,
        },
        policy_revision=policy_revision,
    )
    receipt = committed_receipt(
        operation,
        context,
        policy_revision,
        "approval",
        approval_id,
        gate_kind=row["gate_kind"],
        decision=new_decision,
        prior_decision=required_decision,
    )
    return await finish(
        connection, context, operation, idempotency_key, digest, receipt, 200
    )


async def decide_approval(
    connection: asyncpg.Connection,
    context: ScopeContext,
    approval_id: uuid.UUID,
    payload: DecideApprovalRequest,
    idempotency_key: str,
) -> tuple[int, dict[str, Any], bool]:
    return await _decide_or_revoke(
        connection,
        context,
        approval_id,
        payload,
        idempotency_key,
        operation="coordination.approval.decide",
        new_decision=payload.decision,
        required_decision="pending",
        conflict_code="approval_not_pending",
        action="approval.decided",
        event_type="coordination.approval.decided",
    )


async def revoke_approval(
    connection: asyncpg.Connection,
    context: ScopeContext,
    approval_id: uuid.UUID,
    payload: NoteRequest,
    idempotency_key: str,
) -> tuple[int, dict[str, Any], bool]:
    return await _decide_or_revoke(
        connection,
        context,
        approval_id,
        payload,
        idempotency_key,
        operation="coordination.approval.revoke",
        new_decision="revoked",
        required_decision="approved",
        conflict_code="approval_not_approved",
        action="approval.revoked",
        event_type="coordination.approval.revoked",
    )


async def get_approval(
    connection: asyncpg.Connection,
    context: ScopeContext,
    approval_id: uuid.UUID,
) -> dict[str, Any]:
    row = await connection.fetchrow(
        f"SELECT {APPROVAL_COLUMNS} FROM cortex_coord.approvals AS a "
        "WHERE a.scope_id = $1 AND a.approval_id = $2",
        context.selected.scope_id,
        approval_id,
    )
    if row is None:
        raise problem("approval_not_found")
    audit = await connection.fetch(
        "SELECT audit_seq, action, actor_principal_id, detail, created_at "
        "FROM cortex_coord.audit_log "
        "WHERE scope_id = $1 AND aggregate_kind = 'approval' "
        "AND aggregate_id = $2 ORDER BY audit_seq",
        context.selected.scope_id,
        approval_id,
    )
    return {
        **_approval_view(dict(row)),
        "audit": [
            {
                "audit_seq": item["audit_seq"],
                "action": item["action"],
                "actor_principal_id": str(item["actor_principal_id"]),
                "detail": repository.jsonb_dict(item["detail"]),
                "created_at": item["created_at"].isoformat(),
            }
            for item in audit
        ],
    }
