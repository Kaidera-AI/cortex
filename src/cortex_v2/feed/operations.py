"""Versioned feed operation registry (integration contract).

Same generic mounting contract as coordination: ``OPERATIONS`` entries carry
operation_id, method, path, kind, request_model, handler, summary and usage.
Handler signatures: scoped_write ``async (connection, context,
idempotency_key, payload, path_params) -> (status, data, replayed)``;
scoped_read ``async (connection, context, payload, path_params) -> dict``.

The SSE surface is built by the integrator on :func:`poll_feed` plus
:func:`cortex_v2.feed.stream.stream_feed_entries`; this module imports no
transport framework.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import asyncpg

from ..store import ScopeContext
from . import dispatcher, reads
from .models import (
    AdvanceFeedGenerationRequest,
    DispatchFeedRequest,
    PruneFeedRequest,
)
from .repository import problem


def _read_filters(payload: Any) -> dict[str, Any]:
    if payload is None:
        return {}
    if isinstance(payload, Mapping):
        return dict(payload)
    raise problem("invalid_filter")


async def _feed_poll(
    connection: asyncpg.Connection,
    context: ScopeContext,
    payload: Any,
    path_params: dict[str, Any],
) -> dict[str, Any]:
    return await reads.poll_feed(connection, context, _read_filters(payload))


async def _feed_dashboard(
    connection: asyncpg.Connection,
    context: ScopeContext,
    payload: Any,
    path_params: dict[str, Any],
) -> dict[str, Any]:
    return await reads.feed_dashboard(connection, context)


async def _feed_dispatch(
    connection: asyncpg.Connection,
    context: ScopeContext,
    idempotency_key: str,
    payload: DispatchFeedRequest,
    path_params: dict[str, Any],
) -> tuple[int, dict[str, Any], bool]:
    return await dispatcher.dispatch_scope(
        connection, context, payload, idempotency_key
    )


async def _feed_prune(
    connection: asyncpg.Connection,
    context: ScopeContext,
    idempotency_key: str,
    payload: PruneFeedRequest,
    path_params: dict[str, Any],
) -> tuple[int, dict[str, Any], bool]:
    return await dispatcher.prune_feed(
        connection, context, payload, idempotency_key
    )


async def _feed_generation_advance(
    connection: asyncpg.Connection,
    context: ScopeContext,
    idempotency_key: str,
    payload: AdvanceFeedGenerationRequest,
    path_params: dict[str, Any],
) -> tuple[int, dict[str, Any], bool]:
    return await dispatcher.advance_generation(
        connection, context, payload, idempotency_key
    )


OPERATIONS: list[dict[str, Any]] = [
    {
        "operation_id": "feed.poll",
        "method": "GET",
        "path": "/v1/feed/entries",
        "kind": "scoped_read",
        "request_model": None,
        "handler": _feed_poll,
        "summary": "Poll the scoped durable feed from a cursor, with resync.",
        "usage": "Use when consuming committed coordination/memory changes "
        "incrementally; do not treat an empty page as system health - check "
        "the dashboard, and a resync_required response must be handled with "
        "its snapshot, not ignored.",
    },
    {
        "operation_id": "feed.dashboard",
        "method": "GET",
        "path": "/v1/feed/dashboard",
        "kind": "scoped_read",
        "request_model": None,
        "handler": _feed_dashboard,
        "summary": "Scoped queue/feed projections with explicit degradation.",
        "usage": "Use when presenting work state or feed lag for one scope; "
        "do not render degraded[] projections as zero or green - failed "
        "projections mean unknown, never empty.",
    },
    {
        "operation_id": "feed.dispatch",
        "method": "POST",
        "path": "/v1/feed:dispatch",
        "kind": "scoped_write",
        "request_model": DispatchFeedRequest,
        "handler": _feed_dispatch,
        "summary": "Publish committed outbox rows into the scoped feed.",
        "usage": "Use when a worker/ops role must move one scope's durable "
        "outbox into its feed in one serialized transaction; do not call it "
        "to fabricate events - it only publishes committed outbox rows and "
        "deduplicates re-delivery.",
    },
    {
        "operation_id": "feed.prune",
        "method": "POST",
        "path": "/v1/feed:prune",
        "kind": "scoped_write",
        "request_model": PruneFeedRequest,
        "handler": _feed_prune,
        "summary": "Apply feed retention; pruned cursors must resync.",
        "usage": "Use when bounding feed storage for a scope under its "
        "retention policy; do not use to hide events - outbox delivery state "
        "and audit history stay, and pruned cursors get resync_required.",
    },
    {
        "operation_id": "feed.generation.advance",
        "method": "POST",
        "path": "/v1/feed:generation-advance",
        "kind": "scoped_write",
        "request_model": AdvanceFeedGenerationRequest,
        "handler": _feed_generation_advance,
        "summary": "Start a new feed generation; old cursors must resync.",
        "usage": "Use when a rebuild/restore made prior cursors untrustworthy "
        "and the feed must start a new generation; do not use casually - "
        "every existing client cursor becomes resync_required against the "
        "new generation.",
    },
]
