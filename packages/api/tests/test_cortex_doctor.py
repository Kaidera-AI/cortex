from __future__ import annotations

import importlib.util
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from starlette.requests import Request


API_MAIN_PATH = Path(__file__).resolve().parents[1] / "main.py"
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


class DoctorConn:
    def __init__(
        self,
        *,
        degenerate_messages: bool = False,
        polluted_registry: bool = False,
        transcript_flood: bool = False,
        autovacuum_active: bool = False,
        retention_breach: bool = False,
    ):
        now = datetime(2026, 6, 26, tzinfo=timezone.utc)
        self.degenerate_messages = degenerate_messages
        self.polluted_registry = polluted_registry
        self.transcript_flood = transcript_flood
        self.autovacuum_active = autovacuum_active
        self.retention_breach = retention_breach
        self.rows = {
            "messages": 60000 if degenerate_messages else 1000,
            "decisions": 1000,
            "lessons": 200,
            "knowledge": 500,
            "work_products": 100,
            "team_events": 1000,
        }
        self.embedded = {
            "messages": 60000 if degenerate_messages else 1000,
            "decisions": 1000,
            "lessons": 200,
            "knowledge": 500,
            "work_products": 100,
        }
        self.config_row = {
            "embedding_provider": "openrouter",
            "embedding_model": "nvidia/llama-nemotron-embed-vl-1b-v2:free",
            "embedding_dims": 768,
            "rerank_enabled": False,
            "rerank_provider": "openrouter",
            "rerank_model": "cohere/rerank-4-fast",
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
            "rerank_timeout_ms": 3000,
            "analysis_timeout_ms": 120000,
            "embedding_provider_config_id": uuid.uuid4(),
            "rerank_provider_config_id": uuid.uuid4(),
            "analysis_provider_config_id": uuid.uuid4(),
            "updated_at": now,
        }

    async def fetchrow(self, sql, *args):
        if "FROM pg_class c" in sql and "c.relname = $1" in sql:
            table = args[0]
            if table not in self.rows:
                return None
            return {
                "table_name": table,
                "heap_bytes": 100 * 1024 * 1024,
                "index_bytes": 50 * 1024 * 1024,
                "total_bytes": 150 * 1024 * 1024,
                "estimated_live_rows": self.rows[table],
                "dead_rows": 10,
                "last_autovacuum": datetime(2026, 6, 25, tzinfo=timezone.utc),
            }
        if "COUNT(embedding)::bigint AS embedded_rows" in sql:
            table = re.search(r'FROM public."([^"]+)"', sql).group(1)
            total = self.rows[table]
            embedded = self.embedded[table]
            return {"embedded_rows": embedded, "null_embeddings": total - embedded}
        if "SELECT * FROM cortex_platform_config LIMIT 1" in sql:
            return self.config_row
        if "FROM retention_config" in sql:
            return {"table_name": "messages", "tier2_days": 90}
        if "last_7d_rows" in sql and "FROM public.messages" in sql:
            return {"total_rows": self.rows["messages"], "last_7d_rows": 100}
        if "messages_5m" in sql and "FROM public.messages" in sql:
            if self.transcript_flood:
                return {"messages_5m": 5306, "messages_1h": 25000, "messages_2h": 30740}
            return {"messages_5m": 12, "messages_1h": 120, "messages_2h": 240}
        raise AssertionError(f"Unexpected fetchrow SQL: {sql}")

    async def fetchval(self, sql, *args):
        if "information_schema.columns" in sql and "column_name = 'embedding'" in sql:
            return args[0] in self.embedded
        if sql.startswith('SELECT COUNT(*) FROM public."'):
            table = re.search(r'public."([^"]+)"', sql).group(1)
            return self.rows[table]
        if "table_name = 'cortex_platform_config'" in sql:
            return True
        if "table_name = 'retention_config'" in sql:
            return True
        if "table_name = 'cortex_latency_baselines'" in sql:
            return False
        if "MIN(ts)" in sql and "FROM public.messages" in sql:
            # Age of the oldest message, against the declared 90d retention tier. Default
            # is compliant; retention_breach makes it 161 days, the value measured on the
            # live database on 2026-08-30.
            return 161 if getattr(self, "retention_breach", False) else 30
        raise AssertionError(f"Unexpected fetchval SQL: {sql}")

    async def fetch(self, sql, *args):
        if "FROM public.messages" in sql and "ORDER BY COUNT(*) DESC" in sql:
            if self.transcript_flood:
                return [
                    {
                        "project": "kaidera-os",
                        "agent_name": "kai@kaidera-os",
                        "session_id": "11111111-1111-1111-1111-111111111111",
                        "message_count": 23455,
                        "first_ts": datetime(2026, 6, 26, 10, tzinfo=timezone.utc),
                        "last_ts": datetime(2026, 6, 26, 11, 30, tzinfo=timezone.utc),
                    }
                ]
            return []
        if "FROM pg_stat_activity" in sql:
            if self.autovacuum_active:
                return [
                    {
                        "pid": 123,
                        "state": "active",
                        "age": timedelta(seconds=91),
                        "query": "autovacuum: VACUUM ANALYZE public.messages",
                    }
                ]
            return []
        if "FROM agents a" in sql and "array_agg" in sql:
            if not self.polluted_registry:
                return []
            return [
                {"name": "kai", "projects": ["asw-connect", "kaidera"]},
            ]
        if "FROM agents a" in sql and "LEFT JOIN cortex_projects" in sql:
            assert (
                "COALESCE(a.capabilities->>'visibility', 'active') <> 'history-only'"
                in sql
            )
            rows = [
                {
                    "name": "kai",
                    "project": "kaidera-os",
                    "role": "lead",
                    "status": "available",
                    "capabilities": {"keep_visible": True},
                }
            ]
            if self.polluted_registry:
                rows.extend(
                    [
                        {
                            "name": "hue@kaidera",
                            "project": "kaidera",
                            "role": "analyst",
                            "status": "available",
                            "capabilities": {"keep_visible": True},
                        },
                        {
                            "name": "claude-subagent-deadbeef",
                            "project": "kaidera",
                            "role": "worker",
                            "status": "available",
                            "capabilities": {"keep_visible": True},
                        },
                        {
                            "name": "the",
                            "project": "dxb",
                            "role": None,
                            "status": "available",
                            "capabilities": {},
                        },
                    ]
                )
            return rows
        if "FROM pg_indexes" in sql:
            rows = []
            for table in self.rows:
                if table == "messages" and self.degenerate_messages:
                    rows.append(
                        {
                            "tablename": "messages",
                            "indexname": "idx_messages_embedding",
                            "indexdef": "CREATE INDEX idx_messages_embedding ON public.messages USING ivfflat (embedding vector_cosine_ops) WITH (lists='1')",
                        }
                    )
                elif table in self.embedded:
                    rows.append(
                        {
                            "tablename": table,
                            "indexname": f"idx_{table}_embedding_hnsw",
                            "indexdef": f"CREATE INDEX idx_{table}_embedding_hnsw ON public.{table} USING hnsw (embedding vector_cosine_ops)",
                        }
                    )
            rows.append(
                {
                    "tablename": "messages",
                    "indexname": "idx_messages_project_ts_desc",
                    "indexdef": "CREATE INDEX idx_messages_project_ts_desc ON public.messages USING btree (project, ts DESC)",
                }
            )
            rows.append(
                {
                    "tablename": "team_events",
                    "indexname": "idx_team_events_project_ts_desc",
                    "indexdef": "CREATE INDEX idx_team_events_project_ts_desc ON public.team_events USING btree (project, ts DESC)",
                }
            )
            return rows
        if "pg_get_userbyid(c.relowner)" in sql:
            return [
                {
                    "table_name": "cortex_entities",
                    "owner": "cortex_app",
                    "relrowsecurity": True,
                    "relforcerowsecurity": True,
                },
                {
                    "table_name": "cortex_relationships",
                    "owner": "cortex_app",
                    "relrowsecurity": True,
                    "relforcerowsecurity": True,
                },
            ]
        raise AssertionError(f"Unexpected fetch SQL: {sql}")


def _admin_request(module):
    return Request(
        {
            "type": "http",
            "headers": [(b"x-cortex-admin-token", module.ADMIN_TOKEN.encode("utf-8"))],
        }
    )


@pytest.mark.asyncio
async def test_cortex_doctor_flags_degenerate_ivfflat(monkeypatch):
    module = load_module(API_MAIN_PATH, "cortex_api_doctor_degenerate_test")
    async def _configured(_config, _purpose):
        return True

    monkeypatch.setattr(module, "provider_configured", _configured)
    monkeypatch.setattr(
        module,
        "cortex_doctor_contract_check",
        lambda: module.cortex_doctor_check("contract_enum_drift", "Contract/enum drift", "ok", "ok"),
    )

    result = await module.build_cortex_doctor_report(DoctorConn(degenerate_messages=True))

    vector = next(check for check in result["checks"] if check["id"] == "vector_index_health")
    assert result["status"] == "critical"
    assert vector["status"] == "critical"
    assert vector["evidence"]["tables"][0]["bad_lists"] == [
        {"name": "idx_messages_embedding", "lists": 1}
    ]


@pytest.mark.asyncio
async def test_admin_cortex_doctor_route_is_read_only(monkeypatch):
    module = load_module(API_MAIN_PATH, "cortex_api_doctor_route_test")
    module.ADMIN_TOKEN = TEST_ADMIN_TOKEN
    async def _configured(_config, _purpose):
        return True

    monkeypatch.setattr(module, "provider_configured", _configured)
    monkeypatch.setattr(
        module,
        "cortex_doctor_contract_check",
        lambda: module.cortex_doctor_check("contract_enum_drift", "Contract/enum drift", "ok", "ok"),
    )
    module.pool_admin = FakePool(DoctorConn())

    result = await module.admin_cortex_doctor(_admin_request(module))

    assert result["mode"] == "read_only"
    assert result["summary"]["unknown"] == 1
    assert any(check["id"] == "latency_baselines" for check in result["checks"])


@pytest.mark.asyncio
async def test_cortex_doctor_flags_registry_pollution(monkeypatch):
    module = load_module(API_MAIN_PATH, "cortex_api_doctor_registry_test")
    async def _configured(_config, _purpose):
        return True

    monkeypatch.setattr(module, "provider_configured", _configured)
    monkeypatch.setattr(
        module,
        "cortex_doctor_contract_check",
        lambda: module.cortex_doctor_check("contract_enum_drift", "Contract/enum drift", "ok", "ok"),
    )

    result = await module.build_cortex_doctor_report(DoctorConn(polluted_registry=True))

    registry = next(check for check in result["checks"] if check["id"] == "registry_health")
    assert registry["status"] == "warn"
    counts = registry["evidence"]["issue_counts"]
    assert counts["project_suffix_in_name"] == 1
    assert counts["ephemeral_name"] == 1
    assert counts["blocked_sentence_fragment"] == 1
    candidates = registry["evidence"]["safe_cleanup_candidates"]
    assert {"project": "kaidera", "agent": "hue@kaidera", "role": "analyst", "status": "available",
            "reasons": ["project_suffix_in_name", "invalid_registry_name"]} in candidates
    assert registry["evidence"]["cross_project_duplicates"] == [
        {"agent": "kai", "projects": ["asw-connect", "kaidera"]}
    ]


@pytest.mark.asyncio
async def test_admin_remove_deactivates_agent_for_orphaned_project(monkeypatch):
    module = load_module(API_MAIN_PATH, "cortex_api_remove_orphan_test")
    module.ADMIN_TOKEN = TEST_ADMIN_TOKEN

    class RemoveConn:
        def __init__(self):
            self.executed = []

        async def fetchrow(self, sql, *args):
            assert args == ("hue@kaidera", "retired-project")
            return {
                "name": "hue@kaidera",
                "role": None,
                "capabilities": {},
                "status": "available",
            }

        async def execute(self, sql, *args):
            self.executed.append((sql, args))

    conn = RemoveConn()
    monkeypatch.setattr(module, "acquire_scoped", lambda project: FakeAcquire(conn))

    async def emit_event(_conn, **kwargs):
        assert kwargs["project"] == "retired-project"
        return 1

    monkeypatch.setattr(module, "emit_team_event", emit_event)

    result = await module.remove_agent(
        module.AgentRemove(project="retired-project", agent_name="hue@kaidera"),
        _admin_request(module),
    )

    assert result["removed"] is True
    assert result["project"] == "retired-project"
    assert conn.executed
    assert conn.executed[0][1] == ("hue@kaidera", "retired-project")


@pytest.mark.asyncio
async def test_cortex_doctor_flags_transcript_flood(monkeypatch):
    module = load_module(API_MAIN_PATH, "cortex_api_doctor_transcript_flood_test")
    async def _configured(_config, _purpose):
        return True

    monkeypatch.setattr(module, "provider_configured", _configured)
    monkeypatch.setattr(
        module,
        "cortex_doctor_contract_check",
        lambda: module.cortex_doctor_check("contract_enum_drift", "Contract/enum drift", "ok", "ok"),
    )

    result = await module.build_cortex_doctor_report(
        DoctorConn(transcript_flood=True, autovacuum_active=True)
    )

    transcript = next(check for check in result["checks"] if check["id"] == "transcript_write_pressure")
    assert result["status"] == "critical"
    assert transcript["status"] == "critical"
    assert transcript["evidence"]["messages_5m"] == 5306
    assert transcript["evidence"]["messages_1h"] == 25000
    assert transcript["evidence"]["top_sessions_2h"][0]["message_count"] == 23455
    assert transcript["evidence"]["active_vacuum_on_messages"][0]["pid"] == 123


@pytest.mark.asyncio
async def test_cortex_doctor_flags_a_retention_policy_that_is_not_enforced(monkeypatch):
    """A declared policy nobody applies must not report ok.

    This check previously asked only whether a retention_config ROW existed. Measured on the
    live database 2026-08-30: messages policy 90d, oldest row 161d, 1.78M rows, 8.6 GB --
    and the doctor said ok. Nothing in the tree enforces retention_config; it is read to
    INSERT the defaults and to confirm the row exists, and nowhere else. That is how the
    2026-07-19 memory-saturation outage was quietly rebuilding itself on the same table.
    """
    module = load_module(API_MAIN_PATH, "cortex_api_doctor_retention_breach_test")

    async def _configured(_config, _purpose):
        return True

    monkeypatch.setattr(module, "provider_configured", _configured)
    monkeypatch.setattr(
        module,
        "cortex_doctor_contract_check",
        lambda: module.cortex_doctor_check("contract_enum_drift", "Contract/enum drift", "ok", "ok"),
    )

    breached = await module.build_cortex_doctor_report(DoctorConn(retention_breach=True))
    check = next(c for c in breached["checks"] if c["id"] == "growth_retention")
    assert check["status"] == "warn"
    assert any("not being enforced" in str(i) for i in check["evidence"]["issues"]), check

    compliant = await module.build_cortex_doctor_report(DoctorConn())
    check = next(c for c in compliant["checks"] if c["id"] == "growth_retention")
    assert not any("not being enforced" in str(i) for i in check["evidence"]["issues"]), check
