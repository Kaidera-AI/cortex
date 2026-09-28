"""Pure unit tests for feed cursors and resync decisions (R20).

Cursors bind scope-feed generation and sequence. An expired retention
cursor or a foreign generation must yield resync_required plus a snapshot
route, never silent loss.
"""

from __future__ import annotations

import pytest

from cortex_v2.feed.cursors import (
    FeedCursor,
    FeedCheckpoint,
    decode_feed_cursor,
    encode_feed_cursor,
    evaluate_cursor,
)
from cortex_v2.store import ApiProblem


def test_cursor_codec_round_trips() -> None:
    cursor = encode_feed_cursor(FeedCursor(generation=3, sequence=1042))
    assert decode_feed_cursor(cursor) == FeedCursor(generation=3, sequence=1042)


@pytest.mark.parametrize("cursor", ["", "!!!", "e30=", "aGk=", "eyJnIjogMX0"])
def test_corrupt_cursors_are_typed_problems(cursor: str) -> None:
    with pytest.raises(ApiProblem) as failure:
        decode_feed_cursor(cursor)
    assert failure.value.status == 422
    assert failure.value.code == "invalid_cursor"


def test_negative_sequence_is_rejected() -> None:
    with pytest.raises(ApiProblem):
        decode_feed_cursor(encode_feed_cursor(FeedCursor(1, 5)) + "x")
    with pytest.raises(ValueError):
        FeedCursor(generation=0, sequence=1)
    with pytest.raises(ValueError):
        FeedCursor(generation=1, sequence=-1)


def checkpoint(last_seq: int, oldest: int, generation: int = 1) -> FeedCheckpoint:
    return FeedCheckpoint(
        feed_generation=generation,
        last_published_seq=last_seq,
        oldest_available_seq=oldest,
    )


def test_no_cursor_starts_fresh_with_snapshot() -> None:
    decision = evaluate_cursor(None, checkpoint(10, 1))
    assert decision == "fresh"


def test_matching_cursor_in_range_is_ok() -> None:
    decision = evaluate_cursor(FeedCursor(1, 5), checkpoint(10, 1))
    assert decision == "ok"


def test_cursor_at_last_seq_is_ok_with_no_new_entries() -> None:
    assert evaluate_cursor(FeedCursor(1, 10), checkpoint(10, 1)) == "ok"


def test_pruned_history_requires_resync() -> None:
    decision = evaluate_cursor(FeedCursor(1, 3), checkpoint(100, 50))
    assert decision == "resync_required"


def test_foreign_generation_requires_resync() -> None:
    decision = evaluate_cursor(FeedCursor(2, 5), checkpoint(10, 1, generation=3))
    assert decision == "resync_required"


def test_cursor_ahead_of_checkpoint_requires_resync() -> None:
    decision = evaluate_cursor(FeedCursor(1, 999), checkpoint(10, 1))
    assert decision == "resync_required"


def test_cursor_at_oldest_boundary_is_ok() -> None:
    assert evaluate_cursor(FeedCursor(1, 50), checkpoint(100, 50)) == "ok"
