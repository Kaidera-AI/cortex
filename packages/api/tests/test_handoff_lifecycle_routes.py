from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
from starlette.requests import Request


API_MAIN_PATH = Path(__file__).resolve().parents[1] / "main.py"


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
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


class FakeHandoffConn:
    def __init__(self, rows: list[dict] | None = None):
        self.rows = rows if rows is not None else [self.handoff_row()]
        self.executed: list[tuple[str, tuple]] = []
        self.events: list[dict] = []

    @staticmethod
    def handoff_row(**overrides):
        row = {
            "id": "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa",
            "status": "claimed",
            "kind": "task",
            "from_agent": "ren@kaidera-os",
            "from_role": "full-stack-developer",
            "to_role": "full-stack-developer",
            "to_agent": "kai",
            "priority": "high",
            "summary": "Complete the handoff-route fix",
            "claimed_by": "kai@kaidera-os",
            "claimed_at": "2026-06-26 20:00:00+00",
            "retry_count": 2,
            "terminal_reason": None,
        }
        row.update(overrides)
        return row

    def transaction(self):
        return FakeTransaction()

    async def fetch(self, sql, *args):
        if "FROM handoffs" in sql and "LIMIT 2" in sql:
            return self.rows
        raise AssertionError(f"Unexpected fetch SQL: {sql}")

    async def execute(self, sql, *args):
        self.executed.append((sql, args))
        if "UPDATE handoffs SET status = 'completed'" in sql:
            assert "WHERE id = $1::uuid" in sql
            assert args[0] == "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"
            return "UPDATE 1"
        if "UPDATE handoffs" in sql and "status = 'pending'" in sql:
            assert "retry_count = COALESCE(retry_count, 0) + 1" in sql
            assert "claimed_at = NULL" in sql
            assert args[0] == "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"
            return "UPDATE 1"
        if "pg_notify" in sql:
            return "SELECT 1"
        raise AssertionError(f"Unexpected execute SQL: {sql}")

    async def fetchval(self, sql, *args):
        if "INSERT INTO team_events" in sql:
            event = {
                "project": args[0],
                "agent": args[1],
                "event_type": args[2],
                "summary": args[3],
                "detail": json.loads(args[4]),
                "files": args[5],
            }
            self.events.append(event)
            return len(self.events)
        raise AssertionError(f"Unexpected fetchval SQL: {sql}")

    async def fetchrow(self, sql, *args):
        if "FROM handoffs" in sql and "FOR UPDATE" in sql:
            return self.rows[0]
        if "FROM team_events" in sql and "WHERE id = $1" in sql:
            event = self.events[int(args[0]) - 1]
            return {
                "id": args[0],
                "project": event["project"],
                "agent_name": event["agent"],
                "event_type": event["event_type"],
                "summary": event["summary"],
                "files": event["files"],
            }
        raise AssertionError(f"Unexpected fetchrow SQL: {sql}")


@pytest.fixture
def api_module(monkeypatch):
    module = load_module(API_MAIN_PATH, "cortex_api_handoff_lifecycle_test")

    async def require_registered_project(project):
        assert project == "kaidera-os"
        return {"project_key": project, "project_id": "66666666-6666-4666-8666-666666666666"}

    async def compound_agent(agent, project):
        assert project == "kaidera-os"
        return f"{agent}@{project}"

    # E006 Inc04: the writer guard is registry-driven (async load_roster_policy,
    # which reads pool_admin/pool_app). This fixture stubs the DB-touching helpers
    # instead of wiring full pools, so stub the resolver to the seeded kaidera-os
    # policy — consistent with the require_registered_project / acquire_scoped stubs.
    async def load_roster_policy(project):
        assert project == "kaidera-os"
        return module.RosterPolicy(
            project=project,
            enforce=True,
            default_writer_scope="work",
            work_writers=frozenset({"kai", "ren"}),
            system_event_writers=frozenset({"beat", "migration", "system"}),
            read_only=frozenset(),
            handoff_targets=frozenset({"kai", "ren"}),
            beat_may_create_handoff=True,
            roles={},
            suggest_cutoff=0.6,
        )

    monkeypatch.setattr(module, "require_registered_project", require_registered_project)
    monkeypatch.setattr(module, "compound_agent", compound_agent)
    monkeypatch.setattr(module, "load_roster_policy", load_roster_policy)
    monkeypatch.setattr(module, "ADMIN_TOKEN", "test-admin-token")
    return module


def admin_request() -> Request:
    return Request(
        {
            "type": "http",
            "headers": [
                (b"x-cortex-admin-token", b"test-admin-token"),
            ],
        }
    )


def actor_request(agent: str = "kai") -> Request:
    request = Request(
        {
            "type": "http",
            "headers": [],
        }
    )
    request.state.jwt_claims = {"agent": agent, "project": "kaidera-os"}
    return request


@pytest.mark.asyncio
async def test_handoff_prefix_ambiguity_hard_errors(api_module):
    conn = FakeHandoffConn(
        rows=[
            FakeHandoffConn.handoff_row(id="aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"),
            FakeHandoffConn.handoff_row(id="aaaaaaaa-2222-4222-8222-aaaaaaaaaaaa"),
        ]
    )

    with pytest.raises(api_module.HTTPException) as exc:
        await api_module.resolve_unique_handoff_for_mutation(
            conn,
            project="kaidera-os",
            handoff_id="aaaaaaaa",
        )

    assert exc.value.status_code == 409
    assert "matched multiple rows" in exc.value.detail


@pytest.mark.asyncio
async def test_complete_handoff_uses_resolved_uuid_and_emits_lifecycle_event(api_module, monkeypatch):
    conn = FakeHandoffConn()
    monkeypatch.setattr(api_module, "acquire_scoped", lambda _project: FakeAcquire(conn))

    result = await api_module.complete_handoff(
        "aaaaaaaa",
        request=admin_request(),
        x_agent="kai",
        x_project="kaidera-os",
    )

    assert result["completed"] is True
    assert result.get("warnings") == []
    assert conn.events[0]["event_type"] == "handoff_completed"
    assert conn.events[0]["agent"] == "kai@kaidera-os"
    assert conn.events[0]["detail"]["handoff_id"] == "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"
    assert conn.events[0]["detail"]["status"] == "completed"


@pytest.mark.asyncio
async def test_admin_complete_rejects_completion_handback(api_module, monkeypatch):
    conn = FakeHandoffConn(
        rows=[
            FakeHandoffConn.handoff_row(
                kind="completion_handback",
                reply_to_handoff_id="bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb",
            )
        ]
    )
    monkeypatch.setattr(api_module, "acquire_scoped", lambda _project: FakeAcquire(conn))

    with pytest.raises(api_module.HTTPException) as exc:
        await api_module.complete_handoff(
            "aaaaaaaa",
            request=admin_request(),
            x_agent="kai",
            x_project="kaidera-os",
        )

    assert exc.value.status_code == 409
    assert "explicit accept or rework" in exc.value.detail
    assert conn.events == []


@pytest.mark.asyncio
async def test_complete_unclaimed_cross_project_handoff_uses_local_system_actor(api_module, monkeypatch):
    conn = FakeHandoffConn(
        rows=[
            FakeHandoffConn.handoff_row(
                status="pending",
                from_agent="alpha@kaidera",
                claimed_by=None,
                claimed_at=None,
            )
        ]
    )
    monkeypatch.setattr(api_module, "acquire_scoped", lambda _project: FakeAcquire(conn))

    result = await api_module.complete_handoff(
        "aaaaaaaa",
        request=admin_request(),
        x_agent="",
        x_project="kaidera-os",
    )

    assert result["completed"] is True
    assert conn.events[0]["event_type"] == "handoff_completed"
    assert conn.events[0]["agent"] == "system@kaidera-os"
    assert conn.events[0]["detail"]["from_agent"] == "alpha@kaidera"


@pytest.mark.asyncio
async def test_release_handoff_increments_retry_count_and_emits_lifecycle_event(api_module, monkeypatch):
    conn = FakeHandoffConn()
    monkeypatch.setattr(api_module, "acquire_scoped", lambda _project: FakeAcquire(conn))

    result = await api_module.release_handoff(
        "aaaaaaaa",
        request=actor_request(),
        body=api_module.HandoffTerminate(reason="lease expired"),
        x_agent="kai",
        x_project="kaidera-os",
    )

    assert result == {"released": True, "retry_count": 3}
    assert conn.events[0]["event_type"] == "handoff_released"
    assert conn.events[0]["detail"]["retry_count"] == 3
    assert conn.events[0]["detail"]["terminal_reason"] == "lease expired"
