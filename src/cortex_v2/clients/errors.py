"""Typed client errors. Every failure is explicit; nothing falls back."""

from __future__ import annotations

from typing import Any


class ClientError(Exception):
    """Base class for typed v2 client failures."""


class ClientConfigError(ClientError):
    """The per-installation client configuration is missing or invalid."""


class CortexApiError(ClientError):
    """The server returned a typed problem (or an unparsable error body)."""

    def __init__(
        self,
        *,
        status: int,
        code: str,
        message: str,
        retryable: bool,
        request_id: str | None,
        operation_id: str | None = None,
        fields: list[dict[str, Any]] | None = None,
        key_expires_at: str | None = None,
    ) -> None:
        self.status = status
        self.code = code
        self.message = message
        self.retryable = retryable
        self.request_id = request_id
        self.operation_id = operation_id
        self.fields = fields or []
        self.key_expires_at = key_expires_at
        super().__init__(f"{code} (HTTP {status}): {message}")

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "request_id": self.request_id,
            "operation_id": self.operation_id,
            "fields": self.fields,
            "key_expires_at": self.key_expires_at,
        }


class CortexTransportError(ClientError):
    """The request never reached the API or the response never returned."""


class IncompatibleServerError(ClientError):
    """The server does not speak the v2 API contract; upgrade explicitly."""


class IdempotencyKeyRequired(ClientError):
    def __init__(self, operation_id: str) -> None:
        self.operation_id = operation_id
        super().__init__(
            f"operation {operation_id} is a write and requires an "
            "Idempotency-Key; the client refuses to send it without one"
        )


class ScopeRequired(ClientError):
    def __init__(self, operation_id: str) -> None:
        self.operation_id = operation_id
        super().__init__(
            f"operation {operation_id} requires a scope selection "
            "(X-Cortex-Scope); none was provided"
        )


class OperationUnknownToClient(ClientError):
    def __init__(self, operation_id: str) -> None:
        self.operation_id = operation_id
        super().__init__(
            f"operation {operation_id} is unknown to this client's registry; "
            "upgrade the client or check capability.discover"
        )
