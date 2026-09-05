"""Contract tests for the Cortex MCP server (B.1.2 — full 24-tool surface).

Run:  pytest .agents/api/tests/test_mcp.py -v

Live HTTP round-trip tests live in test_mcp_live.py and skip when
cortex-api is unreachable.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


# ── Expected tool surface (sync with .agents/api/MCP_SERVER_DESIGN.md §5) ──
EXPECTED_TOOLS: set[str] = {
    # Identity + boot
    "cortex_bootstrap",
    "cortex_boot",
    "cortex_persona",
    # Handoffs
    "cortex_handoff_list",
    "cortex_handoff_get",
    "cortex_handoff_create",
    "cortex_handoff_claim",
    "cortex_handoff_complete",
    "cortex_handoff_return",
    # Memory writes
    "cortex_log_decision",
    "cortex_log_lesson",
    "cortex_log_event",
    "cortex_diary_write",
    "cortex_beat_heartbeat",
    "cortex_beat_claim_done",
    # Search + retrieval
    "cortex_search",
    "cortex_graph_search",
    "cortex_entities_search",
    "cortex_history",
    # Code graph
    "cortex_graph_blast",
    "cortex_graph_callers",
    "cortex_graph_impact",
    "cortex_graph_stats",
    # Diagnostic
    "cortex_doctor",
    "cortex_verify_decision",
    "cortex_state",
    "cortex_roster",
}


@pytest.fixture
def mcp_module():
    """Import .agents/api/mcp_server.py without polluting sys.modules."""
    here = Path(__file__).resolve().parent
    src = here.parent / "mcp_server.py"
    spec = importlib.util.spec_from_file_location("cortex_mcp_under_test", src)
    assert spec and spec.loader, f"could not load spec for {src}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _registered_tools(mcp) -> set[str]:
    """FastMCP exposes registered tools across SDK versions via different paths."""
    if hasattr(mcp, "_tool_manager"):
        return {t.name for t in mcp._tool_manager.list_tools()}
    if hasattr(mcp, "_tools"):
        return set(mcp._tools.keys())
    pytest.fail("FastMCP instance has no recognised tool registry attribute")


def test_imports_cleanly(mcp_module):
    """Module loads without ImportError and exposes the FastMCP instance."""
    assert hasattr(mcp_module, "mcp")


@pytest.mark.asyncio
async def test_lifespan_requires_explicit_project(mcp_module, monkeypatch):
    monkeypatch.setattr(mcp_module, "CORTEX_PROJECT", "")

    with pytest.raises(RuntimeError, match="CORTEX_PROJECT is required"):
        async with mcp_module.lifespan(None):
            pass


def test_server_metadata(mcp_module):
    """Server name + version are set per design doc."""
    assert mcp_module.SERVER_NAME == "cortex"
    assert mcp_module.SERVER_VERSION == "0.1.0"


def test_full_tool_surface_registered(mcp_module):
    """B.1.2 ships 24 tools per design §5."""
    registered = _registered_tools(mcp_module.mcp)
    missing = EXPECTED_TOOLS - registered
    extra = registered - EXPECTED_TOOLS
    assert not missing, f"missing tools: {sorted(missing)}"
    # Extras are warnings, not failures — extending the surface is fine.
    if extra:
        print(f"\nINFO: extra tools registered (not in EXPECTED_TOOLS): {sorted(extra)}")
    assert len(registered) >= len(EXPECTED_TOOLS), (
        f"registered {len(registered)} tools, expected >= {len(EXPECTED_TOOLS)}"
    )


def test_main_entrypoint_exists(mcp_module):
    """main() exists and is callable (does not invoke; would block on stdio)."""
    assert callable(getattr(mcp_module, "main", None))


def test_stdin_watchdog_exists(mcp_module):
    """The mandatory stdin-EOF watchdog is present (per design §6)."""
    assert callable(getattr(mcp_module, "_stdin_watchdog", None))
    assert callable(getattr(mcp_module, "_setup_pgroup", None))


def test_helpers_exist(mcp_module):
    """Helper functions used by tools are defined."""
    for name in ("_safe_call", "_post_with_agent", "_put_with_agent", "_http"):
        assert callable(getattr(mcp_module, name, None)), f"missing helper: {name}"


def test_bearer_token_scaffold(mcp_module):
    """Bearer-token scaffold (kept as defense-in-depth after B.5 promotion)."""
    assert callable(getattr(mcp_module, "_check_bearer", None))
    assert hasattr(mcp_module, "_TRANSPORT")
    assert hasattr(mcp_module, "_BEARER_TOKEN")
    assert mcp_module._TRANSPORT in ("stdio", "streamable-http")


def test_bearer_auth_middleware_class(mcp_module):
    """B.5 proper integration: BearerAuthMiddleware ASGI class present."""
    cls = getattr(mcp_module, "BearerAuthMiddleware", None)
    assert cls is not None, "BearerAuthMiddleware not defined"
    # Must accept an ASGI app in __init__ and be callable as ASGI
    assert callable(cls)
    # Quick instantiation smoke (uses dummy app)
    async def dummy_app(scope, receive, send):
        return
    middleware = cls(dummy_app)
    assert middleware.app is dummy_app
    assert callable(middleware._reject)


def test_tool_groups_present(mcp_module):
    """Sanity check — at least 3 tools in each design-doc group are registered."""
    registered = _registered_tools(mcp_module.mcp)
    groups = {
        "identity_boot": {"cortex_bootstrap", "cortex_boot", "cortex_persona"},
        "handoffs": {
            "cortex_handoff_list", "cortex_handoff_get", "cortex_handoff_create",
            "cortex_handoff_claim", "cortex_handoff_complete",
            "cortex_handoff_return",
        },
        "memory_writes": {
            "cortex_log_decision", "cortex_log_lesson",
            "cortex_log_event", "cortex_diary_write",
        },
        "search": {
            "cortex_search", "cortex_graph_search",
            "cortex_entities_search", "cortex_history",
        },
        "code_graph": {
            "cortex_graph_blast", "cortex_graph_callers",
            "cortex_graph_impact", "cortex_graph_stats",
        },
        "diagnostic": {
            "cortex_doctor", "cortex_verify_decision",
            "cortex_state", "cortex_roster",
        },
    }
    for group, tools in groups.items():
        registered_in_group = tools & registered
        assert len(registered_in_group) >= 3, (
            f"group '{group}' has {len(registered_in_group)} tools registered "
            f"(expected >= 3): {sorted(registered_in_group)}"
        )


@pytest.mark.asyncio
async def test_graph_mcp_tools_send_exact_typed_api_contracts(mcp_module, monkeypatch):
    calls = []

    async def fake_safe_call(ctx, method, path, **kwargs):
        calls.append((ctx, method, path, kwargs))
        return {"ok": True}

    monkeypatch.setattr(mcp_module, "_safe_call", fake_safe_call)
    ctx = SimpleNamespace()

    await mcp_module.cortex_graph_blast(
        ctx, files=["api/main.py", "worker.py"], repo="kaidera-os",
        depth=3, max_results=40,
    )
    await mcp_module.cortex_graph_callers(
        ctx, target="graph_build_proxy", repo="kaidera-os",
        pattern="callers_of", max_results=30,
    )
    await mcp_module.cortex_graph_impact(
        ctx, base="origin/main", repo="kaidera-os", max_results=25,
    )
    await mcp_module.cortex_graph_stats(ctx)

    assert calls == [
        (ctx, "POST", "/graph/blast", {"json": {
            "files": ["api/main.py", "worker.py"], "repo": "kaidera-os",
            "depth": 3, "max_results": 40,
        }, "timeout": 140.0}),
        (ctx, "POST", "/graph/callers", {"json": {
            "target": "graph_build_proxy", "repo": "kaidera-os",
            "pattern": "callers_of", "max_results": 30,
        }, "timeout": 80.0}),
        (ctx, "POST", "/graph/impact", {"json": {
            "base": "origin/main", "repo": "kaidera-os", "max_results": 25,
        }, "timeout": 140.0}),
        (ctx, "GET", "/graph/stats", {}),
    ]


def test_graph_mcp_tools_advertise_normalized_output_schemas(mcp_module):
    tools = {tool.name: tool for tool in mcp_module.mcp._tool_manager.list_tools()}

    blast = tools["cortex_graph_blast"].fn_metadata.output_schema
    callers = tools["cortex_graph_callers"].fn_metadata.output_schema
    impact = tools["cortex_graph_impact"].fn_metadata.output_schema

    assert blast is not None
    assert blast["properties"]["status"]["enum"] == ["ok", "error"]
    assert {
        "status", "summary", "changed_files", "changed_nodes", "impacted_nodes",
        "impacted_files", "edges", "totals", "total_impacted", "truncated",
    } <= set(blast["required"])

    assert callers is not None
    assert callers["properties"]["status"]["enum"] == [
        "ok", "ambiguous", "not_found", "error",
    ]
    assert {
        "status", "target_indexed", "pattern", "target", "description", "summary",
        "reason", "error", "hint", "results", "edges", "candidates",
        "indexed_kinds", "indexed_under", "totals", "truncated",
    } <= set(callers["required"])

    assert impact is not None
    assert impact["properties"]["status"]["enum"] == ["ok", "error"]
    assert {"status", "summary", "context", "totals", "truncated"} <= set(
        impact["required"]
    )


@pytest.mark.asyncio
async def test_graph_mcp_transport_failures_preserve_each_typed_shape(
    mcp_module, monkeypatch,
):
    async def fake_safe_call(_ctx, _method, _path, **_kwargs):
        return {"error": "http_error", "status": 503, "detail": "worker unavailable"}

    monkeypatch.setattr(mcp_module, "_safe_call", fake_safe_call)
    ctx = SimpleNamespace()

    blast = await mcp_module.cortex_graph_blast(ctx, ["a.py"], ".")
    callers = await mcp_module.cortex_graph_callers(ctx, "dispatch", ".")
    impact = await mcp_module.cortex_graph_impact(ctx, "HEAD~1", ".")

    assert blast["status"] == "error"
    assert blast["totals"]["impacted_files"] == 0
    assert blast["error"] == "worker unavailable"
    assert callers["status"] == "error"
    assert callers["target_indexed"] is False
    assert callers["totals"]["results"] == 0
    assert impact["status"] == "error"
    assert impact["context"]["review_guidance"] == ""
    assert impact["totals"]["untested_functions"] == 0


@pytest.mark.asyncio
async def test_handoff_list_filters_with_agent_query_param(mcp_module, monkeypatch):
    """The API's role-aware mine filter is `agent=`, not `to_role=`."""

    calls = []

    async def fake_safe_call(ctx, method, path, **kwargs):
        calls.append((method, path, kwargs))
        return {"handoffs": []}

    monkeypatch.setattr(mcp_module, "_safe_call", fake_safe_call)

    result = await mcp_module.cortex_handoff_list(
        SimpleNamespace(),
        agent="alpha",
        status="pending",
    )

    assert result == {"handoffs": []}
    assert calls == [
        (
            "GET",
            "/handoffs",
            {"params": {"status": "pending", "agent": "alpha"}},
        )
    ]


@pytest.mark.asyncio
async def test_handoff_claim_sends_beat_heartbeat_after_success(mcp_module):
    """MCP claim wiring emits the first Beat heartbeat for active tracking."""

    class Response:
        headers = {"content-type": "application/json"}
        text = "{}"

        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    class HTTP:
        def __init__(self):
            self.calls = []

        async def put(self, path, json=None, headers=None):
            self.calls.append(("PUT", path, json, headers))
            return Response({"claimed": True})

        async def post(self, path, json=None, headers=None):
            self.calls.append(("POST", path, json, headers))
            if path.endswith("/claim-with-budget"):
                return Response({"claimed": True, "budget": {"allow_llm": True}})
            return Response({"task": {"state": "executing"}})

    http = HTTP()
    ctx = SimpleNamespace(
        request_context=SimpleNamespace(lifespan_context={"http": http})
    )

    result = await mcp_module.cortex_handoff_claim(
        "77ae5f47-0000-0000-0000-000000000000",
        "root",
        ctx,
    )

    assert result == {
        "claimed": True,
        "beat_heartbeat": {"task": {"state": "executing"}},
    }
    assert http.calls == [
        (
            "PUT",
            "/handoffs/77ae5f47-0000-0000-0000-000000000000/claim",
            {},
            {"X-Agent-Name": "root"},
        ),
        (
            "POST",
            "/beat/tasks/77ae5f47-0000-0000-0000-000000000000/heartbeat",
            {"evidence_summary": "claimed handoff"},
            {"X-Agent-Name": "root"},
        ),
    ]


@pytest.mark.asyncio
async def test_handoff_claim_failure_skips_beat_heartbeat(mcp_module):
    """Failed MCP claims return detail without active tracking."""

    class Response:
        headers = {"content-type": "application/json"}
        text = "{}"

        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    class HTTP:
        def __init__(self):
            self.calls = []

        async def put(self, path, json=None, headers=None):
            self.calls.append(("PUT", path, json, headers))
            return Response({"claimed": False, "reason": "already claimed"})

    http = HTTP()
    ctx = SimpleNamespace(
        request_context=SimpleNamespace(lifespan_context={"http": http})
    )

    result = await mcp_module.cortex_handoff_claim(
        "77ae5f47-0000-0000-0000-000000000000",
        "root",
        ctx,
    )

    assert result["claimed"] is False
    assert len(http.calls) == 1


@pytest.mark.asyncio
async def test_handoff_complete_sends_beat_claim_done_after_success(mcp_module):
    """MCP complete wiring emits the Beat claim-done terminal signal."""

    class Response:
        headers = {"content-type": "application/json"}
        text = "{}"

        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    class HTTP:
        def __init__(self):
            self.calls = []

        async def request(self, method, path, **kwargs):
            self.calls.append((method, path, kwargs.get("json"), kwargs.get("headers")))
            return Response({"ok": True})

        async def post(self, path, json=None, headers=None):
            self.calls.append(("POST", path, json, headers))
            if path.endswith("/return"):
                return Response(
                    {
                        "returned": True,
                        "status": "returned",
                        "handback_id": "88ae5f47-0000-0000-0000-000000000000",
                    }
                )
            return Response({"task": {"state": "verified"}})

    http = HTTP()
    ctx = SimpleNamespace(
        request_context=SimpleNamespace(lifespan_context={"http": http})
    )

    result = await mcp_module.cortex_handoff_complete(
        "77ae5f47-0000-0000-0000-000000000000",
        ctx,
        agent="root",
        outcome="completed",
        evidence_summary="ship verified",
    )

    assert result == {
        "returned": True,
        "status": "returned",
        "handback_id": "88ae5f47-0000-0000-0000-000000000000",
        "beat_claim_done": {"task": {"state": "verified"}},
    }
    assert http.calls[0][0:2] == (
        "POST",
        "/handoffs/77ae5f47-0000-0000-0000-000000000000/return",
    )
    assert http.calls[0][2]["summary"] == "ship verified"
    assert http.calls[0][2]["metadata"] == {"surface": "mcp-legacy-complete"}
    assert http.calls[0][3] == {"X-Agent-Name": "root"}
    assert http.calls[1] == (
        "POST",
        "/beat/tasks/77ae5f47-0000-0000-0000-000000000000/claim-done",
        {"outcome": "completed", "evidence_summary": "ship verified"},
        {"X-Agent-Name": "root"},
    )


@pytest.mark.asyncio
async def test_handoff_complete_no_agent_skips_claim_done(mcp_module):
    """The legacy wrapper fails closed when it cannot create an audited return."""

    class Response:
        headers = {"content-type": "application/json"}
        text = "{}"

        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    class HTTP:
        def __init__(self):
            self.calls = []

        async def request(self, method, path, **kwargs):
            self.calls.append((method, path, kwargs.get("json"), kwargs.get("headers")))
            return Response({"ok": True})

        async def post(self, path, json=None, headers=None):
            self.calls.append(("POST", path, json, headers))
            return Response({})

    http = HTTP()
    ctx = SimpleNamespace(
        request_context=SimpleNamespace(lifespan_context={"http": http})
    )

    result = await mcp_module.cortex_handoff_complete(
        "77ae5f47-0000-0000-0000-000000000000",
        ctx,
    )

    assert result["error"] == "completion_requires_return"
    assert "agent and evidence_summary" in result["detail"]
    assert http.calls == []


@pytest.mark.asyncio
async def test_handoff_return_posts_report_and_finishes_beat(mcp_module):
    class Response:
        headers = {"content-type": "application/json"}
        text = "{}"

        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    class HTTP:
        def __init__(self):
            self.calls = []

        async def post(self, path, json=None, headers=None):
            self.calls.append(("POST", path, json, headers))
            if path.endswith("/return"):
                return Response(
                    {
                        "returned": True,
                        "status": "returned",
                        "handback_id": "handback-1",
                    }
                )
            return Response({"task": {"state": "verified"}})

    http = HTTP()
    ctx = SimpleNamespace(
        request_context=SimpleNamespace(lifespan_context={"http": http})
    )

    result = await mcp_module.cortex_handoff_return(
        "77ae5f47-0000-0000-0000-000000000000",
        "kai",
        "Implemented and tested",
        ctx,
        tests_run=[{"name": "pytest", "status": "passed"}],
    )

    assert result["returned"] is True
    assert result["handback_id"] == "handback-1"
    assert result["beat_claim_done"] == {"task": {"state": "verified"}}
    assert http.calls[0] == (
        "POST",
        "/handoffs/77ae5f47-0000-0000-0000-000000000000/return",
        {
            "outcome": "completed",
            "summary": "Implemented and tested",
            "tests_run": [{"name": "pytest", "status": "passed"}],
            "artifacts": [],
            "risks": [],
            "followups": [],
            "metadata": {"surface": "mcp"},
        },
        {"X-Agent-Name": "kai"},
    )
    assert http.calls[1][1].endswith("/claim-done")
