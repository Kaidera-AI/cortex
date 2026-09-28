"""KOS adapter contract (R21/R28, plan W2 item 6, F12).

KOS maps its scheduler, worktree and return-outbox surfaces onto the same
versioned Cortex v2 operations. Cortex never queries or migrates the KOS
app DB: this adapter accepts an injected client or a selected private key,
never a DSN. Feed consumption is cursor-based with explicit resync;
an expired cursor raises ``FeedResyncRequired`` instead of silently losing
or replaying events. Unavailable operations raise ``OperationUnavailable``.
"""

from __future__ import annotations

from typing import Any, Mapping

from ..client import CallResult, CortexClient
from ..config import load_client_profile
from ..errors import ClientConfigError, ClientError, CortexApiError
from ..key_store import KeyStore

DEFAULT_CLAIM_OPERATION = "coordination.claim_next"
DEFAULT_RETURN_OPERATION = "coordination.return"
DEFAULT_FEED_POLL_OPERATION = "feed.poll"
DEFAULT_FEED_SNAPSHOT_OPERATION = "feed.snapshot"

FEED_RESYNC_CODES = frozenset({"cursor_expired", "resync_required"})


class KosAdapterError(ClientError):
    """Base class for KOS adapter failures."""


class OperationUnavailable(KosAdapterError):
    def __init__(self, operation_id: str, reason: str) -> None:
        self.operation_id = operation_id
        self.reason = reason
        super().__init__(
            f"operation {operation_id} is unavailable: {reason}"
        )


class FeedResyncRequired(KosAdapterError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(
            f"the feed cursor requires resync ({code}): {message}"
        )


class KosAdapter:
    """Scheduler/worktree/return-outbox mapping over a selected v2 identity."""

    def __init__(
        self,
        client: CortexClient | None = None,
        *,
        store: KeyStore | None = None,
        installation: str | None = None,
        project: str | None = None,
        name: str | None = None,
        config: str | None = None,
        registry: Any = None,
        claim_operation: str = DEFAULT_CLAIM_OPERATION,
        return_operation: str = DEFAULT_RETURN_OPERATION,
        feed_poll_operation: str = DEFAULT_FEED_POLL_OPERATION,
        feed_snapshot_operation: str = DEFAULT_FEED_SNAPSHOT_OPERATION,
    ) -> None:
        if client is None:
            if not project or not name:
                raise ClientConfigError("KOS must select a project and its own named key")
            client = CortexClient(load_client_profile(
                config, store=store, installation=installation,
                project=project, name=name,
            ))
        self._client = client
        self._registry = registry if registry is not None else client.registry
        self._operations = {
            "claim": claim_operation,
            "return": return_operation,
            "feed_poll": feed_poll_operation,
            "feed_snapshot": feed_snapshot_operation,
        }

    def _require(self, name: str) -> str:
        operation_id = self._operations[name]
        try:
            self._registry.get(operation_id)
        except KeyError:
            raise OperationUnavailable(
                operation_id,
                "no deployed module publishes this operation (the owning "
                "module is not importable yet)",
            ) from None
        return operation_id

    def publish_return(
        self,
        *,
        scope: str,
        payload: Mapping[str, Any],
        idempotency_key: str,
    ) -> CallResult:
        """Return work through the normal lifecycle.

        An uncertain completion retries *this* command with the same
        idempotency key; the server replays the stored receipt, so the
        original side effect is never executed twice.
        """
        operation_id = self._require("return")
        return self._client.call(
            operation_id,
            payload=dict(payload),
            scope=scope,
            idempotency_key=idempotency_key,
        )

    def claim_next(self, *, scope: str, payload: Mapping[str, Any] | None = None,
                   idempotency_key: str) -> CallResult:
        operation_id = self._require("claim")
        return self._client.call(
            operation_id,
            payload=dict(payload or {}),
            scope=scope,
            idempotency_key=idempotency_key,
        )

    def poll_feed(self, *, scope: str, cursor: str | None = None) -> Any:
        operation_id = self._require("feed_poll")
        payload: dict[str, Any] = {}
        if cursor is not None:
            payload["cursor"] = cursor
        try:
            return self._client.call(operation_id, payload=payload, scope=scope)
        except CortexApiError as exc:
            if exc.code in FEED_RESYNC_CODES:
                raise FeedResyncRequired(exc.code, exc.message) from exc
            raise

    def resync(self, *, scope: str) -> Any:
        """Recover from an expired cursor via the snapshot route; the
        returned snapshot carries the cursor to resume from."""
        operation_id = self._require("feed_snapshot")
        return self._client.call(operation_id, payload={}, scope=scope)
