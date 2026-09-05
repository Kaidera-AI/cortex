"""Live round-trip tests for POST /sessions/ingest.

Background — handoff c8fa34f0 Wave B1, Phase 2 of sql_escape migration.
The legacy cortex-ingest-codex/session/claude-local-state path builds a
multi-statement SQL file in Python and ships it through /admin/sql/exec.
Python's sql_escape there is correct (replace("'", "''")), so message bodies
with apostrophes don't corrupt — but the silencer pattern still hides every
other class of error, the >/dev/null discards row counts, and pivoting to
parameterized asyncpg INSERTs eliminates a whole class of future regressions.

This endpoint atomically writes 4 tables (agents UPSERT + agent_sessions
UPSERT + session_sources UPSERT + messages bulk INSERT) in one transaction,
mirroring what the legacy SQL file did, with proper RETURNING so callers can
report real counts.

Run: pytest .agents/api/tests/test_sessions_ingest.py -v
"""

from __future__ import annotations

import os
import time
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
    reason="CORTEX_TEST_PROJECT and a reachable cortex-api are required for live session tests",
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
        response = client.post(
            "/agents",
            json={
                "name": "kai-test",
                "role": "test-runner",
                "writer_scope": "work",
                "capabilities": {"primary": ["session-ingest-live-tests"]},
            },
        )
        assert response.status_code == 200, (
            "test fixture could not register kai-test; "
            f"status={response.status_code} body={response.text}"
        )
        yield client


# Unique session per test invocation to avoid collisions.
def _new_session_uuid() -> str:
    return str(uuid.uuid4())


# ── Happy path: brand-new session with messages ─────────────────────────────


def test_sessions_ingest_creates_agent_session_source_and_messages(http_client):
    """Single call lands rows in all 4 tables atomically."""
    session_uuid = _new_session_uuid()
    body = {
        "session_uuid": session_uuid,
        "agent": "kai-test",
        "task": f"Test {session_uuid[:8]}",
        "source_path": f"/tmp/kai_sessions_test_{session_uuid}.jsonl",
        "provider": "codex",
        "cwd": "/tmp",
        "git_branch": None,
        "source_kind": "codex-session",
        "metadata": {"test_run": True, "kai_test": session_uuid},
        "messages": [
            {"role": "user", "content": "Beat's first message — em-dash inside.",
             "ts": "2026-05-07T00:00:00Z"},
            {"role": "assistant", "content": "Don't worry about apostrophes.",
             "ts": "2026-05-07T00:00:01Z"},
            {"role": "user", "content": "Plain ASCII works too.",
             "ts": "2026-05-07T00:00:02Z"},
        ],
    }
    r = http_client.post("/sessions/ingest", json=body)
    assert r.status_code == 200, f"POST failed: {r.status_code} body={r.text}"
    resp = r.json()
    assert resp["session_id"] == session_uuid
    assert resp["messages_inserted"] == 3
    assert resp["agent_id"]


def test_sessions_ingest_is_idempotent_on_session_uuid(http_client):
    """Second call with same session_uuid replaces messages, doesn't double-insert.

    Codex/Claude session files are append-only on disk, so the legacy script
    DELETEs all messages for the session_uuid and re-INSERTs. We preserve
    that semantics — a second call with N messages should leave exactly N
    in PG, not 2N.
    """
    session_uuid = _new_session_uuid()
    body = {
        "session_uuid": session_uuid,
        "agent": "kai-test",
        "source_path": f"/tmp/kai_sessions_idem_{session_uuid}.jsonl",
        "provider": "codex",
        "source_kind": "codex-session",
        "messages": [
            {"role": "user", "content": "first version", "ts": "2026-05-07T00:00:00Z"},
            {"role": "assistant", "content": "first reply", "ts": "2026-05-07T00:00:01Z"},
        ],
    }
    r1 = http_client.post("/sessions/ingest", json=body)
    assert r1.status_code == 200, r1.text
    assert r1.json()["messages_inserted"] == 2

    # Resend with 3 messages. Should DELETE prior 2 + INSERT 3 → final count 3.
    body["messages"] = [
        {"role": "user", "content": "first version", "ts": "2026-05-07T00:00:00Z"},
        {"role": "assistant", "content": "first reply", "ts": "2026-05-07T00:00:01Z"},
        {"role": "user", "content": "appended later", "ts": "2026-05-07T00:00:02Z"},
    ]
    r2 = http_client.post("/sessions/ingest", json=body)
    assert r2.status_code == 200, r2.text
    assert r2.json()["messages_inserted"] == 3
    assert r2.json()["session_id"] == session_uuid


def test_sessions_ingest_is_idempotent_on_source_path(http_client):
    """A reimported source file can keep its existing session id.

    Older planners and newer planners may derive different stable UUIDs for the
    same transcript path. Source path is still the real file identity, so the
    endpoint should update the existing session instead of surfacing a 500 from
    the unique source_path constraint.
    """
    first_uuid = _new_session_uuid()
    second_uuid = _new_session_uuid()
    source_path = f"/tmp/kai_sessions_source_path_{first_uuid}.jsonl"
    body = {
        "session_uuid": first_uuid,
        "agent": "kai-test",
        "source_path": source_path,
        "provider": "codex",
        "source_kind": "codex-session",
        "messages": [
            {"role": "user", "content": "first source import", "ts": "2026-05-07T00:00:00Z"},
        ],
    }
    r1 = http_client.post("/sessions/ingest", json=body)
    assert r1.status_code == 200, r1.text
    assert r1.json()["session_id"] == first_uuid

    body["session_uuid"] = second_uuid
    body["messages"] = [
        {"role": "user", "content": "updated source import", "ts": "2026-05-07T00:00:00Z"},
        {"role": "assistant", "content": "updated reply", "ts": "2026-05-07T00:00:01Z"},
    ]
    r2 = http_client.post("/sessions/ingest", json=body)
    assert r2.status_code == 200, r2.text
    assert r2.json()["session_id"] == first_uuid
    assert r2.json()["messages_inserted"] == 2


def test_sessions_ingest_rejects_cross_project_session_uuid(http_client):
    """A provider session UUID cannot be reassigned to a different project."""
    session_uuid = _new_session_uuid()
    body = {
        "session_uuid": session_uuid,
        "agent": "kai-test",
        "source_path": f"/tmp/kai_sessions_cross_{session_uuid}.jsonl",
        "provider": "codex",
        "source_kind": "codex-session",
        "messages": [
            {"role": "user", "content": "original project", "ts": "2026-05-07T00:00:00Z"},
        ],
    }

    r1 = http_client.post("/sessions/ingest", json=body)
    assert r1.status_code == 200, r1.text

    with httpx.Client(
        base_url=CORTEX_API,
        timeout=15.0,
        headers={
            "X-Project": "asw-connect",
            "X-Agent-Name": TEST_AGENT,
            "X-Cortex-Admin-Token": os.environ.get(
                "CORTEX_ADMIN_TOKEN", "cortex-local-admin"
            ),
        },
    ) as other_project:
        r2 = other_project.post("/sessions/ingest", json=body)

    assert r2.status_code == 409, r2.text
    assert "already belongs to project" in r2.text


def test_sessions_ingest_rejects_unregistered_project_before_mutation():
    """Wrong project scope fails with a clear 4xx before any session rows land."""
    session_uuid = _new_session_uuid()
    project = f"kai-unregistered-{uuid.uuid4().hex[:8]}"
    body = {
        "session_uuid": session_uuid,
        "agent": "kai-test",
        "source_path": f"/tmp/kai_sessions_wrong_project_{session_uuid}.jsonl",
        "provider": "codex",
        "source_kind": "codex-session",
        "messages": [
            {"role": "user", "content": "wrong project", "ts": "2026-05-07T00:00:00Z"},
        ],
    }
    with httpx.Client(
        base_url=CORTEX_API,
        timeout=15.0,
        headers={
            "X-Project": project,
            "X-Agent-Name": TEST_AGENT,
            "X-Cortex-Admin-Token": os.environ.get(
                "CORTEX_ADMIN_TOKEN", "cortex-local-admin"
            ),
        },
    ) as wrong_project:
        r = wrong_project.post("/sessions/ingest", json=body)

    assert r.status_code == 404, r.text
    assert "not registered in Cortex" in r.text


def test_project_onboarding_api_registers_custom_agent_then_ingests_session(http_client):
    """A new arbitrary project + custom agent can onboard through API then ingest."""
    project = f"kai-portable-{uuid.uuid4().hex[:8]}"
    session_uuid = _new_session_uuid()
    agent = "nova-agent"
    source_path = f"/tmp/kai_sessions_portable_{session_uuid}.jsonl"

    try:
        create = http_client.post(
            "/projects",
            json={
                "project_key": project,
                "display_name": "Kai Portable Test",
                "repo_root": f"/tmp/{project}",
                "default_agent": agent,
                "agents": [
                    {
                        "name": agent,
                        "role": "creative-director",
                        "capabilities": {"primary": ["campaign-strategy"]},
                    }
                ],
            },
        )
        assert create.status_code == 200, create.text
        created = create.json()
        assert created["project_key"] == project
        assert created["default_agent"] == agent
        assert created["agents"][0]["role"] == "creative-director"

        with httpx.Client(
            base_url=CORTEX_API,
            timeout=15.0,
            headers={
                "X-Project": project,
                "X-Agent-Name": agent,
                "X-Cortex-Admin-Token": os.environ.get(
                    "CORTEX_ADMIN_TOKEN", "cortex-local-admin"
                ),
            },
        ) as project_client:
            ingest = project_client.post(
                "/sessions/ingest",
                json={
                    "session_uuid": session_uuid,
                    "agent": agent,
                    "source_path": source_path,
                    "provider": "codex",
                    "source_kind": "codex-session",
                    "messages": [
                        {
                            "role": "user",
                            "content": "portable custom project ingest",
                            "ts": "2026-05-07T00:00:00Z",
                        }
                    ],
                },
            )
        assert ingest.status_code == 200, ingest.text
        assert ingest.json()["messages_inserted"] == 1
    finally:
        cleanup_sql = f"""
        DELETE FROM messages WHERE project = '{project}';
        DELETE FROM agent_sessions WHERE project = '{project}';
        DELETE FROM session_sources WHERE project = '{project}';
        DELETE FROM agents WHERE project = '{project}';
        DELETE FROM roles WHERE project = '{project}';
        DELETE FROM cortex_project_paths WHERE project_key = '{project}';
        DELETE FROM cortex_projects WHERE project_key = '{project}';
        """
        try:
            http_client.post("/admin/sql/exec", json={"sql": cleanup_sql})
        except Exception:
            pass


def test_sessions_ingest_rejects_noisy_inferred_agent_names(http_client):
    """Session ingest validates body.agent, not only caller headers."""
    session_uuid = _new_session_uuid()
    body = {
        "session_uuid": session_uuid,
        "agent": "you",
        "source_path": f"/tmp/kai_sessions_bad_agent_{session_uuid}.jsonl",
        "provider": "claude",
        "source_kind": "claude-session",
        "messages": [
            {"role": "user", "content": "hi you are assistant", "ts": "2026-05-07T00:00:00Z"},
        ],
    }
    r = http_client.post("/sessions/ingest", json=body)
    assert r.status_code == 400, r.text
    assert "Blocked agent name" in r.text


def test_sessions_ingest_preserves_apostrophes_em_dashes_unicode(http_client):
    """The B-bug killer for chat ingest: special chars survive the round-trip."""
    session_uuid = _new_session_uuid()
    body = {
        "session_uuid": session_uuid,
        "agent": "kai-test",
        "source_path": f"/tmp/kai_sessions_uni_{session_uuid}.jsonl",
        "provider": "codex",
        "source_kind": "codex-session",
        "messages": [
            {"role": "user",
             "content": "Beat's job — agent's apostrophe • emoji 🚀 • quotes \"in\" middle",
             "ts": "2026-05-07T00:00:00Z"},
        ],
    }
    r = http_client.post("/sessions/ingest", json=body)
    assert r.status_code == 200, f"POST failed: {r.status_code} body={r.text}"
    assert r.json()["messages_inserted"] == 1


def test_sessions_ingest_handles_empty_messages_array(http_client):
    """A session with metadata but no messages still creates the session row."""
    session_uuid = _new_session_uuid()
    body = {
        "session_uuid": session_uuid,
        "agent": "kai-test",
        "source_path": f"/tmp/kai_sessions_empty_{session_uuid}.jsonl",
        "provider": "codex",
        "source_kind": "codex-session",
        "messages": [],
    }
    r = http_client.post("/sessions/ingest", json=body)
    assert r.status_code == 200, r.text
    assert r.json()["messages_inserted"] == 0
    assert r.json()["session_id"] == session_uuid


# ── Cleanup ─────────────────────────────────────────────────────────────────


def test_zz_cleanup_test_rows(http_client):
    """Clear test rows so reruns don't accumulate."""
    try:
        # Delete by source_path pattern — only test fixtures match.
        for table_clause in [
            "DELETE FROM messages WHERE session_id IN "
            "(SELECT session_id FROM session_sources WHERE source_path LIKE '/tmp/kai_sessions_%')",
            "DELETE FROM agent_sessions WHERE id IN "
            "(SELECT session_id FROM session_sources WHERE source_path LIKE '/tmp/kai_sessions_%')",
            "DELETE FROM session_sources WHERE source_path LIKE '/tmp/kai_sessions_%'",
            f"DELETE FROM agents WHERE name = 'kai-test' AND project = '{TEST_PROJECT}'",
        ]:
            http_client.post("/admin/sql/exec", json={"sql": table_clause})
    except Exception:
        pass
