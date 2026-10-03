"""The Python v2 client: one registry-driven call surface for CLI, MCP and
adapter code (R23).

Every call resolves method/path/semantics from the same versioned operation
registry the server publishes, sends the v2 bearer, ``X-Cortex-Scope``,
optional ``X-Cortex-Read-Scopes`` and the mandatory ``Idempotency-Key`` on
writes. Errors are typed; there is no legacy fallback of any kind.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from ..interface.registry import NormalizedOperation, OperationRegistry, build_registry
from .config import ClientProfile
from .errors import (
    CortexApiError,
    IdempotencyKeyRequired,
    IncompatibleServerError,
    OperationUnknownToClient,
    ScopeRequired,
)
from .transport import HttpResponse, http_request

EXPECTED_API_VERSION = "v1"


@dataclass(frozen=True, slots=True)
class CallResult:
    operation_id: str
    status: int
    data: Any
    replayed: bool
    request_id: str | None
    contract_version: str | None = None
    key_expires_at: str | None = None


def _encode_query_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


class CortexClient:
    def __init__(
        self,
        profile: ClientProfile,
        *,
        registry: OperationRegistry | None = None,
        transport: Callable[..., HttpResponse] = http_request,
        timeout: float = 30.0,
    ) -> None:
        self.profile = profile
        self._registry = registry
        self._transport = transport
        self._timeout = timeout

    @property
    def registry(self) -> OperationRegistry:
        if self._registry is None:
            self._registry = build_registry()
        return self._registry

    def operation(self, operation_id: str) -> NormalizedOperation:
        try:
            return self.registry.get(operation_id)
        except KeyError:
            raise OperationUnknownToClient(operation_id) from None

    def _resolve_scope(
        self, operation: NormalizedOperation, scope: str | None
    ) -> str | None:
        resolved = scope or self.profile.default_scope
        if operation.requires_scope and not resolved:
            raise ScopeRequired(operation.operation_id)
        return resolved

    def _headers(
        self,
        operation: NormalizedOperation,
        scope: str | None,
        read_scopes: Sequence[str] | None,
        idempotency_key: str | None,
    ) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self.profile.token}"}
        if scope:
            headers["X-Cortex-Scope"] = scope
        if read_scopes:
            headers["X-Cortex-Read-Scopes"] = ",".join(read_scopes)
        elif operation.kind == "scoped_read" and (
            self.profile.default_read_scopes
        ):
            headers["X-Cortex-Read-Scopes"] = ",".join(
                self.profile.default_read_scopes
            )
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        return headers

    def _url(
        self, operation: NormalizedOperation, path_params: Mapping[str, Any]
    ) -> str:
        path = operation.path
        for name in operation.path_params:
            if name not in path_params:
                raise OperationUnknownToClient(
                    f"{operation.operation_id} (missing path parameter "
                    f"{name!r})"
                )
            path = path.replace("{" + name + "}", str(path_params[name]))
        return f"{self.profile.base_url}{path}"

    def call(
        self,
        operation_id: str,
        *,
        payload: Mapping[str, Any] | None = None,
        path_params: Mapping[str, Any] | None = None,
        scope: str | None = None,
        read_scopes: Sequence[str] | None = None,
        idempotency_key: str | None = None,
        query: Mapping[str, Any] | None = None,
    ) -> CallResult:
        operation = self.operation(operation_id)
        if operation.requires_idempotency_key and not idempotency_key:
            raise IdempotencyKeyRequired(operation_id)
        resolved_scope = self._resolve_scope(operation, scope)
        url = self._url(operation, path_params or {})
        headers = self._headers(
            operation, resolved_scope, read_scopes, idempotency_key
        )
        json_body: Any | None = None
        effective_query: dict[str, str] = dict(query or {})
        if operation.method == "POST":
            json_body = dict(payload or {})
        elif payload:
            for name, value in payload.items():
                if value is None:
                    continue
                effective_query.setdefault(name, _encode_query_value(value))
        response = self._transport(
            operation.method,
            url,
            headers=headers,
            json_body=json_body,
            query=effective_query or None,
            timeout=self._timeout,
        )
        return self._result(operation_id, response)

    def _result(self, operation_id: str, response: HttpResponse) -> CallResult:
        key_expires_at = next(
            (value for name, value in response.headers.items()
             if name.lower() == "cortex-key-expires"),
            None,
        )
        request_id = response.headers.get("x-request-id")
        replayed = (
            response.headers.get("idempotent-replay", "").lower() == "true"
        )
        if 200 <= response.status < 300:
            try:
                envelope = json.loads(response.body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                envelope = {}
            return CallResult(
                operation_id=operation_id,
                status=response.status,
                data=envelope.get("data"),
                replayed=replayed,
                request_id=envelope.get("request_id", request_id),
                contract_version=envelope.get("contract_version"),
                key_expires_at=key_expires_at,
            )
        try:
            problem = json.loads(response.body.decode("utf-8"))
            error = problem.get("error", {})
            code = error.get("code")
            if not isinstance(code, str):
                raise ValueError("problem body without an error code")
            raise CortexApiError(
                status=response.status,
                code=code,
                message=error.get("message", ""),
                retryable=bool(error.get("retryable", False)),
                request_id=problem.get("request_id", request_id),
                operation_id=operation_id,
                fields=error.get("fields"),
                key_expires_at=key_expires_at,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError,
                AttributeError):
            raise CortexApiError(
                status=response.status,
                code="unparsable_error_body",
                message=(
                    "the server returned a non-v2 error body; this client "
                    "does not fall back to any legacy interpretation"
                ),
                retryable=False,
                request_id=request_id,
                operation_id=operation_id,
                key_expires_at=key_expires_at,
            ) from None

    def protocol(self) -> dict[str, Any]:
        return self.call("protocol.descriptor").data

    def verify_server(self) -> dict[str, Any]:
        info = self.protocol() or {}
        api_version = info.get("api_version")
        if api_version != EXPECTED_API_VERSION:
            raise IncompatibleServerError(
                f"server api_version {api_version!r} is incompatible with "
                f"this client (expected {EXPECTED_API_VERSION!r}); upgrade "
                "the client or server explicitly — no automatic self-update "
                "or legacy fallback is performed"
            )
        return info

    def discover(self, *, scope: str | None = None) -> CallResult:
        return self.call("capability.discover", scope=scope)

    def card(self, operation_id: str, *, scope: str | None = None) -> CallResult:
        return self.call(
            "capability.card",
            path_params={"operation_id": operation_id},
            scope=scope,
        )
