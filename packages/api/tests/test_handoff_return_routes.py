from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
from starlette.requests import Request


API_MAIN_PATH = Path(__file__).resolve().parents[1] / "main.py"
PARENT_ID = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"
HANDBACK_ID = "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb"
SECOND_HANDBACK_ID = "cccccccc-3333-4333-8333-cccccccccccc"


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


def task_row(**overrides):
    row = {
        "id": PARENT_ID,
        "project": "kaidera-os",
        "status": "claimed",
        "kind": "task",
        "reply_to_handoff_id": None,
        "returned_at": None,
        "completion_report": {},
        "from_agent": "ren@kaidera-os",
        "from_role": "cpo",
        "to_role": "full-stack-developer",
        "to_agent": "kai@kaidera-os",
        "priority": "high",
        "summary": "Implement atomic completion handbacks",
        "branch": "feature/handback",
        "files_changed": [],
        "verification": "pytest",
        "next_steps": None,
        "context": None,
        "parent_goal_id": "goal-1",
        "acceptance": {"criteria": ["tests pass"]},
        "evidence": {},
        "retry": {},
        "escalation": {},
        "claimed_by": "kai@kaidera-os",
        "claimed_at": "2026-07-26T12:00:00+00:00",
        "retry_count": 0,
        "completed_at": None,
        "terminal_reason": None,
    }
    row.update(overrides)
    return row


def test_handoff_policy_decodes_asyncpg_json_strings(api_module):
    policy = {"criteria": ["tests pass"]}

    assert api_module.handoff_policy(json.dumps(policy)) == policy
    assert api_module.handoff_policy(None) == {}
    with pytest.raises(ValueError, match="JSON object"):
        api_module.handoff_policy('["not", "an", "object"]')


class FakeReturnConn:
    def __init__(self, parent=None, *, active_agents=None, default_agent="ren", actors=None):
        self.rows = {PARENT_ID: parent or task_row()}
        self.events: list[dict] = []
        self.handback_insert_count = 0
        self.active_agents = active_agents or {
            "ren": "cpo",
            "kai": "full-stack-developer",
        }
        self.default_agent = default_agent
        self.actors = actors or {}

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
        if "SELECT lower(name) AS name, role" in sql:
            default = str(args[1]).lower()
            if default in self.active_agents:
                return {
                    "name": default,
                    "role": self.active_agents[default],
                }
            if self.active_agents:
                name, role = min(
                    self.active_agents.items(),
                    key=lambda item: (
                        item[1].lower()
                        not in {"lead", "cpo", "cmo", "cto", "pm"},
                        item[0],
                    ),
                )
                return {"name": name, "role": role}
            return None
        if "FROM agents a" in sql and "lower(name) = $2" in sql:
            role = self.active_agents.get(str(args[1]).lower())
            return {"role": role} if role else None
        if "SELECT default_agent FROM cortex_projects" in sql:
            return {"default_agent": self.default_agent}
        if "FROM cortex_actors" in sql and "kind = 'human'" in sql:
            excluded = str(args[1]).lower()
            for name, kinds in self.actors.items():
                if "human" in kinds and name != excluded:
                    return {"slug": name}
            return None
        raise AssertionError(f"Unexpected fetchrow SQL: {sql}")

    async def execute(self, sql, *args):
        if "pg_advisory_xact_lock" in sql or "pg_notify" in sql:
            return "SELECT 1"
        if "status = 'pending'" in sql and "retry_count" in sql:
            row = self.rows[str(args[0])]
            row.update(
                status="pending",
                claimed_by=None,
                claimed_at=None,
                completed_at=None,
                retry_count=row["retry_count"] + 1,
            )
            return "UPDATE 1"
        if "status = 'returned'" in sql and "completion_report = $1" in sql:
            row = self.rows[str(args[1])]
            row.update(
                status="returned",
                returned_at="now",
                completion_report=json.loads(args[0]),
            )
            return "UPDATE 1"
        if (
            "status = 'completed'" in sql
            and "completion_report = $1" in sql
        ):
            row = self.rows[str(args[1])]
            row.update(
                status="completed",
                completed_at="now",
                completion_report=json.loads(args[0]),
            )
            if "returned_at = NOW()" in sql:
                row["returned_at"] = "now"
            return "UPDATE 1"
        if "SET status = 'completed', completed_at = NOW()" in sql:
            self.rows[str(args[0])].update(status="completed", completed_at="now")
            return "UPDATE 1"
        raise AssertionError(f"Unexpected execute SQL: {sql}")

    async def fetchval(self, sql, *args):
        if "INSERT INTO team_events" in sql:
            self.events.append(
                {
                    "project": args[0],
                    "agent_name": args[1],
                    "event_type": args[2],
                    "summary": args[3],
                    "detail": json.loads(args[4]),
                    "files": args[5],
                }
            )
            return len(self.events)
        if "INSERT INTO handoffs" in sql:
            self.handback_insert_count += 1
            handback_id = (
                HANDBACK_ID
                if self.handback_insert_count == 1
                else SECOND_HANDBACK_ID
            )
            self.rows[handback_id] = {
                "id": handback_id,
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
            return handback_id
        raise AssertionError(f"Unexpected fetchval SQL: {sql}")


@pytest.fixture
def api_module(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "cortex_api_handoff_return_test",
        API_MAIN_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    async def registered_project(project):
        assert project == "kaidera-os"
        return {"project_key": project}

    async def writer(project, agent, scope="work"):
        assert project == "kaidera-os"
        assert agent.split("@", 1)[0] in {"kai", "ren"}
        assert scope == "work"

    async def compound(agent, project):
        return f"{agent.split('@', 1)[0]}@{project}"

    monkeypatch.setattr(module, "require_registered_project", registered_project)
    monkeypatch.setattr(module, "require_registered_agent_writer", writer)
    monkeypatch.setattr(module, "compound_agent", compound)
    return module


def report(api_module, *, decision=None):
    return api_module.HandoffReturn(
        outcome="completed",
        summary="Implementation and tests complete",
        decision=decision,
        tests_run=[{"name": "pytest", "status": "passed"}],
    )


def http_request(jwt_agent: str | None = None) -> Request:
    request = Request({"type": "http", "headers": []})
    request.state.jwt_claims = (
        {"agent_name": jwt_agent}
        if jwt_agent
        else {}
    )
    return request


@pytest.mark.asyncio
async def test_return_creates_one_handback_and_is_idempotent(api_module, monkeypatch):
    conn = FakeReturnConn()
    monkeypatch.setattr(api_module, "acquire_scoped", lambda _project: FakeAcquire(conn))

    first = await api_module.return_handoff(
        PARENT_ID[:8],
        report(api_module),
        request=http_request(),
        x_agent="kai",
        x_project="kaidera-os",
    )
    second = await api_module.return_handoff(
        PARENT_ID[:8],
        report(api_module),
        request=http_request(),
        x_agent="kai",
        x_project="kaidera-os",
    )

    assert first["status"] == "returned"
    assert first["handback_id"] == HANDBACK_ID
    assert second["deduped"] is True
    assert second["handback_id"] == HANDBACK_ID
    assert conn.handback_insert_count == 1
    assert conn.rows[PARENT_ID]["status"] == "returned"
    assert conn.rows[HANDBACK_ID]["to_agent"] == "ren@kaidera-os"
    assert [event["event_type"] for event in conn.events] == [
        "handoff_returned",
        "handoff_created",
    ]


@pytest.mark.asyncio
async def test_handback_accept_closes_parent_without_reverse_handback(
    api_module,
    monkeypatch,
):
    conn = FakeReturnConn()
    monkeypatch.setattr(api_module, "acquire_scoped", lambda _project: FakeAcquire(conn))
    await api_module.return_handoff(
        PARENT_ID,
        report(api_module),
        request=http_request(),
        x_agent="kai",
        x_project="kaidera-os",
    )
    conn.rows[HANDBACK_ID].update(
        status="claimed",
        claimed_by="ren@kaidera-os",
        claimed_at="now",
    )

    result = await api_module.return_handoff(
        HANDBACK_ID,
        report(api_module, decision="accept"),
        request=http_request(),
        x_agent="ren",
        x_project="kaidera-os",
    )

    assert result["accepted"] is True
    assert conn.rows[PARENT_ID]["status"] == "completed"
    assert conn.rows[HANDBACK_ID]["status"] == "completed"
    assert conn.handback_insert_count == 1


@pytest.mark.asyncio
async def test_duplicate_handback_accept_is_event_idempotent(api_module, monkeypatch):
    conn = FakeReturnConn()
    monkeypatch.setattr(api_module, "acquire_scoped", lambda _project: FakeAcquire(conn))
    await api_module.return_handoff(
        PARENT_ID,
        report(api_module),
        request=http_request(),
        x_agent="kai",
        x_project="kaidera-os",
    )
    conn.rows[HANDBACK_ID].update(
        status="claimed",
        claimed_by="ren@kaidera-os",
        claimed_at="now",
    )
    first = await api_module.return_handoff(
        HANDBACK_ID,
        report(api_module, decision="accept"),
        request=http_request(),
        x_agent="ren",
        x_project="kaidera-os",
    )
    event_count = len(conn.events)

    second = await api_module.return_handoff(
        HANDBACK_ID,
        report(api_module, decision="accept"),
        request=http_request(),
        x_agent="ren",
        x_project="kaidera-os",
    )

    assert first["accepted"] is True
    assert second["accepted"] is True
    assert second["deduped"] is True
    assert len(conn.events) == event_count
    assert conn.handback_insert_count == 1


@pytest.mark.asyncio
async def test_handback_rework_requeues_parent(api_module, monkeypatch):
    conn = FakeReturnConn()
    monkeypatch.setattr(api_module, "acquire_scoped", lambda _project: FakeAcquire(conn))
    await api_module.return_handoff(
        PARENT_ID,
        report(api_module),
        request=http_request(),
        x_agent="kai",
        x_project="kaidera-os",
    )
    conn.rows[HANDBACK_ID].update(
        status="claimed",
        claimed_by="ren@kaidera-os",
        claimed_at="now",
    )

    result = await api_module.return_handoff(
        HANDBACK_ID,
        report(api_module, decision="rework"),
        request=http_request(),
        x_agent="ren",
        x_project="kaidera-os",
    )

    assert result["rework_requested"] is True
    assert conn.rows[PARENT_ID]["status"] == "pending"
    assert conn.rows[PARENT_ID]["retry_count"] == 1
    assert conn.rows[HANDBACK_ID]["status"] == "completed"


@pytest.mark.asyncio
async def test_duplicate_handback_rework_preserves_rework_semantics(
    api_module,
    monkeypatch,
):
    """Outbox replay after Cortex commit must not turn rework into acceptance."""
    conn = FakeReturnConn()
    monkeypatch.setattr(api_module, "acquire_scoped", lambda _project: FakeAcquire(conn))
    await api_module.return_handoff(
        PARENT_ID,
        report(api_module),
        request=http_request(),
        x_agent="kai",
        x_project="kaidera-os",
    )
    conn.rows[HANDBACK_ID].update(
        status="claimed",
        claimed_by="ren@kaidera-os",
        claimed_at="now",
    )
    first = await api_module.return_handoff(
        HANDBACK_ID,
        report(api_module, decision="rework"),
        request=http_request(),
        x_agent="ren",
        x_project="kaidera-os",
    )
    second = await api_module.return_handoff(
        HANDBACK_ID,
        report(api_module, decision="rework"),
        request=http_request(),
        x_agent="ren",
        x_project="kaidera-os",
    )

    assert first["rework_requested"] is True
    assert second["rework_requested"] is True
    assert second["status"] == "pending"
    assert second["deduped"] is True
    assert second.get("accepted") is not True
    assert conn.rows[PARENT_ID]["status"] == "pending"


@pytest.mark.asyncio
async def test_completed_task_replay_reports_accepted(api_module, monkeypatch):
    conn = FakeReturnConn(
        task_row(
            status="completed",
            completion_report={
                "outcome": "completed",
                "summary": "already accepted",
            },
        )
    )
    monkeypatch.setattr(api_module, "acquire_scoped", lambda _project: FakeAcquire(conn))

    result = await api_module.return_handoff(
        PARENT_ID,
        report(api_module),
        request=http_request(),
        x_agent="kai",
        x_project="kaidera-os",
    )

    assert result["accepted"] is True
    assert result["status"] == "completed"
    assert result["parent_handoff_id"] == PARENT_ID
    assert result["deduped"] is True


@pytest.mark.asyncio
async def test_rework_allows_a_fresh_handback_cycle(api_module, monkeypatch):
    conn = FakeReturnConn()
    monkeypatch.setattr(api_module, "acquire_scoped", lambda _project: FakeAcquire(conn))
    await api_module.return_handoff(
        PARENT_ID,
        report(api_module),
        request=http_request(),
        x_agent="kai",
        x_project="kaidera-os",
    )
    conn.rows[HANDBACK_ID].update(
        status="claimed",
        claimed_by="ren@kaidera-os",
        claimed_at="now",
    )
    await api_module.return_handoff(
        HANDBACK_ID,
        report(api_module, decision="rework"),
        request=http_request(),
        x_agent="ren",
        x_project="kaidera-os",
    )
    conn.rows[PARENT_ID].update(
        status="claimed",
        claimed_by="kai@kaidera-os",
        claimed_at="later",
    )

    second_cycle = await api_module.return_handoff(
        PARENT_ID,
        report(api_module),
        request=http_request(),
        x_agent="kai",
        x_project="kaidera-os",
    )

    assert second_cycle["status"] == "returned"
    assert second_cycle["handback_id"] == SECOND_HANDBACK_ID
    assert conn.handback_insert_count == 2
    assert conn.rows[HANDBACK_ID]["status"] == "completed"
    assert conn.rows[SECOND_HANDBACK_ID]["status"] == "pending"


@pytest.mark.asyncio
async def test_self_delegated_agent_return_is_routed_not_auto_accepted(api_module, monkeypatch):
    """K0 rework (D3 closure): an agent's self-return never auto-completes; with no
    independent registered identity the real endpoints fail closed (409)."""
    conn = FakeReturnConn(
        task_row(
            from_agent="ren@kaidera-os",
            to_agent="ren@kaidera-os",
            claimed_by="ren@kaidera-os",
        ),
        actors={"ren": ["agent"]},
    )
    monkeypatch.setattr(api_module, "acquire_scoped", lambda _project: FakeAcquire(conn))

    with pytest.raises(api_module.HTTPException) as exc:
        await api_module.return_handoff(
            PARENT_ID,
            report(api_module),
            request=http_request(),
            x_agent="ren",
            x_project="kaidera-os",
        )

    assert exc.value.status_code == 409
    assert conn.rows[PARENT_ID]["status"] == "claimed"
    assert conn.handback_insert_count == 0


@pytest.mark.asyncio
async def test_self_delegated_human_return_auto_accepts_without_handback(api_module, monkeypatch):
    """Humans govern themselves: a resolved HUMAN self-return still auto-completes."""
    conn = FakeReturnConn(
        task_row(
            from_agent="ren@kaidera-os",
            to_agent="ren@kaidera-os",
            claimed_by="ren@kaidera-os",
        ),
        actors={"ren": ["human"]},
    )
    monkeypatch.setattr(api_module, "acquire_scoped", lambda _project: FakeAcquire(conn))

    result = await api_module.return_handoff(
        PARENT_ID,
        report(api_module),
        request=http_request(),
        x_agent="ren",
        x_project="kaidera-os",
    )

    assert result["auto_accepted"] is True
    assert result["handback_id"] is None
    assert conn.rows[PARENT_ID]["status"] == "completed"
    assert conn.handback_insert_count == 0


@pytest.mark.asyncio
async def test_handback_requires_explicit_decision(api_module, monkeypatch):
    conn = FakeReturnConn()
    monkeypatch.setattr(api_module, "acquire_scoped", lambda _project: FakeAcquire(conn))
    await api_module.return_handoff(
        PARENT_ID,
        report(api_module),
        request=http_request(),
        x_agent="kai",
        x_project="kaidera-os",
    )
    conn.rows[HANDBACK_ID].update(
        status="claimed",
        claimed_by="ren@kaidera-os",
        claimed_at="now",
    )

    with pytest.raises(api_module.HTTPException) as exc:
        await api_module.return_handoff(
            HANDBACK_ID,
            report(api_module),
            request=http_request(),
            x_agent="ren",
            x_project="kaidera-os",
        )

    assert exc.value.status_code == 409
    assert "explicit decision" in exc.value.detail
    assert conn.rows[PARENT_ID]["status"] == "returned"
    assert conn.rows[HANDBACK_ID]["status"] == "claimed"


@pytest.mark.asyncio
async def test_return_rejects_bearer_actor_header_mismatch(api_module, monkeypatch):
    conn = FakeReturnConn()
    monkeypatch.setattr(api_module, "acquire_scoped", lambda _project: FakeAcquire(conn))

    with pytest.raises(api_module.HTTPException) as exc:
        await api_module.return_handoff(
            PARENT_ID,
            report(api_module),
            request=http_request("ren@kaidera-os"),
            x_agent="kai",
            x_project="kaidera-os",
        )

    assert exc.value.status_code == 403
    assert "does not match" in exc.value.detail
    assert conn.rows[PARENT_ID]["status"] == "claimed"


@pytest.mark.asyncio
async def test_single_handback_cannot_close_multi_approver_parent(api_module, monkeypatch):
    conn = FakeReturnConn(
        task_row(
            acceptance={
                "criteria": ["tests pass"],
                "approval_policy": {
                    "mode": "multi_lead",
                    "min_approvers": 2,
                },
            }
        )
    )
    monkeypatch.setattr(api_module, "acquire_scoped", lambda _project: FakeAcquire(conn))
    await api_module.return_handoff(
        PARENT_ID,
        report(api_module),
        request=http_request(),
        x_agent="kai",
        x_project="kaidera-os",
    )
    conn.rows[HANDBACK_ID].update(
        status="claimed",
        claimed_by="ren@kaidera-os",
        claimed_at="now",
    )

    with pytest.raises(api_module.HTTPException) as exc:
        await api_module.return_handoff(
            HANDBACK_ID,
            report(api_module, decision="accept"),
            request=http_request(),
            x_agent="ren",
            x_project="kaidera-os",
        )

    assert exc.value.status_code == 409
    assert "multi-approver" in exc.value.detail
    assert conn.rows[PARENT_ID]["status"] == "returned"


@pytest.mark.asyncio
async def test_inactive_delegator_routes_handback_to_active_lead(api_module, monkeypatch):
    conn = FakeReturnConn(
        active_agents={
            "kai": "full-stack-developer",
            "lux": "lead",
        },
        default_agent="ren",
    )
    monkeypatch.setattr(api_module, "acquire_scoped", lambda _project: FakeAcquire(conn))

    result = await api_module.return_handoff(
        PARENT_ID,
        report(api_module),
        request=http_request(),
        x_agent="kai",
        x_project="kaidera-os",
    )

    assert result["status"] == "returned"
    assert conn.rows[HANDBACK_ID]["to_agent"] == "lux@kaidera-os"
    assert (
        conn.rows[PARENT_ID]["completion_report"]["metadata"]["return_routing"]
        == "project_lead_fallback"
    )
