"""Pure outbox publication ordering (R20).

The dispatcher publishes committed outbox rows into the scoped feed. Raw
sequence maxima and transaction start times are not safe commit cursors: an
earlier-started transaction can commit after a later one. Ordering is
therefore explicit: per-aggregate order follows the aggregate version the
producer recorded; different aggregates have no invented global causal
order and are sequenced deterministically by first commit time and identity.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class OutboxEvent:
    source_table: str
    event_id: uuid.UUID
    aggregate_kind: str
    aggregate_id: uuid.UUID
    aggregate_version: int | None
    event_type: str
    payload: dict[str, Any]
    created_at: datetime


def _group_key(event: OutboxEvent) -> tuple[str, str]:
    return (event.aggregate_kind, str(event.aggregate_id))


def _group_order(group: list[OutboxEvent]) -> tuple[datetime, str, str]:
    first = min(group, key=lambda event: event.created_at)
    return (first.created_at, first.aggregate_kind, str(first.aggregate_id))


def _within_group_key(event: OutboxEvent) -> tuple[bool, int, datetime, str]:
    return (
        event.aggregate_version is None,
        event.aggregate_version or 0,
        event.created_at,
        str(event.event_id),
    )


def order_for_publication(events: Sequence[OutboxEvent]) -> list[OutboxEvent]:
    groups: dict[tuple[str, str], list[OutboxEvent]] = {}
    for event in events:
        groups.setdefault(_group_key(event), []).append(event)
    ordered: list[OutboxEvent] = []
    for group in sorted(groups.values(), key=_group_order):
        ordered.extend(sorted(group, key=_within_group_key))
    return ordered
