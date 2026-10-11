"""A content read must clamp the time remaining after a slow prefix lookup."""
import asyncio
from contextlib import suppress
import time

import asyncpg

import test_audit_runtime_proofs as runtime_proofs
from test_exact_id_deadline_native import ExactConnection, seed

api = runtime_proofs.api
scratch_pg = runtime_proofs.scratch_pg


def test_content_read_reclamps_remaining_budget_after_native_prefix_lock(api, scratch_pg, monkeypatch):
    async def case():
        native = await asyncpg.connect(**scratch_pg)
        blocker = await asyncpg.connect(**scratch_pg)
        transactions = []
        release_tasks = []
        try:
            await seed(native)

            async def lock_for(seconds):
                transaction = blocker.transaction()
                await transaction.start()
                await blocker.execute("LOCK TABLE decisions IN ACCESS EXCLUSIVE MODE")
                transactions.append(transaction)

                async def release():
                    await asyncio.sleep(seconds)
                    await transaction.rollback()

                release_tasks.append(asyncio.create_task(release()))

            await lock_for(.075)

            class Connection(ExactConnection):
                content_locked = False

                async def fetchrow(self, sql, *args):
                    row = await super().fetchrow(sql, *args)
                    if "FROM decisions" in sql and self.decisions_reads == 1:
                        await release_tasks[0]
                        await lock_for(.8)
                        self.content_locked = True
                    return row

            connection = Connection(native)
            monkeypatch.setattr(api, "SEARCH_TOTAL_BUDGET_MS", 100)
            monkeypatch.setattr(api, "_embed_query_outcome", lambda *_args: asyncio.sleep(
                0, result=api.SearchProviderOutcome("skipped")))
            start = time.monotonic()
            result = await api.execute_search(connection, "audit", "aaaaaaaa", search_type="decisions",
                                              rerank=False, graph=False)
            elapsed = time.monotonic() - start
            assert connection.content_locked, "prefix lookup did not reach the content-lock control"
            assert elapsed < .16, f"content read reused prefix timeout beyond remaining budget: {elapsed:.3f}s"
            assert connection.decisions_reads == 2
            assert "exact-id:decisions" in result["degraded"]
        finally:
            for task, transaction in zip(release_tasks, transactions):
                if not task.done():
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
                    await transaction.rollback()
                else:
                    await task
            await native.close()
            await blocker.close()

    asyncio.run(case())
