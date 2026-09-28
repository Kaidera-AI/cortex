"""Verification use cases and the published read model (R19).

Verification records are append-only evidence about someone else's claim:
exact source/revision citations, readback evidence, verifier identity and
tool provenance. Producers cannot verify their own work-product receipts
(no result laundering), and an unsupported assertion can only ever be
recorded as explicitly UNVERIFIABLE with a reason. Coordination consumes
``latest_for_receipt`` as this module's published read model; it never
decides truth itself.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import asyncpg

from ..receipts import begin_command, commit_receipt, request_digest
from ..store import ApiProblem, ScopeContext
from .models import RecordVerificationRequest
from .rules import validation_violation

PROBLEMS: dict[str, tuple[int, str]] = {
    "verification_not_found": (
        404,
        "The verification record is unavailable in the selected scope.",
    ),
    "receipt_not_found": (
        404,
        "The subject work-product receipt is unavailable in the selected "
        "scope.",
    ),
    "handoff_not_found": (
        404,
        "The subject handoff is unavailable in the selected scope.",
    ),
    "self_verification_denied": (
        409,
        "The producer of a work product cannot independently verify it.",
    ),
    "citation_not_found": (
        422,
        "A cited content revision is unavailable in the readable scopes.",
    ),
    "invalid_filter": (422, "A verification filter value is not supported."),
    "policy_revision_stale": (
        409,
        "The writer-policy revision changed; retry the command.",
    ),
    "writer_policy_denied": (
        403,
        "The active writer policy does not allow this principal to write.",
    ),
    "subject_mismatch": (
        422,
        "The subject fields must match the subject kind exactly.",
    ),
    "verified_requires_citations": (
        422,
        "A verified verdict requires source/revision citations.",
    ),
    "verified_requires_readback_evidence": (
        422,
        "A verified verdict requires readback evidence, not assertion.",
    ),
    "contradicted_requires_citations": (
        422,
        "A contradicted verdict requires source/revision citations.",
    ),
    "unverifiable_requires_reason": (
        422,
        "An unverifiable verdict requires an explicit reason.",
    ),
}


def problem(code: str) -> ApiProblem:
    status, message = PROBLEMS[code]
    return ApiProblem(status, code, message)


async def declare_policy_revision(
    connection: asyncpg.Connection, context: ScopeContext
) -> int:
    """Same W1 convention as content/coordination/feed writes."""
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


VERIFICATION_COLUMNS = """
    v.scope_id, v.verification_id, v.subject_kind, v.subject_receipt_id,
    v.subject_handoff_id, v.subject_claim, v.verdict, v.method,
    v.unverifiable_reason, v.readback_evidence, v.tool_name, v.tool_version,
    v.verified_by_principal, v.verified_at, v.detail
"""


def _json_object(value: str | Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _verification_view(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "verification_id": str(row["verification_id"]),
        "scope_id": str(row["scope_id"]),
        "subject": {
            "kind": row["subject_kind"],
            "receipt_id": (
                str(row["subject_receipt_id"])
                if row["subject_receipt_id"]
                else None
            ),
            "handoff_id": (
                str(row["subject_handoff_id"])
                if row["subject_handoff_id"]
                else None
            ),
            "claim": row["subject_claim"],
        },
        "verdict": row["verdict"],
        "method": row["method"],
        "unverifiable_reason": row["unverifiable_reason"],
        "readback_evidence": _json_object(row["readback_evidence"]),
        "tool_name": row["tool_name"],
        "tool_version": row["tool_version"],
        "verified_by_principal": str(row["verified_by_principal"]),
        "verified_at": row["verified_at"].isoformat(),
        "detail": _json_object(row["detail"]),
    }


async def record_verification(
    connection: asyncpg.Connection,
    context: ScopeContext,
    payload: RecordVerificationRequest,
    idempotency_key: str,
) -> tuple[int, dict[str, Any], bool]:
    operation = "verification.record"
    digest = request_digest(
        {
            "operation": operation,
            "scope_id": str(context.selected.scope_id),
            "request": payload.model_dump(mode="json"),
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

    violation = validation_violation(payload)
    if violation is not None:
        raise problem(violation)

    policy_revision = await declare_policy_revision(connection, context)
    scope_id = context.selected.scope_id

    if payload.subject_kind == "work_product_receipt":
        producer = await connection.fetchval(
            "SELECT created_by_principal "
            "FROM cortex_coord.work_product_receipts "
            "WHERE scope_id = $1 AND receipt_id = $2",
            scope_id,
            payload.subject_receipt_id,
        )
        if producer is None:
            raise problem("receipt_not_found")
        if producer == context.principal.principal_id:
            raise problem("self_verification_denied")
    elif payload.subject_kind == "handoff_return":
        exists = await connection.fetchval(
            "SELECT 1 FROM cortex_coord.handoffs "
            "WHERE scope_id = $1 AND handoff_id = $2",
            scope_id,
            payload.subject_handoff_id,
        )
        if exists is None:
            raise problem("handoff_not_found")

    for citation in payload.citations:
        cited_scope = citation.scope_id or scope_id
        found = await connection.fetchval(
            "SELECT 1 FROM cortex_core.content_revisions "
            "WHERE scope_id = $1 AND content_id = $2 AND revision = $3",
            cited_scope,
            citation.content_id,
            citation.revision,
        )
        if found is None:
            raise problem("citation_not_found")

    verification_id = uuid.uuid4()
    verified_at = await connection.fetchval(
        """
        INSERT INTO cortex_verification.verifications
            (scope_id, verification_id, subject_kind, subject_receipt_id,
             subject_handoff_id, subject_claim, verdict, method,
             unverifiable_reason, readback_evidence, tool_name, tool_version,
             verified_by_principal, detail)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb, $11, $12,
                $13, $14::jsonb)
        RETURNING verified_at
        """,
        scope_id,
        verification_id,
        payload.subject_kind,
        payload.subject_receipt_id,
        payload.subject_handoff_id,
        payload.claim,
        payload.verdict,
        payload.method,
        payload.unverifiable_reason,
        json.dumps(
            [item.model_dump(mode="json") for item in payload.readback_evidence],
            ensure_ascii=False,
            sort_keys=True,
        ),
        payload.tool_name,
        payload.tool_version,
        context.principal.principal_id,
        json.dumps(payload.detail, ensure_ascii=False, sort_keys=True),
    )
    for sequence, citation in enumerate(payload.citations, start=1):
        await connection.execute(
            """
            INSERT INTO cortex_verification.verification_citations
                (scope_id, verification_id, citation_seq, cited_scope_id,
                 cited_content_id, cited_revision, note)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            """,
            scope_id,
            verification_id,
            sequence,
            citation.scope_id or scope_id,
            citation.content_id,
            citation.revision,
            citation.note,
        )
    await connection.execute(
        """
        INSERT INTO cortex_verification.outbox_events
            (event_id, scope_id, aggregate_kind, aggregate_id, event_type,
             payload)
        VALUES ($1, $2, 'verification', $3, 'verification.recorded', $4::jsonb)
        """,
        uuid.uuid4(),
        scope_id,
        verification_id,
        json.dumps(
            {
                "verification_id": str(verification_id),
                "subject_kind": payload.subject_kind,
                "verdict": payload.verdict,
                "method": payload.method,
                "policy_revision": policy_revision,
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
    )

    receipt = {
        "state": "committed",
        "operation": operation,
        "verification_id": str(verification_id),
        "scope_id": str(scope_id),
        "subject_kind": payload.subject_kind,
        "verdict": payload.verdict,
        "method": payload.method,
        "citation_count": len(payload.citations),
        "readback_count": len(payload.readback_evidence),
        "verified_at": verified_at.isoformat(),
        "policy_revision": policy_revision,
    }
    await commit_receipt(
        connection,
        principal_id=context.principal.principal_id,
        operation=operation,
        idempotency_key=idempotency_key,
        digest=digest,
        receipt_kind="committed",
        receipt=receipt,
        scope_id=scope_id,
    )
    return 201, receipt, False


async def _load_citations(
    connection: asyncpg.Connection,
    scope_id: uuid.UUID,
    verification_id: uuid.UUID,
) -> list[dict[str, Any]]:
    rows = await connection.fetch(
        "SELECT citation_seq, cited_scope_id, cited_content_id, "
        "cited_revision, note FROM cortex_verification.verification_citations "
        "WHERE scope_id = $1 AND verification_id = $2 ORDER BY citation_seq",
        scope_id,
        verification_id,
    )
    return [
        {
            "citation_seq": row["citation_seq"],
            "cited_scope_id": str(row["cited_scope_id"]),
            "cited_content_id": str(row["cited_content_id"]),
            "cited_revision": row["cited_revision"],
            "note": row["note"],
        }
        for row in rows
    ]


async def get_verification(
    connection: asyncpg.Connection,
    context: ScopeContext,
    verification_id: uuid.UUID,
) -> dict[str, Any]:
    row = await connection.fetchrow(
        f"SELECT {VERIFICATION_COLUMNS} "
        "FROM cortex_verification.verifications AS v "
        "WHERE v.scope_id = $1 AND v.verification_id = $2",
        context.selected.scope_id,
        verification_id,
    )
    if row is None:
        raise problem("verification_not_found")
    return {
        **_verification_view(dict(row)),
        "citations": await _load_citations(
            connection, context.selected.scope_id, verification_id
        ),
    }


async def list_for_subject(
    connection: asyncpg.Connection,
    context: ScopeContext,
    filters: dict[str, Any],
) -> dict[str, Any]:
    subject_kind = filters.get("subject_kind")
    if subject_kind is not None and subject_kind not in (
        "work_product_receipt",
        "handoff_return",
        "claim",
    ):
        raise problem("invalid_filter")
    receipt_id = filters.get("subject_receipt_id")
    handoff_id = filters.get("subject_handoff_id")
    parsed: tuple[uuid.UUID | None, uuid.UUID | None] = (None, None)
    try:
        parsed = (
            uuid.UUID(receipt_id) if receipt_id else None,
            uuid.UUID(handoff_id) if handoff_id else None,
        )
    except ValueError as exc:
        raise problem("invalid_filter") from exc

    rows = await connection.fetch(
        f"""
        SELECT {VERIFICATION_COLUMNS}
          FROM cortex_verification.verifications AS v
         WHERE ($1::text IS NULL OR v.subject_kind = $1)
           AND ($2::uuid IS NULL OR v.subject_receipt_id = $2)
           AND ($3::uuid IS NULL OR v.subject_handoff_id = $3)
         ORDER BY v.verified_at DESC, v.verification_id DESC
         LIMIT 100
        """,
        subject_kind,
        parsed[0],
        parsed[1],
    )
    items = []
    for row in rows:
        view = _verification_view(dict(row))
        view["citations"] = await _load_citations(
            connection, row["scope_id"], row["verification_id"]
        )
        items.append(view)
    return {"items": items, "filters": filters}


async def latest_for_receipt(
    connection: asyncpg.Connection,
    scope_id: uuid.UUID,
    receipt_id: uuid.UUID,
) -> dict[str, Any] | None:
    """Published read model consumed by the coordination receipt view."""
    row = await connection.fetchrow(
        "SELECT verification_id, verdict, method, verified_by_principal, "
        "verified_at FROM cortex_verification.verifications "
        "WHERE scope_id = $1 AND subject_kind = 'work_product_receipt' "
        "AND subject_receipt_id = $2 "
        "ORDER BY verified_at DESC, verification_id DESC LIMIT 1",
        scope_id,
        receipt_id,
    )
    if row is None:
        return None
    return {
        "verification_id": str(row["verification_id"]),
        "verdict": row["verdict"],
        "method": row["method"],
        "verified_by_principal": str(row["verified_by_principal"]),
        "verified_at": row["verified_at"].isoformat(),
    }
