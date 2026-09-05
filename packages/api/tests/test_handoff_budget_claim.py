from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from conftest import _NotARosterRead, roster_fetch, roster_fetchrow


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


class FakeBudgetConn:
    def __init__(
        self,
        *,
        update_count: int = 1,
        reserved_usage: dict | None = None,
    ):
        self.update_count = update_count
        self.reserved_usage = reserved_usage or {
            "input_tokens_used": 0,
            "output_tokens_used": 0,
            "total_tokens_used": 0,
            "cost_usd_used": 0.0,
        }
        self.executed: list[tuple[str, tuple]] = []
        self.events: list[dict] = []
        self.handoff = {
            "id": "11111111-1111-4111-8111-111111111111",
            "status": "pending",
            "from_agent": "ren@kaidera-os",
            "from_role": "full-stack-developer",
            "to_role": "full-stack-developer",
            "to_agent": "kai",
            "priority": "high",
            "summary": "Budgeted implementation handoff",
            "claimed_by": None,
            "terminal_reason": None,
        }

    def transaction(self):
        return FakeTransaction()

    async def fetchrow(self, sql, *args):
        try:  # E006 Inc04: registry resolver's cortex_projects.metadata read
            return roster_fetchrow(sql, args)
        except _NotARosterRead:
            pass
        if "FROM team_events" in sql and "handoff_budget_reserve" in sql:
            assert args[0] == "kaidera-os"
            assert args[1] == "kai@kaidera-os"
            assert args[2] == "handoff"
            return self.reserved_usage
        if "FROM team_events" in sql and "WHERE id = $1" in sql:
            event = self.events[int(args[0]) - 1]
            return {
                "id": args[0],
                "project": event["project"],
                "agent_name": event["agent"],
                "event_type": event["event_type"],
                "summary": event["summary"],
                "files": event.get("files"),
            }
        raise AssertionError(f"Unexpected fetchrow SQL: {sql}")

    async def fetch(self, sql, *args):
        try:  # E006 Inc04: registry resolver's agents/roles reads
            return roster_fetch(sql, args)
        except _NotARosterRead:
            pass
        if "FROM handoffs" in sql and "LIMIT 2" in sql:
            return [self.handoff]
        raise AssertionError(f"Unexpected fetch SQL: {sql}")

    async def execute(self, sql, *args):
        self.executed.append((sql, args))
        if "UPDATE handoffs SET status = 'claimed'" in sql:
            assert "lower(split_part(COALESCE(to_agent, ''), '@', 1))" in sql
            assert "AND lower(to_role) = ANY($5::text[])" in sql
            assert "WHERE id = $2::uuid" in sql
            assert args[0] == "kai@kaidera-os"
            assert ":" not in args[0]
            return f"UPDATE {self.update_count}"
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
                "files": args[5] if len(args) > 5 else None,
            }
            self.events.append(event)
            return len(self.events)
        raise AssertionError(f"Unexpected fetchval SQL: {sql}")


@pytest.fixture
def api_module(monkeypatch):
    monkeypatch.delenv("CORTEX_EVENT_BACKEND", raising=False)
    module = load_module(API_MAIN_PATH, "cortex_api_handoff_budget_test")

    async def require_registered_project(project):
        assert project == "kaidera-os"
        return {"project_key": project, "project_id": "55555555-5555-4555-8555-555555555555"}

    async def compound_agent(agent, project):
        assert project == "kaidera-os"
        return f"{agent}@{project}"

    async def resolve_agent_roles(_conn, project, agent):
        assert project == "kaidera-os"
        if agent == "kai":
            return ["full-stack-developer"]
        return []

    # E006 Inc04: the writer guard is registry-driven (async load_roster_policy,
    # which reads pool_admin/pool_app). This fixture stubs the DB-touching helpers
    # rather than wiring full pools, so stub the resolver to the seeded kaidera-os
    # policy (kai/ren writers; beat/migration/system) — consistent with the
    # require_registered_project / compound_agent / acquire_scoped stubs.
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
    monkeypatch.setattr(module, "resolve_agent_roles", resolve_agent_roles)
    monkeypatch.setattr(module, "load_roster_policy", load_roster_policy)
    return module


@pytest.mark.asyncio
async def test_claim_with_budget_claims_and_records_observation(api_module, monkeypatch):
    conn = FakeBudgetConn()
    monkeypatch.setattr(api_module, "acquire_scoped", lambda _project: FakeAcquire(conn))

    body = api_module.HandoffClaimWithBudget(
        budget=api_module.HandoffBudgetReserve(input_tokens=200, max_output_tokens=300)
    )
    result = await api_module.claim_handoff_with_budget(
        "11111111-1111-4111-8111-111111111111",
        body,
        x_agent="kai",
        x_project="kaidera-os",
    )

    assert result["claimed"] is True
    assert result["by"] == "kai@kaidera-os"
    assert result["budget"]["allow_llm"] is True
    assert result["budget"]["status"] == "not_enforced"
    assert result["lease"]["budget_enforced"] is False
    assert result["lease"]["budget_event_id"] == 1
    assert conn.events[0]["event_type"] == "handoff_budget_observe"
    assert conn.events[0]["detail"]["budget_status"] == "not_enforced"
    assert conn.events[0]["detail"]["approved"]["limits_applied"] == ["budget_not_enforced"]
    assert conn.events[0]["detail"]["approved"]["estimated_total_tokens"] == 500


@pytest.mark.asyncio
async def test_claim_with_exhausted_budget_still_claims(api_module, monkeypatch):
    conn = FakeBudgetConn()
    monkeypatch.setattr(api_module, "acquire_scoped", lambda _project: FakeAcquire(conn))

    body = api_module.HandoffClaimWithBudget(
        budget=api_module.HandoffBudgetReserve(
            input_tokens=200,
            max_output_tokens=300,
            config={"max_input_tokens": 100, "max_total_tokens": 100},
        )
    )
    result = await api_module.claim_handoff_with_budget(
        "22222222-2222-4222-8222-222222222222",
        body,
        x_agent="kai",
        x_project="kaidera-os",
    )

    assert result["claimed"] is True
    assert result["budget"]["allow_llm"] is True
    assert result["budget"]["status"] == "not_enforced"
    assert "not budget-gated" in result["budget"]["reason"]
    assert conn.events[0]["event_type"] == "handoff_budget_observe"
    assert conn.events[0]["detail"]["budget_status"] == "not_enforced"
    assert conn.events[0]["detail"]["approved"]["estimated_total_tokens"] == 500
    assert any("UPDATE handoffs" in sql for sql, _args in conn.executed)


@pytest.mark.asyncio
async def test_claim_with_budget_rejects_wrong_agent_after_reserve_check(api_module, monkeypatch):
    # A handoff addressed to a role/agent kai is NOT → the claim UPDATE affects 0
    # rows. Post Fix-#1 this is an INFORMATIVE 403 (naming the addressee), not a
    # bare 404, and still emits no budget-observe event.
    conn = FakeBudgetConn(update_count=0)
    conn.handoff = {
        **conn.handoff,
        "to_agent": "",  # role-addressed
        "to_role": "cortex-architect",  # kai does not hold this role
        "claimed_by": None,
    }
    monkeypatch.setattr(api_module, "acquire_scoped", lambda _project: FakeAcquire(conn))

    body = api_module.HandoffClaimWithBudget(
        budget=api_module.HandoffBudgetReserve(input_tokens=50, max_output_tokens=50)
    )
    with pytest.raises(api_module.HTTPException) as exc:
        await api_module.claim_handoff_with_budget(
            "33333333-3333-4333-8333-333333333333",
            body,
            x_agent="kai",
            x_project="kaidera-os",
        )

    assert exc.value.status_code == 403
    assert "cortex-architect" in str(exc.value.detail).lower()
    assert conn.events == []
