"""Cortex v2 Feed module (R20): transactional outbox dispatcher, scoped
durable feed entries, cursor generations, polling/SSE resync, queue
projections and honest dashboard degradation.

Depends only on migrations 0001-0003 plus its own 0007: it consumes the
outbox projections those migrations define and never assumes anything about
0004-0006.
"""

from __future__ import annotations

from .dispatcher import advance_generation, dispatch_scope, prune_feed
from .operations import OPERATIONS
from .reads import build_snapshot, feed_dashboard, poll_feed
from .stream import stream_feed_entries

__all__ = [
    "OPERATIONS",
    "advance_generation",
    "build_snapshot",
    "dispatch_scope",
    "feed_dashboard",
    "poll_feed",
    "prune_feed",
    "stream_feed_entries",
]
