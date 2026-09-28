"""Transactional outbox dispatcher and feed maintenance commands (R20).

One publication transaction per scope: a per-scope advisory lock serializes
publishers, committed outbox rows are ordered per aggregate, delivery
sequence numbers are allocated from the locked checkpoint, and feed rows,
source delivery marks and the checkpoint commit together. Re-delivery is
deduplicated by the unique source-event constraint; a bare sequence maximum
is never used as a commit cursor.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import asyncpg

from ..receipts import begin_command, commit_receipt, request_digest
from ..store import ScopeContext
from . import repository
from .models import (
    AdvanceFeedGenerationRequest,
    DispatchFeedRequest,
    PruneFeedRequest,
)
from .ordering import order_for_publication
from .repository import problem


async def _begin(
    connection: asyncpg.Connection,
    context: ScopeContext,
    operation: str,
    idempotency_key: str,
    parts: dict[str, Any],
) -> tuple[bytes, dict[str, Any] | None, bool]:
    digest = request_digest(
        {
            "operation": operation,
            "scope_id": str(context.selected.scope_id),
            **parts,
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
    return digest, previous, replayed


async def _finish(
    connection: asyncpg.Connection,
    context: ScopeContext,
    operation: str,
    idempotency_key: str,
    digest: bytes,
    receipt: dict[str, Any],
    status: int = 200,
) -> tuple[int, dict[str, Any], bool]:
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
    return status, receipt, False


async def dispatch_scope(
    connection: asyncpg.Connection,
    context: ScopeContext,
    payload: DispatchFeedRequest,
    idempotency_key: str,
) -> tuple[int, dict[str, Any], bool]:
    operation = "feed.dispatch"
    digest, previous, replayed = await _begin(
        connection,
        context,
        operation,
        idempotency_key,
        {"request": payload.model_dump(mode="json")},
    )
    if replayed and previous is not None:
        return 200, previous, True

    policy_revision = await repository.declare_policy_revision(
        connection, context
    )
    scope_id = context.selected.scope_id
    checkpoint = await repository.lock_scope_feed(connection, scope_id)

    events, per_source = await repository.fetch_undelivered(
        connection, scope_id, payload.batch_limit
    )
    ordered = order_for_publication(events)

    sequence = checkpoint.last_published_seq
    by_table: dict[str, list[uuid.UUID]] = {}
    published = 0
    for event in ordered:
        sequence += 1
        inserted = await connection.execute(
            """
            INSERT INTO cortex_feed.feed_entries
                (scope_id, feed_generation, feed_seq, source_event_id,
                 source_table, aggregate_kind, aggregate_id, aggregate_version,
                 event_type, payload)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb)
            ON CONFLICT (scope_id, source_event_id) DO NOTHING
            """,
            scope_id,
            checkpoint.feed_generation,
            sequence,
            event.event_id,
            event.source_table,
            event.aggregate_kind,
            event.aggregate_id,
            event.aggregate_version,
            event.event_type,
            json.dumps(event.payload, ensure_ascii=False, sort_keys=True),
        )
        by_table.setdefault(event.source_table, []).append(event.event_id)
        if inserted == "INSERT 0 1":
            published += 1
    await repository.mark_delivered(connection, scope_id, by_table)

    await connection.execute(
        """
        UPDATE cortex_feed.publication_checkpoints
           SET last_published_seq = $2,
               last_published_at = now(),
               total_published = total_published + $3,
               updated_at = now()
         WHERE scope_id = $1
        """,
        scope_id,
        sequence,
        published,
    )
    receipt = {
        "state": "committed",
        "operation": operation,
        "scope_id": str(scope_id),
        "feed_generation": checkpoint.feed_generation,
        "published_count": published,
        "fetched_count": len(ordered),
        "last_published_seq": sequence,
        "sources": per_source,
        "policy_revision": policy_revision,
    }
    return await _finish(
        connection, context, operation, idempotency_key, digest, receipt
    )


async def prune_feed(
    connection: asyncpg.Connection,
    context: ScopeContext,
    payload: PruneFeedRequest,
    idempotency_key: str,
) -> tuple[int, dict[str, Any], bool]:
    operation = "feed.prune"
    digest, previous, replayed = await _begin(
        connection,
        context,
        operation,
        idempotency_key,
        {"request": payload.model_dump(mode="json")},
    )
    if replayed and previous is not None:
        return 200, previous, True

    policy_revision = await repository.declare_policy_revision(
        connection, context
    )
    scope_id = context.selected.scope_id
    checkpoint = await repository.lock_scope_feed(connection, scope_id)

    deleted = await connection.fetchval(
        """
        WITH pruned AS (
            DELETE FROM cortex_feed.feed_entries
             WHERE scope_id = $1
               AND feed_generation = $2
               AND published_at < now() - make_interval(days => $3)
            RETURNING feed_seq
        )
        SELECT count(*) FROM pruned
        """,
        scope_id,
        checkpoint.feed_generation,
        payload.older_than_days,
    )
    oldest = await connection.fetchval(
        "SELECT min(feed_seq) FROM cortex_feed.feed_entries "
        "WHERE scope_id = $1 AND feed_generation = $2",
        scope_id,
        checkpoint.feed_generation,
    )
    oldest_available = (
        int(oldest) if oldest is not None else checkpoint.last_published_seq
    )
    await connection.execute(
        """
        UPDATE cortex_feed.publication_checkpoints
           SET oldest_available_seq = $2, updated_at = now()
         WHERE scope_id = $1
        """,
        scope_id,
        oldest_available,
    )
    receipt = {
        "state": "committed",
        "operation": operation,
        "scope_id": str(scope_id),
        "feed_generation": checkpoint.feed_generation,
        "pruned_count": int(deleted),
        "oldest_available_seq": oldest_available,
        "policy_revision": policy_revision,
    }
    return await _finish(
        connection, context, operation, idempotency_key, digest, receipt
    )


async def advance_generation(
    connection: asyncpg.Connection,
    context: ScopeContext,
    payload: AdvanceFeedGenerationRequest,
    idempotency_key: str,
) -> tuple[int, dict[str, Any], bool]:
    operation = "feed.generation.advance"
    digest, previous, replayed = await _begin(
        connection,
        context,
        operation,
        idempotency_key,
        {"request": payload.model_dump(mode="json")},
    )
    if replayed and previous is not None:
        return 200, previous, True

    policy_revision = await repository.declare_policy_revision(
        connection, context
    )
    scope_id = context.selected.scope_id
    checkpoint = await repository.lock_scope_feed(connection, scope_id)
    new_generation = checkpoint.feed_generation + 1

    await connection.execute(
        "DELETE FROM cortex_feed.feed_entries "
        "WHERE scope_id = $1 AND feed_generation = $2",
        scope_id,
        checkpoint.feed_generation,
    )
    updated = await connection.execute(
        """
        UPDATE cortex_feed.publication_checkpoints
           SET feed_generation = $2, last_published_seq = 0,
               oldest_available_seq = 0, last_published_at = NULL,
               updated_at = now()
         WHERE scope_id = $1 AND feed_generation = $3
        """,
        scope_id,
        new_generation,
        checkpoint.feed_generation,
    )
    if updated == "UPDATE 0":
        raise problem("feed_state_conflict")
    receipt = {
        "state": "committed",
        "operation": operation,
        "scope_id": str(scope_id),
        "feed_generation": new_generation,
        "prior_generation": checkpoint.feed_generation,
        "reason": payload.reason,
        "policy_revision": policy_revision,
    }
    return await _finish(
        connection, context, operation, idempotency_key, digest, receipt
    )
