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


class FakeEmbeddingConn:
    def __init__(
        self,
        *,
        expected_table: str = "knowledge",
        expected_content_col: str = "content",
        expected_content_fragment: str | None = None,
        unexpected_content_col: str = "summary",
        expected_order_col: str = "created_at",
    ):
        self.expected_table = expected_table
        self.expected_content_col = expected_content_col
        self.expected_content_fragment = expected_content_fragment
        self.unexpected_content_col = unexpected_content_col
        self.expected_order_col = expected_order_col
        self.embedded: list[tuple] = []
        self.failures: list[dict] = []

    async def execute(self, sql, *args):
        if "set_config('cortex.project'" in sql:
            return "SELECT 1"
        if "SET embedding" in sql:
            self.embedded.append(args)
            return "UPDATE 1"
        if "SET metadata" in sql:
            self.failures.append(json.loads(args[0]))
            return "UPDATE 1"
        raise AssertionError(f"Unexpected execute SQL: {sql}")

    async def fetch(self, sql, *args):
        assert f"FROM {self.expected_table}" in sql
        if self.expected_content_fragment:
            assert self.expected_content_fragment in sql
            assert " AS content" in sql
        else:
            assert f"{self.expected_content_col} AS content" in sql
        assert f"{self.unexpected_content_col} AS content" not in sql
        assert f"ORDER BY {self.expected_order_col} DESC" in sql
        assert "embedding_error_count" in sql
        assert "embedding_skip" in sql
        assert args == ("kaidera", 3, 25)
        return [
            {
                "id": "11111111-1111-1111-1111-111111111111",
                "content": "bad row with enough content",
                "embedding_error_count": 2,
            },
            {
                "id": "22222222-2222-2222-2222-222222222222",
                "content": "good row with enough content",
                "embedding_error_count": 0,
            },
        ]


def admin_request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/beat/embeddings/backfill",
            "headers": [(b"x-cortex-admin-token", b"cortex-local-admin")],
            "query_string": b"",
        }
    )


@pytest.mark.asyncio
async def test_embedding_backfill_isolates_bad_row_and_continues(monkeypatch):
    module = load_module(API_MAIN_PATH, "cortex_api_embedding_backfill_test")
    conn = FakeEmbeddingConn()
    module.pool_app = FakePool(conn)
    module.ADMIN_TOKEN = "cortex-local-admin"
    module._provider_key_cache = {"keys": {"openrouter": "test-key"}, "expires": float("inf")}

    async def fake_embed_text(text):
        if text.startswith("bad"):
            return None
        return [0.1, 0.2]

    monkeypatch.setattr(module, "embed_text", fake_embed_text)

    result = await module.beat_embeddings_backfill(
        module.EmbeddingBackfillRequest(
            table="knowledge",
            limit=25,
            max_errors=10,
            error_threshold=3,
        ),
        admin_request(),
        x_project="kaidera",
    )

    assert result["processed"] == 2
    assert result["embedded"] == 1
    assert result["errors"] == 1
    assert result["skipped"] == 1
    assert conn.embedded[0][2] == "22222222-2222-2222-2222-222222222222"
    assert conn.failures == [
        {
            "embedding_error_count": 3,
            "embedding_last_error": "embed_text returned no vector",
            "embedding_last_error_at": conn.failures[0]["embedding_last_error_at"],
            "embedding_skip": True,
        }
    ]


@pytest.mark.asyncio
async def test_embedding_backfill_supports_messages_with_content_and_ts(monkeypatch):
    module = load_module(API_MAIN_PATH, "cortex_api_embedding_messages_test")
    conn = FakeEmbeddingConn(
        expected_table="messages",
        expected_content_col="content",
        unexpected_content_col="summary",
        expected_order_col="ts",
    )
    module.pool_app = FakePool(conn)
    module.ADMIN_TOKEN = "cortex-local-admin"
    module._provider_key_cache = {"keys": {"openrouter": "test-key"}, "expires": float("inf")}

    async def fail_if_called(text):
        raise AssertionError("dry-run should not call embed_text")

    monkeypatch.setattr(module, "embed_text", fail_if_called)

    result = await module.beat_embeddings_backfill(
        module.EmbeddingBackfillRequest(
            table="messages",
            limit=25,
            max_errors=10,
            error_threshold=3,
            dry_run=True,
        ),
        admin_request(),
        x_project="kaidera",
    )

    assert result["processed"] == 2
    assert result["embedded"] == 0
    assert result["tables"]["messages"]["selected"] == 2


@pytest.mark.asyncio
async def test_embedding_backfill_supports_work_products_with_canonical_memory_text(monkeypatch):
    module = load_module(API_MAIN_PATH, "cortex_api_embedding_work_products_test")
    conn = FakeEmbeddingConn(
        expected_table="work_products",
        expected_content_col="",
        expected_content_fragment="CONCAT_WS",
        unexpected_content_col="content",
        expected_order_col="updated_at",
    )
    module.pool_app = FakePool(conn)
    module.ADMIN_TOKEN = "cortex-local-admin"
    module._provider_key_cache = {"keys": {"openrouter": "test-key"}, "expires": float("inf")}

    async def fake_ensure_work_products_schema(_conn):
        return None

    async def fail_if_called(text):
        raise AssertionError("dry-run should not call embed_text")

    monkeypatch.setattr(module, "ensure_work_products_schema", fake_ensure_work_products_schema)
    monkeypatch.setattr(module, "embed_text", fail_if_called)

    result = await module.beat_embeddings_backfill(
        module.EmbeddingBackfillRequest(
            table="work_products",
            limit=25,
            max_errors=10,
            error_threshold=3,
            dry_run=True,
        ),
        admin_request(),
        x_project="kaidera",
    )

    assert result["processed"] == 2
    assert result["embedded"] == 0
    assert result["tables"]["work_products"]["selected"] == 2


@pytest.mark.asyncio
async def test_embedding_backfill_supports_artifacts_with_canonical_content(monkeypatch):
    module = load_module(API_MAIN_PATH, "cortex_api_embedding_artifacts_test")
    conn = FakeEmbeddingConn(
        expected_table="artifacts",
        expected_content_col="",
        expected_content_fragment="COALESCE(NULLIF(raw_content, ''), NULLIF(caption, ''), source_file)",
        unexpected_content_col="content",
        expected_order_col="updated_at",
    )
    module.pool_app = FakePool(conn)
    module.ADMIN_TOKEN = "cortex-local-admin"
    module._provider_key_cache = {"keys": {"openrouter": "test-key"}, "expires": float("inf")}

    async def fail_if_called(text):
        raise AssertionError("dry-run should not call embed_text")

    monkeypatch.setattr(module, "embed_text", fail_if_called)

    result = await module.beat_embeddings_backfill(
        module.EmbeddingBackfillRequest(
            table="artifacts",
            limit=25,
            max_errors=10,
            error_threshold=3,
            dry_run=True,
        ),
        admin_request(),
        x_project="kaidera",
    )

    assert result["processed"] == 2
    assert result["embedded"] == 0
    assert result["tables"]["artifacts"]["selected"] == 2


def test_artifact_embedding_schema_matches_the_768_dimension_backfill_contract():
    root = API_MAIN_PATH.parents[2]
    schema = (root / ".agents/data/schema.sql").read_text(encoding="utf-8")
    full_schema = (root / ".agents/data/cortex-schema-full.sql").read_text(encoding="utf-8")
    migration = (
        root / ".agents/data/migrations/2026-09-01-01-artifact-embedding-768.sql"
    ).read_text(encoding="utf-8")

    artifact_schema = schema.split("CREATE TABLE IF NOT EXISTS artifacts (", 1)[1].split(
        ");", 1
    )[0]
    captured_artifact_schema = full_schema.split(
        "CREATE TABLE public.artifacts (", 1
    )[1].split(");", 1)[0]

    assert "embedding VECTOR(768)" in artifact_schema
    assert "embedding public.vector(768)" in captured_artifact_schema
    assert "idx_artifacts_embedding_hnsw" in schema
    assert "idx_artifacts_embedding_hnsw" in full_schema
    assert "WHERE embedding IS NOT NULL" in migration
    assert "ALTER COLUMN embedding TYPE vector(768)" in migration


@pytest.mark.asyncio
async def test_embedding_backfill_large_request_returns_pollable_job(monkeypatch):
    module = load_module(API_MAIN_PATH, "cortex_api_embedding_backfill_job_test")
    module.ADMIN_TOKEN = "cortex-local-admin"
    module._provider_key_cache = {"keys": {"openrouter": "test-key"}, "expires": float("inf")}

    async def fake_create_job(project, body):
        assert project == "kaidera"
        assert body.table == "all"
        assert body.limit == 500
        return "33333333-3333-3333-3333-333333333333"

    async def fake_run_job(_project, _job_id, _body):
        return None

    scheduled = []

    def fake_create_task(coro):
        scheduled.append(coro)
        coro.close()
        return object()

    monkeypatch.setattr(module, "create_embedding_backfill_job", fake_create_job)
    monkeypatch.setattr(module, "run_embedding_backfill_job", fake_run_job)
    monkeypatch.setattr(module.asyncio, "create_task", fake_create_task)

    result = await module.beat_embeddings_backfill(
        module.EmbeddingBackfillRequest(
            table="all",
            limit=500,
            max_errors=10,
            error_threshold=3,
        ),
        admin_request(),
        x_project="kaidera",
    )

    assert result.status_code == 202
    payload = json.loads(result.body)
    assert payload["job_id"] == "33333333-3333-3333-3333-333333333333"
    assert payload["status_url"] == "/beat/embeddings/jobs/33333333-3333-3333-3333-333333333333"
    assert scheduled
