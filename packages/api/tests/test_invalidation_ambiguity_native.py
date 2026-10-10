"""Frozen scoped invalidation behavior through an owned scratch PostgreSQL."""
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
from uuid import UUID

import asyncpg
import pytest

from test_audit_data_integrity_native import (
    api as frozen_api,
    run,
    scratch_conn as frozen_scratch_conn,
)

TABLES = ("decisions", "lessons", "handoffs")
PREFIX = "aaaaaaaa"
PAST = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def api():
    yield from frozen_api.__wrapped__()


@pytest.fixture
def scratch_conn():
    yield from frozen_scratch_conn.__wrapped__()


def identifier(number):
    return UUID(f"aaaaaaaa-0000-0000-0000-{number:012x}")


@asynccontextmanager
async def case_context(api, scratch_conn, monkeypatch=None):
    conn = await asyncpg.connect(**scratch_conn)
    writes = []
    try:
        for table in TABLES:
            await conn.execute(f"CREATE TABLE {table}(id uuid PRIMARY KEY,project text NOT NULL,summary text NOT NULL DEFAULT 'fixture summary',invalidated_at timestamptz,metadata jsonb NOT NULL DEFAULT '{{}}'::jsonb)")
        if monkeypatch is not None:
            class Proxy:
                def __getattr__(self, name):
                    return getattr(conn, name)

                async def execute(self, statement, *args):
                    if statement.lstrip().upper().startswith("UPDATE"):
                        writes.append(statement)
                    return await conn.execute(statement, *args)

            @asynccontextmanager
            async def scoped(project):
                assert project == "audit"
                yield Proxy()

            monkeypatch.setattr(api, "acquire_scoped", scoped)
        yield conn, writes
    finally:
        await conn.close()


async def seed(conn, table, number, *, project="audit", invalidated=None):
    await conn.execute(f"INSERT INTO {table}(id,project,invalidated_at,metadata) VALUES($1,$2,$3,$4::jsonb)",
                       identifier(number), project, invalidated, json.dumps({"keep": number, "invalidation_reason": "old"}))


async def observe_error(api, coro):
    error, result = None, None
    try:
        result = await coro
    except api.HTTPException as caught:
        error = caught
    return error, result


def assert_ambiguity(api, error, candidates, *, truncated=False):
    assert isinstance(error, api.HTTPException), "ambiguous lookup silently returned a target"
    assert error.status_code == 409
    assert error.detail == {"code": "ambiguous_invalidation_target", "candidates": candidates, "truncated": truncated}


def candidate(table, number):
    return {"table": table, "id": str(identifier(number))}


def test_ambiguous_prefix_is_a_body_assertion(api, scratch_conn):
    async def case():
        async with case_context(api, scratch_conn) as (conn, writes):
            await seed(conn, "decisions", 2)
            await seed(conn, "decisions", 1)
            error, _ = await observe_error(api, api.find_invalidation_target(conn, "audit", PREFIX))
            # Keep the expected assertion in this test's own body for mutation custody.
            assert error is not None, "ambiguous lookup silently selected one row"
            assert_ambiguity(api, error, [candidate("decisions", 1), candidate("decisions", 2)])
    run(case())


def test_cross_kind_prefix_lists_all_scoped_candidates(api, scratch_conn):
    async def case():
        async with case_context(api, scratch_conn) as (conn, writes):
            for table, number in zip(TABLES, (3, 2, 1)):
                await seed(conn, table, number)
            error, _ = await observe_error(api, api.find_invalidation_target(conn, "audit", PREFIX))
            assert_ambiguity(api, error, [candidate(t, n) for t, n in zip(TABLES, (3, 2, 1))])
    run(case())


def test_exact_cross_kind_collision_refuses(api, scratch_conn):
    async def case():
        async with case_context(api, scratch_conn) as (conn, writes):
            await seed(conn, "decisions", 1)
            await seed(conn, "handoffs", 1)
            error, _ = await observe_error(api, api.find_invalidation_target(conn, "audit", str(identifier(1))))
            assert_ambiguity(api, error, [candidate("decisions", 1), candidate("handoffs", 1)])
    run(case())


@pytest.mark.parametrize("mode", ["exact", "prefix"])
def test_unique_target_excludes_other_project_collision(api, scratch_conn, mode):
    async def case():
        async with case_context(api, scratch_conn) as (conn, writes):
            await seed(conn, "lessons", 1)
            await seed(conn, "decisions", 2, project="other")
            key = str(identifier(1)) if mode == "exact" else PREFIX
            assert await api.find_invalidation_target(conn, "audit", key) == ("lessons", str(identifier(1)))
    run(case())


@pytest.mark.parametrize("undo", [False, True])
@pytest.mark.parametrize("kind", ["same_table", "cross_kind", "exact_collision"])
def test_ambiguous_route_never_writes_invalidate_or_undo(api, scratch_conn, monkeypatch, undo, kind):
    async def case():
        async with case_context(api, scratch_conn, monkeypatch) as (conn, writes):
            await seed(conn, "decisions", 1, invalidated=PAST if undo else None)
            other_table = "decisions" if kind == "same_table" else "handoffs"
            number = 1 if kind == "exact_collision" else 2
            await seed(conn, other_table, number, invalidated=PAST if undo else None)
            before = {t: await conn.fetch(f"SELECT * FROM {t} ORDER BY id") for t in TABLES}
            key = str(identifier(1)) if kind == "exact_collision" else PREFIX
            error, _ = await observe_error(api, api.invalidate_item(key, api.InvalidateRequest(undo=undo, reason="fixture"), x_project="audit"))
            assert isinstance(error, api.HTTPException) and error.status_code == 409
            assert isinstance(error.detail, dict) and error.detail["code"] == "ambiguous_invalidation_target"
            assert writes == []
            after = {t: await conn.fetch(f"SELECT * FROM {t} ORDER BY id") for t in TABLES}
            assert after == before, "ambiguous route changed timestamp or metadata"
    run(case())


@pytest.mark.parametrize("undo", [False, True])
def test_unique_route_still_changes_only_its_scoped_target(api, scratch_conn, monkeypatch, undo):
    async def case():
        async with case_context(api, scratch_conn, monkeypatch) as (conn, writes):
            await seed(conn, "decisions", 1, invalidated=PAST if undo else None)
            await seed(conn, "decisions", 2, project="other", invalidated=PAST)
            untouched = await conn.fetchrow("SELECT * FROM decisions WHERE id=$1", identifier(2))
            result = await api.invalidate_item(str(identifier(1)), api.InvalidateRequest(undo=undo, reason="fixture"), x_project="audit")
            assert result["id"] == str(identifier(1)) and result["table"] == "decisions"
            target = await conn.fetchrow("SELECT * FROM decisions WHERE id=$1", identifier(1))
            if undo:
                assert target["invalidated_at"] is None
                assert json.loads(target["metadata"]) == {"keep": 1}
            else:
                assert target["invalidated_at"] is not None
                assert json.loads(target["metadata"]) == {"keep": 1, "invalidation_reason": "fixture"}
            assert await conn.fetchrow("SELECT * FROM decisions WHERE id=$1", identifier(2)) == untouched
            assert len(writes) == 1
    run(case())


def test_zero_matches_returns_404_without_write(api, scratch_conn, monkeypatch):
    async def case():
        async with case_context(api, scratch_conn, monkeypatch) as (conn, writes):
            await seed(conn, "decisions", 1)
            error, _ = await observe_error(api, api.invalidate_item("bbbbbbbb", api.InvalidateRequest(), x_project="audit"))
            assert isinstance(error, api.HTTPException) and error.status_code == 404
            assert writes == []
    run(case())


@pytest.mark.parametrize("literal", ["%", "_", "\\", "aaaa%", "aaaa_"])
def test_prefix_treats_sql_wildcards_as_literal_characters(api, scratch_conn, literal):
    async def case():
        async with case_context(api, scratch_conn) as (conn, writes):
            await seed(conn, "decisions", 1)
            assert await api.find_invalidation_target(conn, "audit", literal) == (None, None)
    run(case())


@pytest.mark.parametrize("count", [50, 51, 120])
def test_candidate_listing_is_stable_bounded_and_honestly_truncated(api, scratch_conn, count):
    async def case():
        async with case_context(api, scratch_conn) as (conn, writes):
            await conn.executemany("INSERT INTO decisions(id,project) VALUES($1,'audit')",
                                   [(identifier(i),) for i in range(count, 0, -1)])
            error, _ = await observe_error(api, api.find_invalidation_target(conn, "audit", PREFIX))
            assert_ambiguity(api, error, [candidate("decisions", i) for i in range(1, 51)], truncated=(count > 50))
    run(case())
