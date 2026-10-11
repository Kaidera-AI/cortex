"""Frozen content-read deadline, healthy fast-path and connection-reset controls."""
import asyncio
from contextlib import suppress
import time
from types import SimpleNamespace

import asyncpg

import test_audit_runtime_proofs as runtime_proofs

api = runtime_proofs.api
scratch_pg = runtime_proofs.scratch_pg


async def seed(connection):
    await connection.execute("""CREATE TABLE decisions(
        id uuid PRIMARY KEY, project text, created_at timestamptz DEFAULT now(),
        summary text, invalidated_at timestamptz)""")
    await connection.execute("""INSERT INTO decisions(id,project,summary)
        VALUES('aaaaaaaa-0000-0000-0000-000000000001','audit','healthy exact decision')""")


class ExactConnection:
    def __init__(self, native):
        self.native = native
        self.sweeps = 0
        self.decisions_reads = 0

    async def fetchrow(self, sql, *args):
        if "FROM decisions" in sql:
            self.decisions_reads += 1
            return await self.native.fetchrow(sql, *args)
        return None

    async def fetch(self, *_args):
        self.sweeps += 1
        return []

    async def execute(self, sql, *_args):
        if sql.startswith("SET statement_timeout"):
            return await self.native.execute(sql)
        return "SET"


def test_content_read_lock_obeys_total_deadline_and_connection_remains_usable(api, scratch_pg, monkeypatch):
    async def case():
        native = await asyncpg.connect(**scratch_pg)
        blocker = await asyncpg.connect(**scratch_pg)
        transaction = None
        release_task = None
        try:
            await seed(native)

            class ContentLockConnection(ExactConnection):
                async def fetchrow(self, sql, *args):
                    nonlocal transaction, release_task
                    row = await super().fetchrow(sql, *args)
                    if "FROM decisions" in sql and self.decisions_reads == 1:
                        transaction = blocker.transaction()
                        await transaction.start()
                        await blocker.execute("LOCK TABLE decisions IN ACCESS EXCLUSIVE MODE")

                        async def release():
                            await asyncio.sleep(.8)
                            await transaction.rollback()

                        release_task = asyncio.create_task(release())
                    return row

            connection = ContentLockConnection(native)
            monkeypatch.setattr(api, "SEARCH_TOTAL_BUDGET_MS", 100)
            monkeypatch.setattr(api, "_embed_query_outcome", lambda *_args: asyncio.sleep(
                0, result=api.SearchProviderOutcome("skipped")))
            start = time.monotonic()
            result = await api.execute_search(connection, "audit", "aaaaaaaa", search_type="decisions",
                                              rerank=False, graph=False)
            elapsed = time.monotonic() - start
            assert elapsed < .4, f"exact-ID content read outlived 100ms budget: {elapsed:.3f}s"
            assert "exact-id:decisions" in result["degraded"]
            assert connection.decisions_reads == 2
            assert await native.fetchval("SELECT 1") == 1, "cancelled connection remained busy"
            await native.execute("RESET statement_timeout")
            assert await native.fetchval("SHOW statement_timeout") == "0"
            await native.execute("SELECT pg_sleep(.15)")
            assert await native.fetchval("SELECT 1") == 1
        finally:
            if release_task is not None and not release_task.done():
                release_task.cancel()
                with suppress(asyncio.CancelledError):
                    await release_task
                await transaction.rollback()
            elif release_task is not None:
                await release_task
            await native.close()
            await blocker.close()

    asyncio.run(case())


def test_unlocked_exact_id_keeps_results_and_skips_sweep(api, scratch_pg, monkeypatch):
    async def case():
        native = await asyncpg.connect(**scratch_pg)
        try:
            await seed(native)
            connection = ExactConnection(native)
            monkeypatch.setattr(api, "SEARCH_TOTAL_BUDGET_MS", 100)
            monkeypatch.setattr(api, "_embed_query_outcome", lambda *_args: asyncio.sleep(
                0, result=api.SearchProviderOutcome("skipped")))
            result = await api.execute_search(connection, "audit", "aaaaaaaa", search_type="decisions",
                                              rerank=False, graph=False)
            assert result["results"][0]["text"] == "[DECISIONS MATCH] healthy exact decision"
            assert result["results"][0]["id"] == "aaaaaaaa-0000-0000-0000-000000000001"
            assert result["degraded"] == []
            assert connection.decisions_reads == 2
            assert connection.sweeps == 0
        finally:
            await native.close()

    asyncio.run(case())


def test_spent_deadline_starts_no_exact_id_database_read(api, monkeypatch):
    async def case():
        class Connection:
            exact_reads = 0

            async def fetchrow(self, *_args):
                self.exact_reads += 1
                return None

            async def fetch(self, *_args):
                return []

            async def execute(self, *_args):
                return "SET"

        ticks = iter([0.0, .2])
        monkeypatch.setattr(api, "time", SimpleNamespace(monotonic=lambda: next(ticks, .2), time=time.time))
        monkeypatch.setattr(api, "SEARCH_TOTAL_BUDGET_MS", 100)
        connection = Connection()
        result = await api.execute_search(connection, "audit", "aaaaaaaa", search_type="decisions",
                                          rerank=False, graph=False)
        assert connection.exact_reads == 0, "spent budget still opened exact-ID SQL"
        assert "exact-id:decisions" in result["degraded"]

    asyncio.run(case())
