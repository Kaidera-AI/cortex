from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


API_MAIN_PATH = Path(__file__).resolve().parents[1] / "main.py"


def load_module(name: str):
    spec = importlib.util.spec_from_file_location(name, API_MAIN_PATH)
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


class MemoryConn:
    def __init__(self, existing: bool = False):
        self.existing = existing
        self.sql: list[str] = []

    async def fetchrow(self, sql, *args):
        self.sql.append(sql)
        if "SELECT id::text AS id" in sql:
            return {"id": "11111111-1111-4111-8111-111111111111"} if self.existing else None
        if "UPDATE knowledge" in sql:
            # KD-7: the update-path UPDATE clears the stale embedding so corrected
            # rows re-embed; the INSERT must still carry no inline embedding.
            assert "embedding = NULL" in sql
            assert "project_id" in sql
            return {"id": "11111111-1111-4111-8111-111111111111", "action": "updated"}
        if "INSERT INTO knowledge" in sql:
            assert "embedding" not in sql
            assert "project_id" in sql
            return {"id": "22222222-2222-4222-8222-222222222222", "action": "created"}
        raise AssertionError(f"Unexpected SQL: {sql}")


@pytest.mark.asyncio
async def test_memory_write_uses_safe_knowledge_upsert(monkeypatch):
    module = load_module("cortex_api_memory_write_create")
    conn = MemoryConn(existing=False)

    async def allow_writer(*args, **kwargs):
        return None

    monkeypatch.setattr(module, "require_registered_agent_writer", allow_writer)
    monkeypatch.setattr(module, "acquire_scoped", lambda project: FakeAcquire(conn))

    result = await module.write_memory(
        module.MemoryWrite(section="Sample Worker", content="Sample project guidance."),
        x_agent="sample-worker",
        x_project="sample-project",
    )

    assert result["status"] == "created"
    assert result["created"] is True
    assert result["embedded"] is False
    assert any("INSERT INTO knowledge" in sql for sql in conn.sql)


@pytest.mark.asyncio
async def test_memory_write_updates_existing_source(monkeypatch):
    module = load_module("cortex_api_memory_write_update")
    conn = MemoryConn(existing=True)

    async def allow_writer(*args, **kwargs):
        return None

    monkeypatch.setattr(module, "require_registered_agent_writer", allow_writer)
    monkeypatch.setattr(module, "acquire_scoped", lambda project: FakeAcquire(conn))

    result = await module.write_memory(
        module.MemoryWrite(
            section="Sample Worker",
            content="Updated sample project guidance.",
            source="sample-worker/manual",
        ),
        x_agent="sample-worker",
        x_project="sample-project",
    )

    assert result["status"] == "updated"
    assert result["updated"] is True
    assert any("UPDATE knowledge" in sql for sql in conn.sql)
