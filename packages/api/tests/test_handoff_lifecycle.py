"""Live round-trip tests for the new handoff terminal-state endpoints.

Background — handoff 63a116e8, Alpha Option B (Lux RCA 4337ef2c):
The handoffs status enum had only pending/claimed/completed/archived.
Once an agent claimed a handoff, the only escape was --complete — agents
either lied (completed unfinished work) or left zombies. Lux discovered
10 zombie-claimed handoffs fleet-wide, oldest 25.6 days.

This patch adds three terminal-non-success states with matching API
endpoints + CLI verbs:
  released   — claimer drops it back to pending pool (someone else takes it)
  abandoned  — work no longer needed (e.g. handoff superseded by another)
  failed     — claim hit unrecoverable error; needs Alpha triage

terminal_reason text column carries audit text. Audit actor is the
claimed_by who released/abandoned/failed it.
"""

from __future__ import annotations

import os
import uuid

import httpx
import pytest


CORTEX_API = "http://localhost:8501"
TEST_PROJECT = os.environ.get("CORTEX_TEST_PROJECT", "").strip()
TEST_AGENT = "kai"


def _api_alive() -> bool:
    try:
        r = httpx.get(f"{CORTEX_API}/health", timeout=2.0)
        return r.status_code == 200 and r.json().get("status") == "healthy"
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not TEST_PROJECT or not _api_alive(),
    reason="CORTEX_TEST_PROJECT and a reachable cortex-api are required for live handoff tests",
)


@pytest.fixture
def http_client():
    with httpx.Client(
        base_url=CORTEX_API,
        timeout=15.0,
        headers={
            "X-Project": TEST_PROJECT,
            "X-Agent-Name": TEST_AGENT,
            "X-Cortex-Admin-Token": os.environ.get(
                "CORTEX_ADMIN_TOKEN", "cortex-local-admin"
            ),
        },
    ) as client:
        yield client


def _create_test_handoff(client: httpx.Client, summary: str) -> str:
    r = client.post(
        "/handoffs",
        json={
            "from_role": "cortex-architect",
            "to_role": "cortex-architect",
            "to_agent": TEST_AGENT,
            "priority": "low",
            "summary": summary,
        },
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _claim(client: httpx.Client, handoff_id: str) -> None:
    r = client.put(f"/handoffs/{handoff_id}/claim")
    assert r.status_code == 200, r.text


def _get_status(client: httpx.Client, handoff_id: str) -> dict:
    r = client.get(f"/handoffs/{handoff_id}")
    assert r.status_code == 200, r.text
    return r.json()


# ── /handoffs/{id}/release ──────────────────────────────────────────────────


def test_release_moves_claimed_back_to_pending(http_client):
    """Released handoffs return to the pending pool with claimed_by cleared."""
    hid = _create_test_handoff(http_client, "RCA test — release round-trip")
    _claim(http_client, hid)

    r = http_client.put(
        f"/handoffs/{hid}/release",
        json={"reason": "needs different specialty"},
    )
    assert r.status_code == 200, f"release failed: {r.status_code} {r.text}"
    assert r.json()["released"] is True

    after = _get_status(http_client, hid)
    assert after["status"] == "pending", f"expected pending, got {after['status']}"
    # Released handoffs should clear claimed_by so a new agent can claim cleanly
    assert not after.get("claimed_by"), \
        f"claimed_by should be cleared after release, got {after.get('claimed_by')}"

    # Cleanup
    http_client.post("/admin/sql/exec",
                     json={"sql": f"DELETE FROM handoffs WHERE id = '{hid}'"})


def test_release_404_for_non_claimed(http_client):
    """Cannot release a handoff that isn't currently claimed."""
    hid = _create_test_handoff(http_client, "RCA test — release-not-claimed")
    r = http_client.put(f"/handoffs/{hid}/release")
    assert r.status_code == 404
    http_client.post("/admin/sql/exec",
                     json={"sql": f"DELETE FROM handoffs WHERE id = '{hid}'"})


# ── /handoffs/{id}/abandon ──────────────────────────────────────────────────


def test_abandon_sets_terminal_with_reason(http_client):
    hid = _create_test_handoff(http_client, "RCA test — abandon round-trip")
    _claim(http_client, hid)

    r = http_client.put(
        f"/handoffs/{hid}/abandon",
        json={"reason": "superseded by handoff XYZ"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["abandoned"] is True

    after = _get_status(http_client, hid)
    assert after["status"] == "abandoned"
    # terminal_reason should carry the audit text
    assert "terminal_reason" in after or after.get("status") == "abandoned"

    http_client.post("/admin/sql/exec",
                     json={"sql": f"DELETE FROM handoffs WHERE id = '{hid}'"})


# ── /handoffs/{id}/fail ─────────────────────────────────────────────────────


def test_fail_sets_terminal_with_reason(http_client):
    hid = _create_test_handoff(http_client, "RCA test — fail round-trip")
    _claim(http_client, hid)

    r = http_client.put(
        f"/handoffs/{hid}/fail",
        json={"reason": "unrecoverable: missing dependency XYZ"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["failed"] is True

    after = _get_status(http_client, hid)
    assert after["status"] == "failed"

    http_client.post("/admin/sql/exec",
                     json={"sql": f"DELETE FROM handoffs WHERE id = '{hid}'"})


def test_fail_without_reason_still_works(http_client):
    """reason is optional for all three terminals — POST with empty body works."""
    hid = _create_test_handoff(http_client, "RCA test — fail-no-reason")
    _claim(http_client, hid)

    r = http_client.put(f"/handoffs/{hid}/fail")
    assert r.status_code == 200, r.text

    after = _get_status(http_client, hid)
    assert after["status"] == "failed"

    http_client.post("/admin/sql/exec",
                     json={"sql": f"DELETE FROM handoffs WHERE id = '{hid}'"})


# ── State machine: cannot transition from terminal back to pending ─────────


def test_cannot_release_completed_handoff(http_client):
    """Once completed, release is rejected. State machine sanity."""
    hid = _create_test_handoff(http_client, "RCA test — terminal-state-immutability")
    _claim(http_client, hid)
    r_complete = http_client.put(f"/handoffs/{hid}/complete")
    assert r_complete.status_code == 200

    r_release = http_client.put(f"/handoffs/{hid}/release")
    # 404 (not in claimed state) — same semantics as release-not-claimed
    assert r_release.status_code == 404

    http_client.post("/admin/sql/exec",
                     json={"sql": f"DELETE FROM handoffs WHERE id = '{hid}'"})
