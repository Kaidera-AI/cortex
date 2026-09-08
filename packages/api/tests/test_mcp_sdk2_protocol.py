"""SDK 2 migration proof using its in-memory protocol transport, no live Cortex."""
import asyncio
import importlib.util
import json
from pathlib import Path

import httpx
from mcp import Client
import pytest


@pytest.mark.asyncio
async def test_sdk2_protocol_lifespan_tool_schema_and_http_boundary(monkeypatch):
    source = Path(__file__).resolve().parents[1] / "mcp_server.py"
    spec = importlib.util.spec_from_file_location("cortex_mcp_sdk2_contract", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "CORTEX_PROJECT", "protocol-test")
    monkeypatch.setattr(module, "CORTEX_AGENT", "ren")
    monkeypatch.setattr(module, "CORTEX_API_BEARER_TOKEN", "test-only-placeholder")
    requests = []
    original_client = httpx.AsyncClient

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"project": "protocol-test", "agents": []})

    def client_factory(**kwargs):
        return original_client(transport=httpx.MockTransport(respond), **kwargs)

    async def no_stdin_watchdog():
        await asyncio.Event().wait()

    monkeypatch.setattr(module.httpx, "AsyncClient", client_factory)
    monkeypatch.setattr(module, "_stdin_watchdog", no_stdin_watchdog)
    async with Client(module.mcp) as client:
        tools = await client.list_tools()
        assert len(tools.tools) == 27
        roster = next(tool for tool in tools.tools if tool.name == "cortex_roster")
        # SDK 2 Python names changed, while the public MCP wire casing is stable.
        wire = roster.model_dump(by_alias=True)
        assert "inputSchema" in wire and "ctx" not in wire["inputSchema"]["properties"]
        result = await client.call_tool("cortex_roster", {"project": "protocol-test"})
        assert not result.is_error
        assert json.loads(result.content[0].text) == {"project": "protocol-test", "agents": []}
    assert len(requests) == 1
    assert requests[0].url.path == "/roster"
    assert requests[0].url.params["project"] == "protocol-test"
    assert requests[0].headers["X-Project"] == "protocol-test"
    assert requests[0].headers["X-Agent-Name"] == "ren"
    assert requests[0].headers["Authorization"] == "Bearer test-only-placeholder"


def test_sdk2_streamable_http_transport_still_exposes_mcp_route():
    source = Path(__file__).resolve().parents[1] / "mcp_server.py"
    spec = importlib.util.spec_from_file_location("cortex_mcp_sdk2_http_contract", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    app = module.mcp.streamable_http_app()
    assert any(route.path == "/mcp" for route in app.routes)
