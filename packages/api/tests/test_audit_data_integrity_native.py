"""Seven source-bound data-integrity regressions on an owned scratch PostgreSQL.

Run with CORTEX_AUDIT_PG_DSN pointing at a disposable loopback PostgreSQL server.
Every test creates and destroys its own database. These are intentionally RED against
the 2026-10-10 audit base; product repairs belong to the respective owners.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import importlib.util
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import asyncpg
import psycopg2
from psycopg2 import sql as pgsql
import pytest


ROOT = Path(__file__).resolve().parents[3]
API_ROOT = ROOT / "packages" / "api"
MIGRATION = ROOT / "packages" / "schema" / "migrations" / "2026-07-29-01-archive-messages-shared-id-sequence.sql"
DSN = os.environ.get("CORTEX_AUDIT_PG_DSN", "")


@pytest.fixture(scope="module")
def api():
    sys.path.insert(0, str(API_ROOT))
    spec = importlib.util.spec_from_file_location("cortex_api_audit_data_integrity", API_ROOT / "main.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    yield module
    sys.path.remove(str(API_ROOT))


@pytest.fixture
def scratch_dsn():
    if not DSN:
        pytest.skip("CORTEX_AUDIT_PG_DSN must name a disposable loopback PostgreSQL")
    parsed = urlsplit(DSN)
    if parsed.hostname not in {"127.0.0.1", "localhost"} or parsed.port in {5500, 8501, None}:
        pytest.fail("audit DSN must use an explicit non-live loopback port")
    name = "mike_audit_" + uuid4().hex
    admin = psycopg2.connect(DSN)
    admin.autocommit = True
    try:
        with admin.cursor() as cur:
            cur.execute(pgsql.SQL("CREATE DATABASE {}").format(pgsql.Identifier(name)))
        yield urlunsplit(parsed._replace(path="/" + name))
    finally:
        with admin.cursor() as cur:
            cur.execute(pgsql.SQL("DROP DATABASE {} WITH (FORCE)").format(pgsql.Identifier(name)))
        admin.close()


def run(coro):
    return asyncio.run(coro)


def test_api1_001_retention_moves_child_before_parent(api, scratch_dsn):
    async def case():
        conn = await asyncpg.connect(scratch_dsn)
        try:
            cols = api._RETENTION_TABLES["handoffs"]["cols"].split(", ")
            defs = []
            for col in cols:
                typ = "uuid" if col in {"id", "reply_to_handoff_id"} else (
                    "timestamptz" if col.endswith("_at") else "text"
                )
                defs.append(f"{col} {typ}")
            columns = ", ".join(defs)
            await conn.execute(f"CREATE TABLE handoffs ({columns}, PRIMARY KEY (id))")
            await conn.execute("ALTER TABLE handoffs ADD FOREIGN KEY (reply_to_handoff_id) REFERENCES handoffs(id)")
            await conn.execute(f"CREATE TABLE archive_handoffs ({columns}, PRIMARY KEY (id))")
            parent, child = uuid4(), uuid4()
            await conn.execute("INSERT INTO handoffs(id, status, created_at) VALUES($1,'completed',now()-interval '30 days')", parent)
            await conn.execute("INSERT INTO handoffs(id, reply_to_handoff_id, status, created_at) VALUES($1,$2,'completed',now()-interval '29 days')", child, parent)
            moved = await conn.fetchval(api.retention_move_sql("handoffs", 7, 1))
            assert moved == 1
            assert await conn.fetchval("SELECT COUNT(*) FROM handoffs") == 1
        finally:
            await conn.close()
    run(case())


def test_api1_005_ambiguous_invalidation_prefix_is_rejected(api, scratch_dsn):
    async def case():
        conn = await asyncpg.connect(scratch_dsn)
        try:
            for table in ("decisions", "lessons", "handoffs"):
                await conn.execute(f"CREATE TABLE {table}(id uuid PRIMARY KEY, project text NOT NULL)")
            await conn.execute("INSERT INTO decisions VALUES('aaaaaaaa-0000-0000-0000-000000000001','audit'),('aaaaaaaa-0000-0000-0000-000000000002','audit')")
            with pytest.raises(api.HTTPException) as caught:
                await api.find_invalidation_target(conn, "audit", "aaaaaaaa")
            assert caught.value.status_code == 409
        finally:
            await conn.close()
    run(case())


def test_api2_001_bm25_excludes_invalidated_decisions(api, scratch_dsn, monkeypatch):
    async def case():
        conn = await asyncpg.connect(scratch_dsn)
        try:
            await conn.execute("CREATE TABLE decisions(id uuid PRIMARY KEY, project text, summary text, category text, agent_name text, search_vector tsvector, invalidated_at timestamptz)")
            stale, current = uuid4(), uuid4()
            await conn.execute("INSERT INTO decisions VALUES($1,'audit','orion stale decision',NULL,NULL,to_tsvector('english','orion stale decision'),now()),($2,'audit','orion current decision',NULL,NULL,to_tsvector('english','orion current decision'),NULL)", stale, current)

            class ScopedSearch:
                async def fetch(self, statement, *args):
                    if "FROM decisions" in statement and "search_vector @@" in statement:
                        return await conn.fetch(statement, *args)
                    return []

                async def execute(self, *_args):
                    return "SET"

            async def no_embedding(*_args):
                return api.SearchProviderOutcome("skipped")

            monkeypatch.setattr(api, "_embed_query_outcome", no_embedding)
            result = await api.execute_search(ScopedSearch(), "audit", "orion", search_type="decisions", rerank=False, graph=False)
            assert str(current) in {str(row["id"]) for row in result["results"]}
            assert str(stale) not in {str(row["id"]) for row in result["results"]}
        finally:
            await conn.close()
    run(case())


def test_api4_002_backfill_does_not_write_vector_for_changed_content(api, scratch_dsn, monkeypatch):
    async def case():
        conn = await asyncpg.connect(scratch_dsn)
        try:
            await conn.execute("CREATE DOMAIN vector AS text")
            await conn.execute("CREATE TABLE knowledge(id uuid PRIMARY KEY, project text, content text, embedding vector, metadata jsonb DEFAULT '{}'::jsonb, created_at timestamptz DEFAULT now())")
            row_id = uuid4()
            await conn.execute("INSERT INTO knowledge(id,project,content) VALUES($1,'audit','original content long enough')", row_id)

            @asynccontextmanager
            async def scoped(_project):
                yield conn

            async def platform():
                return {}

            async def ready(*_args):
                return True

            async def embed(text):
                assert text == "original content long enough"
                await conn.execute("UPDATE knowledge SET content='newer content long enough' WHERE id=$1", row_id)
                return [0.1, 0.2]

            monkeypatch.setattr(api, "acquire_scoped", scoped)
            monkeypatch.setattr(api, "load_cortex_platform_config_cached", platform)
            monkeypatch.setattr(api, "provider_configured", ready)
            monkeypatch.setattr(api, "embed_text", embed)
            await api.execute_embedding_backfill("audit", api.EmbeddingBackfillRequest(table="knowledge", limit=1))
            row = await conn.fetchrow("SELECT content, embedding::text AS embedding FROM knowledge WHERE id=$1", row_id)
            assert row["content"] == "newer content long enough"
            assert row["embedding"] is None
        finally:
            await conn.close()
    run(case())


def test_api4_004_transfer_reset_keeps_shared_sequence_above_archive(api, scratch_dsn):
    async def case():
        conn = await asyncpg.connect(scratch_dsn)
        try:
            await conn.execute("CREATE SEQUENCE messages_id_seq")
            await conn.execute("CREATE TABLE messages(id bigint PRIMARY KEY DEFAULT nextval('messages_id_seq'))")
            await conn.execute("ALTER SEQUENCE messages_id_seq OWNED BY messages.id")
            await conn.execute("CREATE TABLE archive_messages(id bigint PRIMARY KEY DEFAULT nextval('messages_id_seq'))")
            await conn.execute("INSERT INTO messages(id) VALUES(5)")
            await conn.execute("INSERT INTO archive_messages(id) VALUES(100)")
            await conn.fetchval("SELECT setval('messages_id_seq', 101, true)")
            reset = await api.reset_project_transfer_sequences(conn, {("public", "messages"), ("public", "archive_messages")})
            assert "public.messages.id" in reset
            assert await conn.fetchval("SELECT nextval('messages_id_seq')") > 101
        finally:
            await conn.close()
    run(case())


def test_api4_005_transfer_accepts_child_before_parent_in_same_table(api, scratch_dsn):
    async def case():
        conn = await asyncpg.connect(scratch_dsn)
        try:
            await conn.execute("CREATE TABLE handoffs(id uuid PRIMARY KEY, project text, reply_to_handoff_id uuid REFERENCES handoffs(id))")
            deps = await api.project_transfer_dependencies(conn)
            assert (("public", "handoffs"), ("public", "handoffs")) in deps
            order = api.project_transfer_order({("public", "handoffs")}, deps)
            child, parent = uuid4(), uuid4()
            rows = [
                {"id": str(child), "project": "audit", "reply_to_handoff_id": str(parent)},
                {"id": str(parent), "project": "audit", "reply_to_handoff_id": None},
            ]
            async with conn.transaction():
                for _table in order:
                    for row in rows:
                        statement = api.project_transfer_insert_sql("public", "handoffs", list(row))
                        await conn.execute(statement, api.json.dumps(row))
            assert await conn.fetchval("SELECT COUNT(*) FROM handoffs") == 2
        finally:
            await conn.close()
    run(case())


def test_schema_3_001_migration_does_not_rewind_concurrent_message_ids(scratch_dsn):
    async def case():
        first = await asyncpg.connect(scratch_dsn)
        second = await asyncpg.connect(scratch_dsn)
        try:
            await first.execute("CREATE SEQUENCE public.messages_id_seq")
            await first.execute("CREATE TABLE public.messages(id bigint PRIMARY KEY DEFAULT nextval('public.messages_id_seq'))")
            await first.execute("CREATE TABLE public.archive_messages(id bigint PRIMARY KEY)")
            await first.execute("INSERT INTO public.archive_messages(id) VALUES(1)")
            await first.fetchval("SELECT setval('public.messages_id_seq', 100, true)")
            source = MIGRATION.read_text()
            vulnerable_read = "(SELECT last_value FROM public.messages_id_seq)"
            # Instrument only the source statement's read point. The two advisory
            # locks make the competing nextval occur after the read, before setval.
            await first.execute("""CREATE FUNCTION audit_read_then_wait() RETURNS bigint LANGUAGE plpgsql AS $$
                DECLARE captured bigint;
                BEGIN
                    SELECT last_value INTO captured FROM public.messages_id_seq;
                    PERFORM pg_advisory_lock(712331);
                    PERFORM pg_advisory_lock(712332);
                    PERFORM pg_advisory_unlock(712332);
                    PERFORM pg_advisory_unlock(712331);
                    RETURN captured;
                END $$""")
            if vulnerable_read in source:
                await second.execute("SELECT pg_advisory_lock(712332)")
                instrumented = source.replace(vulnerable_read, "(SELECT audit_read_then_wait())")
                migration_task = asyncio.create_task(first.execute(instrumented))
                try:
                    for _ in range(200):
                        if not await second.fetchval("SELECT pg_try_advisory_lock(712331)"):
                            break
                        await second.execute("SELECT pg_advisory_unlock(712331)")
                        await asyncio.sleep(0.01)
                    else:
                        pytest.fail("migration did not reach the sequence read")
                    concurrent_id = await second.fetchval("SELECT nextval('public.messages_id_seq')")
                finally:
                    await second.execute("SELECT pg_advisory_unlock(712332)")
                await migration_task
            else:
                # A repaired migration may use a different expression or avoid
                # setval entirely. Exercise that exact source without injection.
                await first.execute(source)
                concurrent_id = await second.fetchval("SELECT nextval('public.messages_id_seq')")
            next_id = await second.fetchval("SELECT nextval('public.messages_id_seq')")
            assert next_id > concurrent_id
        finally:
            await first.close()
            await second.close()
    run(case())
