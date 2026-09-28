"""Feed cursors and resync decisions (R20).

A feed cursor binds the scope's feed generation and the last consumed
sequence. Evaluation is pure: a pruned history, a foreign generation or an
impossible sequence all yield ``resync_required`` (the caller must then serve
a snapshot), never silent loss.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass

from ..store import ApiProblem


@dataclass(frozen=True, slots=True)
class FeedCursor:
    generation: int
    sequence: int

    def __post_init__(self) -> None:
        if self.generation < 1:
            raise ValueError("feed generation starts at 1")
        if self.sequence < 0:
            raise ValueError("feed sequence cannot be negative")


@dataclass(frozen=True, slots=True)
class FeedCheckpoint:
    feed_generation: int
    last_published_seq: int
    oldest_available_seq: int


FRESH = "fresh"
OK = "ok"
RESYNC_REQUIRED = "resync_required"


def encode_feed_cursor(cursor: FeedCursor) -> str:
    raw = json.dumps(
        {"g": cursor.generation, "seq": cursor.sequence},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_feed_cursor(raw: str) -> FeedCursor:
    try:
        padded = raw + "=" * (-len(raw) % 4)
        parsed = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        return FeedCursor(
            generation=int(parsed["g"]), sequence=int(parsed["seq"])
        )
    except Exception as exc:
        raise ApiProblem(
            422, "invalid_cursor", "The feed cursor is malformed."
        ) from exc


def evaluate_cursor(
    cursor: FeedCursor | None, checkpoint: FeedCheckpoint
) -> str:
    if cursor is None:
        return FRESH
    if cursor.generation != checkpoint.feed_generation:
        return RESYNC_REQUIRED
    if cursor.sequence < checkpoint.oldest_available_seq:
        return RESYNC_REQUIRED
    if cursor.sequence > checkpoint.last_published_seq:
        return RESYNC_REQUIRED
    return OK
