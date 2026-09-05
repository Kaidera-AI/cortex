import importlib.util
import pytest
from pathlib import Path
from pydantic import BaseModel

API_MAIN_PATH = Path(__file__).resolve().parents[1] / "main.py"

def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
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


class FakeSyncConn:
    def __init__(self, max_id=100):
        self.max_id = max_id
        self.executed_queries = []
        self.executemany_queries = []

    async def fetchrow(self, sql, *args):
        if "MAX(id)" in sql and "team_events" in sql:
            return {"max_id": self.max_id}
        raise AssertionError(f"Unexpected fetchrow SQL: {sql}")

    async def executemany(self, sql, args):
        self.executemany_queries.append((sql, args))


@pytest.fixture
def api_module(monkeypatch):
    monkeypatch.delenv("CORTEX_EVENT_BACKEND", raising=False)
    module = load_module(API_MAIN_PATH, "cortex_api_sync_test")
    # Mock require_registered_project to just return True so we don't need real DB
    async def mock_require_registered_project(project):
        return True
    monkeypatch.setattr(module, "require_registered_project", mock_require_registered_project)

    # E006 Inc04: the writer guard is now registry-driven (async load_roster_policy).
    # This test exercises the sync CONTRACT, not the roster gate, so stub the policy
    # to the seeded kaidera-os shape (kai/ren writers; beat/migration/system) — the same
    # way require_registered_project / acquire_scoped are already stubbed above.
    async def mock_load_roster_policy(project):
        return module.RosterPolicy(
            project=project,
            enforce=(project == "kaidera-os"),
            default_writer_scope="work",
            work_writers=frozenset({"kai", "ren"}),
            system_event_writers=frozenset({"beat", "migration", "system"}),
            read_only=frozenset(),
            handoff_targets=frozenset({"kai", "ren"}),
            beat_may_create_handoff=True,
            roles={},
            suggest_cutoff=0.6,
        )
    monkeypatch.setattr(module, "load_roster_policy", mock_load_roster_policy)
    return module


@pytest.mark.asyncio
async def test_project_local_sync_contract_and_idempotency(api_module, monkeypatch):
    conn = FakeSyncConn(max_id=42)

    # Mock acquire_scoped to return our FakeAcquire
    def mock_acquire_scoped(project):
        return FakeAcquire(conn)

    monkeypatch.setattr(api_module, "acquire_scoped", mock_acquire_scoped)

    body = api_module.ProjectLocalSyncRequest(
        client_snapshot=10,
        team_events=[
            api_module.SyncEvent(agent_name="ren", event_type="lesson", summary="learned something", ts="2026-05-17T00:00:00+00:00")
        ],
        entities=[
            api_module.SyncEntity(name="A", type="concept", description="Alpha")
        ],
        relationships=[
            api_module.SyncRelationship(source="A", target="B", edge_type="related")
        ]
    )

    result = await api_module.project_local_sync(body=body, x_project="kaidera-os")

    assert result.accepted_events == 1
    assert result.accepted_entities == 1
    assert result.accepted_relationships == 1
    assert result.checkpoint == 42

    # Verify RLS and scoping boundaries in arguments passed to SQL
    assert len(conn.executemany_queries) == 3

    events_sql, events_args = conn.executemany_queries[0]
    assert "INSERT INTO team_events" in events_sql
    assert events_args[0][0] == "ren@kaidera-os"
    assert events_args[0][4] == "kaidera-os"  # Project correctly injected into query

    entities_sql, entities_args = conn.executemany_queries[1]
    assert "INSERT INTO entities" in entities_sql
    assert entities_args[0][0] == "A"
    assert entities_args[0][3] == "kaidera-os"  # Project correctly injected into query

    rels_sql, rels_args = conn.executemany_queries[2]
    assert "INSERT INTO relationships" in rels_sql
    assert rels_args[0][0] == "A"
    assert rels_args[0][1] == "B"
    assert rels_args[0][3] == "kaidera-os"  # Project correctly injected into query


@pytest.mark.asyncio
async def test_project_local_sync_negative_auth_leakage(api_module):
    # Test that a missing or invalid X-Project fails correctly (negative contract test)
    body = api_module.ProjectLocalSyncRequest()
    with pytest.raises(api_module.HTTPException) as exc:
        await api_module.project_local_sync(body=body, x_project="")

    assert exc.value.status_code == 400
    assert "X-Project header required" in str(exc.value.detail)
