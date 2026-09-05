import contextlib
import importlib.util
import io
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi import HTTPException
from starlette.requests import Request


API_MAIN_PATH = Path(__file__).resolve().parents[1] / "main.py"
CLAUDE_LOCAL_STATE_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "_cortex_claude_local_state.py"
)
TEST_ADMIN_TOKEN = "unit-test-admin-token"


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


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return FakeAcquire(self.conn)


class _FakeTxn:
    """No-op async transaction for the bounded /admin/sql/query cursor path."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _FakePreparedStmt:
    """Fake asyncpg PreparedStatement that reuses the conn's fetch() dispatch,
    so the bounded cursor handler streams the same rows the old fetch() returned."""

    _RETURNING = ("select", "with", "values", "show", "table", "explain")

    def __init__(self, conn, sql):
        self._conn = conn
        self._sql = sql

    def get_attributes(self):
        returns = self._sql.strip().lower().startswith(self._RETURNING)
        return (("col",),) if returns else ()

    async def fetch(self, *a, **k):
        return await self._conn.fetch(self._sql)

    def cursor(self, *a, **k):
        conn, sql = self._conn, self._sql

        class _Cur:
            def __aiter__(self):
                self._rows = None
                return self

            async def __anext__(self):
                if self._rows is None:
                    self._rows = iter(await conn.fetch(sql))
                try:
                    return next(self._rows)
                except StopIteration as exc:
                    raise StopAsyncIteration from exc

        return _Cur()


class FakeRedis:
    async def ping(self):
        return True

    async def execute_command(self, command, *args):
        return [command, *args]


class FakeConn:
    def __init__(self):
        now = datetime(2026, 4, 16, tzinfo=timezone.utc)
        self.config_row = {
            "embedding_provider": "openrouter",
            "embedding_model": "nvidia/llama-nemotron-embed-vl-1b-v2:free",
            "embedding_dims": 768,
            "rerank_enabled": True,
            "rerank_provider": "nvidia",
            "rerank_model": "nv-rerank-qa-mistral-4b:1",
            "analysis_provider": "openrouter",
            "analysis_model": "google/gemma-4-31b-it:free",
            "cortex_api_url": "http://localhost:8501",
            "boot_context_version": "v2.3",
            "max_boot_tokens": 300,
            "search_confidence_threshold": 0.02,
            "rrf_k": 50,
            "embed_input_max_chars": 600,
            "rerank_input_max_chars": 700,
            "embed_timeout_ms": 16000,
            "rerank_timeout_ms": 17000,
            "analysis_timeout_ms": 120000,
            "embedding_provider_config_id": uuid.uuid4(),
            "rerank_provider_config_id": uuid.uuid4(),
            "analysis_provider_config_id": uuid.uuid4(),
            "updated_at": now,
        }

    async def fetchrow(self, sql, *args):
        if "SELECT * FROM cortex_platform_config LIMIT 1" in sql:
            return self.config_row
        if sql.startswith("UPDATE cortex_platform_config SET "):
            assignments = sql.split("SET ", 1)[1].split(" WHERE ", 1)[0].split(", ")
            self.config_row = {
                **self.config_row,
                **{
                    assignment.split(" = ", 1)[0]: value
                    for assignment, value in zip(assignments, args)
                    if not assignment.startswith("updated_at")
                },
                "updated_at": datetime(2026, 4, 17, tzinfo=timezone.utc),
            }
            return self.config_row
        if "FROM agent_profiles" in sql:
            project, agent = args
            roles = {
                ("kaidera", "ren"): "full-stack-senior-developer",
                ("kaidera", "scribe"): "knowledge-keeper",
            }
            role = roles.get((project, agent))
            if role:
                return {"agent_name": agent, "role": role}
            return None
        if "id::text AS project_id" in sql and "FROM cortex_projects" in sql:
            project = args[0]
            return {
                "project_key": project,
                "project_id": "11111111-1111-4111-8111-111111111111",
                "display_name": project,
                "default_agent": "alpha",
                "repo_root": "/tmp/project",
                "repo_type": "repo",
                "status": "active",
            }
        if "SELECT default_agent FROM cortex_projects" in sql:
            return {"default_agent": "alpha"}
        if "FROM agents" in sql:
            return None
        raise AssertionError(f"Unexpected fetchrow SQL: {sql}")

    async def fetch(self, sql, *args):
        if "SELECT role, capabilities" in sql and "FROM agents" in sql:
            project, agent = args
            roles = {
                ("kaidera", "ren"): "full-stack-senior-developer",
                ("kaidera", "scribe"): "knowledge-keeper",
            }
            role = roles.get((project, agent))
            return [{"role": role, "capabilities": {}}] if role else []
        if "FROM cortex_entities" in sql:
            return [
                {
                    "id": uuid.uuid4(),
                    "name": "Auth Boundary",
                    "entity_type": "concept",
                    "project": "kaidera",
                    "created_at": datetime(2026, 4, 16, tzinfo=timezone.utc),
                }
            ]
        if "FROM pattern_metrics" in sql:
            return []
        if "quality_score" in sql and "WHERE id = ANY" in sql:
            return []
        if "cortex_search_decisions_trigram" in sql:
            return []
        if "cortex_search_messages_bm25" in sql:
            return []
        if "ts_rank_cd" in sql and "FROM knowledge" in sql:
            return []
        if "FROM knowledge" in sql:
            query = args[0]
            project = args[1]
            room = args[2]
            if project == "_local_state":
                return [
                    (
                        "knowledge-local-1",
                        "Claude todo for Amad local state",
                        "claude://todo/123",
                        "claude-todo",
                        "knowledge",
                    )
                ]
            if query == "migration":
                return [
                    (
                        "knowledge-project-1",
                        "Migration boundary note",
                        "/docs/migration.md",
                        "workspace-doc",
                        "knowledge",
                    )
                ]
            if room == "migration":
                return [
                    (
                        "knowledge-project-1",
                        "Migration boundary note",
                        "/docs/migration.md",
                        "workspace-doc",
                        "knowledge",
                    )
                ]
            return [
                (
                    "knowledge-project-2",
                    "Auth boundary note",
                    "/docs/auth.md",
                    "workspace-doc",
                    "knowledge",
                )
            ]
        if "FROM decisions" in sql or "FROM lessons" in sql:
            return []
        if "SELECT DISTINCT role" in sql:
            _, agent = args
            if agent == "scribe":
                return [{"role": "knowledge-keeper"}]
            if agent == "ren":
                return [{"role": "full-stack-senior-developer"}]
            return []
        if "FROM handoffs" in sql:
            if "to_role" in sql and "= ANY" in sql:
                roles = set(args[3])
                rows = []
                if "knowledge-keeper" in roles:
                    rows.append(
                        {
                            "id": "abcd1234efgh5678",
                            "from_agent": "alpha@kaidera",
                            "to_role": "knowledge-keeper",
                            "priority": "high",
                            "summary": "Verify Cortex remediation",
                            "status": "pending",
                            "created_at": "2026-04-11T10:00:00+00:00",
                        }
                    )
                if "alpha" in roles:
                    rows.append(
                        {
                            "id": "1234abcd5678efgh",
                            "from_agent": "root@kaidera",
                            "to_role": "alpha",
                            "priority": "high",
                            "summary": "Route Root backend lane",
                            "status": "pending",
                            "created_at": "2026-05-08T10:00:00+00:00",
                        }
                    )
                return rows
            return []
        if "FROM work_products" in sql:
            return []
        if "table_name = 'work_products'" in sql and "information_schema.columns" in sql:
            return []
        if sql.strip() == "SELECT 1":
            return [(1,)]
        raise AssertionError(f"Unexpected fetch SQL: {sql}")

    # Bounded /admin/sql/query support (Part C): a cursor needs a transaction +
    # a prepared statement; both reuse fetch() above for the row data.
    def transaction(self):
        return _FakeTxn()

    async def prepare(self, sql):
        return _FakePreparedStmt(self, sql)

    async def fetchval(self, sql, *args):
        if sql.strip() == "SELECT 1":
            return 1
        if "SELECT value FROM cortex_meta" in sql:
            return "2.2"
        if "table_name = 'cortex_platform_config'" in sql:
            return True
        if "table_name = 'cortex_entities'" in sql:
            return True
        if "table_name = 'cortex_relationships'" in sql:
            return True
        if "to_regclass('public.work_products')" in sql:
            return True
        if "SELECT count(*) FROM decisions" in sql and "embedding IS NOT NULL" in sql:
            return 6
        if "SELECT count(*) FROM decisions" in sql:
            return 8
        if "SELECT count(*) FROM cortex_entities" in sql:
            return 12
        if "SELECT count(*) FROM cortex_relationships" in sql:
            return 4
        if "SELECT count(DISTINCT agent_name) FROM decisions WHERE agent_name IS NOT NULL" in sql:
            return 3
        raise AssertionError(f"Unexpected fetchval SQL: {sql}")

    async def execute(self, sql, *args):
        return "UPDATE 1"


@pytest.fixture
def cortex_api(monkeypatch):
    module = load_module(API_MAIN_PATH, "cortex_api_main_test")
    module.ADMIN_TOKEN = TEST_ADMIN_TOKEN
    fake_pool = FakePool(FakeConn())
    module.pool = fake_pool
    module.pool_app = fake_pool
    module.pool_admin = fake_pool
    module.redis_client = FakeRedis()

    async def fake_embed_text(text):
        return None

    async def fake_search_graph(conn, project, query, room, limit=6):
        return []

    monkeypatch.setattr(module, "embed_text", fake_embed_text)
    monkeypatch.setattr(module, "search_graph", fake_search_graph)
    return module


@pytest.mark.asyncio
async def test_health_reports_api_and_schema_versions(cortex_api):
    result = await cortex_api.health()

    assert result["status"] == "healthy"
    assert result["version"] == "2.3"
    assert result["schema_version"] == "2.2"
    assert result["embed_provider"] == "openrouter"
    assert result["embed_model"] == "nvidia/llama-nemotron-embed-vl-1b-v2:free"
    assert result["rerank_provider"] == "nvidia"
    assert result["rerank_model"] == "nv-rerank-qa-mistral-4b:1"


@pytest.mark.asyncio
async def test_boot_query_returns_topic_recall(cortex_api):
    request = Request({"type": "http", "query_string": b"budget=500"})
    result = await cortex_api.boot(
        "ren",
        request,
        x_project="Kaidera",
        query="migration",
    )

    assert "TOPIC RECALL: migration" in result["boot"]
    assert "[knowledge] Migration boundary note" in result["boot"]


@pytest.mark.asyncio
async def test_boot_filters_pending_handoffs_to_agent_roles(cortex_api):
    request = Request({"type": "http", "query_string": b"budget=500"})

    scribe = await cortex_api.boot("scribe", request, x_project="Kaidera", query=None)
    ren = await cortex_api.boot("ren", request, x_project="Kaidera", query=None)

    assert "Verify Cortex remediation" in scribe["boot"]
    assert "Verify Cortex remediation" not in ren["boot"]
    assert "Route Root backend lane" not in ren["boot"]
    assert "No pending handoffs." in ren["boot"]


@pytest.mark.asyncio
async def test_boot_injects_current_work_product_briefs_for_claimed_handoff(cortex_api):
    class BootBriefConn(FakeConn):
        async def fetch(self, sql, *args):
            if "FROM handoffs" in sql:
                if len(args) > 1 and args[1] == "claimed":
                    return [
                        {
                            "id": "11111111-1111-4111-8111-111111111111",
                            "from_agent": "kai@kaidera",
                            "to_agent": "ren",
                            "priority": "high",
                            "summary": "Harden the run-agent loop",
                            "files_changed": ["beat/run-agent.sh"],
                        }
                    ]
                return []
            if "FROM work_products" in sql:
                return [
                    {
                        "id": "22222222-2222-4222-8222-222222222222",
                        "project": "kaidera",
                        "handoff_id": "11111111-1111-4111-8111-111111111111",
                        "agent_name": "kai@kaidera",
                        "activity_type": "task-completed",
                        "status": "current",
                        "title": "run-agent loop hardening",
                        "summary": "Uses API-backed Cortex boot and preserves loop state.",
                        "behavior_summary": "",
                        "architecture_notes": "",
                        "files_changed": ["beat/run-agent.sh"],
                        "symbols_changed": [],
                        "subject_entities": [],
                        "artifact_refs": [],
                        "risks": [],
                        "followups": [],
                        "tests_run": [],
                        "file_hashes": {},
                        "symbol_hashes": {},
                        "metadata": {},
                        "freshness_status": "current",
                        "projection_status": "projected",
                    }
                ]
            return await super().fetch(sql, *args)

    fake_pool = FakePool(BootBriefConn())
    cortex_api.pool = fake_pool
    cortex_api.pool_app = fake_pool
    cortex_api.pool_admin = fake_pool

    request = Request({"type": "http", "query_string": b"budget=500"})
    result = await cortex_api.boot("ren", request, x_project="Kaidera", query=None)

    assert "CURRENT WORK-PRODUCT BRIEFS" in result["boot"]
    assert "run-agent loop hardening" in result["boot"]
    assert "beat/run-agent.sh" in result["boot"]


def test_truncate_boot_tier_preserves_complete_lines(cortex_api):
    text = "Recent decisions:\n  - [REAL] " + ("x" * 80) + "\n  - second"
    truncated = cortex_api.truncate_boot_tier(text, 114)

    assert truncated.endswith("\n  ... [truncated]")
    assert "  - [REAL]" in truncated
    assert "  - second" not in truncated


@pytest.mark.asyncio
async def test_boot_filters_lib_guard_recent_decisions(cortex_api):
    class BootNoiseConn(FakeConn):
        async def fetch(self, sql, *args):
            if "LEFT(COALESCE(summary, ''), 100) as summary" in sql and "FROM decisions" in sql:
                assert "NOT LIKE '[LIB-GUARD-DIRECT-PG:%'" in sql
                return [{"summary": "[REAL-DECISION] survives compact boot"}]
            return await super().fetch(sql, *args)

    fake_pool = FakePool(BootNoiseConn())
    cortex_api.pool = fake_pool
    cortex_api.pool_app = fake_pool
    cortex_api.pool_admin = fake_pool

    request = Request({"type": "http", "query_string": b"budget=500"})
    result = await cortex_api.boot("ren", request, x_project="Kaidera", query=None)

    assert "[REAL-DECISION] survives compact boot" in result["boot"]
    assert "LIB-GUARD-DIRECT-PG" not in result["boot"]


@pytest.mark.asyncio
async def test_search_supports_local_hall(cortex_api):
    result = await cortex_api.search(
        "amadmalik",
        x_project="kaidera",
        type="all",
        rerank=False,
        room=None,
        hall="local",
        graph=False,
    )

    assert result["hall"] == "local"
    assert result["results"][0]["meta"] == "claude://todo/123"
    assert result["results"][0]["category"] == "claude-todo"


@pytest.mark.asyncio
async def test_search_supports_room_scoping(cortex_api):
    result = await cortex_api.search(
        "auth",
        x_project="kaidera",
        type="all",
        rerank=False,
        room="migration",
        hall="project",
        graph=False,
    )

    assert result["room"] == "migration"
    assert result["results"][0]["text"] == "Migration boundary note"


@pytest.mark.asyncio
async def test_search_respects_limit(cortex_api):
    result = await cortex_api.search(
        "auth",
        x_project="kaidera",
        type="all",
        rerank=False,
        room=None,
        hall="project",
        graph=False,
        limit=1,
    )

    assert len(result["results"]) == 1


@pytest.mark.asyncio
async def test_handoffs_agent_filter_matches_role(cortex_api):
    result = await cortex_api.list_handoffs(
        x_project="kaidera",
        status="pending",
        agent="scribe",
    )

    assert len(result["handoffs"]) == 1
    assert result["handoffs"][0]["to_role"] == "knowledge-keeper"


@pytest.mark.asyncio
async def test_handoffs_agent_filter_matches_direct_agent_role(cortex_api):
    result = await cortex_api.list_handoffs(
        x_project="kaidera",
        status="pending",
        agent="alpha",
    )

    assert len(result["handoffs"]) == 1
    assert result["handoffs"][0]["to_role"] == "alpha"


@pytest.mark.asyncio
async def test_admin_sql_requires_token(cortex_api):
    denied_request = Request({"type": "http", "headers": []})

    with pytest.raises(HTTPException) as excinfo:
        await cortex_api.admin_sql_query(
            cortex_api.SqlRequest(sql="SELECT 1"),
            denied_request,
        )

    assert excinfo.value.status_code == 403

    allowed_request = Request(
        {
            "type": "http",
            "headers": [
                (b"x-cortex-admin-token", cortex_api.ADMIN_TOKEN.encode("utf-8"))
            ],
        }
    )

    result = await cortex_api.admin_sql_query(
        cortex_api.SqlRequest(sql="SELECT 1"),
        allowed_request,
    )

    assert result["rows"] == [[1]]


@pytest.mark.asyncio
async def test_admin_cortex_health_reports_metrics(cortex_api):
    request = Request(
        {
            "type": "http",
            "headers": [
                (b"x-cortex-admin-token", cortex_api.ADMIN_TOKEN.encode("utf-8"))
            ],
        }
    )

    result = await cortex_api.admin_cortex_health(request)

    assert result["status"] == "healthy"
    assert result["embedding_coverage_pct"] == 75.0
    assert result["entity_count"] == 12
    assert result["relationship_count"] == 4


@pytest.mark.asyncio
async def test_admin_cortex_config_round_trip(cortex_api):
    request = Request(
        {
            "type": "http",
            "headers": [
                (b"x-cortex-admin-token", cortex_api.ADMIN_TOKEN.encode("utf-8"))
            ],
        }
    )

    initial = await cortex_api.admin_cortex_config(request)
    updated = await cortex_api.admin_cortex_update_config(
        cortex_api.CortexAdminConfigUpdate(
            analysis_timeout_ms=90000,
            analysis_provider_config_id=None,
        ),
        request,
    )

    assert initial["analysis_model"] == "google/gemma-4-31b-it:free"
    assert updated["analysis_timeout_ms"] == 90000
    assert updated["analysis_provider_config_id"] is None
    assert cortex_api._platform_config_cache["config"]["analysis_timeout_ms"] == 90000


def test_cortex_rerank_timeout_default_is_interactive_safe(cortex_api):
    assert cortex_api.CORTEX_PLATFORM_DEFAULTS["rerank_timeout_ms"] == 2500
    assert cortex_api._provider_timeout({}, "rerank") == 2.5


def test_openapi_exposes_cortex_doctor_route(cortex_api):
    paths = cortex_api.app.openapi()["paths"]

    assert "/admin/cortex/doctor" in paths


def test_claude_local_state_defaults_to_local_hall(monkeypatch, tmp_path):
    helper = load_module(CLAUDE_LOCAL_STATE_PATH, "cortex_claude_local_state_test")
    todo_file = tmp_path / "todo.json"
    todo_file.write_text('[{"content": "Review importer default"}]')
    output_sql = tmp_path / "local_state.sql"

    monkeypatch.setattr(helper, "discover_todos", lambda: [todo_file])
    monkeypatch.setattr(helper, "discover_plans", lambda: [])
    monkeypatch.setattr(helper, "discover_indexeddb_dirs", lambda: [])
    monkeypatch.setattr(helper, "load_indexeddb_modules", lambda vendor_dir: (None, None, None))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cortex-local-state",
            "--output-sql",
            str(output_sql),
            "--skip-plans",
            "--skip-indexeddb",
        ],
    )

    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        helper.main()

    sql_text = output_sql.read_text()
    assert "_local_state" in sql_text


def test_openapi_exposes_layer4_graph_routes(cortex_api):
    paths = cortex_api.app.openapi()["paths"]

    assert "/cortex-graph-extract" in paths
    assert "/cortex-graph-search" in paths
    assert "/beat/projections/status" in paths


def test_openapi_exposes_layer3_worker_proxy_routes(cortex_api):
    paths = cortex_api.app.openapi()["paths"]

    assert "/workers/health" in paths
    assert "/graph/stats" in paths
    assert "/graph/prune" in paths
    assert "/graph/build" in paths
    assert "/graph/build/jobs/{job_id}" in paths
    assert "/graph/blast" in paths
    assert "/graph/callers" in paths
    assert "/graph/impact" in paths
    assert "/graph/large-fn" in paths


def test_runtime_profile_excludes_terminal_adapter_contract(cortex_api):
    profile = cortex_api.build_runtime_profile(
        {
            "project_key": "kaidera-os",
            "display_name": "kaidera-os",
            "project_id": "22222222-2222-4222-8222-222222222222",
            "parent_project_key": None,
            "repo_root": "/tmp/kaidera-os",
            "repo_type": "repo",
            "status": "active",
            "default_agent": "kai",
            "metadata": {
                "roots": [{"path": "/tmp/kaidera-os", "kind": "primary"}],
                "warp": {
                    "window_prefix": "legacy-kaidera-os",
                    "generate_inter": True,
                    "generate_auto": False,
                    "generate_local": True,
                },
                "cmux": {
                    "workspace_prefix": "kaidera-os-fleet",
                    "generate_fleet": True,
                    "generate_inter": False,
                    "generate_auto": True,
                    "config_path": "/tmp/cmux.json",
                    "windows": [{"name": "Kaidera OS"}],
                },
            },
        },
        [
            {
                "root_path": "/tmp/kaidera-os",
                "path_kind": "primary",
                "metadata": {"path": "/tmp/kaidera-os", "kind": "primary"},
            }
        ],
        [
            {
                "name": "kai",
                "role": "full-stack-developer",
                "model": "gpt-5.5",
                "capabilities": {"harness": "codex"},
            }
        ],
        {"cortex_api_url": "http://localhost:8501"},
    )

    assert profile["project_id"] == "22222222-2222-4222-8222-222222222222"
    assert "project_hex" not in profile
    assert "cmux" not in profile
    assert "warp" not in profile
    assert "CORTEX_PROJECT_HEX" not in profile["beat"]["env"]
