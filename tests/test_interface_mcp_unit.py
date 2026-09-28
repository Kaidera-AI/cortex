"""Unit tests for the MCP server (R23/F05): tools come from the same registry,
per-caller authentication on streamable HTTP, and no extra write/heartbeat/
direct-complete semantics."""

from __future__ import annotations

import asyncio
import io
import json

from cortex_v2.clients.client import CallResult
from cortex_v2.mcp.protocol import (
    MCP_PROTOCOL_VERSION,
    McpCredentials,
    McpServer,
    tool_name_for,
)


class StubClient:
    def __init__(self, credentials=None, error=None):
        self.calls = []
        self.credentials = credentials
        self.error = error

    def call(self, operation_id, **kwargs):
        self.calls.append((operation_id, kwargs))
        if self.error is not None:
            raise self.error
        return CallResult(
            operation_id=operation_id,
            status=200,
            data={"ok": True, "operation": operation_id},
            replayed=False,
            request_id="req-mcp",
        )


def make_server(**kwargs):
    clients = []

    def factory(credentials):
        client = StubClient(credentials=credentials)
        clients.append(client)
        return client

    server = McpServer(client_factory=factory, **kwargs)
    return server, clients


def handle(server, message, credentials=None):
    return asyncio.run(
        server.handle_message(message, credentials=credentials)
    )


CREDS = McpCredentials(token="tok", scope="proj-a", read_scopes=())


def test_tool_name_mapping_is_reversible():
    assert tool_name_for("context.prepare") == "context_prepare"
    assert tool_name_for("memory.search.lexical") == "memory_search_lexical"


def test_initialize_handshake():
    server, _ = make_server()
    response = handle(
        server,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "0"},
            },
        },
    )
    result = response["result"]
    assert result["protocolVersion"] == MCP_PROTOCOL_VERSION
    assert result["serverInfo"]["name"] == "cortex-v2"
    assert "tools" in result["capabilities"]


def test_initialize_negotiates_known_older_version():
    server, _ = make_server()
    response = handle(
        server,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {}},
        },
    )
    assert response["result"]["protocolVersion"] == "2024-11-05"


def test_initialized_notification_has_no_response():
    server, _ = make_server()
    assert handle(server, {"jsonrpc": "2.0", "method": "notifications/initialized"}) \
        is None


def test_tools_list_comes_from_registry():
    server, _ = make_server()
    response = handle(server, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    tools = {tool["name"]: tool for tool in response["result"]["tools"]}
    assert "context_prepare" in tools
    assert "memory_record" in tools
    assert "capability_discover" in tools
    tool = tools["context_prepare"]
    assert tool["inputSchema"]["type"] == "object"
    assert "intent" in tool["inputSchema"]["properties"]
    assert tool["description"]
    # Read-only annotations follow the registry kind, nothing else.
    read_tool = tools["content_inspect"]
    assert read_tool["annotations"]["readOnlyHint"] is True
    write_tool = tools["memory_record"]
    assert write_tool["annotations"]["readOnlyHint"] is False
    assert write_tool["annotations"]["idempotentHint"] is True


def test_no_heartbeat_or_direct_complete_tools_exist():
    server, _ = make_server()
    response = handle(server, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    names = {tool["name"] for tool in response["result"]["tools"]}
    assert not any("heartbeat" in name for name in names)
    assert not any("direct_complete" in name for name in names)


def test_tools_call_read_uses_per_caller_credentials():
    server, clients = make_server()
    response = handle(
        server,
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "content_search_lexical",
                "arguments": {
                    "query": "handoff",
                    "read_scopes": ["proj-a"],
                    "limit": 5,
                    "include_historical": False,
                },
            },
        },
        credentials=CREDS,
    )
    result = response["result"]
    assert result["isError"] is False
    client = clients[0]
    assert client.credentials is CREDS
    operation_id, kwargs = client.calls[0]
    assert operation_id == "content.search.lexical"
    assert kwargs["scope"] == "proj-a"
    assert kwargs["payload"]["query"] == "handoff"


def test_tools_call_write_requires_idempotency_key_no_extra_semantics():
    server, clients = make_server()
    response = handle(
        server,
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": "memory_record",
                "arguments": {"record_type": "note", "body": "x"},
            },
        },
        credentials=CREDS,
    )
    result = response["result"]
    assert result["isError"] is True
    payload = json.loads(result["content"][0]["text"])
    assert payload["error"]["code"] == "idempotency_key_required"
    assert clients == []  # rejected before any client was created

    ok = handle(
        server,
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {
                "name": "memory_record",
                "arguments": {
                    "record_type": "note",
                    "body": "x",
                    "idempotency_key": "k-1",
                },
            },
        },
        credentials=CREDS,
    )
    assert ok["result"]["isError"] is False
    _, kwargs = clients[0].calls[0]
    assert kwargs["idempotency_key"] == "k-1"


def test_tools_call_without_credentials_fails_typed():
    server, clients = make_server()
    response = handle(
        server,
        {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {"name": "protocol_descriptor", "arguments": {}},
        },
        credentials=None,
    )
    result = response["result"]
    assert result["isError"] is True
    payload = json.loads(result["content"][0]["text"])
    assert payload["error"]["code"] == "credential_required"
    assert clients == []


def test_unknown_tool_and_method_errors():
    server, _ = make_server()
    response = handle(
        server,
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {"name": "not_a_tool", "arguments": {}},
        },
        credentials=CREDS,
    )
    assert response["error"]["code"] == -32602
    response = handle(server, {"jsonrpc": "2.0", "id": 8, "method": "bogus/method"})
    assert response["error"]["code"] == -32601


def test_api_error_becomes_tool_error_not_protocol_error():
    from cortex_v2.clients.errors import CortexApiError

    server, _ = make_server()

    def factory(credentials):
        return StubClient(
            error=CortexApiError(
                status=403,
                code="scope_write_denied",
                message="denied",
                retryable=False,
                request_id="r",
                operation_id="memory.record",
            )
        )

    server = McpServer(client_factory=factory)
    response = handle(
        server,
        {
            "jsonrpc": "2.0",
            "id": 9,
            "method": "tools/call",
            "params": {
                "name": "memory_record",
                "arguments": {
                    "record_type": "note",
                    "body": "x",
                    "idempotency_key": "k",
                },
            },
        },
        credentials=CREDS,
    )
    result = response["result"]
    assert "error" not in response
    assert result["isError"] is True
    assert json.loads(result["content"][0]["text"])["error"]["code"] == (
        "scope_write_denied"
    )


def test_streamable_http_app_per_request_credentials():
    from fastapi.testclient import TestClient

    from cortex_v2.mcp.server import create_mcp_app

    seen = []

    def factory(credentials):
        seen.append(credentials)
        return StubClient(credentials=credentials)

    app = create_mcp_app(client_factory=factory)
    with TestClient(app) as http:
        response = http.get("/mcp")
        assert response.status_code == 405
        response = http.post(
            "/mcp",
            headers={
                "Authorization": "Bearer per-request-token",
                "X-Cortex-Scope": "proj-b",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "protocol_descriptor", "arguments": {}},
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["result"]["isError"] is False
        assert body["id"] == 1
    assert seen[0].token == "per-request-token"
    assert seen[0].scope == "proj-b"


def test_streamable_http_requires_bearer_for_tools_call():
    from fastapi.testclient import TestClient

    from cortex_v2.mcp.server import create_mcp_app

    app = create_mcp_app(client_factory=lambda credentials: StubClient())
    with TestClient(app) as http:
        response = http.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "protocol_descriptor", "arguments": {}},
            },
        )
        assert response.status_code == 401
        body = response.json()
        assert body["error"]["code"] == "invalid_credential"


def test_stdio_loop_processes_line_delimited_json():
    from cortex_v2.mcp.server import run_stdio_session

    requests = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    inbound = io.StringIO(
        "".join(json.dumps(message) + "\n" for message in requests)
    )
    outbound = io.StringIO()
    server, _ = make_server()
    run_stdio_session(server, inbound, outbound, credentials=CREDS)
    lines = [json.loads(line) for line in outbound.getvalue().splitlines()]
    assert len(lines) == 2  # notifications produce no response
    assert lines[0]["id"] == 1
    assert lines[1]["id"] == 2
    assert any(
        tool["name"] == "context_prepare" for tool in lines[1]["result"]["tools"]
    )
