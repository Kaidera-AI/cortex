"""Transport-agnostic SSE streaming helper (R20).

The integrator owns the HTTP/SSE surface and supplies ``fetch_batch`` on top
of :func:`cortex_v2.feed.reads.poll_feed` with its own connection lifecycle.
This generator adds pacing, heartbeats, bounded idle behavior and the
resync-first contract: a ``resync_required`` batch always yields the
snapshot event before entry delivery continues from the new cursor.
Revocation ends the stream through ``should_continue``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

FetchBatch = Callable[[str | None], Awaitable[dict[str, Any]]]


async def stream_feed_entries(
    fetch_batch: FetchBatch,
    cursor: str | None = None,
    *,
    poll_interval: float = 1.0,
    should_continue: Callable[[], bool] | None = None,
    max_idle_polls: int | None = None,
) -> AsyncIterator[dict[str, Any]]:
    current = cursor
    idle_polls = 0
    while should_continue is None or should_continue():
        batch = await fetch_batch(current)
        if batch.get("resync_required"):
            yield {
                "event": "resync_required",
                "snapshot": batch.get("snapshot"),
                "cursor": batch.get("cursor"),
            }
            current = batch.get("cursor")
            idle_polls = 0
            continue
        entries = batch.get("entries") or []
        if entries:
            for entry in entries:
                yield {
                    "event": "entry",
                    "data": entry,
                    "cursor": batch.get("cursor"),
                }
            current = batch.get("cursor")
            idle_polls = 0
            continue
        idle_polls += 1
        if max_idle_polls is not None and idle_polls >= max_idle_polls:
            yield {"event": "idle_timeout", "cursor": current}
            return
        yield {"event": "heartbeat", "cursor": current}
        if poll_interval > 0:
            await asyncio.sleep(poll_interval)
