"""Actual native cancellation followed by production scoped reset and reuse."""
import asyncio
from contextlib import asynccontextmanager, suppress

import asyncpg

import test_audit_runtime_proofs as runtime_proofs
from test_exact_id_deadline_native import ExactConnection, seed

api = runtime_proofs.api
scratch_pg = runtime_proofs.scratch_pg


def test_cancelled_exact_lookup_uses_production_scope_reset_on_same_native_connection(api, scratch_pg, monkeypatch):
    async def case():
        native = await asyncpg.connect(**scratch_pg)
        blocker = await asyncpg.connect(**scratch_pg)
        transaction = blocker.transaction()
        release_task = None
        try:
            await seed(native)
            pid = await native.fetchval("SELECT pg_backend_pid()")
            await transaction.start()
            await blocker.execute("LOCK TABLE decisions IN ACCESS EXCLUSIVE MODE")

            async def release():
                await asyncio.sleep(.8)
                await transaction.rollback()

            release_task = asyncio.create_task(release())

            class Connection(ExactConnection):
                cancelled = False
                statements = []

                async def fetchrow(self, sql, *args):
                    try:
                        return await super().fetchrow(sql, *args)
                    except asyncpg.exceptions.QueryCanceledError:
                        self.cancelled = True
                        raise

                async def execute(self, sql, *args):
                    self.statements.append(sql)
                    return await native.execute(sql, *args)

                def terminate(self):
                    native.terminate()

            connection = Connection(native)

            class Pool:
                @asynccontextmanager
                async def acquire(self):
                    yield connection  # No fake reset: only production acquire_scoped resets.

            monkeypatch.setattr(api, "pool_app", Pool())
            monkeypatch.setattr(api, "SEARCH_TOTAL_BUDGET_MS", 100)
            monkeypatch.setattr(api, "_embed_query_outcome", lambda *_args: asyncio.sleep(
                0, result=api.SearchProviderOutcome("skipped")))
            async with api.acquire_scoped("audit", statement_timeout_ms=3000) as scoped:
                result = await api.execute_search(scoped, "audit", "aaaaaaaa", search_type="decisions",
                                                  rerank=False, graph=False)
            assert connection.cancelled, "native query never reached deadline cancellation"
            assert "exact-id:decisions" in result["degraded"]
            assert "RESET statement_timeout" in connection.statements
            assert await native.fetchval("SHOW statement_timeout") == "0"
            assert await native.fetchval("SELECT current_setting('cortex.project')") == ""
            assert await native.fetchval("SELECT pg_backend_pid()") == pid
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
