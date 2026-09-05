"""A JSONB scalar in `metadata` must not be able to abort the embedding backfill.

`COALESCE(metadata, '{}'::jsonb)` rescues only a SQL NULL. A JSONB scalar reaches the
`-` and `||` operators, which raise `cannot delete from scalar` — and because neither
backfill UPDATE is wrapped in a per-row rescue, that 500s the whole request and leaves
every remaining row unembedded.

Found live on marlow 2026-08-18: 33 rows holding the double-encoded string "{}" had
blocked a 225,108-row backlog indefinitely (decisions 11.4% embedded, messages 0%).

These tests pin the guard on both UPDATE paths — the success path and the error path.
"""

import importlib.util
import json
from pathlib import Path

import pytest
from starlette.requests import Request


API_MAIN_PATH = Path(__file__).resolve().parents[1] / "main.py"

# The exact shape that survives a scalar. Asserting on this rather than on "some CASE
# expression" is deliberate: a guard that checks the wrong thing still reads as a guard.
GUARD = "jsonb_typeof(metadata) = 'object'"
VULNERABLE = "COALESCE(metadata, '{}'::jsonb)"


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


class SqlRecordingConn:
    """Records the UPDATE statements the backfill actually emits."""

    def __init__(self):
        self.embedding_update = ""
        self.metadata_update = ""

    async def execute(self, sql, *args):
        if "set_config('cortex.project'" in sql:
            return "SELECT 1"
        if "SET embedding" in sql:
            self.embedding_update = sql
            return "UPDATE 1"
        if "SET metadata" in sql:
            self.metadata_update = sql
            json.loads(args[0])  # the patch must still be valid JSON
            return "UPDATE 1"
        raise AssertionError(f"Unexpected execute SQL: {sql}")

    async def fetch(self, sql, *args):
        return [
            {
                "id": "11111111-1111-1111-1111-111111111111",
                "content": "row that fails to embed, long enough to be selected",
                "embedding_error_count": 0,
            },
            {
                "id": "22222222-2222-2222-2222-222222222222",
                "content": "row that embeds fine, long enough to be selected",
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


async def run_backfill(monkeypatch) -> SqlRecordingConn:
    module = load_module(API_MAIN_PATH, "cortex_api_scalar_metadata_test")
    conn = SqlRecordingConn()
    module.pool_app = FakePool(conn)
    module.ADMIN_TOKEN = "cortex-local-admin"
    # Credentials moved to the OpenKai projection seam (sole authority). This test
    # pins the SQL scalar guard, not the credential path, so satisfy the readiness
    # gate with a warm provider cache exactly as a live projection would leave it.
    module._provider_key_cache["keys"] = {"openrouter": "test-key"}
    module._provider_key_cache["expires"] = float("inf")

    async def fake_embed_text(text):
        # One row takes the success path, one takes the error path, so a single run
        # exercises both UPDATE statements.
        return None if text.startswith("row that fails") else [0.1, 0.2]

    monkeypatch.setattr(module, "embed_text", fake_embed_text)

    await module.beat_embeddings_backfill(
        module.EmbeddingBackfillRequest(
            table="knowledge", limit=25, max_errors=10, error_threshold=3
        ),
        admin_request(),
        x_project="kaidera",
    )
    return conn


@pytest.mark.asyncio
async def test_success_path_update_guards_against_scalar_metadata(monkeypatch):
    conn = await run_backfill(monkeypatch)
    assert conn.embedding_update, "success-path UPDATE never ran"
    assert GUARD in conn.embedding_update
    assert VULNERABLE not in conn.embedding_update


@pytest.mark.asyncio
async def test_error_path_update_guards_against_scalar_metadata(monkeypatch):
    conn = await run_backfill(monkeypatch)
    assert conn.metadata_update, "error-path UPDATE never ran"
    assert GUARD in conn.metadata_update
    assert VULNERABLE not in conn.metadata_update


def test_guard_expression_normalises_every_non_object_jsonb_type():
    """The guard must catch strings, numbers, booleans, arrays and JSON null alike.

    The observed corruption was a string, but nothing stops the same double-encoding
    bug from producing another scalar type.
    """
    module = load_module(API_MAIN_PATH, "cortex_api_scalar_metadata_sql_test")
    sql = module.METADATA_AS_OBJECT_SQL
    # Only 'object' passes through; everything else becomes an empty object.
    assert "= 'object'" in sql
    assert "ELSE '{}'::jsonb" in sql
    assert "jsonb_typeof(metadata)" in sql


@pytest.mark.asyncio
async def test_backfill_updates_use_the_primary_key_index(monkeypatch):
    """`WHERE id::text = $n` casts the column and cannot use the primary key.

    Measured on marlow's decisions table (215,943 rows):
        WHERE id::text = $1  -> scan,            112.840ms
        WHERE id = $1::uuid  -> Index Only Scan,   0.057ms
    That cost is paid once per row, so it dominated the whole backlog drain. The id is
    therefore selected in its native type and passed straight back, which also keeps the
    predicate generic -- public.messages.id is bigint while the rest are uuid, and that
    heterogeneity is why the ::text cast existed in the first place.
    """
    conn = await run_backfill(monkeypatch)
    for label, sql in (("success", conn.embedding_update), ("error", conn.metadata_update)):
        assert "id::text" not in sql, f"{label}-path UPDATE casts the id column again: {sql}"
        assert "WHERE id = $" in sql, f"{label}-path UPDATE lost its indexable predicate: {sql}"
