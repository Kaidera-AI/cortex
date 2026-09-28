"""Feed repository: outbox source registry, checkpoint access, problems.

The dispatcher consumes the durable outbox projections published by the
memory (0001), content (0002), coordination (0003), processing (0004) and
verification (0007) modules. Registration is an explicit integration
decision recorded here in migration order; a registered source's migration
must be applied before ``feed.dispatch`` runs, since the fetch fails closed
against a missing table. Later modules extend the feed through their own
versioned migrations plus an entry in this list, never at runtime.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

import asyncpg

from ..store import ApiProblem, ScopeContext
from .cursors import FeedCheckpoint
from .ordering import OutboxEvent

PROBLEMS: dict[str, tuple[int, str]] = {
    "invalid_cursor": (422, "The feed cursor is malformed."),
    "invalid_filter": (422, "A feed filter value is not supported."),
    "policy_revision_stale": (
        409,
        "The writer-policy revision changed; retry the command.",
    ),
    "writer_policy_denied": (
        403,
        "The active writer policy does not allow this principal to write.",
    ),
    "feed_state_conflict": (
        409,
        "A concurrent feed publication won the race; retry the command.",
    ),
}


def problem(code: str) -> ApiProblem:
    status, message = PROBLEMS[code]
    return ApiProblem(status, code, message, retryable=code == "feed_state_conflict")


@dataclass(frozen=True, slots=True)
class OutboxSource:
    table: str
    aggregate_kind_expression: str
    aggregate_id_expression: str
    version_expression: str


OUTBOX_SOURCES: tuple[OutboxSource, ...] = (
    OutboxSource(
        table="cortex_core.outbox_events",
        aggregate_kind_expression="'memory_record'",
        aggregate_id_expression="aggregate_id",
        version_expression="(payload->>'revision')::bigint",
    ),
    OutboxSource(
        table="cortex_core.content_outbox_events",
        aggregate_kind_expression="'content'",
        aggregate_id_expression="content_id",
        version_expression="(payload->>'revision')::bigint",
    ),
    OutboxSource(
        table="cortex_coord.outbox_events",
        aggregate_kind_expression="aggregate_kind",
        aggregate_id_expression="aggregate_id",
        version_expression="(payload->>'aggregate_version')::bigint",
    ),
    OutboxSource(
        table="cortex_processing.outbox_events",
        aggregate_kind_expression="aggregate_kind",
        aggregate_id_expression="aggregate_id",
        version_expression="(payload->>'aggregate_version')::bigint",
    ),
    OutboxSource(
        table="cortex_verification.outbox_events",
        aggregate_kind_expression="aggregate_kind",
        aggregate_id_expression="aggregate_id",
        version_expression="(payload->>'aggregate_version')::bigint",
    ),
)


async def declare_policy_revision(
    connection: asyncpg.Connection, context: ScopeContext
) -> int:
    """Same W1 convention as content/coordination writes."""
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


async def lock_scope_feed(connection: asyncpg.Connection, scope_id: uuid.UUID):
    """Serialize per-scope publication and lock the checkpoint row."""
    await connection.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
        f"cortex-feed:{scope_id}",
    )
    await connection.execute(
        """
        INSERT INTO cortex_feed.publication_checkpoints (scope_id)
        VALUES ($1)
        ON CONFLICT (scope_id) DO NOTHING
        """,
        scope_id,
    )
    row = await connection.fetchrow(
        "SELECT feed_generation, last_published_seq, oldest_available_seq "
        "FROM cortex_feed.publication_checkpoints "
        "WHERE scope_id = $1 FOR UPDATE",
        scope_id,
    )
    return FeedCheckpoint(
        feed_generation=row["feed_generation"],
        last_published_seq=row["last_published_seq"],
        oldest_available_seq=row["oldest_available_seq"],
    )


async def read_checkpoint(
    connection: asyncpg.Connection, scope_id: uuid.UUID
) -> FeedCheckpoint:
    row = await connection.fetchrow(
        "SELECT feed_generation, last_published_seq, oldest_available_seq "
        "FROM cortex_feed.publication_checkpoints WHERE scope_id = $1",
        scope_id,
    )
    if row is None:
        return FeedCheckpoint(
            feed_generation=1, last_published_seq=0, oldest_available_seq=0
        )
    return FeedCheckpoint(
        feed_generation=row["feed_generation"],
        last_published_seq=row["last_published_seq"],
        oldest_available_seq=row["oldest_available_seq"],
    )


def _json_object(value: str | dict[str, Any]) -> dict[str, Any]:
    return json.loads(value) if isinstance(value, str) else value


async def fetch_undelivered(
    connection: asyncpg.Connection, scope_id: uuid.UUID, batch_limit: int
) -> tuple[list[OutboxEvent], dict[str, int]]:
    events: list[OutboxEvent] = []
    per_source: dict[str, int] = {}
    for source in OUTBOX_SOURCES:
        rows = await connection.fetch(
            f"""
            SELECT event_id,
                   {source.aggregate_kind_expression} AS aggregate_kind,
                   {source.aggregate_id_expression} AS aggregate_id,
                   {source.version_expression} AS aggregate_version,
                   event_type, payload, created_at
              FROM {source.table}
             WHERE scope_id = $1 AND delivered_at IS NULL
             ORDER BY created_at, event_id
             LIMIT $2
            """,
            scope_id,
            batch_limit,
        )
        per_source[source.table] = len(rows)
        for row in rows:
            events.append(
                OutboxEvent(
                    source_table=source.table,
                    event_id=row["event_id"],
                    aggregate_kind=row["aggregate_kind"],
                    aggregate_id=row["aggregate_id"],
                    aggregate_version=row["aggregate_version"],
                    event_type=row["event_type"],
                    payload=_json_object(row["payload"]),
                    created_at=row["created_at"],
                )
            )
    return events, per_source


async def mark_delivered(
    connection: asyncpg.Connection,
    scope_id: uuid.UUID,
    by_table: dict[str, list[uuid.UUID]],
) -> None:
    for source in OUTBOX_SOURCES:
        event_ids = by_table.get(source.table)
        if not event_ids:
            continue
        await connection.execute(
            f"UPDATE {source.table} SET delivered_at = now() "
            "WHERE scope_id = $1 AND event_id = ANY($2::uuid[]) "
            "AND delivered_at IS NULL",
            scope_id,
            event_ids,
        )
