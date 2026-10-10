"""Real SQL snapshot guards with deterministic, never-networked providers."""
from contextlib import asynccontextmanager
import json
from uuid import uuid4

import asyncpg
import pytest

from test_audit_data_integrity_native import api as frozen_api, run, scratch_conn as frozen_scratch_conn

TABLES = ("decisions", "lessons", "knowledge", "messages", "artifacts", "work_products")
TEXT = "original content long enough"
VECTOR = "[0.1,0.2]"


@pytest.fixture(scope="module")
def api():
    yield from frozen_api.__wrapped__()


@pytest.fixture
def scratch_conn():
    yield from frozen_scratch_conn.__wrapped__()


@asynccontextmanager
async def case_context(api, scratch_conn, monkeypatch, *, table="knowledge", project="audit", metadata='{"keep":"fixture"}'):
    conn = await asyncpg.connect(**scratch_conn)
    try:
        await conn.execute("CREATE DOMAIN vector AS text")
        for name in TABLES:
            id_type = "bigint" if name == "messages" else "uuid"
            await conn.execute(f"""CREATE TABLE {name}(id {id_type} PRIMARY KEY,project text,
                content text,summary text,raw_content text,caption text,source_file text,
                title text,activity_type text,status text,behavior_summary text,architecture_notes text,
                files_changed text[],symbols_changed text[],subject_entities text[],artifact_refs text[],
                tests_run jsonb,risks text[],followups text[],invalidated_at timestamptz,
                embedding vector,metadata jsonb,created_at timestamptz DEFAULT now(),
                updated_at timestamptz DEFAULT now(),ts timestamptz DEFAULT now())""")
        identifier = 42 if table == "messages" else uuid4()
        await conn.execute(f"INSERT INTO {table}(id,project,content,summary,raw_content,caption,title,metadata) VALUES($1,$2,$3,$3,$3,$3,$3,$4::jsonb)",
                           identifier, project, TEXT, metadata)

        @asynccontextmanager
        async def scoped(scope):
            assert scope == "audit"
            yield conn

        async def platform():
            return {}

        async def configured(*args):
            return True

        async def schema(_conn):
            # Only avoid unrelated runtime DDL: the fixture contains every column
            # in the actual configured work-product content expression.
            return None

        monkeypatch.setattr(api, "acquire_scoped", scoped)
        monkeypatch.setattr(api, "load_cortex_platform_config_cached", platform)
        monkeypatch.setattr(api, "provider_configured", configured)
        monkeypatch.setattr(api, "ensure_work_products_schema", schema)
        selected = await conn.fetchval(f"SELECT {api.embedding_content_sql(api.EMBEDDING_BACKFILL_TABLES[table])} FROM {table} WHERE id=$1", identifier)
        yield conn, identifier, selected
    finally:
        await conn.close()


async def backfill(api, table="knowledge", **kwargs):
    return await api.execute_embedding_backfill("audit", api.EmbeddingBackfillRequest(table=table, limit=1, **kwargs))


@pytest.mark.parametrize("table", TABLES)
def test_unchanged_effective_text_embeds_each_config_and_native_id(api, scratch_conn, monkeypatch, table):
    async def case():
        async with case_context(api, scratch_conn, monkeypatch, table=table) as (conn, identifier, selected):
            calls = []

            async def embed(text):
                calls.append(text)
                return [0.1, 0.2]

            monkeypatch.setattr(api, "embed_text", embed)
            result = await backfill(api, table)
            assert calls == [selected]
            assert result["processed"] == 1 and result["embedded"] == 1
            assert result["errors"] == result["skipped"] == 0
            assert await conn.fetchval(f"SELECT embedding::text FROM {table} WHERE id=$1", identifier) == VECTOR
            assert isinstance(identifier, int) if table == "messages" else not isinstance(identifier, int)
    run(case())


@pytest.mark.parametrize("outcome", ["success", "none", "exception"])
def test_content_edit_refuses_vector_and_error_metadata_and_counts_noop(api, scratch_conn, monkeypatch, outcome):
    async def case():
        async with case_context(api, scratch_conn, monkeypatch) as (conn, identifier, selected):
            before = await conn.fetchval("SELECT metadata::text FROM knowledge")

            async def embed(text):
                assert text == selected
                await conn.execute("UPDATE knowledge SET content='new content long enough' WHERE id=$1", identifier)
                if outcome == "exception":
                    raise RuntimeError("synthetic fixture provider failure")
                return [0.1, 0.2] if outcome == "success" else None

            monkeypatch.setattr(api, "embed_text", embed)
            result = await backfill(api)
            data = await conn.fetchrow("SELECT content,embedding::text AS embedding,metadata::text AS metadata FROM knowledge")
            assert data["content"] == "new content long enough"
            assert data["embedding"] is None and data["metadata"] == before
            assert result["processed"] == 1 and result["embedded"] == result["errors"] == 0
            assert result["skipped"] == 1
    run(case())


@pytest.mark.parametrize("outcome", ["success", "failure"])
def test_concurrent_completed_vector_and_metadata_are_preserved(api, scratch_conn, monkeypatch, outcome):
    async def case():
        async with case_context(api, scratch_conn, monkeypatch) as (conn, identifier, selected):
            async def embed(text):
                await conn.execute("UPDATE knowledge SET embedding='[9,9]'::vector,metadata='{\"fresh\":true}'::jsonb WHERE id=$1", identifier)
                return [0.1, 0.2] if outcome == "success" else None

            monkeypatch.setattr(api, "embed_text", embed)
            result = await backfill(api)
            data = await conn.fetchrow("SELECT embedding::text AS embedding,metadata::text AS metadata FROM knowledge")
            assert data["embedding"] == "[9,9]" and json.loads(data["metadata"]) == {"fresh": True}
            assert result["embedded"] == result["errors"] == 0 and result["skipped"] == 1
    run(case())


@pytest.mark.parametrize("change", ["project", "skip", "threshold", "delete"])
def test_row_no_longer_eligible_is_not_changed(api, scratch_conn, monkeypatch, change):
    async def case():
        async with case_context(api, scratch_conn, monkeypatch) as (conn, identifier, selected):
            expected = []

            async def embed(text):
                statement = {
                    "project": "UPDATE knowledge SET project='other' WHERE id=$1",
                    "skip": "UPDATE knowledge SET metadata='{\"embedding_skip\":true}'::jsonb WHERE id=$1",
                    "threshold": "UPDATE knowledge SET metadata='{\"embedding_error_count\":3}'::jsonb WHERE id=$1",
                    "delete": "DELETE FROM knowledge WHERE id=$1",
                }[change]
                await conn.execute(statement, identifier)
                expected.extend(await conn.fetch("SELECT * FROM knowledge"))
                return [0.1, 0.2]

            monkeypatch.setattr(api, "embed_text", embed)
            result = await backfill(api)
            assert list(await conn.fetch("SELECT * FROM knowledge")) == expected
            assert result["embedded"] == result["errors"] == 0 and result["skipped"] == 1
    run(case())


@pytest.mark.parametrize("table,change", [("artifacts", "content"), ("work_products", "content"), ("work_products", "invalidated")])
def test_effective_expression_or_workproduct_eligibility_change_is_skipped(api, scratch_conn, monkeypatch, table, change):
    async def case():
        async with case_context(api, scratch_conn, monkeypatch, table=table) as (conn, identifier, selected):
            async def embed(text):
                column = "raw_content" if table == "artifacts" else "summary"
                statement = f"UPDATE {table} SET {column}='changed effective source long enough' WHERE id=$1"
                if change == "invalidated":
                    statement = f"UPDATE {table} SET invalidated_at=now() WHERE id=$1"
                await conn.execute(statement, identifier)
                return [0.1, 0.2]

            monkeypatch.setattr(api, "embed_text", embed)
            result = await backfill(api, table)
            assert await conn.fetchval(f"SELECT embedding::text FROM {table} WHERE id=$1", identifier) is None
            assert result["embedded"] == 0 and result["skipped"] == 1
    run(case())


def test_global_knowledge_remains_eligible(api, scratch_conn, monkeypatch):
    async def case():
        async with case_context(api, scratch_conn, monkeypatch, project="_global") as (conn, identifier, selected):
            async def embed(text):
                return [0.1, 0.2]

            monkeypatch.setattr(api, "embed_text", embed)
            result = await backfill(api)
            assert result["embedded"] == 1
            assert await conn.fetchval("SELECT embedding::text FROM knowledge") == VECTOR
    run(case())


@pytest.mark.parametrize("outcome", ["success", "failure"])
def test_applied_scalar_metadata_and_threshold_accounting(api, scratch_conn, monkeypatch, outcome):
    async def case():
        metadata = '"legacy scalar"' if outcome == "success" else '{"embedding_error_count":2,"keep":true}'
        async with case_context(api, scratch_conn, monkeypatch, metadata=metadata) as (conn, identifier, selected):
            async def embed(text):
                return [0.1, 0.2] if outcome == "success" else None

            monkeypatch.setattr(api, "embed_text", embed)
            result = await backfill(api)
            data = json.loads(await conn.fetchval("SELECT metadata::text FROM knowledge"))
            if outcome == "success":
                assert result["embedded"] == 1 and data["embedding_error_count"] == 0
            else:
                assert result["errors"] == result["skipped"] == 1
                assert data["embedding_error_count"] == 3 and data["embedding_skip"] is True
                assert data["keep"] is True
    run(case())


def test_dry_run_never_calls_provider_or_changes_row(api, scratch_conn, monkeypatch):
    async def case():
        async with case_context(api, scratch_conn, monkeypatch) as (conn, identifier, selected):
            async def forbidden(text):
                raise AssertionError("dryrun contacted provider")

            monkeypatch.setattr(api, "embed_text", forbidden)
            before = await conn.fetchrow("SELECT * FROM knowledge")
            result = await backfill(api, dry_run=True)
            assert result["processed"] == 1 and result["embedded"] == result["errors"] == result["skipped"] == 0
            assert await conn.fetchrow("SELECT * FROM knowledge") == before
    run(case())
