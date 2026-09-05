import importlib.util
import json
import uuid
from pathlib import Path

import pytest
from fastapi import HTTPException


API_MAIN_PATH = Path(__file__).resolve().parents[1] / "main.py"


def load_api_module():
    spec = importlib.util.spec_from_file_location(
        "cortex_api_main_identity_v2_ingress_test",
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


class FakeSessionIngestConn:
    def __init__(self, agent_id=None):
        self.agent_id = agent_id
        self.executed: list[tuple[str, tuple]] = []
        self.fetchvals: list[tuple[str, tuple]] = []

    def transaction(self):
        return FakeTransaction()

    async def fetchrow(self, sql, *args):
        if "FROM session_sources" in sql:
            return None
        if "FROM agent_sessions" in sql and "UNION ALL" in sql:
            return None
        raise AssertionError(f"Unexpected fetchrow SQL: {sql}")

    async def fetchval(self, sql, *args):
        self.fetchvals.append((sql, args))
        if "SELECT id FROM agents" in sql:
            return self.agent_id
        if "INSERT INTO archive_messages" in sql:
            return 9001
        raise AssertionError(f"Unexpected fetchval SQL: {sql}")

    async def execute(self, sql, *args):
        if "INSERT INTO agents" in sql:
            raise AssertionError("sessions/ingest must not mint agents")
        self.executed.append((sql, args))
        return "INSERT 0 1"

    async def executemany(self, sql, rows):
        self.executed.extend((sql, tuple(row)) for row in rows)


class FakeProjectMoveConn:
    def __init__(self, *, target_exists: bool = False):
        self.projects = {
            "doha-dt-bid": {
                "id": "11111111-1111-4111-8111-111111111111",
                "parent_project_key": None,
            },
            "doha-child": {
                "id": "33333333-3333-4333-8333-333333333333",
                "parent_project_key": "doha-dt-bid",
            },
        }
        if target_exists:
            self.projects["doha-dt"] = {
                "id": "22222222-2222-4222-8222-222222222222",
                "parent_project_key": None,
            }
        self.project_paths = [{"project_key": "doha-dt-bid", "root_path": "/repo/doha"}]
        self.columns = {
            "cortex_projects": {"project_key", "parent_project_key"},
            "agents": {"project", "project_id"},
            "decisions": {"project", "project_id"},
            "cortex_project_paths": {"project_key"},
            "cortex_legacy_identity_archive": {"project_key"},
        }
        self.executed: list[tuple[str, tuple]] = []
        self.project_paths_fk_cascade = False
        self.parent_project_fk_cascade = False

    async def fetchrow(self, sql, *args):
        if "FROM cortex_projects" in sql and "project_key = $1" in sql:
            row = self.projects.get(args[0])
            return row.copy() if row else None
        raise AssertionError(f"Unexpected fetchrow SQL: {sql}")

    async def fetchval(self, sql, *args):
        if "information_schema.columns" in sql:
            table, column = args
            return column in self.columns.get(table, set())
        raise AssertionError(f"Unexpected fetchval SQL: {sql}")

    async def execute(self, sql, *args):
        self.executed.append((sql, args))
        if "ALTER TABLE public.cortex_project_paths" in sql:
            self.project_paths_fk_cascade = "ON UPDATE CASCADE" in sql
            return "ALTER TABLE"
        if "ALTER TABLE public.cortex_projects" in sql:
            self.parent_project_fk_cascade = "ON UPDATE CASCADE" in sql
            return "ALTER TABLE"
        if sql.startswith("UPDATE cortex_projects") and "SET project_key" in sql:
            old_key, new_key = args
            if not self.project_paths_fk_cascade:
                raise AssertionError("project paths FK was not made ON UPDATE CASCADE first")
            if not self.parent_project_fk_cascade:
                raise AssertionError("parent project FK was not made ON UPDATE CASCADE first")
            self.projects[new_key] = self.projects.pop(old_key)
            for path in self.project_paths:
                if path["project_key"] == old_key:
                    path["project_key"] = new_key
            for project in self.projects.values():
                if project.get("parent_project_key") == old_key:
                    project["parent_project_key"] = new_key
            return "UPDATE 1"
        if sql.startswith("UPDATE cortex_projects") and "SET parent_project_key" in sql:
            old_key, new_key = args
            count = 0
            for project in self.projects.values():
                if project.get("parent_project_key") == old_key:
                    project["parent_project_key"] = new_key
                    count += 1
            return f"UPDATE {count}"
        if 'UPDATE "cortex_project_paths" SET project_key' in sql:
            old_key, new_key = args
            count = 0
            for path in self.project_paths:
                if path["project_key"] == old_key:
                    path["project_key"] = new_key
                    count += 1
            return f"UPDATE {count}"
        return "UPDATE 1"


class FakeConsoleAppDbConn:
    def __init__(self):
        self.tables = {
            "agent_settings": [
                {"project": "doha-dt-bid", "agent": "kai", "model": "old-model"},
                {"project": "doha-dt", "agent": "kai", "model": "partial-new-model"},
            ],
            "project_autonomy": [
                {"project": "doha-dt-bid", "enabled": True},
                {"project": "doha-dt", "enabled": False},
            ],
            "project_propose_mode": [
                {"project": "doha-dt-bid", "enabled": True},
            ],
            "pending_approval": [
                {"project": "doha-dt-bid", "handoff_id": "handoff-1"},
                {"project": "doha-dt", "handoff_id": "handoff-1"},
            ],
            "handoff_orchestration": [
                {"project": "doha-dt-bid", "handoff_id": "handoff-2"},
            ],
            "run_state": [
                {"project": "doha-dt-bid", "run_id": "run-1"},
            ],
            "usage_events": [
                {"project": "doha-dt-bid", "id": 1},
            ],
            "scheduled_jobs": [
                {
                    "project": "doha-dt-bid",
                    "id": "planning",
                    "payload": {"project": "doha-dt-bid", "summary": "Plan doha-dt-bid"},
                },
            ],
            "mailbox_feeders": [
                {
                    "project": "doha-dt-bid",
                    "id": "inbox",
                    "config": {"project": "doha-dt-bid"},
                    "state": {"cursor": "doha-dt-bid:1"},
                },
            ],
            "app_settings": [
                {"key": "cortex_default_project", "value": "doha-dt-bid"},
            ],
        }
        self.columns = {
            "agent_settings": {"project", "agent", "model"},
            "project_autonomy": {"project", "enabled"},
            "project_propose_mode": {"project", "enabled"},
            "pending_approval": {"project", "handoff_id"},
            "handoff_orchestration": {"project", "handoff_id"},
            "run_state": {"project", "run_id"},
            "usage_events": {"project", "id"},
            "scheduled_jobs": {"project", "id", "payload"},
            "mailbox_feeders": {"project", "id", "config", "state"},
            "app_settings": {"key", "value"},
        }
        self.executed: list[tuple[str, tuple]] = []
        self.transactions_started = 0

    def transaction(self):
        self.transactions_started += 1
        return FakeTransaction()

    async def fetchval(self, sql, *args):
        if "information_schema.columns" in sql:
            table, column = args
            return column in self.columns.get(table, set())
        raise AssertionError(f"Unexpected appdb fetchval SQL: {sql}")

    async def execute(self, sql, *args):
        self.executed.append((sql, args))
        if sql.startswith('DELETE FROM "'):
            table = sql.split('"', 2)[1]
            old_key, new_key = args
            rows = self.tables[table]
            if table == "agent_settings":
                keys = {
                    row["agent"] for row in rows
                    if row.get("project") == old_key
                }
                keep = [
                    row for row in rows
                    if not (row.get("project") == new_key and row.get("agent") in keys)
                ]
            elif table == "pending_approval":
                keys = {
                    row["handoff_id"] for row in rows
                    if row.get("project") == old_key
                }
                keep = [
                    row for row in rows
                    if not (
                        row.get("project") == new_key
                        and row.get("handoff_id") in keys
                    )
                ]
            else:
                has_source = any(row.get("project") == old_key for row in rows)
                keep = [
                    row for row in rows
                    if not (has_source and row.get("project") == new_key)
                ]
            deleted = len(rows) - len(keep)
            self.tables[table] = keep
            return f"DELETE {deleted}"
        if sql.startswith('UPDATE "') and " SET project = $2 WHERE project = $1" in sql:
            table = sql.split('"', 2)[1]
            old_key, new_key = args
            count = 0
            for row in self.tables[table]:
                if row.get("project") == old_key:
                    row["project"] = new_key
                    count += 1
            return f"UPDATE {count}"
        if sql.startswith("UPDATE app_settings"):
            old_key, new_key = args
            count = 0
            for row in self.tables["app_settings"]:
                if (
                    row.get("key") == "cortex_default_project"
                    and row.get("value") == old_key
                ):
                    row["value"] = new_key
                    count += 1
            return f"UPDATE {count}"
        if sql.startswith('UPDATE "') and "replace(" in sql and "::jsonb" in sql:
            table = sql.split('"', 2)[1]
            column = sql.split('SET "', 1)[1].split('"', 1)[0]
            old_key, new_key = args
            count = 0
            for row in self.tables[table]:
                if row.get("project") != new_key:
                    continue
                raw = json.dumps(row[column])
                if old_key not in raw:
                    continue
                row[column] = json.loads(raw.replace(old_key, new_key))
                count += 1
            return f"UPDATE {count}"
        raise AssertionError(f"Unexpected appdb execute SQL: {sql}")


@pytest.fixture
def cortex_api(monkeypatch):
    module = load_api_module()

    async def fake_require_registered_project(project):
        return {"project_key": project}

    async def fake_require_registered_agent_writer(project, agent, *, scope="work"):
        return None

    monkeypatch.setattr(module, "require_registered_project", fake_require_registered_project)
    monkeypatch.setattr(module, "require_registered_agent_writer", fake_require_registered_agent_writer)
    return module


def session_body(api, *, agent: str):
    return api.SessionIngest(
        session_uuid=str(uuid.uuid4()),
        agent=agent,
        source_path=f"/tmp/session-{uuid.uuid4()}.jsonl",
        provider="codex",
        source_kind="codex-session",
        messages=[
            api.SessionMessage(
                role="user",
                content="hello",
                ts="2026-06-18T00:00:00Z",
            )
        ],
    )


@pytest.mark.asyncio
async def test_sessions_ingest_rejects_unregistered_agent_without_insert(cortex_api, monkeypatch):
    conn = FakeSessionIngestConn(agent_id=None)
    pool = FakePool(conn)
    cortex_api.pool_admin = pool
    monkeypatch.setattr(cortex_api, "acquire_scoped", lambda _project: FakeAcquire(conn))

    with pytest.raises(HTTPException) as exc:
        await cortex_api.ingest_session(session_body(cortex_api, agent="onyx"), x_project="doha-dt")

    assert exc.value.status_code == 403
    assert "Agent 'onyx' is not registered in doha-dt" in exc.value.detail
    assert all("INSERT INTO agents" not in sql for sql, _ in conn.executed)


@pytest.mark.asyncio
async def test_sessions_ingest_uses_existing_agent_and_never_upserts_agents(cortex_api, monkeypatch):
    agent_id = uuid.uuid4()
    conn = FakeSessionIngestConn(agent_id=agent_id)
    pool = FakePool(conn)
    cortex_api.pool_admin = pool
    monkeypatch.setattr(cortex_api, "acquire_scoped", lambda _project: FakeAcquire(conn))

    result = await cortex_api.ingest_session(
        session_body(cortex_api, agent="Kai@kaidera-os"),
        x_project="kaidera-os",
    )

    assert result["agent_id"] == str(agent_id)
    assert result["messages_inserted"] == 1
    assert all("INSERT INTO agents" not in sql for sql, _ in conn.executed)
    assert any(
        "INSERT INTO session_sources" in sql and args[4] == "kai"
        for sql, args in conn.executed
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("e2_enabled", "e4_enabled"),
    [(False, False), (True, False), (False, True), (True, True)],
)
async def test_held_transform_internals_distill_then_compact_the_actual_hot_row(
    cortex_api,
    monkeypatch,
    e2_enabled,
    e4_enabled,
):
    agent_id = uuid.uuid4()
    conn = FakeSessionIngestConn(agent_id=agent_id)
    pool = FakePool(conn)
    cortex_api.pool_admin = pool
    monkeypatch.setattr(cortex_api, "acquire_scoped", lambda _project: FakeAcquire(conn))
    if e4_enabled:
        monkeypatch.setenv("CORTEX_E4_COMPACT", "1")
    else:
        monkeypatch.delenv("CORTEX_E4_COMPACT", raising=False)
    if e2_enabled:
        monkeypatch.setenv("CORTEX_E2_DISTILL", "1")
    else:
        monkeypatch.delenv("CORTEX_E2_DISTILL", raising=False)

    raw = "raw original that must remain recoverable"
    commitment = "distilled commitment"
    compact_inputs: list[str] = []

    def fake_compact(text):
        compact_inputs.append(text)
        return f"compact:{text}", True, 10

    monkeypatch.setattr(cortex_api, "compact_for_storage", fake_compact)
    monkeypatch.setattr(
        cortex_api,
        "distill_message",
        lambda _text: [{"content": commitment, "metadata": {"kind": "commitment"}}],
    )
    monkeypatch.setattr(cortex_api, "is_always_keep", lambda _text: False)
    body = session_body(cortex_api, agent="Kai@kaidera-os")
    body.messages = [
        cortex_api.SessionMessage(
            role="user",
            content=raw,
            ts="2026-06-18T00:00:00Z",
        )
    ]

    result = await cortex_api.ingest_session(body, x_project="kaidera-os")

    assert result["messages_inserted"] == 1
    archive_rows = [
        args for sql, args in conn.fetchvals if "INSERT INTO archive_messages" in sql
    ]
    if e2_enabled:
        assert len(archive_rows) == 1
        assert archive_rows[0][4] == raw
    else:
        assert archive_rows == []
    hot_args = next(args for sql, args in conn.executed if "INSERT INTO messages" in sql)
    expected_input = commitment if e2_enabled else raw
    assert compact_inputs == ([expected_input] if e4_enabled else [])
    expected_hot = f"compact:{expected_input}" if e4_enabled else expected_input
    assert hot_args[4] == expected_hot
    assert hot_args[7] is e2_enabled
    hot_metadata = json.loads(hot_args[5])
    if e2_enabled:
        assert hot_metadata["distilled_from_archive_id"] == 9001
    else:
        assert "distilled_from_archive_id" not in hot_metadata
    assert hot_metadata.get("compacted", False) is e4_enabled


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", ["CORTEX_E2_DISTILL", "CORTEX_E4_COMPACT"])
async def test_v02002_startup_refuses_held_memory_transforms(
    cortex_api,
    monkeypatch,
    flag,
):
    monkeypatch.delenv("CORTEX_E2_DISTILL", raising=False)
    monkeypatch.delenv("CORTEX_E4_COMPACT", raising=False)
    monkeypatch.setenv(flag, "1")

    async def unexpected_pool(*_args, **_kwargs):
        raise AssertionError("database pool must not open before the release hold")

    monkeypatch.setattr(cortex_api.asyncpg, "create_pool", unexpected_pool)

    with pytest.raises(RuntimeError, match="v0.2.002 does not support"):
        async with cortex_api.lifespan(object()):
            pytest.fail("held memory transform reached API startup")


@pytest.mark.asyncio
async def test_project_key_migration_moves_known_project_scoped_tables(cortex_api):
    conn = FakeProjectMoveConn()
    appdb = FakeConsoleAppDbConn()

    result = await cortex_api.migrate_project_key(
        conn,
        old_key="doha-dt-bid",
        new_key="doha-dt",
        appdb_conn=appdb,
    )

    assert result["migrated"] is True
    assert result["old_key"] == "doha-dt-bid"
    assert result["new_key"] == "doha-dt"
    assert "doha-dt" in conn.projects
    assert "doha-dt-bid" not in conn.projects
    assert conn.project_paths == [{"project_key": "doha-dt", "root_path": "/repo/doha"}]
    assert conn.projects["doha-child"]["parent_project_key"] == "doha-dt"
    assert result["fk_constraints"] == [
        "cortex_project_paths_project_key_fkey",
        "cortex_projects_parent_project_key_fkey",
    ]
    assert any("UPDATE \"agents\" SET project = $2" in sql for sql, _ in conn.executed)
    assert any("UPDATE \"decisions\" SET project = $2" in sql for sql, _ in conn.executed)
    assert result["appdb"]["available"] is True
    assert appdb.transactions_started == 1
    assert all(
        row["project"] == "doha-dt"
        for table, rows in appdb.tables.items()
        if table != "app_settings"
        for row in rows
    )
    assert appdb.tables["agent_settings"] == [
        {"project": "doha-dt", "agent": "kai", "model": "old-model"},
    ]
    assert appdb.tables["project_autonomy"] == [
        {"project": "doha-dt", "enabled": True},
    ]
    assert appdb.tables["pending_approval"] == [
        {"project": "doha-dt", "handoff_id": "handoff-1"},
    ]
    assert appdb.tables["scheduled_jobs"][0]["payload"] == {
        "project": "doha-dt",
        "summary": "Plan doha-dt",
    }
    assert appdb.tables["mailbox_feeders"][0]["config"] == {"project": "doha-dt"}
    assert appdb.tables["mailbox_feeders"][0]["state"] == {"cursor": "doha-dt:1"}
    assert appdb.tables["app_settings"] == [
        {"key": "cortex_default_project", "value": "doha-dt"},
    ]


@pytest.mark.asyncio
async def test_project_key_migration_refuses_implicit_merge_when_target_exists(cortex_api):
    conn = FakeProjectMoveConn(target_exists=True)

    with pytest.raises(HTTPException) as exc:
        await cortex_api.migrate_project_key(
            conn,
            old_key="doha-dt-bid",
            new_key="doha-dt",
        )

    assert exc.value.status_code == 409
    assert "target project already exists" in exc.value.detail
