"""Feed reads: cursor polling, resync snapshots and honest dashboards (R20).

Polls replay durable entries after the caller's cursor. An expired retention
cursor or a foreign generation yields ``resync_required`` plus a snapshot -
never silent loss and never an empty green state: each dashboard projection
degrades individually and explicitly when its query fails.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Awaitable, Callable

import asyncpg

from ..store import ScopeContext
from . import repository
from .cursors import (
    OK,
    RESYNC_REQUIRED,
    FeedCursor,
    decode_feed_cursor,
    encode_feed_cursor,
    evaluate_cursor,
)
from .repository import problem


def _json_object(value: str | dict[str, Any]) -> dict[str, Any]:
    return json.loads(value) if isinstance(value, str) else value


def _entry_view(row: Any) -> dict[str, Any]:
    return {
        "feed_seq": row["feed_seq"],
        "feed_generation": row["feed_generation"],
        "source_event_id": str(row["source_event_id"]),
        "source_table": row["source_table"],
        "aggregate_kind": row["aggregate_kind"],
        "aggregate_id": str(row["aggregate_id"]),
        "aggregate_version": row["aggregate_version"],
        "event_type": row["event_type"],
        "payload": _json_object(row["payload"]),
        "published_at": row["published_at"].isoformat(),
    }


async def _section(
    connection: asyncpg.Connection,
    name: str,
    loader: Callable[[asyncpg.Connection], Awaitable[Any]],
    projections: dict[str, Any],
    degraded: list[dict[str, str]],
) -> None:
    """Run one projection in a savepoint; failures degrade explicitly."""
    try:
        async with connection.transaction():
            projections[name] = await loader(connection)
    except asyncpg.PostgresError as exc:
        degraded.append(
            {
                "projection": name,
                "reason": type(exc).__name__,
                "detail": "projection unavailable; state is unknown, not empty",
            }
        )


async def build_snapshot(
    connection: asyncpg.Connection, context: ScopeContext
) -> dict[str, Any]:
    """Coherent scoped snapshot used for fresh starts and resync."""
    scope_id = context.selected.scope_id
    projections: dict[str, Any] = {}
    degraded: list[dict[str, str]] = []

    async def checkpoint_state(conn: asyncpg.Connection) -> dict[str, Any]:
        checkpoint = await repository.read_checkpoint(conn, scope_id)
        return {
            "feed_generation": checkpoint.feed_generation,
            "last_published_seq": checkpoint.last_published_seq,
            "oldest_available_seq": checkpoint.oldest_available_seq,
        }

    async def outbox_backlog(conn: asyncpg.Connection) -> dict[str, Any]:
        backlog: dict[str, Any] = {}
        for source in repository.OUTBOX_SOURCES:
            row = await conn.fetchrow(
                f"SELECT count(*) AS pending, min(created_at) AS oldest "
                f"FROM {source.table} "
                "WHERE scope_id = $1 AND delivered_at IS NULL",
                scope_id,
            )
            backlog[source.table] = {
                "pending": int(row["pending"]),
                "oldest_undelivered_at": (
                    row["oldest"].isoformat() if row["oldest"] else None
                ),
            }
        return backlog

    async def handoff_counts(conn: asyncpg.Connection) -> dict[str, int]:
        rows = await conn.fetch(
            "SELECT status, count(*) AS n FROM cortex_coord.handoffs "
            "WHERE scope_id = $1 GROUP BY status",
            scope_id,
        )
        return {row["status"]: int(row["n"]) for row in rows}

    async def task_counts(conn: asyncpg.Connection) -> dict[str, int]:
        rows = await conn.fetch(
            "SELECT status, count(*) AS n FROM cortex_coord.tasks "
            "WHERE scope_id = $1 GROUP BY status",
            scope_id,
        )
        return {row["status"]: int(row["n"]) for row in rows}

    async def gate_counts(conn: asyncpg.Connection) -> dict[str, int]:
        rows = await conn.fetch(
            "SELECT decision, count(*) AS n FROM cortex_coord.approvals "
            "WHERE scope_id = $1 GROUP BY decision",
            scope_id,
        )
        return {row["decision"]: int(row["n"]) for row in rows}

    await _section(connection, "feed", checkpoint_state, projections, degraded)
    await _section(connection, "outbox_backlog", outbox_backlog, projections, degraded)
    await _section(connection, "handoffs_by_status", handoff_counts, projections, degraded)
    await _section(connection, "tasks_by_status", task_counts, projections, degraded)
    await _section(connection, "approvals_by_decision", gate_counts, projections, degraded)

    return {
        "scope_id": str(scope_id),
        "coverage": "partial" if degraded else "complete",
        "projections": projections,
        "degraded": degraded,
    }


async def poll_feed(
    connection: asyncpg.Connection,
    context: ScopeContext,
    filters: dict[str, Any],
) -> dict[str, Any]:
    raw_cursor = filters.get("cursor")
    cursor: FeedCursor | None = (
        decode_feed_cursor(str(raw_cursor)) if raw_cursor else None
    )
    try:
        limit = int(filters.get("limit", 100))
    except (TypeError, ValueError) as exc:
        raise problem("invalid_filter") from exc
    if not 1 <= limit <= 500:
        raise problem("invalid_filter")

    scope_id = context.selected.scope_id
    checkpoint = await repository.read_checkpoint(connection, scope_id)
    decision = evaluate_cursor(cursor, checkpoint)
    current_cursor = encode_feed_cursor(
        FeedCursor(checkpoint.feed_generation, checkpoint.last_published_seq)
    )

    if decision != OK:
        snapshot = await build_snapshot(connection, context)
        return {
            "resync_required": decision == RESYNC_REQUIRED,
            "fresh_start": decision != RESYNC_REQUIRED,
            "entries": [],
            "cursor": current_cursor,
            "snapshot": snapshot,
            "limit": limit,
        }

    rows = await connection.fetch(
        """
        SELECT scope_id, feed_generation, feed_seq, source_event_id,
               source_table, aggregate_kind, aggregate_id, aggregate_version,
               event_type, payload, published_at
          FROM cortex_feed.feed_entries
         WHERE scope_id = $1 AND feed_generation = $2 AND feed_seq > $3
         ORDER BY feed_seq
         LIMIT $4
        """,
        scope_id,
        checkpoint.feed_generation,
        cursor.sequence if cursor else 0,
        limit,
    )
    entries = [_entry_view(row) for row in rows]
    next_cursor = (
        encode_feed_cursor(
            FeedCursor(checkpoint.feed_generation, entries[-1]["feed_seq"])
        )
        if entries
        else current_cursor
    )
    return {
        "resync_required": False,
        "fresh_start": False,
        "entries": entries,
        "cursor": next_cursor,
        "snapshot": None,
        "limit": limit,
    }


async def feed_dashboard(
    connection: asyncpg.Connection,
    context: ScopeContext,
) -> dict[str, Any]:
    snapshot = await build_snapshot(connection, context)
    return {
        **snapshot,
        "dashboard": True,
        "honest_degradation": (
            "failed projections are listed in degraded[] with unknown state; "
            "they are never rendered as zero or green"
        ),
    }
