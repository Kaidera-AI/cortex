"""Frozen route regressions; requires an owned disposable audit PostgreSQL."""
from contextlib import asynccontextmanager
import json
import re
from uuid import uuid4

import asyncpg
import pytest
from starlette.requests import Request

from test_audit_data_integrity_native import (
    SCHEMA,
    api as frozen_api,
    run,
    scratch_conn as frozen_scratch_conn,
)


@pytest.fixture(scope="module")
def api():
    yield from frozen_api.__wrapped__()


@pytest.fixture
def scratch_conn():
    yield from frozen_scratch_conn.__wrapped__()


@asynccontextmanager
async def case_context(api, scratch_conn, monkeypatch, ddl=None):
    conn = await asyncpg.connect(**scratch_conn)
    attempts = []
    try:
        await conn.execute("CREATE TABLE cortex_projects(id uuid PRIMARY KEY, project_key text UNIQUE, status text)")
        await conn.execute("INSERT INTO cortex_projects VALUES($1,'audit','active')", uuid4())
        if ddl is None:
            ddl = """CREATE TABLE transfer_nodes(
                id uuid PRIMARY KEY, project text NOT NULL,
                parent_id uuid REFERENCES transfer_nodes(id),
                marker text NOT NULL DEFAULT 'kept' CHECK(marker <> 'invalid'))"""
        await conn.execute(ddl)

        class Proxy:
            def __getattr__(self, name):
                return getattr(conn, name)

            async def execute(self, statement, *args):
                if statement.startswith("INSERT INTO"):
                    attempts.append(json.loads(args[0]))
                return await conn.execute(statement, *args)

        class Pool:
            @asynccontextmanager
            async def acquire(self):
                yield Proxy()

        monkeypatch.setattr(api, "pool_admin", Pool())
        monkeypatch.setattr(api, "ADMIN_TOKEN", "audit-test-only")
        request = Request({"type": "http", "method": "POST", "path": "/admin/projects/audit/import",
                           "headers": [(b"x-cortex-admin-token", b"audit-test-only")], "query_string": b""})

        async def import_rows(rows, *, table="transfer_nodes", allow_existing=False, extra=()):
            body = api.ProjectImportRequest(
                format="kaidera.cortex-project.v1",
                source_project={"project_key": "source", "project_id": str(uuid4())},
                tables=[*extra, api.ProjectTransferTable(schema_name="public", table_name=table, rows=rows)],
                allow_existing=allow_existing,
            )
            return await api.import_project("audit", body, request)

        yield conn, attempts, import_rows
    finally:
        await conn.close()


def row(identifier, parent=None, **kwargs):
    return {"id": str(identifier), "project": "source",
            "parent_id": str(parent) if parent is not None else None, **kwargs}


def test_child_first_is_a_body_assertion_not_fixture_error(api, scratch_conn, monkeypatch):
    async def case():
        handoffs = re.search(r"CREATE TABLE IF NOT EXISTS handoffs \(.*?\n\);", SCHEMA.read_text(), re.DOTALL)
        assert handoffs is not None
        ddl = "CREATE TABLE sprints(id uuid PRIMARY KEY);" + handoffs.group()
        async with case_context(api, scratch_conn, monkeypatch, ddl) as (conn, attempts, import_rows):
            parent, child = uuid4(), uuid4()
            rows = [
                {"id": str(child), "project": "source", "kind": "completion_handback", "from_agent": "worker",
                 "to_role": "lead", "summary": "child", "reply_to_handoff_id": str(parent)},
                {"id": str(parent), "project": "source", "kind": "task", "from_agent": "lead",
                 "to_role": "worker", "summary": "parent", "reply_to_handoff_id": None},
            ]
            error, result = None, None
            try:
                result = await import_rows(rows, table="handoffs")
            except (asyncpg.PostgresError, api.HTTPException) as caught:
                error = type(caught).__name__
            assert error is None, f"child-first import refused: {error}"
            assert result["inserted"] == 2 and result["skipped"] == 0
            assert [r["id"] for r in attempts] == [str(parent), str(child)]
            data = await conn.fetch("SELECT id,project,reply_to_handoff_id FROM handoffs")
            assert {r["id"] for r in data} == {parent, child}
            assert all(r["project"] == "audit" for r in data)
            assert next(r for r in data if r["id"] == child)["reply_to_handoff_id"] == parent
    run(case())


def test_reverse_chain_and_stable_ready_order(api, scratch_conn, monkeypatch):
    async def case():
        async with case_context(api, scratch_conn, monkeypatch) as (conn, attempts, import_rows):
            leaf, middle, root, sibling, unrelated = [uuid4() for _ in range(5)]
            result = await import_rows([row(leaf, middle), row(middle, root), row(root), row(sibling, root), row(unrelated)])
            assert result["inserted"] == 5
            assert [r["id"] for r in attempts] == list(map(str, [root, middle, leaf, sibling, unrelated]))
            assert await conn.fetchval("SELECT count(*) FROM transfer_nodes WHERE project='audit' AND marker='kept'") == 5
    run(case())


def test_existing_parent_is_free_and_duplicate_counts_preserved(api, scratch_conn, monkeypatch):
    async def case():
        async with case_context(api, scratch_conn, monkeypatch) as (conn, attempts, import_rows):
            parent, child = uuid4(), uuid4()
            await conn.execute("INSERT INTO transfer_nodes(id,project,marker) VALUES($1,'audit','existing')", parent)
            result = await import_rows([row(child, parent), row(parent, marker="ignored")], allow_existing=True)
            assert result["inserted"] == 1 and result["skipped"] == 1
            assert [r["id"] for r in attempts] == [str(child), str(parent)]
            assert await conn.fetchval("SELECT marker FROM transfer_nodes WHERE id=$1", parent) == "existing"
            assert await conn.fetchval("SELECT parent_id FROM transfer_nodes WHERE id=$1", child) == parent
    run(case())


def test_payload_duplicate_parent_keeps_first_value_and_defaults(api, scratch_conn, monkeypatch):
    async def case():
        async with case_context(api, scratch_conn, monkeypatch) as (conn, attempts, import_rows):
            parent, child = uuid4(), uuid4()
            result = await import_rows([row(child, parent), row(parent, marker="first"), row(parent, marker="second")])
            assert result["inserted"] == 2 and result["skipped"] == 1
            assert await conn.fetchval("SELECT marker FROM transfer_nodes WHERE id=$1", parent) == "first"
            assert await conn.fetchval("SELECT marker FROM transfer_nodes WHERE id=$1", child) == "kept"
    run(case())


@pytest.mark.parametrize("kind", ["missing", "cycle", "self_cycle"])
def test_refusal_is_explicit_bounded_and_rolls_back_other_table(api, scratch_conn, monkeypatch, kind):
    async def case():
        async with case_context(api, scratch_conn, monkeypatch) as (conn, attempts, import_rows):
            await conn.execute("CREATE TABLE a_transfer(id uuid PRIMARY KEY,project text)")
            first, second = uuid4(), uuid4()
            rows = [row(first, second)]
            if kind == "cycle":
                rows.append(row(second, first))
            if kind == "self_cycle":
                rows = [row(first, first)]
            extra = [api.ProjectTransferTable(schema_name="public", table_name="a_transfer", rows=[row(uuid4())])]
            error = None
            try:
                await __import__("asyncio").wait_for(import_rows(rows, extra=extra), 3)
            except (asyncpg.PostgresError, api.HTTPException) as caught:
                error = caught
            assert isinstance(error, api.HTTPException) and error.status_code == 409
            assert ("missing self-referenced parent" if kind == "missing" else "cyclic self-references") in error.detail
            assert await conn.fetchval("SELECT count(*) FROM transfer_nodes") == 0
            assert await conn.fetchval("SELECT count(*) FROM a_transfer") == 0
            assert len(attempts) == 1, "self-FK refusal must precede any INSERT into that table"
    run(case())


def test_multiple_self_fk_columns_are_all_ordered(api, scratch_conn, monkeypatch):
    async def case():
        ddl = """CREATE TABLE transfer_nodes(id uuid PRIMARY KEY,project text,
                 parent_id uuid REFERENCES transfer_nodes(id), other_id uuid REFERENCES transfer_nodes(id))"""
        async with case_context(api, scratch_conn, monkeypatch, ddl) as (conn, attempts, import_rows):
            child, first, second = [uuid4() for _ in range(3)]
            result = await import_rows([row(child, first, other_id=str(second)), row(first), row(second)])
            assert result["inserted"] == 3
            assert [r["id"] for r in attempts] == list(map(str, [first, second, child]))
    run(case())


@pytest.mark.parametrize("match", ["SIMPLE", "FULL"])
def test_composite_self_fk_uses_typed_database_keys(api, scratch_conn, monkeypatch, match):
    async def case():
        ddl = f"""CREATE TABLE transfer_nodes(project text, a uuid,b integer,pa uuid,pb integer,
                 PRIMARY KEY(a,b), FOREIGN KEY(pa,pb) REFERENCES transfer_nodes(a,b) MATCH {match})"""
        async with case_context(api, scratch_conn, monkeypatch, ddl) as (conn, attempts, import_rows):
            child, parent = uuid4(), uuid4()
            result = await import_rows([
                {"project": "source", "a": str(child), "b": 2, "pa": str(parent).upper(), "pb": "1"},
                {"project": "source", "a": str(parent), "b": "1", "pa": None, "pb": None},
            ])
            assert result["inserted"] == 2
            assert [r["a"] for r in attempts] == [str(parent), str(child)]
            assert await conn.fetchval("SELECT pa FROM transfer_nodes WHERE a=$1", child) == parent
    run(case())


@pytest.mark.parametrize("match", ["SIMPLE", "FULL"])
def test_composite_partial_null_preserves_match_semantics(api, scratch_conn, monkeypatch, match):
    async def case():
        ddl = f"""CREATE TABLE transfer_nodes(project text,a integer,b integer,pa integer,pb integer,
                 PRIMARY KEY(a,b), FOREIGN KEY(pa,pb) REFERENCES transfer_nodes(a,b) MATCH {match})"""
        async with case_context(api, scratch_conn, monkeypatch, ddl) as (conn, attempts, import_rows):
            rows = [{"project": "source", "a": 1, "b": 1, "pa": 99, "pb": None}]
            error, result = None, None
            try:
                result = await import_rows(rows)
            except (asyncpg.PostgresError, api.HTTPException) as caught:
                error = caught
            if match == "SIMPLE":
                assert error is None and result["inserted"] == 1
            else:
                assert isinstance(error, api.HTTPException) and error.status_code == 409
                assert attempts == [] and await conn.fetchval("SELECT count(*) FROM transfer_nodes") == 0
    run(case())


def test_non_fk_error_still_rolls_back_whole_import(api, scratch_conn, monkeypatch):
    async def case():
        async with case_context(api, scratch_conn, monkeypatch) as (conn, attempts, import_rows):
            with pytest.raises(asyncpg.CheckViolationError):
                await import_rows([row(uuid4()), row(uuid4(), marker="invalid")])
            assert await conn.fetchval("SELECT count(*) FROM transfer_nodes") == 0
    run(case())


def test_non_self_table_keeps_input_order(api, scratch_conn, monkeypatch):
    async def case():
        ddl = "CREATE TABLE transfer_nodes(id uuid PRIMARY KEY,project text,parent_id uuid,marker text DEFAULT 'kept')"
        async with case_context(api, scratch_conn, monkeypatch, ddl) as (conn, attempts, import_rows):
            first, second = uuid4(), uuid4()
            result = await import_rows([row(first, second), row(second)])
            assert result["inserted"] == 2
            assert [r["id"] for r in attempts] == [str(first), str(second)]
    run(case())


@pytest.mark.parametrize("duplicate", ["payload", "existing"])
def test_skipped_duplicate_does_not_validate_its_unused_reference(api, scratch_conn, monkeypatch, duplicate):
    async def case():
        async with case_context(api, scratch_conn, monkeypatch) as (conn, attempts, import_rows):
            parent, child, absent = uuid4(), uuid4(), uuid4()
            if duplicate == "existing":
                await conn.execute("INSERT INTO transfer_nodes(id,project,marker) VALUES($1,'audit','first')", parent)
                rows = [row(child, parent), row(parent, absent, marker="second")]
            else:
                rows = [row(child, parent), row(parent, marker="first"), row(parent, absent, marker="second")]
            error, result = None, None
            try:
                result = await import_rows(rows, allow_existing=(duplicate == "existing"))
            except (asyncpg.PostgresError, api.HTTPException) as caught:
                error = type(caught).__name__
            assert error is None, f"reference of skipped duplicate was evaluated: {error}"
            assert result["inserted"] == (1 if duplicate == "existing" else 2)
            assert result["skipped"] == 1
            assert await conn.fetchval("SELECT marker FROM transfer_nodes WHERE id=$1", parent) == "first"
            assert await conn.fetchval("SELECT parent_id FROM transfer_nodes WHERE id=$1", child) == parent
    run(case())


def test_secondary_unique_conflict_skips_unused_missing_parent_full_route(api, scratch_conn, monkeypatch):
    async def case():
        ddl = """CREATE TABLE transfer_nodes(id uuid PRIMARY KEY,project text NOT NULL,
                 parent_id uuid REFERENCES transfer_nodes(id),marker text UNIQUE NOT NULL)"""
        async with case_context(api, scratch_conn, monkeypatch, ddl) as (conn, attempts, import_rows):
            existing, incoming, absent = uuid4(), uuid4(), uuid4()
            await conn.execute("INSERT INTO transfer_nodes(id,project,marker) VALUES($1,'audit','duplicate')", existing)
            error, result = None, None
            try:
                result = await import_rows([row(incoming, absent, marker="duplicate")], allow_existing=True)
            except (asyncpg.PostgresError, api.HTTPException) as caught:
                error = type(caught).__name__
            assert error is None, f"unused FK of secondary-unique conflict was evaluated: {error}"
            assert result["inserted"] == 0 and result["skipped"] == 1
            assert await conn.fetchval("SELECT count(*) FROM transfer_nodes") == 1
            assert await conn.fetchval("SELECT id FROM transfer_nodes") == existing
            assert await conn.fetchval("SELECT count(*) FROM transfer_nodes c LEFT JOIN transfer_nodes p ON p.id=c.parent_id WHERE c.parent_id IS NOT NULL AND p.id IS NULL") == 0
    run(case())


def test_pg_self_reference_is_valid_input_policy_control(api, scratch_conn, monkeypatch):
    async def case():
        async with case_context(api, scratch_conn, monkeypatch) as (conn, attempts, import_rows):
            identifier = uuid4()
            await conn.execute("INSERT INTO transfer_nodes(id,project,parent_id) VALUES($1,'audit',$1)", identifier)
            assert await conn.fetchval("SELECT count(*) FROM transfer_nodes WHERE id=parent_id") == 1
    run(case())
