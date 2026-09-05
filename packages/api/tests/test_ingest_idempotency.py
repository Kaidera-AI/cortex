import importlib.util
import uuid
from pathlib import Path

import pytest
from fastapi import HTTPException

from conftest import _NotARosterRead, roster_fetch, roster_fetchrow


API_MAIN_PATH = Path(__file__).resolve().parents[1] / "main.py"


def load_api_module():
    spec = importlib.util.spec_from_file_location(
        "cortex_api_main_ingest_idempotency_test",
        API_MAIN_PATH,
    )
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


class FakeTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return FakeAcquire(self.conn)


class IngestConn:
    def __init__(self, *, existing=None):
        self.existing = existing
        self.row_id = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
        self.updated = False
        self.inserted = False

    def transaction(self):
        return FakeTransaction()

    async def execute(self, sql, *args):
        if "set_config('cortex.project'" in sql:
            return "SELECT 1"
        raise AssertionError(f"Unexpected execute SQL: {sql}")

    async def fetchrow(self, sql, *args):
        try:  # E006 Inc04: registry resolver's cortex_projects.metadata read
            return roster_fetchrow(sql, args)
        except _NotARosterRead:
            pass
        if "FROM knowledge" in sql or "FROM lessons" in sql or "FROM decisions" in sql:
            return self.existing
        raise AssertionError(f"Unexpected fetchrow SQL: {sql}")

    async def fetch(self, sql, *args):
        try:  # E006 Inc04: registry resolver's agents/roles reads
            return roster_fetch(sql, args)
        except _NotARosterRead:
            pass
        raise AssertionError(f"Unexpected fetch SQL: {sql}")

    async def fetchval(self, sql, *args):
        if "pg_advisory_xact_lock" in sql:
            return None
        if "INSERT INTO" in sql:
            self.inserted = True
            return self.row_id
        if "UPDATE" in sql:
            self.updated = True
            return self.row_id
        raise AssertionError(f"Unexpected fetchval SQL: {sql}")


def install_conn(module, conn):
    pool = FakePool(conn)
    module.pool = pool
    module.pool_app = pool
    module.pool_admin = pool
    return conn


@pytest.mark.asyncio
async def test_knowledge_ingest_duplicate_unchanged_returns_status():
    api = load_api_module()
    row_id = uuid.UUID("11111111-1111-4111-8111-111111111111")
    conn = install_conn(
        api,
        IngestConn(
            existing={
                "id": row_id,
                "content": "same",
                "category": "imported",
                "section": "Same",
            }
        ),
    )

    result = await api.ingest_knowledge(
        api.KnowledgeIngest(
            content="same",
            source_file="/tmp/same.md",
            category="imported",
            section="Same",
        ),
        x_project="kaidera-os",
    )

    assert result == {"id": str(row_id), "status": "unchanged", "created": False, "embedded": False}
    assert conn.updated is False
    assert conn.inserted is False


@pytest.mark.asyncio
async def test_knowledge_ingest_duplicate_changed_conflicts_by_default():
    api = load_api_module()
    install_conn(
        api,
        IngestConn(
            existing={
                "id": uuid.UUID("11111111-1111-4111-8111-111111111111"),
                "content": "old",
                "category": "imported",
                "section": "Old",
            }
        ),
    )

    with pytest.raises(HTTPException) as excinfo:
        await api.ingest_knowledge(
            api.KnowledgeIngest(
                content="new",
                source_file="/tmp/same.md",
                category="imported",
                section="New",
            ),
            x_project="kaidera-os",
        )

    assert excinfo.value.status_code == 409
    assert excinfo.value.detail["status"] == "conflict"
    assert set(excinfo.value.detail["changed_fields"]) == {"content", "section"}


@pytest.mark.asyncio
async def test_knowledge_ingest_duplicate_changed_can_update_explicitly():
    api = load_api_module()
    conn = install_conn(
        api,
        IngestConn(
            existing={
                "id": uuid.UUID("11111111-1111-4111-8111-111111111111"),
                "content": "old",
                "category": "imported",
                "section": "Old",
            }
        ),
    )

    result = await api.ingest_knowledge(
        api.KnowledgeIngest(
            content="new",
            source_file="/tmp/same.md",
            category="imported",
            section="New",
            on_conflict="update",
        ),
        x_project="kaidera-os",
    )

    assert result["status"] == "updated"
    assert result["updated"] is True
    assert conn.updated is True


@pytest.mark.asyncio
async def test_lesson_and_decision_ingest_changed_duplicates_conflict():
    api = load_api_module()

    install_conn(
        api,
        IngestConn(
            existing={
                "id": uuid.UUID("22222222-2222-4222-8222-222222222222"),
                "detail": "old",
                "agent_name": "migration",
                "importance": 5,
            }
        ),
    )
    with pytest.raises(HTTPException) as lesson_exc:
        await api.ingest_lesson(
            api.LessonIngest(summary="same", detail="new", category="imported"),
            x_project="kaidera-os",
        )
    assert lesson_exc.value.status_code == 409
    assert lesson_exc.value.detail["kind"] == "lesson"

    install_conn(
        api,
        IngestConn(
            existing={
                "id": uuid.UUID("33333333-3333-4333-8333-333333333333"),
                "rationale": "old",
                "agent_name": "migration",
            }
        ),
    )
    with pytest.raises(HTTPException) as decision_exc:
        await api.ingest_decision(
            api.DecisionIngest(summary="same", rationale="new", category="imported"),
            x_project="kaidera-os",
        )
    assert decision_exc.value.status_code == 409
    assert decision_exc.value.detail["kind"] == "decision"
