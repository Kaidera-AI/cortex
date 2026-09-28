"""Handoff use cases: create, claim, lease, release, return, review and
terminal transitions (R07, R10).

Every command runs inside one caller-owned transaction and commits the
canonical handoff row, the append-only audit row, the outbox event and the
idempotency receipt together (F04). Claims are atomic via row locking plus a
revision CAS; the claim generation is the fencing token, so a stale claimant
can never publish a return, release or renewal against a newer claim.
Coordination never launches processes, merges Git, or decides that an
agent's report is true.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import asyncpg

from ..content import get_content
from ..store import ApiProblem, ScopeContext
from . import repository
from .approvals import load_approval_for_gate
from .commands import (
    begin as _begin,
    begin_digest as _begin_digest,
    finish as _finish,
)
from .models import (
    AbandonHandoffRequest,
    AcceptHandoffRequest,
    ClaimHandoffRequest,
    CreateHandoffRequest,
    FailHandoffRequest,
    ReleaseHandoffRequest,
    RenewLeaseRequest,
    ReworkHandoffRequest,
    RetryHandoffRequest,
    ReturnHandoffRequest,
    WithdrawHandoffRequest,
)
from .repository import problem
from .state import HandoffState, fencing_violation, validate_transition


def _base_receipt(
    operation: str,
    context: ScopeContext,
    policy_revision: int,
    handoff: dict[str, Any],
    **extra: Any,
) -> dict[str, Any]:
    return {
        "state": "committed",
        "operation": operation,
        "handoff_id": str(handoff["handoff_id"]),
        "scope_id": str(context.selected.scope_id),
        "status": handoff["status"],
        "claim_generation": handoff["claim_generation"],
        "revision": handoff["revision"],
        "policy_revision": policy_revision,
        **extra,
    }


def _check_reason(reason: str | None) -> None:
    if reason is not None:
        raise problem(reason)


async def create_handoff(
    connection: asyncpg.Connection,
    context: ScopeContext,
    payload: CreateHandoffRequest,
    idempotency_key: str,
) -> tuple[int, dict[str, Any], bool]:
    operation = "coordination.handoff.create"
    body = payload.model_dump(mode="json")
    digest = _begin_digest(operation, context, {"request": body})
    previous, replayed = await _begin(
        connection, context, operation, idempotency_key, digest
    )
    if replayed and previous is not None:
        return 200, previous, True

    policy_revision = await repository.declare_policy_revision(connection, context)
    handoff_id = uuid.uuid4()
    try:
        created_at = await connection.fetchval(
            """
            INSERT INTO cortex_coord.handoffs
                (scope_id, handoff_id, dedup_key, title, brief, addressed_role,
                 addressed_actor_id, task_id, require_human_accept, status,
                 claim_generation, revision, created_by_principal)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, 'open', 0, 1, $10)
            RETURNING created_at
            """,
            context.selected.scope_id,
            handoff_id,
            payload.dedup_key,
            payload.title,
            payload.brief,
            payload.addressed_role,
            payload.addressed_actor_id,
            payload.task_id,
            payload.require_human_accept,
            context.principal.principal_id,
        )
    except asyncpg.UniqueViolationError as exc:
        raise problem("duplicate_handoff") from exc
    except asyncpg.ForeignKeyViolationError as exc:
        if payload.task_id is not None:
            raise problem("task_not_found") from exc
        raise problem("addressed_actor_unavailable") from exc

    handoff = {
        "handoff_id": handoff_id,
        "status": "open",
        "claim_generation": 0,
        "revision": 1,
    }
    await repository.record_change(
        connection,
        scope_id=context.selected.scope_id,
        aggregate_kind="handoff",
        aggregate_id=handoff_id,
        action="handoff.created",
        event_type="coordination.handoff.created",
        actor_principal_id=context.principal.principal_id,
        detail={
            "title": payload.title,
            "dedup_key": payload.dedup_key,
            "addressed_role": payload.addressed_role,
            "addressed_actor_id": (
                str(payload.addressed_actor_id) if payload.addressed_actor_id else None
            ),
            "task_id": str(payload.task_id) if payload.task_id else None,
            "require_human_accept": payload.require_human_accept,
        },
        policy_revision=policy_revision,
    )
    receipt = {
        **_base_receipt(operation, context, policy_revision, handoff),
        "title": payload.title,
        "dedup_key": payload.dedup_key,
        "require_human_accept": payload.require_human_accept,
        "created_at": created_at.isoformat(),
    }
    return await _finish(
        connection, context, operation, idempotency_key, digest, receipt, 201
    )


async def _load_and_validate(
    connection: asyncpg.Connection,
    context: ScopeContext,
    handoff_id: uuid.UUID,
    action: str,
    *,
    expected_claim_generation: int | None = None,
) -> tuple[dict[str, Any], Any]:
    row = await repository.lock_handoff(
        connection, context.selected.scope_id, handoff_id
    )
    now = await repository.transaction_now(connection)
    state = HandoffState(
        status=row["status"],
        claim_generation=row["claim_generation"],
        active_lease_expires_at=row["active_lease_expires_at"],
    )
    if expected_claim_generation is not None:
        _check_reason(
            fencing_violation(
                expected_claim_generation=expected_claim_generation, state=state
            )
        )
    _check_reason(validate_transition(action, state, now=now))
    return row, now


async def claim_handoff(
    connection: asyncpg.Connection,
    context: ScopeContext,
    handoff_id: uuid.UUID,
    payload: ClaimHandoffRequest,
    idempotency_key: str,
) -> tuple[int, dict[str, Any], bool]:
    operation = "coordination.handoff.claim"
    digest = _begin_digest(
        operation,
        context,
        {"handoff_id": str(handoff_id), "request": payload.model_dump(mode="json")},
    )
    previous, replayed = await _begin(
        connection, context, operation, idempotency_key, digest
    )
    if replayed and previous is not None:
        return 200, previous, True

    policy_revision = await repository.declare_policy_revision(connection, context)
    row, now = await _load_and_validate(connection, context, handoff_id, "claim")

    if row["addressed_actor_id"] is not None and not (
        await repository.principal_matches_actor(
            connection,
            context.selected.scope_id,
            context.principal.principal_id,
            row["addressed_actor_id"],
        )
    ):
        raise problem("not_addressed_to_claimant")
    if row["addressed_role"] is not None and not (
        await repository.principal_has_role(
            connection,
            context.selected.scope_id,
            context.principal.principal_id,
            row["addressed_role"],
        )
    ):
        raise problem("not_addressed_to_claimant")

    reclaimed = row["status"] == "claimed"
    if reclaimed:
        await repository.end_active_claim(
            connection,
            scope_id=context.selected.scope_id,
            handoff_id=handoff_id,
            claim_generation=row["claim_generation"],
            end_reason="lease_expired",
        )
    new_generation = row["claim_generation"] + 1
    lease_expires = now + timedelta(seconds=payload.lease_seconds)

    await repository.update_handoff_state(
        connection,
        scope_id=context.selected.scope_id,
        handoff_id=handoff_id,
        expected_revision=row["revision"],
        status="claimed",
        claim_generation=new_generation,
        active_lease_expires_at=lease_expires,
    )
    await connection.execute(
        """
        INSERT INTO cortex_coord.claims
            (scope_id, handoff_id, claim_generation, claimant_principal_id,
             claimed_at, lease_expires_at)
        VALUES ($1, $2, $3, $4, $5, $6)
        """,
        context.selected.scope_id,
        handoff_id,
        new_generation,
        context.principal.principal_id,
        now,
        lease_expires,
    )
    handoff = {
        "handoff_id": handoff_id,
        "status": "claimed",
        "claim_generation": new_generation,
        "revision": row["revision"] + 1,
    }
    await repository.record_change(
        connection,
        scope_id=context.selected.scope_id,
        aggregate_kind="handoff",
        aggregate_id=handoff_id,
        action="handoff.claimed",
        event_type="coordination.handoff.claimed",
        actor_principal_id=context.principal.principal_id,
        detail={
            "claim_generation": new_generation,
            "lease_expires_at": lease_expires.isoformat(),
            "reclaimed_after_expiry": reclaimed,
        },
        policy_revision=policy_revision,
    )
    receipt = _base_receipt(operation, context, policy_revision, handoff)
    receipt["lease_expires_at"] = lease_expires.isoformat()
    receipt["reclaimed_after_expiry"] = reclaimed
    return await _finish(
        connection, context, operation, idempotency_key, digest, receipt, 200
    )


async def renew_lease(
    connection: asyncpg.Connection,
    context: ScopeContext,
    handoff_id: uuid.UUID,
    payload: RenewLeaseRequest,
    idempotency_key: str,
) -> tuple[int, dict[str, Any], bool]:
    operation = "coordination.handoff.lease.renew"
    digest = _begin_digest(
        operation,
        context,
        {"handoff_id": str(handoff_id), "request": payload.model_dump(mode="json")},
    )
    previous, replayed = await _begin(
        connection, context, operation, idempotency_key, digest
    )
    if replayed and previous is not None:
        return 200, previous, True

    policy_revision = await repository.declare_policy_revision(connection, context)
    row, now = await _load_and_validate(
        connection,
        context,
        handoff_id,
        "renew_lease",
        expected_claim_generation=payload.expected_claim_generation,
    )
    lease_expires = now + timedelta(seconds=payload.lease_seconds)
    if (
        row["active_lease_expires_at"] is not None
        and lease_expires <= row["active_lease_expires_at"]
    ):
        raise problem("renewal_must_extend")
    updated = await connection.execute(
        """
        UPDATE cortex_coord.claims
           SET lease_expires_at = $3
         WHERE scope_id = $1 AND handoff_id = $2
           AND claim_generation = $4 AND ended_at IS NULL
           AND lease_expires_at < $3
        """,
        context.selected.scope_id,
        handoff_id,
        lease_expires,
        payload.expected_claim_generation,
    )
    if updated == "UPDATE 0":
        raise problem("coordination_state_conflict")
    await repository.update_handoff_state(
        connection,
        scope_id=context.selected.scope_id,
        handoff_id=handoff_id,
        expected_revision=row["revision"],
        status="claimed",
        active_lease_expires_at=lease_expires,
    )
    handoff = {
        "handoff_id": handoff_id,
        "status": "claimed",
        "claim_generation": row["claim_generation"],
        "revision": row["revision"] + 1,
    }
    await repository.record_change(
        connection,
        scope_id=context.selected.scope_id,
        aggregate_kind="handoff",
        aggregate_id=handoff_id,
        action="handoff.lease_renewed",
        event_type="coordination.handoff.lease_renewed",
        actor_principal_id=context.principal.principal_id,
        detail={
            "claim_generation": payload.expected_claim_generation,
            "lease_expires_at": lease_expires.isoformat(),
        },
        policy_revision=policy_revision,
    )
    receipt = _base_receipt(operation, context, policy_revision, handoff)
    receipt["lease_expires_at"] = lease_expires.isoformat()
    return await _finish(
        connection, context, operation, idempotency_key, digest, receipt, 200
    )


async def _fenced_transition(
    connection: asyncpg.Connection,
    context: ScopeContext,
    *,
    operation: str,
    handoff_id: uuid.UUID,
    action: str,
    event_type: str,
    expected_claim_generation: int,
    end_reason: str | None,
    idempotency_key: str,
    digest: bytes,
    detail: dict[str, Any],
    to_status: str,
    clear_lease: bool,
) -> tuple[int, dict[str, Any], bool]:
    policy_revision = await repository.declare_policy_revision(connection, context)
    row, _now = await _load_and_validate(
        connection,
        context,
        handoff_id,
        action,
        expected_claim_generation=expected_claim_generation,
    )
    if end_reason is not None:
        await repository.end_active_claim(
            connection,
            scope_id=context.selected.scope_id,
            handoff_id=handoff_id,
            claim_generation=expected_claim_generation,
            end_reason=end_reason,
        )
    await repository.update_handoff_state(
        connection,
        scope_id=context.selected.scope_id,
        handoff_id=handoff_id,
        expected_revision=row["revision"],
        status=to_status,
        clear_lease=clear_lease,
    )
    handoff = {
        "handoff_id": handoff_id,
        "status": to_status,
        "claim_generation": row["claim_generation"],
        "revision": row["revision"] + 1,
    }
    await repository.record_change(
        connection,
        scope_id=context.selected.scope_id,
        aggregate_kind="handoff",
        aggregate_id=handoff_id,
        action=action.replace("_", "."),
        event_type=event_type,
        actor_principal_id=context.principal.principal_id,
        detail={"claim_generation": expected_claim_generation, **detail},
        policy_revision=policy_revision,
    )
    receipt = _base_receipt(operation, context, policy_revision, handoff, **detail)
    return await _finish(
        connection, context, operation, idempotency_key, digest, receipt, 200
    )


async def release_handoff(
    connection: asyncpg.Connection,
    context: ScopeContext,
    handoff_id: uuid.UUID,
    payload: ReleaseHandoffRequest,
    idempotency_key: str,
) -> tuple[int, dict[str, Any], bool]:
    operation = "coordination.handoff.release"
    digest = _begin_digest(
        operation,
        context,
        {"handoff_id": str(handoff_id), "request": payload.model_dump(mode="json")},
    )
    previous, replayed = await _begin(
        connection, context, operation, idempotency_key, digest
    )
    if replayed and previous is not None:
        return 200, previous, True
    return await _fenced_transition(
        connection,
        context,
        operation=operation,
        handoff_id=handoff_id,
        action="release",
        event_type="coordination.handoff.released",
        expected_claim_generation=payload.expected_claim_generation,
        end_reason="released",
        idempotency_key=idempotency_key,
        digest=digest,
        detail={"reason": payload.reason},
        to_status="open",
        clear_lease=True,
    )


async def fail_handoff(
    connection: asyncpg.Connection,
    context: ScopeContext,
    handoff_id: uuid.UUID,
    payload: FailHandoffRequest,
    idempotency_key: str,
) -> tuple[int, dict[str, Any], bool]:
    operation = "coordination.handoff.fail"
    digest = _begin_digest(
        operation,
        context,
        {"handoff_id": str(handoff_id), "request": payload.model_dump(mode="json")},
    )
    previous, replayed = await _begin(
        connection, context, operation, idempotency_key, digest
    )
    if replayed and previous is not None:
        return 200, previous, True
    return await _fenced_transition(
        connection,
        context,
        operation=operation,
        handoff_id=handoff_id,
        action="fail",
        event_type="coordination.handoff.failed",
        expected_claim_generation=payload.expected_claim_generation,
        end_reason="failed",
        idempotency_key=idempotency_key,
        digest=digest,
        detail={"reason": payload.reason, "error_class": payload.error_class},
        to_status="failed",
        clear_lease=True,
    )


async def abandon_handoff(
    connection: asyncpg.Connection,
    context: ScopeContext,
    handoff_id: uuid.UUID,
    payload: AbandonHandoffRequest,
    idempotency_key: str,
) -> tuple[int, dict[str, Any], bool]:
    operation = "coordination.handoff.abandon"
    digest = _begin_digest(
        operation,
        context,
        {"handoff_id": str(handoff_id), "request": payload.model_dump(mode="json")},
    )
    previous, replayed = await _begin(
        connection, context, operation, idempotency_key, digest
    )
    if replayed and previous is not None:
        return 200, previous, True
    return await _fenced_transition(
        connection,
        context,
        operation=operation,
        handoff_id=handoff_id,
        action="abandon",
        event_type="coordination.handoff.abandoned",
        expected_claim_generation=payload.expected_claim_generation,
        end_reason="abandoned",
        idempotency_key=idempotency_key,
        digest=digest,
        detail={"reason": payload.reason},
        to_status="abandoned",
        clear_lease=True,
    )


async def return_handoff(
    connection: asyncpg.Connection,
    context: ScopeContext,
    handoff_id: uuid.UUID,
    payload: ReturnHandoffRequest,
    idempotency_key: str,
) -> tuple[int, dict[str, Any], bool]:
    operation = "coordination.handoff.return"
    digest = _begin_digest(
        operation,
        context,
        {"handoff_id": str(handoff_id), "request": payload.model_dump(mode="json")},
    )
    previous, replayed = await _begin(
        connection, context, operation, idempotency_key, digest
    )
    if replayed and previous is not None:
        return 200, previous, True

    policy_revision = await repository.declare_policy_revision(connection, context)
    row, _now = await _load_and_validate(
        connection,
        context,
        handoff_id,
        "return",
        expected_claim_generation=payload.expected_claim_generation,
    )

    pinned = []
    for reference in payload.work_products:
        try:
            content_row = await get_content(
                connection, reference.content_id, reference.revision, False
            )
        except ApiProblem as exc:
            if exc.code == "content_not_found":
                raise problem("content_not_found") from exc
            raise
        if content_row["scope_id"] != str(context.selected.scope_id):
            raise problem("work_product_scope_mismatch")
        if content_row["status"] != "current":
            raise problem("work_product_not_current")
        pinned.append(
            {
                "receipt_id": uuid.uuid4(),
                "content_id": reference.content_id,
                "content_revision": content_row["revision"],
                "content_hash": bytes.fromhex(content_row["content_hash"]),
                "evidence_class": reference.evidence_class,
            }
        )

    await repository.end_active_claim(
        connection,
        scope_id=context.selected.scope_id,
        handoff_id=handoff_id,
        claim_generation=payload.expected_claim_generation,
        end_reason="returned",
    )
    await repository.update_handoff_state(
        connection,
        scope_id=context.selected.scope_id,
        handoff_id=handoff_id,
        expected_revision=row["revision"],
        status="returned",
        clear_lease=True,
    )
    return_seq = await connection.fetchval(
        """
        INSERT INTO cortex_coord.returns
            (scope_id, handoff_id, return_seq, claim_generation,
             returned_by_principal, summary)
        VALUES ($1, $2, NULL, $3, $4, $5)
        RETURNING return_seq
        """,
        context.selected.scope_id,
        handoff_id,
        payload.expected_claim_generation,
        context.principal.principal_id,
        payload.summary,
    )
    for item in pinned:
        await connection.execute(
            """
            INSERT INTO cortex_coord.work_product_receipts
                (scope_id, receipt_id, handoff_id, return_seq, content_id,
                 content_revision, content_hash, evidence_class, attestation,
                 created_by_principal)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'self_reported', $9)
            """,
            context.selected.scope_id,
            item["receipt_id"],
            handoff_id,
            return_seq,
            item["content_id"],
            item["content_revision"],
            item["content_hash"],
            item["evidence_class"],
            context.principal.principal_id,
        )

    handoff = {
        "handoff_id": handoff_id,
        "status": "returned",
        "claim_generation": row["claim_generation"],
        "revision": row["revision"] + 1,
    }
    await repository.record_change(
        connection,
        scope_id=context.selected.scope_id,
        aggregate_kind="handoff",
        aggregate_id=handoff_id,
        action="handoff.returned",
        event_type="coordination.handoff.returned",
        actor_principal_id=context.principal.principal_id,
        detail={
            "claim_generation": payload.expected_claim_generation,
            "return_seq": return_seq,
            "work_product_receipts": [str(item["receipt_id"]) for item in pinned],
        },
        policy_revision=policy_revision,
    )
    receipt = _base_receipt(operation, context, policy_revision, handoff)
    receipt["return_seq"] = return_seq
    receipt["work_product_receipts"] = [
        {
            "receipt_id": str(item["receipt_id"]),
            "content_id": str(item["content_id"]),
            "content_revision": item["content_revision"],
            "content_hash": item["content_hash"].hex(),
            "evidence_class": item["evidence_class"],
            "attestation": "self_reported",
        }
        for item in pinned
    ]
    return await _finish(
        connection, context, operation, idempotency_key, digest, receipt, 200
    )


async def accept_handoff(
    connection: asyncpg.Connection,
    context: ScopeContext,
    handoff_id: uuid.UUID,
    payload: AcceptHandoffRequest,
    idempotency_key: str,
) -> tuple[int, dict[str, Any], bool]:
    operation = "coordination.handoff.accept"
    digest = _begin_digest(
        operation,
        context,
        {"handoff_id": str(handoff_id), "request": payload.model_dump(mode="json")},
    )
    previous, replayed = await _begin(
        connection, context, operation, idempotency_key, digest
    )
    if replayed and previous is not None:
        return 200, previous, True
    return await _review_transition(
        connection,
        context,
        operation=operation,
        handoff_id=handoff_id,
        decision="accept",
        to_status="accepted",
        event_type="coordination.handoff.accepted",
        payload=payload,
        idempotency_key=idempotency_key,
        digest=digest,
    )


async def rework_handoff(
    connection: asyncpg.Connection,
    context: ScopeContext,
    handoff_id: uuid.UUID,
    payload: ReworkHandoffRequest,
    idempotency_key: str,
) -> tuple[int, dict[str, Any], bool]:
    operation = "coordination.handoff.rework"
    digest = _begin_digest(
        operation,
        context,
        {"handoff_id": str(handoff_id), "request": payload.model_dump(mode="json")},
    )
    previous, replayed = await _begin(
        connection, context, operation, idempotency_key, digest
    )
    if replayed and previous is not None:
        return 200, previous, True
    return await _review_transition(
        connection,
        context,
        operation=operation,
        handoff_id=handoff_id,
        decision="rework",
        to_status="rework",
        event_type="coordination.handoff.rework",
        payload=payload,
        idempotency_key=idempotency_key,
        digest=digest,
    )


async def _review_transition(
    connection: asyncpg.Connection,
    context: ScopeContext,
    *,
    operation: str,
    handoff_id: uuid.UUID,
    decision: str,
    to_status: str,
    event_type: str,
    payload: AcceptHandoffRequest | ReworkHandoffRequest,
    idempotency_key: str,
    digest: bytes,
) -> tuple[int, dict[str, Any], bool]:
    policy_revision = await repository.declare_policy_revision(connection, context)
    action = "accept" if decision == "accept" else "rework"
    row, _now = await _load_and_validate(connection, context, handoff_id, action)

    approval_id: uuid.UUID | None = getattr(payload, "approval_id", None)
    note = getattr(payload, "note", None) or getattr(payload, "instructions", None)
    if decision == "accept" and row["require_human_accept"]:
        if approval_id is None:
            raise problem("human_review_required")
        await load_approval_for_gate(
            connection,
            scope_id=context.selected.scope_id,
            approval_id=approval_id,
            gate_kind="handoff_accept",
            subject_handoff_id=handoff_id,
        )

    await repository.update_handoff_state(
        connection,
        scope_id=context.selected.scope_id,
        handoff_id=handoff_id,
        expected_revision=row["revision"],
        status=to_status,
    )
    review_seq = await connection.fetchval(
        """
        INSERT INTO cortex_coord.reviews
            (scope_id, handoff_id, review_seq, decision, approval_id,
             reviewer_principal_id, note)
        VALUES ($1, $2, NULL, $3, $4, $5, $6)
        RETURNING review_seq
        """,
        context.selected.scope_id,
        handoff_id,
        decision,
        approval_id,
        context.principal.principal_id,
        note,
    )
    handoff = {
        "handoff_id": handoff_id,
        "status": to_status,
        "claim_generation": row["claim_generation"],
        "revision": row["revision"] + 1,
    }
    await repository.record_change(
        connection,
        scope_id=context.selected.scope_id,
        aggregate_kind="handoff",
        aggregate_id=handoff_id,
        action=f"handoff.{decision}" if decision == "accept" else "handoff.rework",
        event_type=event_type,
        actor_principal_id=context.principal.principal_id,
        detail={
            "decision": decision,
            "review_seq": review_seq,
            "approval_id": str(approval_id) if approval_id else None,
            "require_human_accept": row["require_human_accept"],
        },
        policy_revision=policy_revision,
    )
    receipt = _base_receipt(operation, context, policy_revision, handoff)
    receipt["review_seq"] = review_seq
    receipt["decision"] = decision
    return await _finish(
        connection, context, operation, idempotency_key, digest, receipt, 200
    )


async def retry_handoff(
    connection: asyncpg.Connection,
    context: ScopeContext,
    handoff_id: uuid.UUID,
    payload: RetryHandoffRequest,
    idempotency_key: str,
) -> tuple[int, dict[str, Any], bool]:
    operation = "coordination.handoff.retry"
    digest = _begin_digest(
        operation,
        context,
        {"handoff_id": str(handoff_id), "request": payload.model_dump(mode="json")},
    )
    previous, replayed = await _begin(
        connection, context, operation, idempotency_key, digest
    )
    if replayed and previous is not None:
        return 200, previous, True

    policy_revision = await repository.declare_policy_revision(connection, context)
    row, _now = await _load_and_validate(connection, context, handoff_id, "retry")
    await repository.update_handoff_state(
        connection,
        scope_id=context.selected.scope_id,
        handoff_id=handoff_id,
        expected_revision=row["revision"],
        status="open",
    )
    handoff = {
        "handoff_id": handoff_id,
        "status": "open",
        "claim_generation": row["claim_generation"],
        "revision": row["revision"] + 1,
    }
    await repository.record_change(
        connection,
        scope_id=context.selected.scope_id,
        aggregate_kind="handoff",
        aggregate_id=handoff_id,
        action="handoff.retry",
        event_type="coordination.handoff.retried",
        actor_principal_id=context.principal.principal_id,
        detail={"reason": payload.reason, "from_status": "failed"},
        policy_revision=policy_revision,
    )
    receipt = _base_receipt(operation, context, policy_revision, handoff)
    receipt["reason"] = payload.reason
    return await _finish(
        connection, context, operation, idempotency_key, digest, receipt, 200
    )


async def withdraw_handoff(
    connection: asyncpg.Connection,
    context: ScopeContext,
    handoff_id: uuid.UUID,
    payload: WithdrawHandoffRequest,
    idempotency_key: str,
) -> tuple[int, dict[str, Any], bool]:
    operation = "coordination.handoff.withdraw"
    digest = _begin_digest(
        operation,
        context,
        {"handoff_id": str(handoff_id), "request": payload.model_dump(mode="json")},
    )
    previous, replayed = await _begin(
        connection, context, operation, idempotency_key, digest
    )
    if replayed and previous is not None:
        return 200, previous, True

    policy_revision = await repository.declare_policy_revision(connection, context)
    row, _now = await _load_and_validate(connection, context, handoff_id, "withdraw")
    if row["created_by_principal"] != context.principal.principal_id and not (
        await repository.is_human_lead(
            connection, context.selected.scope_id, context.principal.principal_id
        )
    ):
        raise problem("withdraw_not_allowed")

    if row["status"] == "claimed":
        await repository.end_active_claim(
            connection,
            scope_id=context.selected.scope_id,
            handoff_id=handoff_id,
            claim_generation=row["claim_generation"],
            end_reason="withdrawn",
        )
    await repository.update_handoff_state(
        connection,
        scope_id=context.selected.scope_id,
        handoff_id=handoff_id,
        expected_revision=row["revision"],
        status="withdrawn",
        clear_lease=True,
    )
    handoff = {
        "handoff_id": handoff_id,
        "status": "withdrawn",
        "claim_generation": row["claim_generation"],
        "revision": row["revision"] + 1,
    }
    await repository.record_change(
        connection,
        scope_id=context.selected.scope_id,
        aggregate_kind="handoff",
        aggregate_id=handoff_id,
        action="handoff.withdrawn",
        event_type="coordination.handoff.withdrawn",
        actor_principal_id=context.principal.principal_id,
        detail={"reason": payload.reason, "from_status": row["status"]},
        policy_revision=policy_revision,
    )
    receipt = _base_receipt(operation, context, policy_revision, handoff)
    receipt["reason"] = payload.reason
    return await _finish(
        connection, context, operation, idempotency_key, digest, receipt, 200
    )


def _claim_row(row: Any) -> dict[str, Any]:
    return {
        "claim_generation": row["claim_generation"],
        "claimant_principal_id": str(row["claimant_principal_id"]),
        "claimed_at": row["claimed_at"].isoformat(),
        "lease_expires_at": row["lease_expires_at"].isoformat(),
        "ended_at": repository.iso(row["ended_at"]),
        "end_reason": row["end_reason"],
    }


async def get_handoff(
    connection: asyncpg.Connection,
    context: ScopeContext,
    handoff_id: uuid.UUID,
) -> dict[str, Any]:
    row = await repository.load_handoff(
        connection, context.selected.scope_id, handoff_id
    )
    scope_id = context.selected.scope_id
    claims = await connection.fetch(
        "SELECT * FROM cortex_coord.claims WHERE scope_id = $1 AND handoff_id = $2 "
        "ORDER BY claim_generation",
        scope_id,
        handoff_id,
    )
    returns = await connection.fetch(
        "SELECT * FROM cortex_coord.returns WHERE scope_id = $1 AND handoff_id = $2 "
        "ORDER BY return_seq",
        scope_id,
        handoff_id,
    )
    reviews = await connection.fetch(
        "SELECT * FROM cortex_coord.reviews WHERE scope_id = $1 AND handoff_id = $2 "
        "ORDER BY review_seq",
        scope_id,
        handoff_id,
    )
    receipts = await connection.fetch(
        "SELECT * FROM cortex_coord.work_product_receipts "
        "WHERE scope_id = $1 AND handoff_id = $2 ORDER BY created_at, receipt_id",
        scope_id,
        handoff_id,
    )
    audit = await connection.fetch(
        "SELECT * FROM cortex_coord.audit_log "
        "WHERE scope_id = $1 AND aggregate_kind = 'handoff' AND aggregate_id = $2 "
        "ORDER BY audit_seq",
        scope_id,
        handoff_id,
    )
    return {
        "handoff_id": str(row["handoff_id"]),
        "scope_id": str(row["scope_id"]),
        "status": row["status"],
        "title": row["title"],
        "brief": row["brief"],
        "dedup_key": row["dedup_key"],
        "addressed_role": row["addressed_role"],
        "addressed_actor_id": (
            str(row["addressed_actor_id"]) if row["addressed_actor_id"] else None
        ),
        "task_id": str(row["task_id"]) if row["task_id"] else None,
        "claim_generation": row["claim_generation"],
        "active_lease_expires_at": repository.iso(row["active_lease_expires_at"]),
        "require_human_accept": row["require_human_accept"],
        "revision": row["revision"],
        "created_by_principal": str(row["created_by_principal"]),
        "created_at": row["created_at"].isoformat(),
        "updated_at": row["updated_at"].isoformat(),
        "relay": (
            None
            if row["relay_id"] is None
            else {
                "relay_id": str(row["relay_id"]),
                "source_scope_id": str(row["relay_source_scope_id"]),
                "source_handoff_id": str(row["relay_source_handoff_id"]),
            }
        ),
        "claims": [_claim_row(claim) for claim in claims],
        "returns": [
            {
                "return_seq": item["return_seq"],
                "claim_generation": item["claim_generation"],
                "returned_by_principal": str(item["returned_by_principal"]),
                "summary": item["summary"],
                "created_at": item["created_at"].isoformat(),
            }
            for item in returns
        ],
        "reviews": [
            {
                "review_seq": item["review_seq"],
                "decision": item["decision"],
                "approval_id": (
                    str(item["approval_id"]) if item["approval_id"] else None
                ),
                "reviewer_principal_id": str(item["reviewer_principal_id"]),
                "note": item["note"],
                "created_at": item["created_at"].isoformat(),
            }
            for item in reviews
        ],
        "work_product_receipts": [
            {
                "receipt_id": str(item["receipt_id"]),
                "content_id": str(item["content_id"]),
                "content_revision": item["content_revision"],
                "content_hash": item["content_hash"].hex(),
                "evidence_class": item["evidence_class"],
                "attestation": item["attestation"],
                "return_seq": item["return_seq"],
                "created_at": item["created_at"].isoformat(),
            }
            for item in receipts
        ],
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


async def list_handoffs(
    connection: asyncpg.Connection,
    context: ScopeContext,
    filters: dict[str, Any],
) -> dict[str, Any]:
    from .state import HANDOFF_STATUSES

    status = filters.get("status")
    if status is not None and status not in HANDOFF_STATUSES:
        raise problem("invalid_filter")
    try:
        limit = int(filters.get("limit", 25))
    except (TypeError, ValueError) as exc:
        raise problem("invalid_filter") from exc
    if not 1 <= limit <= 100:
        raise problem("invalid_filter")
    cursor = filters.get("cursor")
    cursor_at: Any = None
    cursor_id: Any = None
    if cursor:
        cursor_at, cursor_id = repository.decode_list_cursor(str(cursor))

    rows = await connection.fetch(
        """
        SELECT h.scope_id, h.handoff_id, h.title, h.status, h.claim_generation,
               h.addressed_role, h.addressed_actor_id, h.task_id, h.created_at
          FROM cortex_coord.handoffs AS h
         WHERE ($1::text IS NULL OR h.status = $1)
           AND ($2::timestamptz IS NULL
                OR (h.created_at, h.handoff_id) < ($2::timestamptz, $3::uuid))
         ORDER BY h.created_at DESC, h.handoff_id DESC
         LIMIT $4
        """,
        status,
        cursor_at,
        cursor_id,
        limit,
    )
    items = [
        {
            "scope_id": str(row["scope_id"]),
            "handoff_id": str(row["handoff_id"]),
            "title": row["title"],
            "status": row["status"],
            "claim_generation": row["claim_generation"],
            "addressed_role": row["addressed_role"],
            "addressed_actor_id": (
                str(row["addressed_actor_id"]) if row["addressed_actor_id"] else None
            ),
            "task_id": str(row["task_id"]) if row["task_id"] else None,
            "created_at": row["created_at"].isoformat(),
        }
        for row in rows
    ]
    next_cursor = (
        repository.encode_list_cursor(
            rows[-1]["created_at"], rows[-1]["handoff_id"]
        )
        if len(rows) == limit
        else None
    )
    return {
        "items": items,
        "filters": {"status": status},
        "limit": limit,
        "next_cursor": next_cursor,
    }
