"""DB-backed integration suite for the Feed module (R20).

Exercises the transactional outbox dispatcher, scoped feed entries, cursor
generation, poll/resync semantics, retention pruning and dashboard shape
against a fresh candidate database with migrations 0001-0003 and 0007
applied. Gated on the same environment as the coordination integration
suite and reuses its seeded fixture.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid

import asyncpg
import pytest

DATABASE_URL = os.environ.get("CORTEX_V2_TEST_DATABASE_URL")
MIGRATOR_URL = os.environ.get("CORTEX_V2_TEST_MIGRATOR_DATABASE_URL")

if not DATABASE_URL or not MIGRATOR_URL:
    pytest.skip(
        "feed integration suite requires a candidate database "
        "(CORTEX_V2_TEST_DATABASE_URL / CORTEX_V2_TEST_MIGRATOR_DATABASE_URL)",
        allow_module_level=True,
    )

from test_coordination_integration import (  # noqa: E402
    FIXTURE,
    _command,
    _create_handoff,
    _create_request,
    as_owner,
    as_worker_a,
)

from cortex_v2 import content as content_uc  # noqa: E402
from cortex_v2.feed import dispatcher as feed_uc  # noqa: E402
from cortex_v2.feed import reads as feed_reads  # noqa: E402
from cortex_v2.feed.models import (  # noqa: E402
    AdvanceFeedGenerationRequest,
    DispatchFeedRequest,
    PruneFeedRequest,
)
from cortex_v2.models import CreateContentRequest  # noqa: E402


def _code(excinfo) -> str:
    return excinfo.value.code


async def _migrator_execute(query: str, *args) -> None:
    connection = await asyncpg.connect(MIGRATOR_URL)
    try:
        await connection.execute(query, *args)
    finally:
        await connection.close()


def _dispatch(key: str | None = None, batch_limit: int = 500):
    return as_owner(
        lambda conn, ctx: feed_uc.dispatch_scope(
            conn, ctx, DispatchFeedRequest(batch_limit=batch_limit),
            key or f"k-{uuid.uuid4()}",
        )
    )


def _poll(cursor: str | None = None, limit: int = 100):
    filters: dict[str, object] = {"limit": limit}
    if cursor is not None:
        filters["cursor"] = cursor
    return as_owner(
        lambda conn, ctx: feed_reads.poll_feed(conn, ctx, filters),
        write=False,
    )


def test_dispatch_publishes_committed_outbox_from_multiple_sources() -> None:
    # Produce a coordination event and a content event.
    asyncio.run(_create_handoff(FIXTURE.worker_a, _create_request(title="feed-me")))
    asyncio.run(
        as_worker_a(
            lambda conn, ctx: content_uc.create_content(
                conn, ctx,
                CreateContentRequest(
                    content_class="knowledge",
                    payload={"content": "feed knowledge"},
                    body="feed knowledge",
                ),
                f"k-{uuid.uuid4()}",
            )
        )
    )

    _status, receipt, replayed = asyncio.run(_dispatch())
    assert replayed is False
    assert receipt["state"] == "committed"
    assert receipt["published_count"] >= 2
    assert receipt["sources"]["cortex_coord.outbox_events"] >= 1
    assert receipt["sources"]["cortex_core.content_outbox_events"] >= 1
    assert receipt["last_published_seq"] >= receipt["published_count"]

    # Re-dispatch under a new key finds nothing undelivered: no duplicates.
    _status, second, _ = asyncio.run(_dispatch())
    assert second["published_count"] == 0


def test_dispatch_replays_receipt_for_same_key() -> None:
    asyncio.run(_create_handoff(FIXTURE.worker_a))
    key = f"k-{uuid.uuid4()}"
    _s1, first, replay1 = asyncio.run(_dispatch(key))
    _s2, second, replay2 = asyncio.run(_dispatch(key))
    assert replay1 is False
    assert replay2 is True
    assert second == first


def test_poll_fresh_start_snapshot_then_incremental_entries() -> None:
    asyncio.run(_create_handoff(FIXTURE.worker_a, _create_request(title="p1")))
    asyncio.run(_dispatch())

    fresh = asyncio.run(_poll())
    assert fresh["fresh_start"] is True
    assert fresh["resync_required"] is False
    assert fresh["entries"] == []
    assert fresh["snapshot"]["scope_id"] == str(FIXTURE.scope_a)
    assert fresh["snapshot"]["coverage"] in ("complete", "partial")

    # From sequence zero we replay the durable history for this generation.
    from cortex_v2.feed.cursors import FeedCursor, encode_feed_cursor

    zero = encode_feed_cursor(
        FeedCursor(generation=_generation(fresh), sequence=0)
    )
    replay = asyncio.run(_poll(zero, limit=500))
    assert replay["resync_required"] is False
    assert len(replay["entries"]) >= 1
    versions_by_aggregate: dict[str, list[int]] = {}
    last_seq = 0
    for entry in replay["entries"]:
        assert entry["feed_seq"] > last_seq
        last_seq = entry["feed_seq"]
        key = f"{entry['aggregate_kind']}:{entry['aggregate_id']}"
        if entry["aggregate_version"] is not None:
            versions_by_aggregate.setdefault(key, []).append(
                entry["aggregate_version"]
            )
    for versions in versions_by_aggregate.values():
        assert versions == sorted(versions)

    # Incremental polling from the returned cursor yields nothing new until
    # more work commits and dispatches.
    idle = asyncio.run(_poll(replay["cursor"]))
    assert idle["entries"] == []
    asyncio.run(_create_handoff(FIXTURE.worker_a, _create_request(title="p2")))
    asyncio.run(_dispatch())
    incremental = asyncio.run(_poll(replay["cursor"]))
    assert len(incremental["entries"]) >= 1
    assert all(
        entry["feed_seq"] > last_seq for entry in incremental["entries"]
    )


def _generation(poll_result: dict) -> int:
    return poll_result["snapshot"]["projections"]["feed"]["feed_generation"]


def test_prune_ages_out_entries_and_old_cursors_resync() -> None:
    asyncio.run(_create_handoff(FIXTURE.worker_a))
    asyncio.run(_dispatch())
    poll = asyncio.run(_poll())
    generation = _generation(poll)

    # Retention never touches fresh entries.
    _status, receipt, _ = asyncio.run(
        as_owner(
            lambda conn, ctx: feed_uc.prune_feed(
                conn, ctx, PruneFeedRequest(older_than_days=30),
                f"k-{uuid.uuid4()}",
            )
        )
    )
    assert receipt["pruned_count"] == 0

    # Age one entry through the migrator (retention is time-based), then
    # prune: the entry is gone and the oldest cursor advances.
    async def age_one_entry() -> int | None:
        """Age the oldest entry the only legitimate way: the owner role
        removes the immutable row and re-inserts it byte-identical with an
        explicit past published_at. The insert trigger requires the declared
        writer-policy revision, so declare it for this transaction."""
        connection = await asyncpg.connect(MIGRATOR_URL)
        try:
            async with connection.transaction():
                await connection.execute(
                    "SELECT set_config('cortex.policy_revision', ("
                    "  SELECT COALESCE(max(revision), 0)::text"
                    "    FROM cortex_auth.writer_policies"
                    "   WHERE scope_id = $1), true)",
                    FIXTURE.scope_a,
                )
                row = await connection.fetchrow(
                    "WITH target AS ("
                    "  DELETE FROM cortex_feed.feed_entries"
                    "   WHERE scope_id = $1 AND feed_generation = $2"
                    "     AND feed_seq = ("
                    "       SELECT min(feed_seq) FROM cortex_feed.feed_entries"
                    "        WHERE scope_id = $1 AND feed_generation = $2)"
                    "  RETURNING *) SELECT * FROM target",
                    FIXTURE.scope_a,
                    generation,
                )
                if row is None:
                    return None
                payload = row["payload"]
                await connection.execute(
                    "INSERT INTO cortex_feed.feed_entries"
                    " (scope_id, feed_generation, feed_seq, source_event_id,"
                    "  source_table, aggregate_kind, aggregate_id,"
                    "  aggregate_version, event_type, payload, published_at)"
                    " VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10::jsonb,"
                    "         now() - interval '40 days')",
                    row["scope_id"],
                    row["feed_generation"],
                    row["feed_seq"],
                    row["source_event_id"],
                    row["source_table"],
                    row["aggregate_kind"],
                    row["aggregate_id"],
                    row["aggregate_version"],
                    row["event_type"],
                    payload if isinstance(payload, str) else json.dumps(payload),
                )
                return row["feed_seq"]
        finally:
            await connection.close()

    aged_seq = asyncio.run(age_one_entry())
    assert aged_seq is not None

    _status, pruned, _ = asyncio.run(
        as_owner(
            lambda conn, ctx: feed_uc.prune_feed(
                conn, ctx, PruneFeedRequest(older_than_days=30),
                f"k-{uuid.uuid4()}",
            )
        )
    )
    assert pruned["pruned_count"] == 1
    assert pruned["oldest_available_seq"] > aged_seq

    from cortex_v2.feed.cursors import FeedCursor, encode_feed_cursor

    stale = encode_feed_cursor(FeedCursor(generation=generation, sequence=0))
    resync = asyncio.run(_poll(stale))
    assert resync["resync_required"] is True
    assert resync["snapshot"] is not None
    assert resync["entries"] == []


def test_generation_advance_forces_resync_for_every_old_cursor() -> None:
    asyncio.run(_create_handoff(FIXTURE.worker_a))
    asyncio.run(_dispatch())
    before = asyncio.run(_poll())
    old_cursor = before["cursor"]

    _status, receipt, _ = asyncio.run(
        as_owner(
            lambda conn, ctx: feed_uc.advance_generation(
                conn, ctx,
                AdvanceFeedGenerationRequest(reason="rebuild rehearsal"),
                f"k-{uuid.uuid4()}",
            )
        )
    )
    assert receipt["feed_generation"] == receipt["prior_generation"] + 1

    resync = asyncio.run(_poll(old_cursor))
    assert resync["resync_required"] is True
    assert resync["snapshot"]["projections"]["feed"]["feed_generation"] == (
        receipt["feed_generation"]
    )

    # The new generation republishes only undelivered outbox rows.
    asyncio.run(_create_handoff(FIXTURE.worker_a))
    asyncio.run(_dispatch())
    fresh = asyncio.run(_poll())
    assert fresh["fresh_start"] is True
    assert fresh["resync_required"] is False


def test_dashboard_reports_backlog_and_degradation_honestly() -> None:
    asyncio.run(_create_handoff(FIXTURE.worker_a))
    dashboard = asyncio.run(
        as_owner(
            lambda conn, ctx: feed_reads.feed_dashboard(conn, ctx),
            write=False,
        )
    )
    assert dashboard["coverage"] == "complete"
    assert dashboard["degraded"] == []
    projections = dashboard["projections"]
    assert (
        projections["outbox_backlog"]["cortex_coord.outbox_events"]["pending"]
        >= 1
    )
    assert "handoffs_by_status" in projections
    assert "tasks_by_status" in projections

    asyncio.run(_dispatch())
    after = asyncio.run(
        as_owner(
            lambda conn, ctx: feed_reads.feed_dashboard(conn, ctx),
            write=False,
        )
    )
    assert (
        after["projections"]["outbox_backlog"]["cortex_coord.outbox_events"][
            "pending"
        ]
        == 0
    )


def test_feed_rows_are_immutable_and_rls_scoped() -> None:
    asyncio.run(_create_handoff(FIXTURE.worker_a))
    asyncio.run(_dispatch())

    async def naked_counts() -> tuple[int, int]:
        connection = await asyncpg.connect(DATABASE_URL)
        try:
            entries = await connection.fetchval(
                "SELECT count(*) FROM cortex_feed.feed_entries"
            )
            checkpoints = await connection.fetchval(
                "SELECT count(*) FROM cortex_feed.publication_checkpoints"
            )
            return entries, checkpoints
        finally:
            await connection.close()

    entries, checkpoints = asyncio.run(naked_counts())
    assert entries == 0
    assert checkpoints == 0

    async def tamper() -> None:
        connection = await asyncpg.connect(MIGRATOR_URL)
        try:
            await connection.execute(
                "UPDATE cortex_feed.feed_entries SET event_type = 'tampered'"
            )
        finally:
            await connection.close()

    # Even the owner role cannot mutate feed entries: the reject trigger
    # fires for every role.
    with pytest.raises(asyncpg.PostgresError):
        asyncio.run(tamper())


def test_outbox_delivery_marked_exactly_once() -> None:
    asyncio.run(_create_handoff(FIXTURE.worker_a))
    asyncio.run(_dispatch())
    asyncio.run(_dispatch())

    async def delivered() -> int:
        connection = await asyncpg.connect(MIGRATOR_URL)
        try:
            return await connection.fetchval(
                "SELECT count(*) FROM cortex_coord.outbox_events "
                "WHERE scope_id = $1 AND delivered_at IS NULL",
                FIXTURE.scope_a,
            )
        finally:
            await connection.close()

    assert asyncio.run(delivered()) == 0

    from cortex_v2.feed.cursors import FeedCursor, encode_feed_cursor

    fresh = asyncio.run(_poll())
    zero = encode_feed_cursor(
        FeedCursor(generation=_generation(fresh), sequence=0)
    )
    payload_probe = asyncio.run(_poll(zero, limit=500))
    kinds = {entry["aggregate_kind"] for entry in payload_probe["entries"]}
    assert "handoff" in kinds
