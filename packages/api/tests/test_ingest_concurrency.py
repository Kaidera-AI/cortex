"""Natural-key ingest decisions are atomic under concurrent/lost-response retries."""

from __future__ import annotations

import asyncio
import copy
import importlib.util
from pathlib import Path
import sys

import pytest


API_MAIN = Path(__file__).resolve().parents[1] / "main.py"


def load_api_module():
    spec = importlib.util.spec_from_file_location("cortex_api_ingest_concurrency_test", API_MAIN)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(API_MAIN.parent))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(API_MAIN.parent))
    return module


class SharedState:
    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.rows: dict[str, dict] = {}
        self.inserts = 0


class Transaction:
    def __init__(self, conn: "Conn") -> None:
        self.conn = conn

    async def __aenter__(self):
        self.conn.in_transaction = True
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.conn.in_transaction = False
        if self.conn.owns_lock:
            self.conn.state.lock.release()
            self.conn.owns_lock = False
        return False


class Conn:
    def __init__(self, state: SharedState) -> None:
        self.state = state
        self.in_transaction = False
        self.owns_lock = False

    def transaction(self):
        return Transaction(self)

    async def fetchval(self, sql, *args):
        if "pg_advisory_xact_lock" in sql:
            assert self.in_transaction, "the ingest lock must live inside the mutation transaction"
            await self.state.lock.acquire()
            self.owns_lock = True
            return None
        if "INSERT INTO knowledge" in sql:
            key = f"knowledge:{args[0]}:{args[2]}"
            row = {
                "id": f"row-{self.state.inserts + 1}",
                "content": args[1],
                "category": args[3],
                "section": args[4],
            }
        elif "INSERT INTO lessons" in sql:
            key = f"lesson:{args[0]}:{args[1]}:{args[3] or ''}"
            row = {
                "id": f"row-{self.state.inserts + 1}",
                "detail": args[2],
                "agent_name": args[4],
                "importance": args[5],
            }
        elif "INSERT INTO decisions" in sql:
            key = f"decision:{args[0]}:{args[1]}:{args[3] or ''}"
            row = {
                "id": f"row-{self.state.inserts + 1}",
                "rationale": args[2],
                "agent_name": args[4],
            }
        else:
            raise AssertionError(f"unexpected fetchval SQL: {sql}")
        assert self.in_transaction and self.owns_lock
        # Make the old SELECT-then-INSERT implementation deterministically interleave.
        await asyncio.sleep(0)
        assert key not in self.state.rows, "natural-key duplicate reached INSERT"
        self.state.inserts += 1
        self.state.rows[key] = row
        return row["id"]

    async def fetchrow(self, sql, *args):
        assert self.in_transaction and self.owns_lock, "SELECT must be protected by the xact lock"
        if "FROM knowledge" in sql:
            key = f"knowledge:{args[0]}:{args[1]}"
        elif "FROM lessons" in sql:
            key = f"lesson:{args[0]}:{args[1]}:{args[2] or ''}"
        elif "FROM decisions" in sql:
            key = f"decision:{args[0]}:{args[1]}:{args[2] or ''}"
        else:
            raise AssertionError(f"unexpected fetchrow SQL: {sql}")
        await asyncio.sleep(0)
        row = self.state.rows.get(key)
        return copy.deepcopy(row) if row is not None else None


class Acquire:
    def __init__(self, conn: Conn) -> None:
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


@pytest.fixture
def api_and_state(monkeypatch):
    api = load_api_module()
    state = SharedState()

    def acquire(_project):
        return Acquire(Conn(state))

    monkeypatch.setattr(api, "acquire_scoped", acquire)
    return api, state


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["knowledge", "lesson", "decision"])
async def test_two_concurrent_exact_ingests_create_one_row_and_replay_unchanged(
    api_and_state,
    kind,
):
    api, state = api_and_state
    if kind == "knowledge":
        body = api.KnowledgeIngest(
            content="seed bytes",
            source_file=".kaidera-os/project-packs/dev-os/seed.md",
            category="bootstrap",
            section="seed",
        )
        call = lambda: api.ingest_knowledge(body, x_project="dev-os")
    elif kind == "lesson":
        body = api.LessonIngest(
            summary="same lesson",
            detail="same detail",
            category="bootstrap",
            agent_name=None,
        )
        call = lambda: api.ingest_lesson(body, x_project="dev-os")
    else:
        body = api.DecisionIngest(
            summary="same decision",
            rationale="same rationale",
            category="bootstrap",
            agent_name=None,
        )
        call = lambda: api.ingest_decision(body, x_project="dev-os")

    first, second = await asyncio.gather(call(), call())

    assert state.inserts == 1
    assert {first["status"], second["status"]} == {"created", "unchanged"}
    assert first["id"] == second["id"]
