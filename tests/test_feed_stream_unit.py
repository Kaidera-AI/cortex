"""Pure unit tests for the SSE stream helper (R20).

The generator is transport-agnostic: the integrator supplies fetch_batch on
top of poll_feed. Resync always comes first with its snapshot, entries are
delivered with the advancing cursor, heartbeats pace idle polls, idle
polling is bounded, and revocation (should_continue) ends the stream.
"""

from __future__ import annotations

import asyncio
from typing import Any

from cortex_v2.feed.stream import stream_feed_entries


def collect(fetch_batch, cursor=None, **kwargs) -> list[dict[str, Any]]:
    async def run() -> list[dict[str, Any]]:
        return [
            event
            async for event in stream_feed_entries(
                fetch_batch, cursor, **kwargs
            )
        ]

    return asyncio.run(run())


def test_entries_are_streamed_with_advancing_cursor() -> None:
    async def fetch(cursor: str | None) -> dict[str, Any]:
        if cursor is None:
            return {
                "entries": [{"feed_seq": 1}, {"feed_seq": 2}],
                "cursor": "c2",
                "resync_required": False,
                "snapshot": None,
            }
        return {
            "entries": [],
            "cursor": cursor,
            "resync_required": False,
            "snapshot": None,
        }

    calls = {"n": 0}

    def stop_after_two() -> bool:
        calls["n"] += 1
        return calls["n"] <= 2

    events = collect(fetch, should_continue=stop_after_two, poll_interval=0)
    # Two loop iterations: the first delivers both entries, the second finds
    # an empty batch and heartbeats before should_continue ends the stream.
    assert [event["event"] for event in events] == [
        "entry", "entry", "heartbeat",
    ]
    assert events[1]["cursor"] == "c2"


def test_resync_event_comes_first_with_snapshot() -> None:
    state = {"phase": "resync"}

    async def fetch(cursor: str | None) -> dict[str, Any]:
        if state["phase"] == "resync":
            state["phase"] = "live"
            return {
                "entries": [],
                "cursor": "snapshot-cursor",
                "resync_required": True,
                "snapshot": {"coverage": "complete"},
            }
        return {
            "entries": [{"feed_seq": 9}],
            "cursor": "c9",
            "resync_required": False,
            "snapshot": None,
        }

    calls = {"n": 0}

    def stop() -> bool:
        calls["n"] += 1
        return calls["n"] <= 2

    events = collect(fetch, cursor="old", should_continue=stop, poll_interval=0)
    assert events[0]["event"] == "resync_required"
    assert events[0]["snapshot"] == {"coverage": "complete"}
    assert events[1]["event"] == "entry"
    assert events[1]["cursor"] == "c9"


def test_idle_polls_heartbeat_and_are_bounded() -> None:
    async def fetch(cursor: str | None) -> dict[str, Any]:
        return {
            "entries": [],
            "cursor": "c0",
            "resync_required": False,
            "snapshot": None,
        }

    events = collect(fetch, max_idle_polls=2, poll_interval=0)
    # The first idle poll heartbeats; the second reaches the bound and ends
    # the stream with an explicit idle_timeout instead of looping forever.
    assert [event["event"] for event in events] == [
        "heartbeat",
        "idle_timeout",
    ]


def test_revocation_ends_the_stream() -> None:
    async def fetch(cursor: str | None) -> dict[str, Any]:
        return {
            "entries": [{"feed_seq": 1}],
            "cursor": "c1",
            "resync_required": False,
            "snapshot": None,
        }

    events = collect(fetch, should_continue=lambda: False, poll_interval=0)
    assert events == []
