"""Phase C cutover — verifies RLS enforcement through cortex-api itself.

These tests go through the HTTP API (not direct PG) and prove that under
the two-pool architecture (CORTEX_PG_DSN_APP=cortex_app), cross-tenant
queries cannot leak project-scoped rows.

Companion to test_phase_c_rls.py which tests RLS at the PG layer directly.
"""
from __future__ import annotations

import os
import uuid

import httpx
import pytest


CORTEX_API = os.environ.get("CORTEX_API_URL", "http://localhost:8501")
PROJECT_A = os.environ.get("CORTEX_TEST_PROJECT_A", "").strip()
PROJECT_B = os.environ.get("CORTEX_TEST_PROJECT_B", "").strip()


def _api_alive() -> bool:
    try:
        r = httpx.get(f"{CORTEX_API}/health", timeout=2.0)
        return r.status_code == 200
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not PROJECT_A or not PROJECT_B or not _api_alive(),
    reason="two explicit Cortex test projects and a reachable API are required",
)


@pytest.fixture
def client_a():
    """HTTP client scoped to PROJECT_A."""
    with httpx.Client(
        base_url=CORTEX_API,
        timeout=10.0,
        headers={"X-Project": PROJECT_A, "X-Agent-Name": "kai"},
    ) as client:
        yield client


@pytest.fixture
def client_b():
    """HTTP client scoped to PROJECT_B."""
    with httpx.Client(
        base_url=CORTEX_API,
        timeout=10.0,
        headers={"X-Project": PROJECT_B, "X-Agent-Name": "kai"},
    ) as client:
        yield client


def test_decisions_scoped_to_project(client_a, client_b):
    """A decision logged in project A is searchable in A but not in B.

    This is the cross-tenant defense test. With RLS enforcing on the app
    pool (cortex_app role), project B's connection cannot see project A's
    rows even if it queries by exact text match.
    """
    # Sentinel string unique to this run
    sentinel = f"phase-c-cutover-test-{uuid.uuid4().hex[:8]}"

    # Write to PROJECT_A
    log_resp = client_a.post(
        "/log",
        json={"event_type": "decision", "summary": f"sentinel-decision: {sentinel}"},
    )
    assert log_resp.status_code == 200
    assert log_resp.json().get("id"), f"log returned no id: {log_resp.json()}"

    # Search for it in PROJECT_A — should find
    a_resp = client_a.get("/search", params={"q": sentinel, "type": "decisions"})
    assert a_resp.status_code == 200
    a_hits = a_resp.json().get("results", [])
    a_sentinel_match = [r for r in a_hits if sentinel in (r.get("text") or "")]
    assert a_sentinel_match, (
        f"PROJECT_A search did not find its own decision (sentinel={sentinel}); "
        f"got {len(a_hits)} unrelated hits"
    )

    # Search for it in PROJECT_B — should NOT find
    b_resp = client_b.get("/search", params={"q": sentinel, "type": "decisions"})
    assert b_resp.status_code == 200
    b_hits = b_resp.json().get("results", [])
    b_sentinel_match = [r for r in b_hits if sentinel in (r.get("text") or "")]
    assert not b_sentinel_match, (
        f"PROJECT_B leak: found {len(b_sentinel_match)} match(es) for "
        f"sentinel={sentinel} that belong to PROJECT_A. RLS not enforcing!"
    )


def test_handoffs_scoped_to_project(client_a, client_b):
    """Handoffs in project A are not visible to project B.

    Handoffs is one of the most-accessed scoped tables. Cross-tenant leak
    here would expose other customers' work routing.
    """
    a_resp = client_a.get("/handoffs", params={"project": PROJECT_A})
    b_resp = client_b.get("/handoffs", params={"project": PROJECT_B})
    assert a_resp.status_code == 200
    assert b_resp.status_code == 200

    a_handoffs = a_resp.json().get("handoffs", [])
    b_handoffs = b_resp.json().get("handoffs", [])

    assert "project_hex" not in a_resp.json()
    assert "project_hex" not in b_resp.json()
    for payload in (a_handoffs, b_handoffs):
        for handoff in payload:
            for actor_field in ("from_agent", "to_agent", "claimed_by"):
                actor = handoff.get(actor_field) or ""
                assert ":" not in actor, (
                    f"retired colon identity leaked in {actor_field}: {actor}"
                )
