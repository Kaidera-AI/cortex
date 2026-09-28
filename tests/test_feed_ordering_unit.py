"""Pure unit tests for outbox publication ordering (R20).

The dispatcher must preserve per-aggregate version order and must not invent
a global causal order across aggregates. Ordering is a pure function over
the committed outbox batch so it can be proven without a database.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from cortex_v2.feed.ordering import OutboxEvent, order_for_publication

T0 = datetime(2026, 9, 25, 12, 0, 0, tzinfo=UTC)

AGG_A = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
AGG_B = uuid.UUID("bbbbbbbb-0000-4000-8000-000000000002")


def event(
    aggregate_id: uuid.UUID,
    version: int,
    created_at: datetime,
    *,
    kind: str = "handoff",
    event_type: str = "coordination.test",
) -> OutboxEvent:
    return OutboxEvent(
        source_table="cortex_coord.outbox_events",
        event_id=uuid.uuid4(),
        aggregate_kind=kind,
        aggregate_id=aggregate_id,
        aggregate_version=version,
        event_type=event_type,
        payload={"aggregate_version": version},
        created_at=created_at,
    )


def versions(events: list[OutboxEvent], aggregate: uuid.UUID) -> list[int]:
    return [e.aggregate_version for e in events if e.aggregate_id == aggregate]


def test_per_aggregate_order_follows_version_even_with_skewed_clocks() -> None:
    # Aggregate A committed version 1 in a transaction that started later
    # than version 2's transaction (blocked on row locks). created_at order
    # alone would invert them; aggregate_version decides.
    batch = [
        event(AGG_A, 2, T0),
        event(AGG_A, 1, T0 + timedelta(seconds=5)),
    ]
    ordered = order_for_publication(batch)
    assert versions(ordered, AGG_A) == [1, 2]


def test_distinct_aggregates_keep_deterministic_relative_order() -> None:
    batch = [
        event(AGG_B, 1, T0 + timedelta(seconds=2)),
        event(AGG_A, 1, T0 + timedelta(seconds=1)),
        event(AGG_B, 2, T0 + timedelta(seconds=3)),
    ]
    first = order_for_publication(batch)
    second = order_for_publication(list(reversed(batch)))
    assert [e.event_id for e in first] == [e.event_id for e in second]
    assert versions(first, AGG_A) == [1]
    assert versions(first, AGG_B) == [1, 2]
    # Group A starts earlier, so all of A publishes before B.
    assert [e.aggregate_id for e in first] == [AGG_A, AGG_B, AGG_B]


def test_versionless_events_fall_back_to_commit_order() -> None:
    first = event(AGG_A, 1, T0)
    second = event(AGG_A, 2, T0 + timedelta(seconds=1))
    batch = [
        OutboxEvent(
            source_table="cortex_core.outbox_events",
            event_id=uuid.uuid4(),
            aggregate_kind="memory_record",
            aggregate_id=AGG_A,
            aggregate_version=None,
            event_type="memory.recorded",
            payload={},
            created_at=T0 - timedelta(seconds=1),
        ),
        first,
        second,
    ]
    ordered = order_for_publication(batch)
    assert [v for v in versions(ordered, AGG_A) if v is not None] == [1, 2]
    assert ordered[0].aggregate_version is None


def test_empty_batch_orders_to_empty() -> None:
    assert order_for_publication([]) == []


def test_ordering_is_stable_for_identical_inputs() -> None:
    batch = [
        event(AGG_A, 1, T0),
        event(AGG_B, 1, T0),
        event(AGG_A, 2, T0 + timedelta(seconds=1)),
    ]
    assert [e.event_id for e in order_for_publication(batch)] == [
        e.event_id for e in order_for_publication(batch)
    ]
