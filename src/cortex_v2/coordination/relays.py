"""Cross-project relay with explicit approved authorization (R08).

A relay moves handoff work from a source scope to a target scope only
through an approved relay authorization whose target IDs are immutable
UUIDs. Alias renames never affect a relay because nothing here resolves or
stores aliases. Dispatch consumes the authorization and the still-approved
gate inside one transaction and writes a durable receipt in the source
scope; materialization creates the linked target handoff in the target
scope under that scope's own write authority.
"""

from __future__ import annotations

import uuid
from typing import Any

import asyncpg

from ..store import ScopeContext
from . import repository
from .approvals import load_approval_for_gate
from .commands import begin, begin_digest, committed_receipt, finish
from .models import AuthorizeRelayRequest, NoteRequest
from .repository import problem
from .state import TERMINAL_HANDOFF_STATUSES

RELAY_COLUMNS = """
    r.scope_id, r.relay_id, r.source_handoff_id, r.target_scope_id,
    r.target_actor_id, r.approval_id, r.status, r.authorized_at,
    r.authorized_by_principal, r.consumed_at, r.revoked_at
"""

RECEIPT_COLUMNS = """
    rr.scope_id, rr.relay_id, rr.source_handoff_id, rr.target_scope_id,
    rr.target_actor_id, rr.approval_id, rr.handoff_snapshot, rr.dispatched_at,
    rr.dispatched_by_principal
"""


def _relay_view(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "relay_id": str(row["relay_id"]),
        "source_scope_id": str(row["scope_id"]),
        "source_handoff_id": str(row["source_handoff_id"]),
        "target_scope_id": str(row["target_scope_id"]),
        "target_actor_id": str(row["target_actor_id"]),
        "approval_id": str(row["approval_id"]),
        "status": row["status"],
        "authorized_at": row["authorized_at"].isoformat(),
        "authorized_by_principal": str(row["authorized_by_principal"]),
        "consumed_at": repository.iso(row["consumed_at"]),
        "revoked_at": repository.iso(row["revoked_at"]),
    }


async def authorize_relay(
    connection: asyncpg.Connection,
    context: ScopeContext,
    payload: AuthorizeRelayRequest,
    idempotency_key: str,
) -> tuple[int, dict[str, Any], bool]:
    operation = "coordination.relay.authorize"
    digest = begin_digest(
        operation, context, {"request": payload.model_dump(mode="json")}
    )
    previous, replayed = await begin(
        connection, context, operation, idempotency_key, digest
    )
    if replayed and previous is not None:
        return 200, previous, True

    policy_revision = await repository.declare_policy_revision(connection, context)
    scope_id = context.selected.scope_id
    if payload.target_scope_id == scope_id:
        raise problem("relay_same_scope")

    # The approved immutable targets must match the request exactly; aliases
    # are never consulted.
    await load_approval_for_gate(
        connection,
        scope_id=scope_id,
        approval_id=payload.approval_id,
        gate_kind="relay",
        relay_source_handoff_id=payload.source_handoff_id,
        relay_target_scope_id=payload.target_scope_id,
        relay_target_actor_id=payload.target_actor_id,
    )
    await repository.load_handoff(connection, scope_id, payload.source_handoff_id)

    relay_id = uuid.uuid4()
    try:
        authorized_at = await connection.fetchval(
            """
            INSERT INTO cortex_coord.relay_authorizations
                (scope_id, relay_id, source_handoff_id, target_scope_id,
                 target_actor_id, approval_id, status, authorized_by_principal)
            VALUES ($1, $2, $3, $4, $5, $6, 'authorized', $7)
            RETURNING authorized_at
            """,
            scope_id,
            relay_id,
            payload.source_handoff_id,
            payload.target_scope_id,
            payload.target_actor_id,
            payload.approval_id,
            context.principal.principal_id,
        )
    except asyncpg.UniqueViolationError as exc:
        raise problem("duplicate_relay_target") from exc

    await repository.record_change(
        connection,
        scope_id=scope_id,
        aggregate_kind="relay",
        aggregate_id=relay_id,
        action="relay.authorized",
        event_type="coordination.relay.authorized",
        actor_principal_id=context.principal.principal_id,
        detail={
            "source_handoff_id": str(payload.source_handoff_id),
            "target_scope_id": str(payload.target_scope_id),
            "target_actor_id": str(payload.target_actor_id),
            "approval_id": str(payload.approval_id),
        },
        policy_revision=policy_revision,
    )
    receipt = committed_receipt(
        operation,
        context,
        policy_revision,
        "relay",
        relay_id,
        status="authorized",
        source_handoff_id=str(payload.source_handoff_id),
        target_scope_id=str(payload.target_scope_id),
        target_actor_id=str(payload.target_actor_id),
        approval_id=str(payload.approval_id),
        authorized_at=authorized_at.isoformat(),
    )
    return await finish(
        connection, context, operation, idempotency_key, digest, receipt, 201
    )


async def revoke_relay(
    connection: asyncpg.Connection,
    context: ScopeContext,
    relay_id: uuid.UUID,
    payload: NoteRequest,
    idempotency_key: str,
) -> tuple[int, dict[str, Any], bool]:
    operation = "coordination.relay.revoke"
    digest = begin_digest(
        operation,
        context,
        {"relay_id": str(relay_id), "request": payload.model_dump(mode="json")},
    )
    previous, replayed = await begin(
        connection, context, operation, idempotency_key, digest
    )
    if replayed and previous is not None:
        return 200, previous, True

    policy_revision = await repository.declare_policy_revision(connection, context)
    row = await _lock_relay(connection, context.selected.scope_id, relay_id)
    if row["status"] != "authorized":
        raise problem("relay_not_authorized")
    updated = await connection.execute(
        """
        UPDATE cortex_coord.relay_authorizations
           SET status = 'revoked', revoked_at = now()
         WHERE scope_id = $1 AND relay_id = $2 AND status = 'authorized'
        """,
        context.selected.scope_id,
        relay_id,
    )
    if updated == "UPDATE 0":
        raise problem("coordination_state_conflict")
    await repository.record_change(
        connection,
        scope_id=context.selected.scope_id,
        aggregate_kind="relay",
        aggregate_id=relay_id,
        action="relay.revoked",
        event_type="coordination.relay.revoked",
        actor_principal_id=context.principal.principal_id,
        detail={"note": payload.note},
        policy_revision=policy_revision,
    )
    receipt = committed_receipt(
        operation,
        context,
        policy_revision,
        "relay",
        relay_id,
        status="revoked",
    )
    return await finish(
        connection, context, operation, idempotency_key, digest, receipt, 200
    )


async def _lock_relay(
    connection: asyncpg.Connection, scope_id: uuid.UUID, relay_id: uuid.UUID
) -> dict[str, Any]:
    row = await connection.fetchrow(
        f"SELECT {RELAY_COLUMNS} FROM cortex_coord.relay_authorizations AS r "
        "WHERE r.scope_id = $1 AND r.relay_id = $2 FOR UPDATE",
        scope_id,
        relay_id,
    )
    if row is None:
        raise problem("relay_not_found")
    return dict(row)


async def dispatch_relay(
    connection: asyncpg.Connection,
    context: ScopeContext,
    relay_id: uuid.UUID,
    payload: NoteRequest,
    idempotency_key: str,
) -> tuple[int, dict[str, Any], bool]:
    operation = "coordination.relay.dispatch"
    digest = begin_digest(
        operation,
        context,
        {"relay_id": str(relay_id), "request": payload.model_dump(mode="json")},
    )
    previous, replayed = await begin(
        connection, context, operation, idempotency_key, digest
    )
    if replayed and previous is not None:
        return 200, previous, True

    policy_revision = await repository.declare_policy_revision(connection, context)
    scope_id = context.selected.scope_id
    row = await _lock_relay(connection, scope_id, relay_id)
    if row["status"] != "authorized":
        raise problem("relay_not_authorized")

    # Consume/revoke ordering shares this transaction: the approval is locked
    # and re-checked, so a concurrent revocation blocks the dispatch.
    await load_approval_for_gate(
        connection,
        scope_id=scope_id,
        approval_id=row["approval_id"],
        gate_kind="relay",
        relay_source_handoff_id=row["source_handoff_id"],
        relay_target_scope_id=row["target_scope_id"],
        relay_target_actor_id=row["target_actor_id"],
    )
    source = await repository.load_handoff(
        connection, scope_id, row["source_handoff_id"]
    )
    if source["status"] in TERMINAL_HANDOFF_STATUSES:
        raise problem("relay_source_not_relayable")

    updated = await connection.execute(
        """
        UPDATE cortex_coord.relay_authorizations
           SET status = 'consumed', consumed_at = now()
         WHERE scope_id = $1 AND relay_id = $2 AND status = 'authorized'
        """,
        scope_id,
        relay_id,
    )
    if updated == "UPDATE 0":
        raise problem("coordination_state_conflict")

    snapshot = {
        "title": source["title"],
        "brief": source["brief"],
        "addressed_role": source["addressed_role"],
        "require_human_accept": source["require_human_accept"],
    }
    dispatched_at = await connection.fetchval(
        """
        INSERT INTO cortex_coord.relay_receipts
            (scope_id, relay_id, source_handoff_id, target_scope_id,
             target_actor_id, approval_id, handoff_snapshot,
             dispatched_by_principal)
        VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8)
        RETURNING dispatched_at
        """,
        scope_id,
        relay_id,
        row["source_handoff_id"],
        row["target_scope_id"],
        row["target_actor_id"],
        row["approval_id"],
        repository.json_dumps(snapshot),
        context.principal.principal_id,
    )
    await repository.record_change(
        connection,
        scope_id=scope_id,
        aggregate_kind="relay",
        aggregate_id=relay_id,
        action="relay.dispatched",
        event_type="coordination.relay.dispatched",
        actor_principal_id=context.principal.principal_id,
        detail={
            "source_handoff_id": str(row["source_handoff_id"]),
            "target_scope_id": str(row["target_scope_id"]),
            "target_actor_id": str(row["target_actor_id"]),
            "approval_id": str(row["approval_id"]),
            "note": payload.note,
        },
        policy_revision=policy_revision,
    )
    # The durable, source-readable receipt keeps only immutable IDs; renames
    # of either scope's aliases cannot change what it references.
    receipt = committed_receipt(
        operation,
        context,
        policy_revision,
        "relay",
        relay_id,
        status="consumed",
        source_scope_id=str(scope_id),
        source_handoff_id=str(row["source_handoff_id"]),
        target_scope_id=str(row["target_scope_id"]),
        target_actor_id=str(row["target_actor_id"]),
        approval_id=str(row["approval_id"]),
        dispatched_at=dispatched_at.isoformat(),
    )
    return await finish(
        connection, context, operation, idempotency_key, digest, receipt, 200
    )


async def materialize_relay(
    connection: asyncpg.Connection,
    context: ScopeContext,
    relay_id: uuid.UUID,
    payload: NoteRequest,
    idempotency_key: str,
) -> tuple[int, dict[str, Any], bool]:
    operation = "coordination.relay.materialize"
    digest = begin_digest(
        operation,
        context,
        {"relay_id": str(relay_id), "request": payload.model_dump(mode="json")},
    )
    previous, replayed = await begin(
        connection, context, operation, idempotency_key, digest
    )
    if replayed and previous is not None:
        return 200, previous, True

    policy_revision = await repository.declare_policy_revision(connection, context)
    target_scope_id = context.selected.scope_id
    receipt_row = await connection.fetchrow(
        f"SELECT {RECEIPT_COLUMNS} FROM cortex_coord.relay_receipts AS rr "
        "WHERE rr.relay_id = $1",
        relay_id,
    )
    if receipt_row is None:
        raise problem("relay_receipt_not_found")
    if receipt_row["target_scope_id"] != target_scope_id:
        raise problem("relay_scope_mismatch")

    allowed = await repository.principal_matches_actor(
        connection,
        target_scope_id,
        context.principal.principal_id,
        receipt_row["target_actor_id"],
    ) or await repository.is_human_lead(
        connection, target_scope_id, context.principal.principal_id
    )
    if not allowed:
        raise problem("materialize_not_allowed")

    snapshot = repository.jsonb_dict(receipt_row["handoff_snapshot"])
    handoff_id = uuid.uuid4()
    try:
        created_at = await connection.fetchval(
            """
            INSERT INTO cortex_coord.handoffs
                (scope_id, handoff_id, title, brief, addressed_role,
                 addressed_actor_id, require_human_accept, status,
                 claim_generation, revision, created_by_principal, relay_id,
                 relay_source_scope_id, relay_source_handoff_id)
            VALUES ($1, $2, $3, $4, $5, $6, $7, 'open', 0, 1, $8, $9, $10, $11)
            RETURNING created_at
            """,
            target_scope_id,
            handoff_id,
            snapshot["title"],
            snapshot["brief"],
            snapshot.get("addressed_role"),
            receipt_row["target_actor_id"],
            snapshot.get("require_human_accept", False),
            context.principal.principal_id,
            relay_id,
            receipt_row["scope_id"],
            receipt_row["source_handoff_id"],
        )
    except asyncpg.ForeignKeyViolationError as exc:
        raise problem("addressed_actor_unavailable") from exc
    try:
        await connection.execute(
            """
            INSERT INTO cortex_coord.relay_materializations
                (scope_id, relay_id, source_scope_id, source_handoff_id,
                 handoff_id, materialized_by_principal)
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            target_scope_id,
            relay_id,
            receipt_row["scope_id"],
            receipt_row["source_handoff_id"],
            handoff_id,
            context.principal.principal_id,
        )
    except asyncpg.UniqueViolationError as exc:
        raise problem("relay_already_materialized") from exc

    await repository.record_change(
        connection,
        scope_id=target_scope_id,
        aggregate_kind="handoff",
        aggregate_id=handoff_id,
        action="handoff.created",
        event_type="coordination.handoff.created",
        actor_principal_id=context.principal.principal_id,
        detail={
            "title": snapshot["title"],
            "relay_id": str(relay_id),
            "source_scope_id": str(receipt_row["scope_id"]),
            "source_handoff_id": str(receipt_row["source_handoff_id"]),
        },
        policy_revision=policy_revision,
    )
    await repository.record_change(
        connection,
        scope_id=target_scope_id,
        aggregate_kind="relay",
        aggregate_id=relay_id,
        action="relay.materialized",
        event_type="coordination.relay.materialized",
        actor_principal_id=context.principal.principal_id,
        detail={
            "handoff_id": str(handoff_id),
            "source_scope_id": str(receipt_row["scope_id"]),
            "source_handoff_id": str(receipt_row["source_handoff_id"]),
            "note": payload.note,
        },
        policy_revision=policy_revision,
    )
    receipt = committed_receipt(
        operation,
        context,
        policy_revision,
        "relay",
        relay_id,
        status="materialized",
        handoff_id=str(handoff_id),
        source_scope_id=str(receipt_row["scope_id"]),
        source_handoff_id=str(receipt_row["source_handoff_id"]),
        target_scope_id=str(target_scope_id),
        target_actor_id=str(receipt_row["target_actor_id"]),
        created_at=created_at.isoformat(),
    )
    return await finish(
        connection, context, operation, idempotency_key, digest, receipt, 201
    )


async def get_relay(
    connection: asyncpg.Connection,
    context: ScopeContext,
    relay_id: uuid.UUID,
) -> dict[str, Any]:
    row = await connection.fetchrow(
        f"SELECT {RELAY_COLUMNS} FROM cortex_coord.relay_authorizations AS r "
        "WHERE r.relay_id = $1",
        relay_id,
    )
    if row is None:
        raise problem("relay_not_found")
    view = _relay_view(dict(row))
    receipt_row = await connection.fetchrow(
        f"SELECT {RECEIPT_COLUMNS} FROM cortex_coord.relay_receipts AS rr "
        "WHERE rr.relay_id = $1",
        relay_id,
    )
    materialization = await connection.fetchrow(
        "SELECT scope_id, handoff_id, materialized_at, "
        "materialized_by_principal FROM cortex_coord.relay_materializations "
        "WHERE relay_id = $1",
        relay_id,
    )
    audit = await connection.fetch(
        "SELECT scope_id, audit_seq, action, actor_principal_id, detail, "
        "created_at FROM cortex_coord.audit_log "
        "WHERE aggregate_kind = 'relay' AND aggregate_id = $1 ORDER BY created_at",
        relay_id,
    )
    view["dispatch_receipt"] = (
        None
        if receipt_row is None
        else {
            "receipt_scope_id": str(receipt_row["scope_id"]),
            "source_handoff_id": str(receipt_row["source_handoff_id"]),
            "target_scope_id": str(receipt_row["target_scope_id"]),
            "target_actor_id": str(receipt_row["target_actor_id"]),
            "approval_id": str(receipt_row["approval_id"]),
            "handoff_snapshot": repository.jsonb_dict(
                receipt_row["handoff_snapshot"]
            ),
            "dispatched_at": receipt_row["dispatched_at"].isoformat(),
            "dispatched_by_principal": str(
                receipt_row["dispatched_by_principal"]
            ),
        }
    )
    view["materialization"] = (
        None
        if materialization is None
        else {
            "target_scope_id": str(materialization["scope_id"]),
            "handoff_id": str(materialization["handoff_id"]),
            "materialized_at": materialization["materialized_at"].isoformat(),
            "materialized_by_principal": str(
                materialization["materialized_by_principal"]
            ),
        }
    )
    view["audit"] = [
        {
            "scope_id": str(item["scope_id"]),
            "audit_seq": item["audit_seq"],
            "action": item["action"],
            "actor_principal_id": str(item["actor_principal_id"]),
            "detail": repository.jsonb_dict(item["detail"]),
            "created_at": item["created_at"].isoformat(),
        }
        for item in audit
    ]
    return view
