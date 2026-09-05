"""Live round-trip tests for the Cortex MCP server tools.

These tests instantiate each tool's underlying HTTP call against the actual
running cortex-api on http://localhost:8501. They SKIP gracefully when the
API is unreachable (CI / fresh machine / docker not started).

Run:  pytest .agents/api/tests/test_mcp_live.py -v
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import time
import uuid

import httpx
import pytest


CORTEX_API = "http://localhost:8501"
TEST_PROJECT = os.environ.get("CORTEX_TEST_PROJECT", "").strip()
TEST_AGENT = "kai"
ADMIN_TOKEN = "cortex-local-admin"
ROOT = Path(__file__).resolve().parents[3]


# ── Skip-if-down fixture ────────────────────────────────────────────────────


def _api_alive() -> bool:
    try:
        r = httpx.get(f"{CORTEX_API}/health", timeout=2.0)
        return r.status_code == 200 and r.json().get("status") == "healthy"
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not TEST_PROJECT or not _api_alive(),
    reason="CORTEX_TEST_PROJECT and a reachable cortex-api are required for live MCP tests",
)


@pytest.fixture
def http_client():
    """Configured client mimicking what the MCP server's lifespan creates."""
    with httpx.Client(
        base_url=CORTEX_API,
        timeout=10.0,
        headers={"X-Project": TEST_PROJECT, "X-Agent-Name": TEST_AGENT},
    ) as client:
        yield client


# ── Smoke: read-only tools ──────────────────────────────────────────────────


def test_cortex_doctor_health_endpoint(http_client):
    """cortex_doctor → GET /health returns healthy status."""
    r = http_client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "healthy"
    assert body["postgres"] == "connected"
    assert body["event_backend"] == "postgres"
    assert body["event_bus"] == "postgres"
    assert "redis" not in body
    assert isinstance(body["pg_notification_queue_usage"], (int, float))
    assert body["pg_notification_queue_usage"] >= 0


def test_cortex_boot_endpoint(http_client):
    """cortex_boot → GET /boot/kai returns boot text."""
    r = http_client.get(f"/boot/{TEST_AGENT}")
    assert r.status_code == 200
    # Boot output is text (or JSON wrapper) — accept either
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {"text": r.text}
    assert body, "boot returned empty"


def test_cortex_handoff_list_endpoint(http_client):
    """cortex_handoff_list → GET /handoffs returns a list."""
    r = http_client.get("/handoffs", params={"status": "pending"})
    assert r.status_code == 200
    body = r.json()
    # API returns a list directly OR an object containing one — accept both shapes
    assert isinstance(body, (list, dict))


def test_cortex_search_endpoint(http_client):
    """cortex_search → GET /search returns search results."""
    r = http_client.get("/search", params={"q": "kai", "type": "all"})
    assert r.status_code == 200


def test_cortex_state_endpoint(http_client):
    """cortex_state → GET /state returns project state."""
    r = http_client.get("/state")
    assert r.status_code == 200


def test_cortex_roster_endpoint(http_client):
    """cortex_roster → GET /roster returns the agent roster."""
    r = http_client.get("/roster")
    assert r.status_code == 200


# ── Smoke: write round-trip (decision) ──────────────────────────────────────


def test_cortex_log_decision_round_trip(http_client):
    """cortex_log_decision → POST /log with em-dash + apostrophe persists.

    This is the B.2 silent-fail fix dogfood: if this passes, the MCP layer
    routes through cortex-api correctly and the API handles UTF-8.
    """
    summary = "MCP live test — round-trip with user's apostrophe and em-dash"
    r = http_client.post(
        "/log",
        json={"event_type": "decision", "summary": summary},
        headers={"X-Agent-Name": TEST_AGENT},
    )
    assert r.status_code == 200, f"POST /log failed: {r.status_code} {r.text}"
    body = r.json()
    assert "id" in body, f"expected id in response: {body}"
    assert body.get("embedded") in (True, False)


def current_team_event_cursor(http_client) -> str:
    r = http_client.get(
        "/beat/events",
        params={"count": 1, "team_events": "true"},
        headers={"X-Cortex-Admin-Token": ADMIN_TOKEN},
    )
    assert r.status_code == 200, r.text
    return str(r.json().get("last_id") or "0")


def wait_for_team_event(http_client, cursor: str, summary_marker: str, event_type: str) -> dict:
    deadline = time.monotonic() + 6
    last_id = cursor
    while time.monotonic() < deadline:
        r = http_client.get(
            "/beat/events",
            params={"last_id": last_id, "count": 50, "team_events": "true"},
            headers={"X-Cortex-Admin-Token": ADMIN_TOKEN},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        last_id = str(body.get("last_id") or last_id)
        for event in body.get("events", []):
            fields = event.get("fields") or {}
            if fields.get("type") == event_type and summary_marker in fields.get("summary", ""):
                return event
        time.sleep(0.25)
    raise AssertionError(f"team event not visible after cursor {cursor}: {summary_marker}")


def test_postgres_team_event_visible_through_beat_events(http_client):
    marker = f"inc26-event-{uuid.uuid4().hex}"
    cursor = current_team_event_cursor(http_client)

    r = http_client.post(
        "/log",
        json={"event_type": "decision", "summary": marker},
        headers={"X-Agent-Name": TEST_AGENT},
    )
    assert r.status_code == 200, r.text

    event = wait_for_team_event(http_client, cursor, marker, "decision")
    fields = event["fields"]
    assert event["id"].isdigit()
    assert fields["project"] == TEST_PROJECT
    assert fields["agent"].startswith(TEST_AGENT)


def test_artifact_visible_through_api_event_search_and_cli(http_client):
    marker = f"inc26-artifact-{uuid.uuid4().hex}"
    source_file = (
        "Program/Kaidera/Release_v0.3.0/"
        f"E75_LOCAL_CORTEX_MODERNISATION/inc26/{marker}.md"
    )
    raw_content = f"{marker} proves artifact read parity through API and CLI"
    cursor = current_team_event_cursor(http_client)

    r = http_client.post(
        "/artifacts",
        json={
            "source_file": source_file,
            "content_hash": hashlib.sha256(raw_content.encode("utf-8")).hexdigest(),
            "modality": "text",
            "source_type": "inc26-live-test",
            "extraction_method": "pytest",
            "raw_content": raw_content,
            "metadata": {"test": "inc26"},
        },
        headers={"X-Agent-Name": TEST_AGENT},
    )
    assert r.status_code == 200, r.text
    artifact_id = r.json()["id"]

    event = wait_for_team_event(http_client, cursor, source_file, "artifact")
    fields = event["fields"]
    assert fields["project"] == TEST_PROJECT
    assert source_file in fields.get("files", "")
    assert artifact_id in fields.get("detail", "")

    search = http_client.get("/search", params={"q": marker, "type": "artifacts"})
    assert search.status_code == 200, search.text
    results = search.json().get("results") or []
    assert any(
        result.get("source") == "artifacts" and result.get("id") == artifact_id
        for result in results
    )

    env = os.environ.copy()
    env.update(
        {
            "CORTEX_PROJECT": TEST_PROJECT,
            "CORTEX_API_URL": CORTEX_API,
            # The test may run from a checkout whose active project differs from
            # TEST_PROJECT. Use a one-command override so the CLI exercises the same
            # cross-project live surface as the API half without weakening the
            # default workspace isolation guard.
            "CORTEX_CTO_OVERRIDE": "MCP-LIVE-ARTIFACT-TEST",
        }
    )
    cli = subprocess.run(
        [
            str(ROOT / ".agents/scripts/cortex-search"),
            marker,
            "--type",
            "artifacts",
            "--limit",
            "10",
        ],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )
    assert cli.returncode == 0, cli.stderr or cli.stdout
    body = json.loads(cli.stdout)
    assert any(
        result.get("source") == "artifacts" and result.get("id") == artifact_id
        for result in body.get("results") or []
    )


# ── Skeleton tools end-to-end via the actual MCP module ─────────────────────


@pytest.fixture
def mcp_module():
    """Import the MCP server module for in-process tool dispatch."""
    here = Path(__file__).resolve().parent
    src = here.parent / "mcp_server.py"
    spec = importlib.util.spec_from_file_location("cortex_mcp_live_under_test", src)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_module_imports_with_live_api(mcp_module):
    """Sanity: with API up, the module still imports cleanly and FastMCP is configured."""
    assert hasattr(mcp_module, "mcp")
    assert mcp_module.CORTEX_API_URL.startswith("http://")
