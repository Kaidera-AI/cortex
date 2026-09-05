import importlib.util
import json
import uuid
from pathlib import Path

import pytest
from starlette.requests import Request


API_MAIN_PATH = Path(__file__).resolve().parents[1] / "main.py"
TEST_ADMIN_TOKEN = "test-admin-token"
MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "data"
    / "migrations"
    / "2026-05-16-team-events-hardening.sql"
)


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    module.ADMIN_TOKEN = TEST_ADMIN_TOKEN
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


class FakeHealthConn:
    async def fetchval(self, sql, *args):
        if sql.strip() == "SELECT 1":
            return 1
        if "SELECT value FROM cortex_meta" in sql:
            return "2.3"
        if "pg_notification_queue_usage" in sql:
            return 0
        raise AssertionError(f"Unexpected fetchval SQL: {sql}")


class HealthyRedis:
    async def ping(self):
        return True


class FailingRedis:
    async def ping(self):
        raise RuntimeError("redis unavailable")


class FakeTransaction:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        self.conn.transaction_entered = True
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.conn.transaction_exited = True
        return False


class FakeEventConn:
    def __init__(self):
        self.transaction_entered = False
        self.transaction_exited = False
        self.insert_args = None
        self.notify_args = None

    def transaction(self):
        return FakeTransaction(self)

    async def fetchval(self, sql, *args):
        assert "INSERT INTO team_events" in sql
        assert "RETURNING id" in sql
        self.insert_args = args
        return 4456

    async def execute(self, sql, *args):
        assert sql == "SELECT pg_notify('cortex_events', $1)"
        self.notify_args = args
        return "SELECT 1"


class FakeTeamEventConn:
    def __init__(self, rows):
        self.rows = rows

    async def execute(self, sql, *args):
        if "set_config('cortex.project'" in sql:
            return "SELECT 1"
        raise AssertionError(f"Unexpected execute SQL: {sql}")

    async def fetchval(self, sql, *args):
        if "COALESCE(MAX(id), 0)" in sql and "FROM team_events" in sql:
            project = args[0]
            ids = [row["id"] for row in self.rows if row["project"] == project]
            return max(ids) if ids else 0
        raise AssertionError(f"Unexpected fetchval SQL: {sql}")

    async def fetch(self, sql, *args):
        if "id > $2" in sql and "FROM team_events" in sql:
            project, cursor, count = args
            return [
                row
                for row in sorted(self.rows, key=lambda item: item["id"])
                if row["project"] == project and row["id"] > cursor
            ][:count]
        if "FROM (" in sql and "FROM team_events" in sql:
            project, count = args
            latest = [
                row
                for row in sorted(self.rows, key=lambda item: item["id"], reverse=True)
                if row["project"] == project
            ][:count]
            return list(reversed(latest))
        raise AssertionError(f"Unexpected fetch SQL: {sql}")


class FakeCondition:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def wait(self):
        return True


def admin_request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/beat/events",
            "headers": [(b"x-cortex-admin-token", TEST_ADMIN_TOKEN.encode())],
            "query_string": b"",
        }
    )


def team_event_row(
    event_id: int,
    project: str = "kaidera",
    event_type: str = "lesson",
    summary: str = "event summary",
) -> dict:
    return {
        "id": event_id,
        "project": project,
        "agent_name": "ren@kaidera",
        "event_type": event_type,
        "summary": summary,
        "detail": {"event_id": event_id},
        "files": ["Program/E75.md"] if event_id % 2 == 0 else None,
        "sprint_id": None,
        "related_decision_id": None,
        "ts": "2026-05-17T00:00:00+00:00",
    }


@pytest.mark.asyncio
async def test_emit_team_event_inserts_and_notifies_id_only(monkeypatch):
    monkeypatch.delenv("CORTEX_EVENT_BACKEND", raising=False)
    module = load_module(API_MAIN_PATH, "cortex_api_event_helper_test")
    conn = FakeEventConn()
    sprint_id = uuid.uuid4()
    decision_id = uuid.uuid4()

    event_id = await module.emit_team_event(
        conn,
        project="Kaidera",
        agent_name="ren@kaidera",
        event_type="decision",
        summary="Inc 21 helper contract",
        detail={"decision_id": str(decision_id)},
        files=["Program/E75.md"],
        sprint_id=sprint_id,
        related_decision_id=decision_id,
    )

    assert event_id == 4456
    assert conn.transaction_entered is True
    assert conn.transaction_exited is True
    assert conn.insert_args[0] == "kaidera"
    assert conn.insert_args[1:4] == (
        "ren@kaidera",
        "decision",
        "Inc 21 helper contract",
    )
    assert json.loads(conn.insert_args[4]) == {"decision_id": str(decision_id)}
    assert conn.insert_args[5] == ["Program/E75.md"]
    assert conn.insert_args[6] == str(sprint_id)
    assert conn.insert_args[7] == str(decision_id)
    assert conn.notify_args == ("4456",)


@pytest.mark.asyncio
async def test_emit_team_event_can_skip_notify(monkeypatch):
    monkeypatch.delenv("CORTEX_EVENT_BACKEND", raising=False)
    module = load_module(API_MAIN_PATH, "cortex_api_event_no_notify_test")
    conn = FakeEventConn()

    event_id = await module.emit_team_event(
        conn,
        project="kaidera",
        agent_name="ren@kaidera",
        event_type="started",
        summary="No notify fixture",
        notify=False,
    )

    assert event_id == 4456
    assert conn.notify_args is None


def test_event_backend_defaults_to_postgres(monkeypatch):
    monkeypatch.delenv("CORTEX_EVENT_BACKEND", raising=False)
    module = load_module(API_MAIN_PATH, "cortex_api_event_default_test")

    assert module.CORTEX_EVENT_BACKEND == "postgres"
    assert module.parse_event_backend("POSTGRES") == "postgres"
    for invalid_backend in ("redis", "dual", "kafka"):
        with pytest.raises(RuntimeError, match="Invalid CORTEX_EVENT_BACKEND"):
            module.parse_event_backend(invalid_backend)


@pytest.mark.asyncio
async def test_admin_redis_passthrough_returns_gone(monkeypatch):
    monkeypatch.delenv("CORTEX_EVENT_BACKEND", raising=False)
    module = load_module(API_MAIN_PATH, "cortex_api_admin_redis_gone_test")

    with pytest.raises(module.HTTPException) as exc:
        await module.admin_redis(admin_request())

    assert exc.value.status_code == 410
    assert "/admin/redis has been removed" in exc.value.detail


def test_invalid_event_backend_fails_clearly(monkeypatch):
    monkeypatch.setenv("CORTEX_EVENT_BACKEND", "kafka")

    with pytest.raises(RuntimeError, match="Invalid CORTEX_EVENT_BACKEND"):
        load_module(API_MAIN_PATH, "cortex_api_event_invalid_test")


@pytest.mark.asyncio
async def test_health_postgres_backend_does_not_require_redis(monkeypatch):
    monkeypatch.delenv("CORTEX_EVENT_BACKEND", raising=False)
    module = load_module(API_MAIN_PATH, "cortex_api_health_postgres_test")
    module.CORTEX_EVENT_BACKEND = "postgres"
    module.pool_admin = FakePool(FakeHealthConn())

    result = await module.health()

    assert result["status"] == "healthy"
    assert result["postgres"] == "connected"
    assert "redis" not in result
    assert result["event_store"] == "postgres"
    assert result["event_backend"] == "postgres"
    assert result["event_bus"] == "postgres"
    assert result["pg_notification_queue_usage"] == 0.0


@pytest.mark.asyncio
async def test_beat_events_postgres_initial_subscribe_uses_project_max(monkeypatch):
    monkeypatch.delenv("CORTEX_EVENT_BACKEND", raising=False)
    module = load_module(API_MAIN_PATH, "cortex_api_beat_events_initial_test")
    module.CORTEX_EVENT_BACKEND = "postgres"
    module.pool_app = FakePool(
        FakeTeamEventConn([
            team_event_row(7, project="other"),
            team_event_row(12, project="kaidera"),
        ])
    )

    result = await module.beat_events(
        admin_request(),
        x_project="kaidera",
        last_id="",
        count=50,
    )

    assert result == {"stream": "kaidera:cortex:events", "last_id": "12", "events": []}


@pytest.mark.asyncio
async def test_beat_events_postgres_legacy_redis_cursor_resubscribes(monkeypatch):
    monkeypatch.delenv("CORTEX_EVENT_BACKEND", raising=False)
    module = load_module(API_MAIN_PATH, "cortex_api_beat_events_legacy_test")
    module.CORTEX_EVENT_BACKEND = "postgres"
    module.pool_app = FakePool(FakeTeamEventConn([team_event_row(25)]))

    result = await module.beat_events(
        admin_request(),
        x_project="kaidera",
        last_id="1715918200000-0",
        count=50,
    )

    assert result["last_id"] == "25"
    assert result["events"] == []


@pytest.mark.asyncio
async def test_beat_events_postgres_filters_and_advances_by_project(monkeypatch):
    monkeypatch.delenv("CORTEX_EVENT_BACKEND", raising=False)
    module = load_module(API_MAIN_PATH, "cortex_api_beat_events_filter_test")
    module.CORTEX_EVENT_BACKEND = "postgres"
    module.pool_app = FakePool(
        FakeTeamEventConn([
            team_event_row(10, project="other", summary="other older"),
            team_event_row(11, project="kaidera", summary="local first"),
            team_event_row(12, project="other", summary="other newer"),
            team_event_row(13, project="kaidera", event_type="artifact", summary="local second"),
        ])
    )

    result = await module.beat_events(
        admin_request(),
        x_project="kaidera",
        last_id="10",
        count=50,
    )

    assert result["last_id"] == "13"
    assert [event["id"] for event in result["events"]] == ["11", "13"]
    assert [event["fields"]["project"] for event in result["events"]] == ["kaidera", "kaidera"]
    assert result["events"][1]["fields"]["type"] == "artifact"


@pytest.mark.asyncio
async def test_beat_events_postgres_other_project_wakeup_does_not_advance(monkeypatch):
    monkeypatch.delenv("CORTEX_EVENT_BACKEND", raising=False)
    module = load_module(API_MAIN_PATH, "cortex_api_beat_events_no_advance_test")
    module.CORTEX_EVENT_BACKEND = "postgres"
    module.pool_app = FakePool(
        FakeTeamEventConn([
            team_event_row(20, project="kaidera"),
            team_event_row(99, project="other"),
        ])
    )
    monkeypatch.setattr(module, "ensure_event_condition", lambda: FakeCondition())

    async def no_wait(awaitable, timeout):
        if hasattr(awaitable, "close"):
            awaitable.close()
        raise module.asyncio.TimeoutError

    monkeypatch.setattr(module.asyncio, "wait_for", no_wait)

    result = await module.beat_events(
        admin_request(),
        x_project="kaidera",
        last_id="20",
        count=50,
    )

    assert result["last_id"] == "20"
    assert result["events"] == []


@pytest.mark.asyncio
async def test_beat_events_recent_returns_latest_team_events(monkeypatch):
    monkeypatch.delenv("CORTEX_EVENT_BACKEND", raising=False)
    module = load_module(API_MAIN_PATH, "cortex_api_beat_events_recent_test")
    module.CORTEX_EVENT_BACKEND = "postgres"
    module.pool_app = FakePool(
        FakeTeamEventConn([
            team_event_row(1, summary="old"),
            team_event_row(2, summary="middle"),
            team_event_row(3, summary="new"),
            team_event_row(4, project="other", summary="foreign"),
        ])
    )

    result = await module.beat_events(
        admin_request(),
        x_project="kaidera",
        last_id="",
        count=2,
        recent=True,
    )

    assert result["last_id"] == "3"
    assert [event["id"] for event in result["events"]] == ["2", "3"]
    assert result["events"][0]["fields"]["detail"] == '{"event_id": 2}'


@pytest.mark.asyncio
async def test_beat_events_recent_uses_team_events_before_backend_cutover(monkeypatch):
    monkeypatch.delenv("CORTEX_EVENT_BACKEND", raising=False)
    module = load_module(API_MAIN_PATH, "cortex_api_beat_events_recent_test_2")
    module.CORTEX_EVENT_BACKEND = "postgres"
    module.pool_app = FakePool(FakeTeamEventConn([team_event_row(8)]))

    result = await module.beat_events(
        admin_request(),
        x_project="kaidera",
        last_id="",
        count=20,
        recent=True,
    )

    assert result["last_id"] == "8"
    assert [event["id"] for event in result["events"]] == ["8"]


@pytest.mark.asyncio
async def test_beat_events_team_events_flag_forces_pg_cursor_before_cutover(monkeypatch):
    monkeypatch.delenv("CORTEX_EVENT_BACKEND", raising=False)
    module = load_module(API_MAIN_PATH, "cortex_api_beat_events_force_pg_test")
    module.CORTEX_EVENT_BACKEND = "postgres"
    module.pool_app = FakePool(FakeTeamEventConn([team_event_row(31)]))

    result = await module.beat_events(
        admin_request(),
        x_project="kaidera",
        last_id="",
        count=20,
        team_events=True,
    )

    assert result == {"stream": "kaidera:cortex:events", "last_id": "31", "events": []}


def test_inc22_publishers_use_event_helper_contract():
    source = API_MAIN_PATH.read_text()
    bootstrap = source.split('@app.get("/bootstrap/{agent}")', 1)[1].split(
        "# ---------------------------------------------------------------------------\n# POST /log",
        1,
    )[0]
    artifacts = source.split('@app.post("/artifacts")', 1)[1].split(
        "# ---------------------------------------------------------------------------\n# GET /search",
        1,
    )[0]

    assert "await emit_team_event(" in bootstrap
    assert "event_type=\"session_start\"" in bootstrap
    assert "INSERT INTO team_events (agent_name, event_type, summary, project, ts)" not in bootstrap
    assert "event_backend_uses_redis" not in bootstrap
    assert "redis_client" not in bootstrap

    assert "await emit_team_event(" in artifacts
    assert "event_type=\"artifact\"" in artifacts
    assert '"artifact_id": row["id"]' in artifacts
    assert '"source_file": row["source_file"]' in artifacts
    assert "event_backend_uses_redis" not in artifacts
    assert "redis_client" not in artifacts


def test_log_decisions_and_lessons_publish_team_events_without_redis():
    source = API_MAIN_PATH.read_text()
    log_event = source.split('@app.post("/log")', 1)[1].split(
        "    # Generic event (commit, started, stopped, etc.)",
        1,
    )[0]

    assert "await emit_team_event(" in log_event
    assert "event_type=body.event_type" in log_event
    assert '"source_table": table' in log_event
    assert '"row_id": str(row_id)' in log_event
    assert "redis_client" not in log_event
    assert ".xadd(" not in log_event


def test_api_source_has_no_redis_runtime_dependency():
    source = API_MAIN_PATH.read_text()

    assert "import redis.asyncio" not in source
    assert "CORTEX_REDIS_URL" not in source
    assert "redis_client" not in source
    assert ".xadd(" not in source
    assert ".xread(" not in source
    assert ".xrevrange(" not in source


class FakeDisconnectRequest:
    """Minimal Request stand-in exposing the disconnect probe the SSE generator
    uses. ``disconnect_after`` controls how many probes return False (connected)
    before it reports True (gone), so a test can bound the generator's lifetime.
    """

    def __init__(self, disconnect_after: int = 10):
        self._remaining = disconnect_after

    async def is_disconnected(self) -> bool:
        if self._remaining <= 0:
            return True
        self._remaining -= 1
        return False


async def _drain_sse(generator, limit: int = 50) -> list:
    """Collect SSE items from the additive generator until it stops or ``limit``."""
    items = []
    async for item in generator:
        items.append(item)
        if len(items) >= limit:
            await generator.aclose()
            break
    return items


@pytest.mark.asyncio
async def test_events_sse_emits_only_project_scoped_rows(monkeypatch):
    """No cross-project leak: a stream for `kaidera` emits only kaidera rows and
    advances its cursor past the foreign row without ever surfacing it."""
    monkeypatch.delenv("CORTEX_EVENT_BACKEND", raising=False)
    module = load_module(API_MAIN_PATH, "cortex_api_events_sse_scope_test")
    module.CORTEX_EVENT_BACKEND = "postgres"
    module.pool_app = FakePool(
        FakeTeamEventConn([
            team_event_row(10, project="other", summary="foreign older"),
            team_event_row(11, project="kaidera", summary="local first"),
            team_event_row(12, project="other", summary="foreign newer"),
            team_event_row(13, project="kaidera", event_type="artifact", summary="local second"),
        ])
    )

    request = FakeDisconnectRequest(disconnect_after=1)
    generator = module.team_events_sse_generator(
        request, "kaidera", cursor=10, count=50, ping_seconds=0.01
    )
    items = await _drain_sse(generator)

    data_items = [item for item in items if "data" in item and item.get("event") != "error"]
    payloads = [json.loads(item["data"]) for item in data_items]
    # Only kaidera rows, in id order, foreign rows 10/12 never appear.
    assert [p["id"] for p in payloads] == ["11", "13"]
    assert {p["fields"]["project"] for p in payloads} == {"kaidera"}
    assert [item["id"] for item in data_items] == ["11", "13"]
    assert data_items[1]["event"] == "artifact"


@pytest.mark.asyncio
async def test_events_sse_foreign_wakeup_yields_keepalive_not_event(monkeypatch):
    """Reusing the shared NOTIFY Condition: a wakeup with no NEW project rows
    must not emit a data frame for this project — it falls through to a ping."""
    monkeypatch.delenv("CORTEX_EVENT_BACKEND", raising=False)
    module = load_module(API_MAIN_PATH, "cortex_api_events_sse_keepalive_test")
    module.CORTEX_EVENT_BACKEND = "postgres"
    module.pool_app = FakePool(
        FakeTeamEventConn([
            team_event_row(20, project="kaidera"),
            team_event_row(99, project="other"),
        ])
    )
    # Force the Condition path and make wait() time out (idle / foreign wakeup).
    monkeypatch.setattr(module, "ensure_event_condition", lambda: FakeCondition())

    async def timeout_wait(awaitable, timeout):
        if hasattr(awaitable, "close"):
            awaitable.close()
        raise module.asyncio.TimeoutError

    monkeypatch.setattr(module.asyncio, "wait_for", timeout_wait)

    # Cursor already at the project max (20): no new kaidera rows -> keep-alive.
    request = FakeDisconnectRequest(disconnect_after=1)
    generator = module.team_events_sse_generator(
        request, "kaidera", cursor=20, count=50, ping_seconds=0.01
    )
    items = await _drain_sse(generator)

    assert {"comment": "ping"} in items
    assert all("data" not in item for item in items)


@pytest.mark.asyncio
async def test_events_sse_stops_on_client_disconnect(monkeypatch):
    """Clean cancellation: the generator terminates once the client disconnects."""
    monkeypatch.delenv("CORTEX_EVENT_BACKEND", raising=False)
    module = load_module(API_MAIN_PATH, "cortex_api_events_sse_disconnect_test")
    module.CORTEX_EVENT_BACKEND = "postgres"
    module.pool_app = FakePool(FakeTeamEventConn([team_event_row(5, project="kaidera")]))

    request = FakeDisconnectRequest(disconnect_after=0)  # disconnected immediately
    generator = module.team_events_sse_generator(
        request, "kaidera", cursor=0, count=50, ping_seconds=0.01
    )
    items = [item async for item in generator]

    assert items == []


@pytest.mark.asyncio
async def test_events_endpoint_initial_cursor_uses_project_max(monkeypatch):
    """GET /events with no last_id resolves the start cursor from the project max
    under the scoped pool, and returns an SSE response object."""
    monkeypatch.delenv("CORTEX_EVENT_BACKEND", raising=False)
    module = load_module(API_MAIN_PATH, "cortex_api_events_endpoint_test")
    module.CORTEX_EVENT_BACKEND = "postgres"
    module.pool_app = FakePool(
        FakeTeamEventConn([
            team_event_row(7, project="other"),
            team_event_row(12, project="kaidera"),
        ])
    )

    captured = {}

    def fake_generator(request, project, cursor, count, ping_seconds):
        captured["project"] = project
        captured["cursor"] = cursor

        async def _gen():
            if False:
                yield {}

        return _gen()

    monkeypatch.setattr(module, "team_events_sse_generator", fake_generator)

    async def fake_require_registered_project(project):
        return {"project_key": project, "project_id": "44444444-4444-4444-8444-444444444444"}

    monkeypatch.setattr(module, "require_registered_project", fake_require_registered_project)

    response = await module.stream_team_events(
        FakeDisconnectRequest(),
        x_project="kaidera",
        last_id="",
        count=50,
        ping_seconds=15.0,
    )

    # Initial cursor is the project max (12), NOT the foreign row (7).
    assert captured == {"project": "kaidera", "cursor": 12}
    assert response.media_type == "text/event-stream" or "event-stream" in str(
        getattr(response, "media_type", "")
    )


def test_team_events_hardening_migration_shape():
    sql = MIGRATION_PATH.read_text()

    assert "ALTER COLUMN project DROP DEFAULT" in sql
    assert "ALTER COLUMN project SET NOT NULL" in sql
    assert "idx_team_events_project_id" in sql
    assert "ON team_events (project, id)" in sql
    assert "idx_team_events_project_ts_desc" in sql
    assert "ON team_events (project, ts DESC)" in sql
    assert "RAISE EXCEPTION" in sql
    assert "SET project = 'kaidera'" not in sql
    assert "SET project = 'tam'" not in sql
