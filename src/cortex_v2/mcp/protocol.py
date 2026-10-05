"""MCP JSON-RPC protocol handling built directly on the operation registry.

One registry → one tool list. Tool names are the operation ids with dots
mapped to underscores (MCP tool-name charset), and the operation id stays
authoritative in every result. Local stdio MCP may inherit one
installation-bound identity; shared streamable HTTP authenticates each
caller per request (handled in ``server.py``) and never forwards callers
through one administrator credential.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

from ..clients.client import CortexClient
from ..clients.errors import ClientError, CortexApiError
from ..clients.member_reader import MemberKeyReader
from ..interface.registry import NormalizedOperation, OperationRegistry, build_registry

MCP_PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOL_VERSIONS = (
    "2025-06-18",
    "2025-03-26",
    "2024-11-05",
)
SERVER_NAME = "cortex-v2"

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602


@dataclass(frozen=True, slots=True)
class McpCredentials:
    token: str | None = field(repr=False)
    scope: str | None
    read_scopes: tuple[str, ...] = ()
    # Trusted local stdio context only; never parsed from JSON or HTTP headers.
    member_reader: MemberKeyReader | None = field(default=None, repr=False)


def tool_name_for(operation_id: str) -> str:
    return operation_id.replace(".", "_").replace(":", "_").replace("-", "_")


def _result(message_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": message_id, "result": result}


def _error(
    message_id: Any, code: int, message: str, data: Any = None
) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": message_id, "error": error}


def _tool_error(message_id: Any, error: dict[str, Any]) -> dict[str, Any]:
    return _result(
        message_id,
        {
            "content": [
                {"type": "text", "text": json.dumps({"error": error},
                                                    sort_keys=True)}
            ],
            "isError": True,
        },
    )


def _input_schema(operation: NormalizedOperation) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    required: list[str] = []
    definitions: dict[str, Any] = {}
    if operation.request_model is not None:
        schema = operation.request_model.model_json_schema()
        definitions = schema.pop("$defs", {}) or {}
        properties.update(schema.get("properties", {}))
        required.extend(schema.get("required", []))
    if operation.requires_scope:
        properties["scope"] = {
            "type": "string",
            "description": "X-Cortex-Scope alias (defaults to the caller's "
            "bound scope).",
        }
    if operation.kind == "scoped_read":
        properties["read_scopes"] = {
            "type": "array",
            "items": {"type": "string"},
            "description": "Optional X-Cortex-Read-Scopes aliases.",
        }
    if operation.path_params:
        properties["path_params"] = {
            "type": "object",
            "properties": {
                name: {"type": "string"} for name in operation.path_params
            },
            "required": list(operation.path_params),
            "description": "URL path parameters.",
        }
        required.append("path_params")
    if operation.requires_idempotency_key:
        properties["idempotency_key"] = {
            "type": "string",
            "maxLength": 128,
            "description": "Mandatory for writes, identical semantics to the "
            "Idempotency-Key header. MCP never generates one for you and "
            "adds no extra write semantics.",
        }
        required.append("idempotency_key")
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }
    if definitions:
        schema["$defs"] = definitions
    return schema


def _description(operation: NormalizedOperation, state: str,
                 reason: str | None) -> str:
    usage = operation.usage
    parts = [
        f"{operation.summary}",
        f"Operation id: {operation.operation_id} ({operation.method} "
        f"{operation.path}).",
        f"Use when: {usage['use_when']}",
        f"Avoid when: {usage['avoid_when']}",
        f"Cost class: {usage['cost_class']}.",
    ]
    if state != "ready":
        parts.append(f"Availability: {state} ({reason}).")
    return " ".join(parts)[:2048]


def build_tools(registry: OperationRegistry) -> list[dict[str, Any]]:
    tools = []
    for operation in registry.operations_sorted():
        module = registry.module_state(operation.module)
        tools.append(
            {
                "name": tool_name_for(operation.operation_id),
                "title": operation.operation_id,
                "description": _description(
                    operation, module.state, module.reason
                ),
                "inputSchema": _input_schema(operation),
                "annotations": {
                    "title": operation.operation_id,
                    "readOnlyHint": operation.kind == "scoped_read",
                    "destructiveHint": False,
                    "idempotentHint": operation.kind == "scoped_write",
                    "openWorldHint": True,
                },
            }
        )
    return tools


class McpServer:
    """Transports call ``handle_message``; clients are created per
    credential set by the injected factory."""

    def __init__(
        self,
        *,
        client_factory: Callable[[McpCredentials], CortexClient],
        registry: OperationRegistry | None = None,
        server_version: str | None = None,
    ) -> None:
        self._client_factory = client_factory
        self._registry = registry or build_registry()
        if server_version is None:
            from .. import __version__ as server_version
        self._server_version = server_version
        self._tools = build_tools(self._registry)
        self._tools_by_name = {tool["name"]: tool for tool in self._tools}
        self._operation_by_tool: dict[str, str] = {}
        for operation in self._registry.operations_sorted():
            name = tool_name_for(operation.operation_id)
            if name in self._operation_by_tool:
                raise ValueError(
                    f"tool name collision: {name!r} maps to both "
                    f"{self._operation_by_tool[name]!r} and "
                    f"{operation.operation_id!r}"
                )
            self._operation_by_tool[name] = operation.operation_id

    @property
    def registry(self) -> OperationRegistry:
        return self._registry

    async def handle_message(
        self, message: Any, *, credentials: McpCredentials | None = None
    ) -> dict[str, Any] | None:
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return _error(None, INVALID_REQUEST, "Not a JSON-RPC 2.0 message")
        message_id = message.get("id")
        method = message.get("method")
        if not isinstance(method, str):
            return _error(message_id, INVALID_REQUEST, "Missing method")
        if method == "initialize":
            return _result(message_id, self._initialize(message.get("params")))
        if method.startswith("notifications/"):
            return None
        if method == "ping":
            return _result(message_id, {})
        if method == "tools/list":
            return _result(message_id, {"tools": self._tools})
        if method == "tools/call":
            return await self._tools_call(message_id, message.get("params"),
                                          credentials)
        return _error(message_id, METHOD_NOT_FOUND, f"Unknown method {method}")

    def _initialize(self, params: Any) -> dict[str, Any]:
        requested = (params or {}).get("protocolVersion")
        negotiated = (
            requested
            if requested in SUPPORTED_PROTOCOL_VERSIONS
            else MCP_PROTOCOL_VERSION
        )
        return {
            "protocolVersion": negotiated,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {
                "name": SERVER_NAME,
                "version": self._server_version,
            },
            "instructions": (
                "Tools are generated from the one versioned Cortex v2 "
                "operation registry. Use capability_discover for live "
                "availability and capability_card for a full usage card "
                "before first use of a specialized operation. Writes require "
                "an idempotency_key argument with exactly the HTTP "
                "Idempotency-Key semantics; this server adds no heartbeat, "
                "direct-complete or secondary write behavior."
            ),
        }

    async def _tools_call(
        self, message_id: Any, params: Any,
        credentials: McpCredentials | None,
    ) -> dict[str, Any]:
        params = params or {}
        name = params.get("name")
        if not isinstance(name, str) or name not in self._tools_by_name:
            return _error(message_id, INVALID_PARAMS, f"Unknown tool {name!r}")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            return _error(message_id, INVALID_PARAMS,
                          "arguments must be an object")
        operation_id = self._operation_by_tool.get(name)
        if operation_id is None:
            return _error(message_id, INVALID_PARAMS, f"Unknown tool {name!r}")
        operation = self._registry.get(operation_id)
        if credentials is None or (not credentials.token and credentials.member_reader is None):
            return _tool_error(
                message_id,
                {
                    "code": "credential_required",
                    "message": (
                        "this MCP transport authenticates every caller; "
                        "supply the installation-bound v2 bearer credential"
                    ),
                    "retryable": False,
                },
            )
        if operation.requires_idempotency_key and not arguments.get(
            "idempotency_key"
        ):
            return _tool_error(
                message_id,
                {
                    "code": "idempotency_key_required",
                    "message": (
                        f"operation {operation.operation_id} is a write and "
                        "requires the idempotency_key argument; MCP adds no "
                        "key generation or extra write semantics"
                    ),
                    "retryable": False,
                },
            )
        payload = {
            key: value
            for key, value in arguments.items()
            if key
            not in ("scope", "read_scopes", "idempotency_key", "path_params")
        }
        scope = arguments.get("scope") or credentials.scope
        read_scopes = arguments.get("read_scopes") or list(
            credentials.read_scopes
        )
        try:
            client = self._client_factory(credentials)
            result = client.call(
                operation.operation_id,
                payload=payload or None,
                path_params=arguments.get("path_params") or {},
                scope=scope,
                read_scopes=tuple(read_scopes) or None,
                idempotency_key=arguments.get("idempotency_key"),
            )
        except CortexApiError as exc:
            return _tool_error(
                message_id,
                {
                    "code": exc.code,
                    "message": exc.message,
                    "retryable": exc.retryable,
                    "status": exc.status,
                    "request_id": exc.request_id,
                    "key_expires_at": exc.key_expires_at,
                },
            )
        except ClientError as exc:
            return _tool_error(
                message_id,
                {
                    "code": type(exc).__name__,
                    "message": str(exc),
                    "retryable": False,
                },
            )
        return _result(
            message_id,
            {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "operation_id": result.operation_id,
                                "status": result.status,
                                "replayed": result.replayed,
                                "request_id": result.request_id,
                                "data": result.data,
                                "key_expires_at": result.key_expires_at,
                            },
                            sort_keys=True,
                        ),
                    }
                ],
                "isError": False,
            },
        )
