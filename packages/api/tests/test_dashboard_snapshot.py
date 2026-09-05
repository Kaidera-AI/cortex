"""Project isolation and truth-contract tests for GET /dashboard/snapshot."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import pytest
from fastapi import HTTPException
from starlette.requests import Request


API_MAIN_PATH = Path(__file__).resolve().parents[1] / "main.py"


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.path.insert(0, str(path.parent))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(path.parent))
    return module


class FakeAcquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return FakeAcquire(self.conn)


class FakeTransaction:
    def __init__(self, conn, isolation, readonly):
        self.conn = conn
        self.conn.transactions.append((isolation, readonly))

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class SnapshotConn:
    def __init__(self):
        self.scope_calls = []
        self.transactions = []
        self.handoffs = [
            {
                "project": "kaidera-os",
                "id": "urgent-pending",
                "status": "pending",
                "priority": "urgent",
                "from_agent": "ren",
                "to_role": "lead",
                "to_agent": "kai",
                "claimed_by": "",
                "summary": "Fold dashboard truth fix into v0.2.002",
                "created_at": "2026-08-13T00:00:00+00:00",
                "claimed_at": None,
                "age_hours": 30.0,
                "claimed_age_hours": None,
                "is_stale": True,
            },
            {
                "project": "kaidera-os",
                "id": "claimed",
                "status": "claimed",
                "priority": "high",
                "from_agent": "kai",
                "to_role": "lead",
                "to_agent": "kai",
                "claimed_by": "kai@kaidera-os",
                "summary": "Build release appliance",
                "created_at": "2026-08-14T00:00:00+00:00",
                "claimed_at": "2026-08-14T00:05:00+00:00",
                "age_hours": 2.0,
                "claimed_age_hours": 1.9,
                "is_stale": False,
            },
            {
                "project": "other-project",
                "id": "other-secret",
                "status": "pending",
                "priority": "urgent",
                "from_agent": "other",
                "to_role": "lead",
                "to_agent": "other",
                "claimed_by": "",
                "summary": "must not leak",
                "created_at": "2026-08-12T00:00:00+00:00",
                "claimed_at": None,
                "age_hours": 50.0,
                "claimed_age_hours": None,
                "is_stale": True,
            },
        ]
        self.epics = [
            {
                "project": "kaidera-os",
                "epic_id": "E017",
                "title": "Harness Intelligence Upgrade",
                "status": "programmed-review",
                "overall_pct": 6,
                "increments": [{"num": 2, "title": "v0.2.002", "status": "in_progress", "pct": 10}],
                "updated_at": "2026-08-14T00:00:00+00:00",
            },
            {
                "project": "other-project",
                "epic_id": "E999",
                "title": "Other",
                "status": "active",
                "overall_pct": 50,
                "increments": [],
                "updated_at": "2026-08-14T00:00:00+00:00",
            },
        ]

    def transaction(self, *, isolation, readonly):
        return FakeTransaction(self, isolation, readonly)

    async def execute(self, sql, *args):
        if "set_config('cortex.project', $1" in sql:
            self.scope_calls.append(args[0])
            return "SELECT 1"
        if "set_config('cortex.project', ''" in sql:
            self.scope_calls.append("")
            return "SELECT 1"
        raise AssertionError(f"Unexpected execute SQL: {sql}")

    async def fetchrow(self, sql, *args):
        project = args[0]
        if "COUNT(*) FILTER" in sql and "FROM handoffs" in sql:
            assert args[1:] == (24, 12)
            assert "COALESCE(claimed_at, created_at)" in sql
            rows = [r for r in self.handoffs if r["project"] == project]
            open_rows = [r for r in rows if r["status"] in {"pending", "claimed"}]
            return {
                "pending": sum(r["status"] == "pending" for r in open_rows),
                "claimed": sum(r["status"] == "claimed" for r in open_rows),
                "open": len(open_rows),
                "urgent": sum(r["priority"] == "urgent" for r in open_rows),
                "consults": 0,
                "stale": sum(bool(r["is_stale"]) for r in open_rows),
            }
        if "FROM decisions" in sql and "MAX(created_at)" in sql:
            assert "invalidated_at IS NULL" in sql
            age = 120 if project == "kaidera-os" else None
            return {
                "generated_at": "2026-08-14T02:00:00+00:00",
                "last_at": "2026-08-14T01:58:00+00:00" if age is not None else None,
                "age_seconds": age,
            }
        raise AssertionError(f"Unexpected fetchrow SQL: {sql}")

    async def fetch(self, sql, *args):
        project = args[0]
        if "FROM handoffs" in sql:
            assert args[1:] == (24, 12, 20)
            assert "COALESCE(claimed_at, created_at)" in sql
            limit = args[3]
            order = {"urgent": 0, "high": 1, "medium": 2, "low": 3}
            rows = [r for r in self.handoffs if r["project"] == project]
            rows.sort(key=lambda r: (order.get(r["priority"], 4), not r["is_stale"], r["created_at"]))
            return [{k: v for k, v in row.items() if k != "project"} for row in rows[:limit]]
        if "FROM epics" in sql:
            return [dict(row) for row in self.epics if row["project"] == project]
        if "FROM team_events" in sql:
            if project != "kaidera-os":
                return []
            return [
                {
                    "id": 42,
                    "project": project,
                    "agent_name": "ren@kaidera-os",
                    "event_type": "decision",
                    "summary": "Dashboard contract implemented",
                    "detail": {},
                    "files": [],
                    "sprint_id": None,
                    "related_decision_id": None,
                    "ts": "2026-08-14T01:59:00+00:00",
                }
            ]
        raise AssertionError(f"Unexpected fetch SQL: {sql}")


def request(headers: list[tuple[bytes, bytes]] | None = None) -> Request:
    return Request({"type": "http", "method": "GET", "path": "/", "headers": headers or []})


async def call_snapshot(api, project: str):
    return await api.dashboard_snapshot(
        request=request(),
        x_project=project,
        queue_limit=20,
        activity_limit=20,
        heartbeat_fresh_seconds=3300,
        pending_stale_hours=24,
        claimed_stale_hours=12,
    )


@pytest.fixture
def api():
    return load_module(API_MAIN_PATH, "cortex_api_main_dashboard_snapshot_test")


@pytest.mark.asyncio
async def test_snapshot_is_coherent_complete_and_project_scoped(api, monkeypatch):
    conn = SnapshotConn()
    registered = []

    async def require_registered(project):
        registered.append(project)
        return {"project_key": project}

    monkeypatch.setattr(api, "pool_app", FakePool(conn))
    monkeypatch.setattr(api, "require_admin_access", lambda _request: None)
    monkeypatch.setattr(api, "require_registered_project", require_registered)

    result = await call_snapshot(api, "kaidera-os")

    assert result["contract"] == "cortex.dashboard.snapshot.v1"
    assert result["source"]["consistency"] == "repeatable-read"
    assert result["queue"]["counts"] == {
        "pending": 1,
        "claimed": 1,
        "open": 2,
        "urgent": 1,
        "consults": 0,
        "stale": 1,
    }
    assert [row["id"] for row in result["queue"]["items"]] == ["urgent-pending", "claimed"]
    assert result["heartbeat"]["fresh"] is True
    assert result["heartbeat"]["fresh_seconds"] == 3300
    assert [epic["epic_id"] for epic in result["epics"]] == ["E017"]
    assert result["activity"]["events"][0]["fields"]["summary"] == "Dashboard contract implemented"
    assert "must not leak" not in str(result)
    assert "E999" not in str(result)
    assert registered == ["kaidera-os"]
    assert conn.transactions == [("repeatable_read", True)]
    assert conn.scope_calls == ["kaidera-os", ""]


@pytest.mark.asyncio
async def test_other_project_cannot_read_kaidera_os_snapshot(api, monkeypatch):
    conn = SnapshotConn()

    async def require_registered(project):
        return {"project_key": project}

    monkeypatch.setattr(api, "pool_app", FakePool(conn))
    monkeypatch.setattr(api, "require_admin_access", lambda _request: None)
    monkeypatch.setattr(api, "require_registered_project", require_registered)

    result = await call_snapshot(api, "other-project")

    assert [row["id"] for row in result["queue"]["items"]] == ["other-secret"]
    assert result["heartbeat"]["stale"] is True
    assert "urgent-pending" not in str(result)
    assert [epic["epic_id"] for epic in result["epics"]] == ["E999"]


@pytest.mark.asyncio
async def test_snapshot_requires_admin_token(api):
    api.ADMIN_TOKEN = "expected-token"
    with pytest.raises(HTTPException) as exc:
        await call_snapshot(api, "kaidera-os")
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_snapshot_requires_project_header(api, monkeypatch):
    monkeypatch.setattr(api, "require_admin_access", lambda _request: None)
    with pytest.raises(HTTPException) as exc:
        await call_snapshot(api, "")
    assert exc.value.status_code == 400
