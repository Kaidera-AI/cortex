"""MCP transports: streamable HTTP (per-caller auth) and stdio
(installation-bound identity).

Streamable HTTP authenticates every request from its own headers — it never
forwards all users through one administrator credential and never reads an
ambient token. Stdio inherits exactly one installation-bound identity from
the explicit client profile, which is the documented local-MCP model.
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from typing import Any, Callable, TextIO

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ..clients.client import CortexClient
from ..clients.config import (
    ClientProfile,
    load_client_profile,
    load_connection_url,
    profile_from_credentials,
)
from .protocol import (
    PARSE_ERROR,
    McpCredentials,
    McpServer,
    _error,
)

# The API validates credential shape/strength; this transport only
# requires that each caller presents its own bearer token.
BEARER_PATTERN = re.compile(r"^Bearer ([^\s]+)$")

AUTH_REQUIRED_METHODS = frozenset({"tools/call"})


def credentials_from_headers(request: Request) -> McpCredentials | None:
    match = BEARER_PATTERN.fullmatch(request.headers.get("authorization", ""))
    if not match:
        return None
    read_scopes = tuple(
        part.strip()
        for part in request.headers.get("x-cortex-read-scopes", "").split(",")
        if part.strip()
    )
    return McpCredentials(
        token=match.group(1),
        scope=request.headers.get("x-cortex-scope") or None,
        read_scopes=read_scopes,
    )


def create_mcp_app(
    *,
    client_factory: Callable[[McpCredentials], CortexClient] | None = None,
    server: McpServer | None = None,
) -> FastAPI:
    if server is None:
        factory = client_factory or _rejecting_factory
        server = McpServer(client_factory=factory)

    async def dispatch(
        message: Any, credentials: McpCredentials | None
    ) -> Any:
        if isinstance(message, list):
            responses = []
            for item in message:
                response = await server.handle_message(
                    item, credentials=credentials
                )
                if response is not None:
                    responses.append(response)
            return responses or None
        return await server.handle_message(message, credentials=credentials)

    application = FastAPI(
        title="Cortex v2 MCP (streamable HTTP)",
        version=server._server_version,
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
    )

    @application.post("/mcp")
    async def mcp_post(request: Request) -> Any:
        try:
            message = await request.json()
        except json.JSONDecodeError:
            return JSONResponse(
                status_code=400,
                content=_error(None, PARSE_ERROR, "Parse error"),
            )
        method = message.get("method") if isinstance(message, dict) else None
        if isinstance(message, list):
            methods = {
                item.get("method") for item in message if isinstance(item, dict)
            }
            needs_auth = bool(methods & AUTH_REQUIRED_METHODS)
        else:
            needs_auth = method in AUTH_REQUIRED_METHODS
        credentials = credentials_from_headers(request)
        if needs_auth and (credentials is None or not credentials.token):
            return JSONResponse(
                status_code=401,
                content={
                    "error": {
                        "code": "invalid_credential",
                        "message": (
                            "shared HTTP MCP authenticates each caller with "
                            "a v2 bearer credential; no administrator "
                            "credential is substituted"
                        ),
                        "retryable": False,
                    }
                },
            )
        response = await dispatch(message, credentials)
        if response is None:
            return JSONResponse(status_code=202, content=None)
        return JSONResponse(status_code=200, content=response)

    @application.get("/mcp")
    async def mcp_get() -> JSONResponse:
        # This server answers JSON-RPC directly and offers no SSE stream.
        return JSONResponse(
            status_code=405,
            content={
                "error": {
                    "code": "stream_not_offered",
                    "message": (
                        "this deployment answers streamable-HTTP requests "
                        "with single JSON responses; no server-initiated "
                        "stream is offered"
                    ),
                }
            },
        )

    @application.delete("/mcp")
    async def mcp_delete() -> JSONResponse:
        return JSONResponse(
            status_code=405,
            content={
                "error": {
                    "code": "no_session",
                    "message": "this server is stateless; no session exists",
                }
            },
        )

    return application


def _rejecting_factory(credentials: McpCredentials) -> CortexClient:
    raise RuntimeError(
        "no client factory configured for this MCP server; pass "
        "client_factory or a prebuilt McpServer"
    )


def stdio_client_factory(profile: ClientProfile
                         ) -> Callable[[McpCredentials], CortexClient]:
    def factory(credentials: McpCredentials) -> CortexClient:
        bound = replace(
            profile,
            default_scope=credentials.scope or profile.default_scope,
            default_read_scopes=tuple(credentials.read_scopes or profile.default_read_scopes),
        )
        return CortexClient(bound)

    return factory


def run_stdio_session(
    server: McpServer,
    inbound: TextIO,
    outbound: TextIO,
    *,
    credentials: McpCredentials | None,
) -> None:
    """Line-delimited JSON-RPC over the given streams (stdio transport)."""
    import asyncio

    for line in inbound:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            outbound.write(
                json.dumps(_error(None, PARSE_ERROR, "Parse error")) + "\n"
            )
            outbound.flush()
            continue
        response = asyncio.run(
            server.handle_message(message, credentials=credentials)
        )
        if response is not None:
            outbound.write(json.dumps(response) + "\n")
            outbound.flush()


def run_stdio(
    *, config: str | None = None, installation: str | None = None,
    project: str | None = None, name: str | None = None,
) -> None:
    import sys

    profile = load_client_profile(config, installation=installation, project=project, name=name)
    server = McpServer(client_factory=stdio_client_factory(profile))
    credentials = McpCredentials(
        token=profile.token,
        scope=profile.default_scope,
        read_scopes=profile.default_read_scopes,
    )
    run_stdio_session(server, sys.stdin, sys.stdout, credentials=credentials)


def run_http(*, host: str, port: int, config: str | None = None) -> None:
    import uvicorn

    base_url = load_connection_url(config)

    def factory(credentials: McpCredentials) -> CortexClient:
        # HTTP callers supply their own bearer; only a non-secret API URL
        # comes from this process's connection profile.
        if not credentials.token:
            raise RuntimeError("per-request credential is required")
        return CortexClient(
            profile_from_credentials(
                base_url,
                credentials.token,
                scope=credentials.scope,
                read_scopes=credentials.read_scopes,
            )
        )

    application = create_mcp_app(client_factory=factory)
    uvicorn.run(application, host=host, port=port, log_level="warning")
