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
import re
import sys
from urllib.parse import unquote, urlsplit
from uuid import uuid4

import asyncpg
import psycopg2
from psycopg2 import sql as pgsql
import pytest
from starlette.requests import Request


ROOT = Path(__file__).resolve().parents[3]
API_ROOT = ROOT / "packages" / "api"
MIGRATION = ROOT / "packages" / "schema" / "migrations" / "2026-07-29-01-archive-messages-shared-id-sequence.sql"
SCHEMA = ROOT / "packages" / "schema" / "schema.sql"
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
def scratch_conn():
    if not DSN:
        pytest.skip("CORTEX_AUDIT_PG_DSN must name a disposable loopback PostgreSQL")
    parsed = urlsplit(DSN)
    if parsed.query or parsed.fragment:
        pytest.fail("audit DSN query parameters and fragments are forbidden before any connection")
    try:
        port = parsed.port
    except ValueError:
        pytest.fail("audit DSN has an invalid port")
    host = parsed.hostname
    hostaddr = "127.0.0.1"
    database = unquote(parsed.path.removeprefix("/"))
    user = unquote(parsed.username or "")
    if parsed.scheme not in {"postgresql", "postgres"} or host not in {"127.0.0.1", "localhost"} or port in {5500, 8501, None} or not database or "/" in database or not user:
        pytest.fail("audit DSN must use an explicit non-live loopback port")
    for key, target in (("PGHOSTADDR", hostaddr), ("PGHOST", host), ("PGPORT", str(port))):
        inherited = os.environ.get(key)
        if inherited and inherited != target:
            pytest.fail(f"audit DSN environment {key} disagrees with validated target")
    for key in ("PGSERVICE", "PGSERVICEFILE"):
        if os.environ.get(key):
            pytest.fail(f"audit DSN environment {key} can redirect the connection")
    password = unquote(parsed.password) if parsed.password is not None else None
    admin_params = {"host": host, "hostaddr": hostaddr, "port": port, "dbname": database, "user": user}
    name = "mike_audit_" + uuid4().hex
    scratch_params = {"host": hostaddr, "port": port, "database": name, "user": user}
    if password is not None:
        admin_params["password"] = password
        scratch_params["password"] = password
    admin = psycopg2.connect(**admin_params)
    admin.autocommit = True
    try:
        with admin.cursor() as cur:
            cur.execute(pgsql.SQL("CREATE DATABASE {}").format(pgsql.Identifier(name)))
        yield scratch_params
    finally:
        with admin.cursor() as cur:
            cur.execute(pgsql.SQL("DROP DATABASE {} WITH (FORCE)").format(pgsql.Identifier(name)))
        admin.close()


def run(coro):
    return asyncio.run(coro)


@pytest.mark.parametrize("override", ["host=example.com", "hostaddr=203.0.113.10", "port=5500"])
def test_audit_dsn_rejects_libpq_target_overrides_before_connect(monkeypatch, override):
    monkeypatch.setattr(sys.modules[__name__], "DSN", f"postgresql://postgres@127.0.0.1:37407/postgres?{override}")
    calls = []

    def forbidden_connect(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("audit fixture attempted to connect before rejecting a routing override")

    monkeypatch.setattr(psycopg2, "connect", forbidden_connect)
    with pytest.raises(pytest.fail.Exception, match="query parameters"):
        next(scratch_conn.__wrapped__())
    assert calls == []


@pytest.mark.parametrize("name,value", [
    ("PGHOSTADDR", "203.0.113.10"),
    ("PGHOST", "example.com"),
    ("PGPORT", "5500"),
    ("PGSERVICE", "other-cluster"),
    ("PGSERVICEFILE", "/tmp/other-service.conf"),
])
def test_audit_dsn_rejects_inherited_routing_before_connect(monkeypatch, name, value):
    monkeypatch.setattr(sys.modules[__name__], "DSN", "postgresql://postgres@127.0.0.1:37407/postgres")
    for key in ("PGHOSTADDR", "PGHOST", "PGPORT", "PGSERVICE", "PGSERVICEFILE"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv(name, value)
    calls = []

    def forbidden_connect(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("audit fixture attempted to connect before rejecting inherited routing")

    monkeypatch.setattr(psycopg2, "connect", forbidden_connect)
    with pytest.raises(pytest.fail.Exception, match="environment"):
        next(scratch_conn.__wrapped__())
    assert calls == []


def test_api1_001_retention_moves_child_before_parent(api, scratch_conn):
    async def case():
        conn = await asyncpg.connect(**scratch_conn)
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


def test_api1_005_ambiguous_invalidation_prefix_is_rejected(api, scratch_conn):
    async def case():
        conn = await asyncpg.connect(**scratch_conn)
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


def test_api2_001_bm25_excludes_invalidated_decisions(api, scratch_conn, monkeypatch):
    async def case():
        conn = await asyncpg.connect(**scratch_conn)
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


def test_api4_002_backfill_does_not_write_vector_for_changed_content(api, scratch_conn, monkeypatch):
    async def case():
        conn = await asyncpg.connect(**scratch_conn)
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


def test_api4_004_transfer_reset_keeps_shared_sequence_above_archive(api, scratch_conn):
    async def case():
        conn = await asyncpg.connect(**scratch_conn)
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


@pytest.mark.parametrize(
    ("hot_id", "archive_id", "prior", "imported"),
    [
        (100, 5, 10, {("public", "messages"), ("public", "archive_messages")}),
        (5, 100, 10, {("public", "messages"), ("public", "archive_messages")}),
        (5, 10, 101, {("public", "messages"), ("public", "archive_messages")}),
        (5, 100, 10, {("public", "messages")}),
        (5, 100, 10, {("public", "archive_messages")}),
    ],
)
def test_api4_004_reset_uses_all_shared_sequence_high_water_marks(
    api, scratch_conn, hot_id, archive_id, prior, imported,
):
    async def case():
        conn = await asyncpg.connect(**scratch_conn)
        try:
            await conn.execute("CREATE SEQUENCE messages_id_seq")
            await conn.execute("CREATE TABLE messages(id bigint PRIMARY KEY DEFAULT nextval('messages_id_seq'))")
            await conn.execute("ALTER SEQUENCE messages_id_seq OWNED BY messages.id")
            await conn.execute("CREATE TABLE archive_messages(id bigint PRIMARY KEY DEFAULT nextval('messages_id_seq'))")
            await conn.execute("INSERT INTO messages(id) VALUES($1)", hot_id)
            await conn.execute("INSERT INTO archive_messages(id) VALUES($1)", archive_id)
            await conn.fetchval("SELECT setval('messages_id_seq', $1, true)", prior)
            await api.reset_project_transfer_sequences(conn, imported)
            assert await conn.fetchval("SELECT nextval('messages_id_seq')") > max(hot_id, archive_id, prior)
        finally:
            await conn.close()
    run(case())


def test_api4_004_reset_holds_sequence_fence_through_import_transaction(api, scratch_conn):
    async def case():
        first = await asyncpg.connect(**scratch_conn)
        second = await asyncpg.connect(**scratch_conn)
        try:
            await first.execute("CREATE SEQUENCE messages_id_seq")
            await first.execute("CREATE TABLE messages(id bigint PRIMARY KEY DEFAULT nextval('messages_id_seq'))")
            await first.execute("ALTER SEQUENCE messages_id_seq OWNED BY messages.id")
            await first.execute("CREATE TABLE archive_messages(id bigint PRIMARY KEY DEFAULT nextval('messages_id_seq'))")
            await first.execute("INSERT INTO messages(id) VALUES(5)")
            await first.execute("INSERT INTO archive_messages(id) VALUES(100)")
            await first.fetchval("SELECT setval('messages_id_seq', 101, true)")
            transaction = first.transaction()
            await transaction.start()
            try:
                await api.reset_project_transfer_sequences(
                    first, {("public", "messages"), ("public", "archive_messages")},
                )
                started = asyncio.Event()

                async def concurrent_nextval():
                    started.set()
                    return await second.fetchval("SELECT nextval('messages_id_seq')")

                waiting = asyncio.create_task(concurrent_nextval())
                await started.wait()
                await asyncio.sleep(0.1)
                blocked_until_commit = not waiting.done()
            finally:
                await transaction.commit()
            next_id = await asyncio.wait_for(waiting, 2)
            assert blocked_until_commit, "concurrent nextval bypassed the import high-water fence"
            assert next_id > 101
        finally:
            await first.close()
            await second.close()
    run(case())


def test_api4_lock001_partial_imports_finish_without_deadlock(api, scratch_conn, monkeypatch):
    async def case():
        setup = await asyncpg.connect(**scratch_conn)
        try:
            await setup.execute("CREATE SEQUENCE public.messages_id_seq")
            await setup.execute("CREATE TABLE public.cortex_projects(id uuid PRIMARY KEY, project_key text, status text)")
            await setup.execute("CREATE TABLE public.messages(id bigint PRIMARY KEY DEFAULT nextval('public.messages_id_seq'), project text)")
            await setup.execute("ALTER SEQUENCE public.messages_id_seq OWNED BY public.messages.id")
            await setup.execute("CREATE TABLE public.archive_messages(id bigint PRIMARY KEY DEFAULT nextval('public.messages_id_seq'), project text)")
            await setup.execute("INSERT INTO public.cortex_projects VALUES('00000000-0000-0000-0000-000000000003','dest','active')")
            await setup.fetchval("SELECT setval('public.messages_id_seq', 100, true)")
        finally:
            await setup.close()

        @asynccontextmanager
        async def acquired():
            conn = await asyncpg.connect(**scratch_conn)
            try:
                await conn.execute("SET deadlock_timeout = '100ms'")
                yield conn
            finally:
                await conn.close()

        class ScratchPool:
            def acquire(self):
                return acquired()

        monkeypatch.setattr(api, "pool_admin", ScratchPool())
        monkeypatch.setattr(api, "require_admin_access", lambda request: None)
        actual_reset = api.reset_project_transfer_sequences
        release_reset = asyncio.Event()
        reached = {name: asyncio.Event() for name in ("messages", "archive_messages")}

        async def paused_reset(conn, tables):
            name = next(iter(tables))[1]
            reached[name].set()  # The caller's INSERT has already run in its outer transaction.
            await release_reset.wait()
            return await actual_reset(conn, tables)

        monkeypatch.setattr(api, "reset_project_transfer_sequences", paused_reset)
        request = Request({"type": "http", "method": "POST", "path": "/", "headers": []})

        def payload(table_name, message_id):
            return api.ProjectImportRequest(
                format="kaidera.cortex-project.v1",
                source_project={"project_key": "source", "project_id": "00000000-0000-0000-0000-000000000002"},
                tables=[api.ProjectTransferTable(
                    schema_name="public", table_name=table_name,
                    rows=[{"id": message_id, "project": "source"}],
                )],
                allow_existing=True,
            )

        first = asyncio.create_task(api.import_project("dest", payload("messages", 5), request))
        await asyncio.wait_for(reached["messages"].wait(), 3)
        second = asyncio.create_task(api.import_project("dest", payload("archive_messages", 101), request))
        try:
            await asyncio.wait_for(reached["archive_messages"].wait(), 1.0)
            both_inserted_before_reset = True
        except asyncio.TimeoutError:
            both_inserted_before_reset = False
        release_reset.set()
        outcomes = await asyncio.wait_for(asyncio.gather(first, second, return_exceptions=True), 5)
        assert all(isinstance(item, dict) and item.get("ok") for item in outcomes), (
            f"partial imports failed; both_inserted_before_reset={both_inserted_before_reset}; "
            f"sqlstates={[getattr(item, 'sqlstate', None) for item in outcomes]}"
        )
        check = await asyncpg.connect(**scratch_conn)
        try:
            assert await check.fetchval("SELECT count(*) FROM public.messages WHERE project='dest'") == 1
            assert await check.fetchval("SELECT count(*) FROM public.archive_messages WHERE project='dest'") == 1
            assert await check.fetchval("SELECT nextval('public.messages_id_seq')") > 101
        finally:
            await check.close()
    run(case())


def test_api4_005_transfer_accepts_child_before_parent_in_same_table(api, scratch_conn, monkeypatch):
    async def case():
        conn = await asyncpg.connect(**scratch_conn)
        try:
            await conn.execute("CREATE TABLE sprints(id uuid PRIMARY KEY)")
            handoffs_ddl = re.search(
                r"CREATE TABLE IF NOT EXISTS handoffs \(.*?\n\);",
                SCHEMA.read_text(),
                re.DOTALL,
            )
            assert handoffs_ddl is not None, "source handoffs schema is unavailable"
            await conn.execute(handoffs_ddl.group())
            await conn.execute("CREATE TABLE cortex_projects(id uuid PRIMARY KEY, project_key text UNIQUE, status text)")
            target_project_id = uuid4()
            await conn.execute("INSERT INTO cortex_projects VALUES($1,'audit','active')", target_project_id)
            deps = await api.project_transfer_dependencies(conn)
            assert (("public", "handoffs"), ("public", "handoffs")) in deps
            child, parent = uuid4(), uuid4()
            rows = [
                {"id": str(child), "project": "source", "kind": "completion_handback", "from_agent": "worker", "to_role": "lead", "summary": "Child before parent", "reply_to_handoff_id": str(parent)},
                {"id": str(parent), "project": "source", "kind": "task", "from_agent": "lead", "to_role": "worker", "summary": "Parent task", "reply_to_handoff_id": None},
            ]

            class NativePool:
                @asynccontextmanager
                async def acquire(self):
                    yield conn

            monkeypatch.setattr(api, "pool_admin", NativePool())
            monkeypatch.setattr(api, "ADMIN_TOKEN", "audit-test-only")
            request = Request({
                "type": "http", "method": "POST", "path": "/admin/projects/audit/import",
                "headers": [(b"x-cortex-admin-token", b"audit-test-only")], "query_string": b"",
            })
            transfer = api.ProjectImportRequest(
                format="kaidera.cortex-project.v1",
                source_project={"project_key": "source", "project_id": str(uuid4())},
                tables=[api.ProjectTransferTable(schema_name="public", table_name="handoffs", rows=rows)],
            )
            try:
                result = await api.import_project("audit", transfer, request)
            except asyncpg.ForeignKeyViolationError:
                # A parent-first import through this same route must succeed;
                # otherwise a broken fixture could masquerade as the RED proof.
                assert await conn.fetchval("SELECT COUNT(*) FROM handoffs") == 0
                control = api.ProjectImportRequest(
                    format="kaidera.cortex-project.v1",
                    source_project=transfer.source_project,
                    tables=[api.ProjectTransferTable(schema_name="public", table_name="handoffs", rows=list(reversed(rows)))],
                )
                control_result = await api.import_project("audit", control, request)
                assert control_result["inserted"] == 2
                assert await conn.fetchval("SELECT reply_to_handoff_id FROM handoffs WHERE id=$1", child) == parent
                raise
            assert result["inserted"] == 2
            imported = await conn.fetch("SELECT id, project, reply_to_handoff_id FROM handoffs ORDER BY id")
            assert {row["id"] for row in imported} == {child, parent}
            assert all(row["project"] == "audit" for row in imported)
            assert next(row for row in imported if row["id"] == child)["reply_to_handoff_id"] == parent
        finally:
            await conn.close()
    run(case())


def test_schema_3_001_migration_does_not_rewind_concurrent_message_ids(scratch_conn):
    async def case():
        first = await asyncpg.connect(**scratch_conn)
        second = await asyncpg.connect(**scratch_conn)
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
