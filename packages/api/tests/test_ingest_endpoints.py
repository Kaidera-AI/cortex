"""Live round-trip tests for the new ingest endpoints.

Background — handoff d6018d86, Option C ratified by rex:
The previous ingest path went through /admin/sql/exec with bash sql_escape() —
which has a quoting bug that corrupts any input containing an apostrophe into
"X\\'\\'Y" (4 chars instead of 2). asyncpg rejects the resulting SQL and the
script's silencers ('>/dev/null' + '|| true') hide the failure while a counter
falsely reports success. Mirrors the B.2 fix already shipped for cortex-log
and cortex-handoff (handoff a85e9082).

These tests prove the new endpoints persist content with apostrophes and
em-dashes through to PG. Skip-if-down so CI without docker still passes.

Run: pytest .agents/api/tests/test_ingest_endpoints.py -v
"""

from __future__ import annotations

import os
import time

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
    reason="CORTEX_TEST_PROJECT and a reachable cortex-api are required for live ingest tests",
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


# A unique tag per pytest invocation so reruns never collide on dedup keys.
RUN_TAG = f"KAI_INGEST_TEST_{int(time.time())}"


# ── /knowledge/ingest ───────────────────────────────────────────────────────


def test_knowledge_ingest_persists_content_with_apostrophe(http_client):
    """The B-bug killer: content like 'Beat's job' must round-trip to PG.

    Pre-Option-C this would silently fail because bash sql_escape corrupts
    apostrophes. Post-fix: 200 + row exists with the same byte content.
    """
    body = {
        "content": (
            "Beat's job — em-dash test. agent's apostrophe. "
            "Don't drop these bytes."
        ),
        "source_file": f"/tmp/{RUN_TAG}_knowledge.md",
        "category": "rca-ingest-test",
        "section": "RCA test row",
    }
    r = http_client.post("/knowledge/ingest", json=body)
    assert r.status_code == 200, f"POST failed: {r.status_code} body={r.text}"
    resp = r.json()
    assert "id" in resp and resp["id"], f"missing id: {resp}"
    assert resp.get("created") is True, f"first ingest must be created=true: {resp}"

    # Round-trip: read back via /search to prove content survived intact.
    s = http_client.get(
        "/search",
        params={"q": "Beat's job", "type": "knowledge"},
    )
    assert s.status_code == 200, f"search failed: {s.status_code} {s.text}"


def test_knowledge_ingest_is_idempotent_on_source_file(http_client):
    """Same duplicate is unchanged; changed duplicate is a conflict."""
    body = {
        "content": "first version",
        "source_file": f"/tmp/{RUN_TAG}_idem.md",
        "category": "rca-ingest-test",
    }
    r1 = http_client.post("/knowledge/ingest", json=body)
    assert r1.status_code == 200, r1.text
    id1 = r1.json()["id"]
    assert r1.json()["created"] is True

    r2 = http_client.post("/knowledge/ingest", json=body)
    assert r2.status_code == 200, r2.text
    assert r2.json()["status"] == "unchanged", f"second call must be unchanged: {r2.json()}"
    assert r2.json()["id"] == id1, "must return existing row id on dedup"

    r3 = http_client.post("/knowledge/ingest", json={**body, "content": "changed"})
    assert r3.status_code == 409, r3.text
    assert r3.json()["detail"]["status"] == "conflict"


# ── /lessons/ingest ─────────────────────────────────────────────────────────


def test_lessons_ingest_persists_content_with_apostrophe(http_client):
    body = {
        "summary": f"{RUN_TAG} — agent's apostrophe lesson",
        "detail": "Don't lose these bytes. Beat's body. Two ''doubled'' apostrophes.",
        "category": "rca-ingest-test",
        "agent_name": "migration",
        "importance": 5,
    }
    r = http_client.post("/lessons/ingest", json=body)
    assert r.status_code == 200, f"POST failed: {r.status_code} body={r.text}"
    resp = r.json()
    assert "id" in resp and resp["id"]
    assert resp.get("created") is True


def test_lessons_ingest_is_idempotent_on_summary_category(http_client):
    body = {
        "summary": f"{RUN_TAG}_lesson_idem",
        "detail": "first version",
        "category": "rca-ingest-test",
        "agent_name": "migration",
    }
    r1 = http_client.post("/lessons/ingest", json=body)
    assert r1.status_code == 200, r1.text
    id1 = r1.json()["id"]
    assert r1.json()["created"] is True

    r2 = http_client.post("/lessons/ingest", json=body)
    assert r2.status_code == 200, r2.text
    assert r2.json()["status"] == "unchanged"
    assert r2.json()["id"] == id1

    r3 = http_client.post("/lessons/ingest", json={**body, "detail": "changed"})
    assert r3.status_code == 409, r3.text
    assert r3.json()["detail"]["status"] == "conflict"


# ── /decisions/ingest ───────────────────────────────────────────────────────


def test_decisions_ingest_persists_content_with_apostrophe(http_client):
    body = {
        "summary": f"{RUN_TAG} — agent's apostrophe decision",
        "rationale": "Don't lose this. Beat's body — em-dashes too.",
        "category": "rca-ingest-test",
        "agent_name": "migration",
    }
    r = http_client.post("/decisions/ingest", json=body)
    assert r.status_code == 200, f"POST failed: {r.status_code} body={r.text}"
    resp = r.json()
    assert "id" in resp and resp["id"]
    assert resp.get("created") is True


def test_decisions_ingest_is_idempotent_on_summary_category(http_client):
    body = {
        "summary": f"{RUN_TAG}_decision_idem",
        "rationale": "first version",
        "category": "rca-ingest-test",
        "agent_name": "migration",
    }
    r1 = http_client.post("/decisions/ingest", json=body)
    assert r1.status_code == 200, r1.text
    id1 = r1.json()["id"]
    assert r1.json()["created"] is True

    r2 = http_client.post("/decisions/ingest", json=body)
    assert r2.status_code == 200, r2.text
    assert r2.json()["status"] == "unchanged"
    assert r2.json()["id"] == id1

    r3 = http_client.post("/decisions/ingest", json={**body, "rationale": "changed"})
    assert r3.status_code == 409, r3.text
    assert r3.json()["detail"]["status"] == "conflict"


# ── Cleanup ─────────────────────────────────────────────────────────────────


def test_zz_cleanup_test_rows(http_client):
    """Best-effort cleanup so reruns don't pile up. Runs last (alphabetical)."""
    # Use /admin/sql/exec for cleanup — that's an admin path, not the migrated
    # ingest path being tested. Cleanup failure is non-fatal.
    for table in ("knowledge", "lessons", "decisions"):
        col = "section" if table == "knowledge" else "summary"
        try:
            http_client.post(
                "/admin/sql/exec",
                json={
                    "sql": (
                        f"DELETE FROM {table} "
                        f"WHERE project = '{TEST_PROJECT}' "
                        f"AND {col} LIKE '%{RUN_TAG}%' "
                        f"AND COALESCE(category,'') = 'rca-ingest-test'"
                    )
                },
            )
        except Exception:
            pass
