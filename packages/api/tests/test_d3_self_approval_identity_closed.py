"""K0 finding 1 (v0.2.006 lane, 2026-09-04): no agent self-approval by any path.

Drives the REAL endpoints (return_handoff) through a fake connection and
asserts the governed semantics the fold review proved absent at 19883f24:

- an agent returning its own (self-delegated) handoff never auto-completes; an
  unknown, ambiguous or unavailable identity is treated as AGENT, never human
  (fail closed); with no independent registered reviewer the return itself
  fails closed (409) instead of creating a self-addressed handback;
- when a registered human distinct from the returner exists, the handback is
  routed to that independent identity, never back to the returner;
- accepting a completion handback re-enforces independence: the original
  worker cannot accept the review of its own work even if it somehow holds
  the handback (403), and self_review_required rows require a human accepter;
- human self-returns still auto-complete (humans govern themselves).

Written RED-first: every test fails at the reviewed fold tip and passes only
with the rework in place.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
from starlette.requests import Request


API_MAIN_PATH = Path(__file__).resolve().parents[1] / "main.py"
PARENT_ID = "dddddddd-4444-4444-8444-dddddddddddd"
HANDBACK_ID = "eeeeeeee-5555-4555-8555-eeeeeeeeeeee"


def load_api(module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, API_MAIN_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.path.insert(0, str(API_MAIN_PATH.parent))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(API_MAIN_PATH.parent))
    return module


class FakeTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeAcquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


def parent_row(**overrides):
    """A self-delegated task: ren delegated to ren; ren holds the claim."""
    row = {
        "id": PARENT_ID,
        "project": "kaidera-os",
        "status": "claimed",
        "kind": "task",
        "reply_to_handoff_id": None,
        "returned_at": None,
        "completion_report": {},
        "from_agent": "ren@kaidera-os",
        "from_role": "lead",
        "to_role": "full-stack-developer",
        "to_agent": "ren@kaidera-os",
        "priority": "high",
        "summary": "Self-delegated queue task",
        "branch": None,
        "files_changed": [],
        "verification": None,
        "next_steps": None,
        "context": None,
        "parent_goal_id": None,
        "acceptance": {},
        "evidence": {},
        "retry": {},
        "escalation": {},
        "claimed_by": "ren@kaidera-os",
        "claimed_at": "2026-09-04T00:00:00+00:00",
        "retry_count": 0,
        "completed_at": None,
        "terminal_reason": None,
    }
    row.update(overrides)
    return row


class D3FakeConn:
    """Answers the return/handback SQL shapes plus a cortex_actors identity table.

    `actors` maps slug -> list of kinds, mirroring the (project_id, slug, kind)
    identity uniqueness: a slug with two kinds is the ambiguity case.
    """

    def __init__(self, *, actors=None, agents=None, default_agent="ren"):
        self.rows = {PARENT_ID: parent_row()}
        self.events: list[dict] = []
        self.handback_insert_count = 0
        self.actors = actors or {}
        self.agents = agents or {"ren": "full-stack-developer", "kai": "full-stack-developer"}
        self.default_agent = default_agent

    def transaction(self):
        return FakeTransaction()

    async def fetch(self, sql, *args):
        if "FROM handoffs" in sql and "LIMIT 2" in sql:
            project, prefix = args[:2]
            return [
                row
                for row in self.rows.values()
                if row["project"] == project and row["id"].startswith(prefix)
            ][:2]
        if "FROM cortex_actors" in sql:
            slug = str(args[1]).lower()
            return [{"kind": kind} for kind in self.actors.get(slug, [])]
        raise AssertionError(f"Unexpected fetch SQL: {sql}")

    async def fetchrow(self, sql, *args):
        if "FROM handoffs" in sql and "FOR UPDATE" in sql:
            return self.rows.get(str(args[0]))
        if "kind = 'completion_handback'" in sql and "LIMIT 1" in sql:
            project, parent_id = args[:2]
            return next(
                (
                    {"id": row["id"], "status": row["status"]}
                    for row in self.rows.values()
                    if row["project"] == project
                    and row["kind"] == "completion_handback"
                    and row["reply_to_handoff_id"] == parent_id
                    and row["status"] in {"pending", "claimed"}
                ),
                None,
            )
        if "FROM team_events" in sql and "WHERE id = $1" in sql:
            event = self.events[int(args[0]) - 1]
            return {
                "id": args[0],
                "project": event["project"],
                "agent_name": event["agent_name"],
                "event_type": event["event_type"],
                "summary": event["summary"],
                "files": event["files"],
            }
        if "FROM cortex_actors" in sql:
            # independent human reviewer read, excluding the returning actor
            slug = str(args[1]).lower() if len(args) > 1 else ""
            for name, kinds in self.actors.items():
                if "human" in kinds and name != slug:
                    return {"slug": name}
            return None
        if "SELECT lower(name) AS name, role" in sql:
            default = str(args[1]).lower()
            if default in self.agents:
                return {"name": default, "role": self.agents[default]}
            return None
        if "FROM agents a" in sql and "lower(name) = $2" in sql:
            role = self.agents.get(str(args[1]).lower())
            return {"role": role} if role else None
        if "SELECT default_agent FROM cortex_projects" in sql:
            return {"default_agent": self.default_agent}
        raise AssertionError(f"Unexpected fetchrow SQL: {sql}")

    async def execute(self, sql, *args):
        if "pg_advisory_xact_lock" in sql or "pg_notify" in sql:
            return "SELECT 1"
        if "status = 'pending'" in sql and "retry_count" in sql:
            row = self.rows[str(args[0])]
            row.update(status="pending", claimed_by=None, claimed_at=None,
                       completed_at=None, retry_count=row["retry_count"] + 1)
            return "UPDATE 1"
        if "status = 'returned'" in sql and "completion_report = $1" in sql:
            row = self.rows[str(args[1])]
            row.update(status="returned", returned_at="now",
                       completion_report=json.loads(args[0]))
            return "UPDATE 1"
        if "status = 'completed'" in sql and "completion_report = $1" in sql:
            row = self.rows[str(args[1])]
            row.update(status="completed", completed_at="now",
                       completion_report=json.loads(args[0]))
            if "returned_at = NOW()" in sql:
                row["returned_at"] = "now"
            return "UPDATE 1"
        if "SET status = 'completed', completed_at = NOW()" in sql:
            self.rows[str(args[0])].update(status="completed", completed_at="now")
            return "UPDATE 1"
        raise AssertionError(f"Unexpected execute SQL: {sql}")

    async def fetchval(self, sql, *args):
        if "INSERT INTO team_events" in sql:
            self.events.append({
                "project": args[0], "agent_name": args[1], "event_type": args[2],
                "summary": args[3], "detail": json.loads(args[4]), "files": args[5],
            })
            return len(self.events)
        if "INSERT INTO handoffs" in sql:
            self.handback_insert_count += 1
            self.rows[HANDBACK_ID] = {
                "id": HANDBACK_ID,
                "project": args[0],
                "status": "pending",
                "kind": "completion_handback",
                "reply_to_handoff_id": args[1],
                "returned_at": None,
                "completion_report": {},
                "from_agent": args[2],
                "from_role": args[3],
                "to_role": args[4],
                "to_agent": args[5],
                "priority": args[6],
                "summary": args[7],
                "branch": args[8],
                "files_changed": [],
                "verification": None,
                "next_steps": None,
                "context": args[9],
                "parent_goal_id": args[10],
                "acceptance": json.loads(args[11]),
                "evidence": json.loads(args[12]),
                "retry": json.loads(args[13]),
                "escalation": json.loads(args[14]),
                "claimed_by": None,
                "claimed_at": None,
                "retry_count": 0,
                "completed_at": None,
                "terminal_reason": None,
            }
            return HANDBACK_ID
        raise AssertionError(f"Unexpected fetchval SQL: {sql}")


@pytest.fixture
def api_module(monkeypatch):
    module = load_api("cortex_api_d3_self_approval_test")

    async def registered_project(project):
        assert project == "kaidera-os"
        return {"project_key": project}

    async def writer(project, agent, scope="work"):
        assert project == "kaidera-os"
        assert scope == "work"

    async def compound(agent, project):
        return f"{agent.split('@', 1)[0]}@{project}"

    monkeypatch.setattr(module, "require_registered_project", registered_project)
    monkeypatch.setattr(module, "require_registered_agent_writer", writer)
    monkeypatch.setattr(module, "compound_agent", compound)
    return module


def report(module, *, decision=None, **overrides):
    return module.HandoffReturn(
        outcome="completed",
        summary="Implementation and tests complete",
        decision=decision,
        tests_run=[{"name": "pytest", "status": "passed"}],
        **overrides,
    )


def http_request():
    request = Request({"type": "http", "headers": []})
    request.state.jwt_claims = {}
    return request


@pytest.mark.asyncio
async def test_unknown_identity_never_auto_completes_a_self_return(api_module, monkeypatch):
    """Identity lookup absent => treated as agent (fail closed), never human.

    Pre-rework: actor_kind_for returned None and the same-recipient branch
    auto-completed (probe: unknown_actor_lookup completed), then the lead
    fallback addressed the handback to the returner itself. Post-rework the
    return fails closed: no independent reviewer, no self-addressed handback.
    """
    conn = D3FakeConn(actors={})  # ren has NO cortex_actors row
    monkeypatch.setattr(api_module, "acquire_scoped", lambda _p: FakeAcquire(conn))

    with pytest.raises(api_module.HTTPException) as exc:
        await api_module.return_handoff(
            PARENT_ID, report(api_module), request=http_request(),
            x_agent="ren", x_project="kaidera-os",
        )

    assert exc.value.status_code == 409
    assert conn.rows[PARENT_ID]["status"] != "completed"
    assert conn.handback_insert_count == 0


@pytest.mark.asyncio
async def test_agent_self_return_routes_to_an_independent_identity(api_module, monkeypatch):
    """With a registered human present, the handback must not go to the returner.

    Pre-rework probe: same-agent handback recipient=('ren@kaidera-os', 'lead',
    False) — the handback was addressed to the agent itself.
    """
    conn = D3FakeConn(actors={"ren": ["agent"], "cto": ["human"]})
    monkeypatch.setattr(api_module, "acquire_scoped", lambda _p: FakeAcquire(conn))

    await api_module.return_handoff(
        PARENT_ID, report(api_module), request=http_request(),
        x_agent="ren", x_project="kaidera-os",
    )

    handback = conn.rows[HANDBACK_ID]
    assert str(handback["to_agent"] or "").lower().startswith("ren") is False, (
        f"self-approval path: handback addressed to the returning agent {handback['to_agent']}"
    )
    assert str(handback["to_agent"] or "").lower().startswith("cto"), (
        "independent human reviewer must receive the self-review handback"
    )


@pytest.mark.asyncio
async def test_original_agent_cannot_accept_its_own_completion_handback(api_module, monkeypatch):
    """Accept re-enforces independence at the handback, not only at creation."""
    conn = D3FakeConn(actors={"ren": ["agent"], "cto": ["human"]})
    monkeypatch.setattr(api_module, "acquire_scoped", lambda _p: FakeAcquire(conn))

    # Seed the hostile state directly: a completion handback for ren's own work,
    # somehow claimed by ren.
    conn.rows[HANDBACK_ID] = {
        **parent_row(),
        "id": HANDBACK_ID,
        "kind": "completion_handback",
        "reply_to_handoff_id": PARENT_ID,
        "from_agent": "ren@kaidera-os",
        "to_agent": "ren@kaidera-os",
        "status": "claimed",
        "claimed_by": "ren@kaidera-os",
    }

    with pytest.raises(api_module.HTTPException) as exc:
        await api_module.return_handoff(
            HANDBACK_ID, report(api_module, decision="accept"),
            request=http_request(), x_agent="ren", x_project="kaidera-os",
        )
    assert exc.value.status_code in (403, 409)
    assert conn.rows[PARENT_ID]["status"] != "completed"


@pytest.mark.asyncio
async def test_human_self_return_still_auto_completes(api_module, monkeypatch):
    """Humans govern themselves: a resolved human self-return auto-completes."""
    conn = D3FakeConn(actors={"ren": ["human"]})
    monkeypatch.setattr(api_module, "acquire_scoped", lambda _p: FakeAcquire(conn))

    result = await api_module.return_handoff(
        PARENT_ID, report(api_module), request=http_request(),
        x_agent="ren", x_project="kaidera-os",
    )

    assert result.get("auto_accepted") is True
    assert conn.rows[PARENT_ID]["status"] == "completed"


@pytest.mark.asyncio
async def test_ambiguous_identity_fails_closed(api_module, monkeypatch):
    """A slug registered as BOTH agent and human must never resolve to human: the
    self-return fails closed exactly like an unknown identity."""
    conn = D3FakeConn(actors={"ren": ["agent", "human"]})
    monkeypatch.setattr(api_module, "acquire_scoped", lambda _p: FakeAcquire(conn))

    with pytest.raises(api_module.HTTPException) as exc:
        await api_module.return_handoff(
            PARENT_ID, report(api_module), request=http_request(),
            x_agent="ren", x_project="kaidera-os",
        )

    assert exc.value.status_code == 409
    assert conn.handback_insert_count == 0
