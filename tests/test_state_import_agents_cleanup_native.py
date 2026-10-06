"""R191 author finding: cancellation must not interrupt owned session cleanup."""
import asyncio
import hashlib

import pytest

from fixtures.state_import_agents_native import agent_cluster, state_cluster
from test_state_import_agents_native import setup, prepare
from test_state_import_agents_rework_native import no_authority
from test_state_import_agents_checkpoints_native import (
    PauseAfterFirstCommit, ledger_count, no_advisory_locks,
)


class PauseUnlock(PauseAfterFirstCommit):
    def __init__(self, connection):
        super().__init__(connection)
        self.unlock_entered = asyncio.Event()
        self.unlock_release = asyncio.Event()

    async def execute(self, query, *args):
        if query == "SELECT pg_advisory_unlock($1::bigint)":
            self.unlock_entered.set()
            await self.unlock_release.wait()
        return await self.connection.execute(query, *args)


@pytest.mark.parametrize("mode,cancellations", [("success", 1), ("prefix", 2), ("prefix", 4)])
def test_cancellation_during_cleanup_releases_native_lock_and_unblocks_next_caller(agent_cluster, mode, cancellations):
    async def run():
        async with agent_cluster() as pair:
            project_run = await setup(pair)
            engine, adapter, snapshot, binding, policy = await prepare(pair)
            wrapped = PauseUnlock(pair["target"])
            other = await pair["connect"](pair["target_name"], "cortex_v2_migrator")
            importer_lock = int.from_bytes(hashlib.sha256(b"cortex.state.import_agents.run").digest()[:8], "big", signed=True)
            caller_lock = 271828182
            await pair["target"].execute("SELECT pg_advisory_lock($1::bigint)", caller_lock)
            task = None
            try:
                if mode == "success":
                    wrapped.release.set()
                task = asyncio.create_task(engine.import_agents(wrapped, snapshot, binding, policy))
                if mode == "prefix":
                    await asyncio.wait_for(wrapped.entered.wait(), 15)
                    assert await ledger_count(pair["target"], binding.run_id) == (2, 9)
                    task.cancel()
                await asyncio.wait_for(wrapped.unlock_entered.wait(), 15)
                for _ in range(cancellations - (mode == "prefix")):
                    task.cancel()
                    await asyncio.sleep(0)
                # Permit actual cleanup before awaiting termination. The release
                # must finish even when cancellation reaches this await again.
                wrapped.unlock_release.set()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 15)
                assert await other.fetchval("SELECT pg_try_advisory_lock($1::bigint)", importer_lock) is True
                assert await other.fetchval("SELECT pg_advisory_unlock($1::bigint)", importer_lock) is True
                assert await pair["target"].fetchval(
                    "SELECT count(*) FROM pg_locks WHERE pid=pg_backend_pid() AND locktype='advisory'"
                ) == 1
                assert await pair["target"].fetchval("SELECT pg_advisory_unlock($1::bigint)", caller_lock) is True
                await no_advisory_locks(pair["target"])
                result = await engine.import_agents(other, snapshot, binding, policy)
                assert result["functional_pass"] is True and result["counts"] == {"migrated": 10}
                assert await engine.read_inverse(other, binding.run_id) == {r.source_reference: r.original_bytes for r in snapshot.records}
                assert await other.fetchval("SELECT count(*) FROM cortex_core.import_runs WHERE run_id=$1 AND event_kind='complete'", binding.run_id) == 1
                assert await other.fetchval("SELECT count(*) FROM cortex_core.import_rows WHERE run_id=$1", project_run) == 12
                await no_authority(pair)
                await no_advisory_locks(other)
            finally:
                wrapped.release.set()
                wrapped.unlock_release.set()
                if task is not None:
                    if not task.done():
                        task.cancel()
                    await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 15)
                # Retire only this fixture's exact acquisitions after any RED.
                await pair["target"].execute("SELECT pg_advisory_unlock($1::bigint)", importer_lock)
                await pair["target"].execute("SELECT pg_advisory_unlock($1::bigint)", caller_lock)
                await other.close()
    asyncio.run(run())
