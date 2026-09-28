"""Work-product receipt reads with freshness semantics (R10).

A receipt pins the exact content revision and hash returned with a handoff.
Freshness is recomputed at read time against the memory module's published
read use case: a newer revision or a changed hash makes the receipt stale;
invalidated, superseded, tombstoned or unreadable source content makes it
source_unavailable. A receipt records what the returning worker attested;
it is never labeled independent correctness verification. Independent
verification, when it exists, is attached by the verification module and
referenced here explicitly.
"""

from __future__ import annotations

import uuid
from typing import Any

import asyncpg

from ..content import get_content
from ..store import ApiProblem, ScopeContext
from . import repository
from .repository import problem


async def get_work_product_receipt(
    connection: asyncpg.Connection,
    context: ScopeContext,
    receipt_id: uuid.UUID,
) -> dict[str, Any]:
    row = await connection.fetchrow(
        """
        SELECT r.scope_id, r.receipt_id, r.handoff_id, r.return_seq,
               r.content_id, r.content_revision, r.content_hash,
               r.evidence_class, r.attestation, r.created_by_principal,
               r.created_at,
               h.status AS handoff_status
          FROM cortex_coord.work_product_receipts AS r
          JOIN cortex_coord.handoffs AS h
            ON h.scope_id = r.scope_id AND h.handoff_id = r.handoff_id
         WHERE r.scope_id = $1 AND r.receipt_id = $2
        """,
        context.selected.scope_id,
        receipt_id,
    )
    if row is None:
        raise problem("work_product_receipt_not_found")

    pinned_revision = row["content_revision"]
    pinned_hash = row["content_hash"]
    current: dict[str, Any] | None = None
    freshness = "fresh"
    try:
        content_row = await get_content(connection, row["content_id"], None, False)
    except ApiProblem as exc:
        if exc.code != "content_not_found":
            raise
        freshness = "source_unavailable"
    else:
        current = {
            "revision": content_row["revision"],
            "content_hash": content_row["content_hash"],
            "status": content_row["status"],
        }
        if content_row["status"] != "current":
            freshness = "source_unavailable"
        elif (
            content_row["revision"] != pinned_revision
            or bytes.fromhex(content_row["content_hash"]) != pinned_hash
        ):
            freshness = "stale"

    verification = await _independent_verification(
        connection, context.selected.scope_id, receipt_id
    )
    return {
        "receipt_id": str(row["receipt_id"]),
        "scope_id": str(row["scope_id"]),
        "handoff_id": str(row["handoff_id"]),
        "handoff_status": row["handoff_status"],
        "return_seq": row["return_seq"],
        "content_id": str(row["content_id"]),
        "pinned": {
            "revision": pinned_revision,
            "content_hash": pinned_hash.hex(),
        },
        "current": current,
        "freshness": freshness,
        "evidence_class": row["evidence_class"],
        "attestation": row["attestation"],
        "independent_verification": verification,
        "created_by_principal": str(row["created_by_principal"]),
        "created_at": row["created_at"].isoformat(),
    }


async def _independent_verification(
    connection: asyncpg.Connection,
    scope_id: uuid.UUID,
    receipt_id: uuid.UUID,
) -> dict[str, Any] | None:
    """Published read model of the verification module (phase 2, R19).

    Coordination never decides that a report is true; it only references the
    latest independent verification record when one exists.
    """
    from ..verification import latest_for_receipt

    return await latest_for_receipt(connection, scope_id, receipt_id)
