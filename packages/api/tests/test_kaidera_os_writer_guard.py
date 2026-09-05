import importlib.util
import json
import uuid
from pathlib import Path

import pytest
from fastapi import HTTPException


API_MAIN_PATH = Path(__file__).resolve().parents[1] / "main.py"


def load_api_module():
    spec = importlib.util.spec_from_file_location(
        "cortex_api_main_kaidera_os_writer_guard_test",
        API_MAIN_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class FakeAcquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return FakeAcquire(self.conn)


class FakeConn:
    def __init__(self):
        self.executed: list[tuple[str, tuple]] = []
        self.fetchvals: list[tuple[str, tuple]] = []
        self.decisions: dict[str, dict] = {}
        self.handoffs: dict[str, dict] = {}
        self.team_events: dict[int, dict] = {}

    def transaction(self):
        return FakeTransaction()

    async def fetchrow(self, sql, *args):
        # E006 Inc04: the registry resolver reads only `metadata` from cortex_projects.
        # Seeded kaidera-os shape => computed work_writers={kai,ren},
        # system_event_writers={beat,migration,system} (byte-for-byte today's frozensets).
        if "SELECT metadata FROM cortex_projects" in sql:
            return {
                "metadata": {
                    "enforce_writer_roster": True,
                    "roster_policy": {
                        "enforce_writer_roster": True,
                        "roster_schema_version": "1",
                        "default_writer_scope": "work",
                        "system_event_writers": ["beat", "migration", "system"],
                        "beat_may_create_handoff": True,
                        "handoff_targets": "writers",
                        "suggest_cutoff": 0.6,
                    },
                }
            }
        if "id::text AS project_id" in sql and "FROM cortex_projects" in sql:
            project = args[0]
            return {
                "project_key": project,
                "project_id": "22222222-2222-4222-8222-222222222222",
                "display_name": project,
                "default_agent": "kai",
                "repo_root": "/tmp/kaidera-os",
                "repo_type": "repo",
                "status": "active",
            }
        if "FROM decisions" in sql:
            row = self.decisions.get(str(args[0]))
            if row and row["project"] == args[1]:
                return row
            return None
        if "FROM handoffs" in sql:
            row = self.handoffs.get(str(args[0]))
            if row and row["project"] == args[1]:
                return row
            return None
        if "FROM team_events" in sql:
            row = self.team_events.get(int(args[0]))
            if row and row["project"] == args[1]:
                return row
            return None
        raise AssertionError(f"Unexpected fetchrow SQL: {sql}")

    async def fetchval(self, sql, *args):
        self.fetchvals.append((sql, args))
        if "INSERT INTO decisions" in sql:
            row_id = uuid.UUID("11111111-1111-4111-8111-111111111111")
            self.decisions[str(row_id)] = {
                "id": str(row_id),
                "project": args[0],
                "agent_name": args[1],
                "summary": args[2],
                "category": args[3],
                "metadata": args[-1],
            }
            return row_id
        if "INSERT INTO handoffs" in sql:
            row_id = uuid.UUID("22222222-2222-4222-8222-222222222222")
            self.handoffs[str(row_id)] = {
                "id": str(row_id),
                "project": args[0],
                "from_agent": args[1],
                "from_role": args[2],
                "to_role": args[3],
                "to_agent": args[4],
                "priority": args[5],
                "summary": args[6],
                "branch": args[7],
                "files_changed": args[8],
                "verification": args[9],
                "next_steps": args[10],
                "context": args[11],
                "parent_goal_id": args[12],
                "acceptance": json.loads(args[13]),
                "evidence": json.loads(args[14]),
                "retry": json.loads(args[15]),
                "escalation": json.loads(args[16]),
            }
            return row_id
        if "INSERT INTO team_events" in sql:
            event_id = len(self.team_events) + 1
            self.team_events[event_id] = {
                "id": event_id,
                "project": args[0],
                "agent_name": args[1],
                "event_type": args[2],
                "summary": args[3],
                "detail": args[4],
                "files": args[5],
                "sprint_id": args[6],
                "related_decision_id": args[7],
            }
            return event_id
        raise AssertionError(f"Unexpected fetchval SQL: {sql}")

    async def execute(self, sql, *args):
        self.executed.append((sql, args))
        return "INSERT 0 1"

    async def fetch(self, sql, *args):
        # E006 Inc04: registry resolver reads (agents writer_scope + role defaults).
        project = args[0] if args else None
        if "writer_scope" in sql and "FROM agents a" in sql:
            if project == "kaidera-os":
                return [
                    {"n": "kai", "scope": "work", "role": "full-stack-developer"},
                    {"n": "ren", "scope": "work", "role": "full-stack-developer"},
                ]
            return []
        if "SELECT a.name, a.role, a.model, a.capabilities" in sql:
            if project == "kaidera-os":
                return [
                    {
                        "name": "kai",
                        "role": "full-stack-developer",
                        "model": "gpt-5.5",
                        "capabilities": {"writer_scope": "work", "keep_visible": True},
                    },
                    {
                        "name": "ren",
                        "role": "full-stack-developer",
                        "model": "gemini-3.1-pro-preview",
                        "capabilities": {"writer_scope": "work", "keep_visible": True},
                    },
                ]
            return []
        if "FROM roles" in sql and "default_capabilities" in sql:
            return []
        raise AssertionError(f"Unexpected fetch SQL: {sql}")


@pytest.fixture
def cortex_api(monkeypatch):
    module = load_api_module()
    conn = FakeConn()
    fake_pool = FakePool(conn)
    module.pool = fake_pool
    module.pool_app = fake_pool
    module.pool_admin = fake_pool
    module._kaidera_os_writer_guard_conn = conn

    async def fake_embed_text(_text):
        return None

    monkeypatch.setattr(module, "embed_text", fake_embed_text)
    return module


@pytest.mark.asyncio
async def test_log_rejects_non_roster_kaidera_os_agent(cortex_api):
    with pytest.raises(HTTPException) as excinfo:
        await cortex_api.log_event(
            cortex_api.LogRequest(
                event_type="decision",
                summary="[ALPHA-SHOULD-NOT-WRITE] kaidera-os scope leak",
            ),
            x_agent="alpha",
            x_project="kaidera-os",
        )

    assert excinfo.value.status_code == 403
    assert "not registered to write in kaidera-os" in excinfo.value.detail


@pytest.mark.asyncio
async def test_log_allows_beat_system_evidence_in_kaidera_os(cortex_api):
    result = await cortex_api.log_event(
        cortex_api.LogRequest(
            event_type="decision",
            summary="[BEAT-HEARTBEAT] kaidera-os PM heartbeat",
        ),
        x_agent="beat",
        x_project="kaidera-os",
    )

    assert result == {
        "id": "11111111-1111-4111-8111-111111111111",
        "embedded": False,
        "verified": True,
        "team_event_id": 1,
    }
    inserted_decision = [
        args
        for sql, args in cortex_api._kaidera_os_writer_guard_conn.fetchvals
        if "INSERT INTO decisions" in sql
    ][0]
    assert inserted_decision[1] == "beat@kaidera-os"


@pytest.mark.asyncio
async def test_beat_allowance_is_explicit_per_path(cortex_api):
    # Beat's effective scope is system-event: it fails the bare work gate but
    # passes the explicit system-event and work-handoff carve-outs. This is now
    # driven by the seeded registry policy (beat in system_event_writers +
    # beat_may_create_handoff=true), not a hardcoded frozenset.
    with pytest.raises(HTTPException):
        await cortex_api.require_registered_agent_writer("kaidera-os", "beat")

    await cortex_api.require_registered_agent_writer(
        "kaidera-os",
        "beat",
        scope="system-event",
    )
    await cortex_api.require_registered_agent_writer(
        "kaidera-os",
        "beat",
        scope="work-handoff",
    )


@pytest.mark.asyncio
async def test_handoff_create_rejects_non_roster_sender_and_target(cortex_api):
    with pytest.raises(HTTPException) as sender_exc:
        await cortex_api.create_handoff(
            cortex_api.HandoffCreate(
                to_role="full-stack-developer",
                summary="Non-roster sender should not create kaidera-os work",
            ),
            x_agent="alpha",
            x_project="kaidera-os",
        )

    assert sender_exc.value.status_code == 403

    with pytest.raises(HTTPException) as target_exc:
        await cortex_api.create_handoff(
            cortex_api.HandoffCreate(
                to_role="full-stack-developer",
                to_agent="alpha",
                summary="Non-roster target should not receive kaidera-os work",
            ),
            x_agent="kai",
            x_project="kaidera-os",
        )

    assert target_exc.value.status_code == 403
    assert "route kaidera-os work only to kai or ren" in target_exc.value.detail


@pytest.mark.asyncio
async def test_handoff_create_allows_kai_to_ren(cortex_api):
    result = await cortex_api.create_handoff(
        cortex_api.HandoffCreate(
            to_role="full-stack-developer",
            to_agent="ren",
            summary="Review kaidera-os guard",
        ),
        x_agent="kai",
        x_project="kaidera-os",
    )

    assert result == {
        "id": "22222222-2222-4222-8222-222222222222",
        "status": "pending",
        "verified": True,
        "deduped": False,
    }
    inserted_handoff = [
        args
        for sql, args in cortex_api._kaidera_os_writer_guard_conn.fetchvals
        if "INSERT INTO handoffs" in sql
    ][0]
    assert inserted_handoff[1] == "kai@kaidera-os"
    assert inserted_handoff[4] == "ren@kaidera-os"


@pytest.mark.asyncio
async def test_project_local_sync_rejects_non_roster_kaidera_os_events(cortex_api):
    with pytest.raises(HTTPException) as excinfo:
        await cortex_api.project_local_sync(
            cortex_api.ProjectLocalSyncRequest(
                team_events=[
                    cortex_api.SyncEvent(
                        agent_name="root",
                        event_type="decision",
                        summary="Root should not sync into kaidera-os",
                    )
                ]
            ),
            x_project="kaidera-os",
        )

    assert excinfo.value.status_code == 403


@pytest.mark.asyncio
async def test_roster_and_writers_endpoints_expose_writer_scope(cortex_api):
    roster = await cortex_api.get_roster(x_project="kaidera-os")
    assert roster["agents"] == [
        {"name": "kai", "role": "full-stack-developer", "model": "gpt-5.5", "writer_scope": "work"},
        {"name": "ren", "role": "full-stack-developer", "model": "gemini-3.1-pro-preview", "writer_scope": "work"},
    ]

    writers = await cortex_api.get_project_writers("kaidera-os")
    assert writers["enforce"] is True
    assert writers["approved_agents"] == ["kai", "ren"]
    assert [row["name"] for row in writers["writers"]] == ["kai", "ren"]
    assert all(row["writer_scope"] == "work" for row in writers["writers"])
    assert all(row["is_handoff_target"] is True for row in writers["writers"])


@pytest.mark.asyncio
async def test_register_agent_gates_caller_not_new_subject(cortex_api):
    result = await cortex_api.register_agent(
        cortex_api.AgentRegister(
            name="bob",
            role="full-stack-developer",
            writer_scope="work",
            capabilities={"primary": ["implementation"]},
        ),
        x_agent="kai",
        x_project="kaidera-os",
    )

    assert result == {
        "registered": True,
        "agent": "bob",
        "role": "full-stack-developer",
        "writer_scope": "work",
        "registered_by": "kai",
    }
    agent_insert = [
        args
        for sql, args in cortex_api._kaidera_os_writer_guard_conn.executed
        if "INSERT INTO agents" in sql
    ][0]
    assert agent_insert[0] == "bob"
    assert '"writer_scope": "work"' in agent_insert[3]


@pytest.mark.asyncio
async def test_register_agent_rejects_non_roster_caller(cortex_api):
    with pytest.raises(HTTPException) as excinfo:
        await cortex_api.register_agent(
            cortex_api.AgentRegister(name="bob", role="full-stack-developer"),
            x_agent="alpha",
            x_project="kaidera-os",
        )

    assert excinfo.value.status_code == 403
