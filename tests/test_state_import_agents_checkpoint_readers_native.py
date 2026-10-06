"""R186 additional native reader and lock-acquisition boundary regressions."""
import asyncio
import hashlib

import pytest

from fixtures.state_import_agents_native import agent_cluster, state_cluster
from test_state_import_agents_native import PROJECTS, setup, prepare
from test_state_import_agents_rework_native import no_authority
from test_state_import_agents_checkpoints_native import (
    later_quarantine, ledger_count, no_advisory_locks,
)


def test_native_roster_reads_only_committed_prefix_before_later_quarantine(agent_cluster):
    async def run():
        async with agent_cluster() as pair:
            project_run, engine, adapter, snapshot, binding, policy = await later_quarantine(pair)
            def crash(stage, checkpoint):
                if stage == "after_commit" and checkpoint == 1:
                    raise RuntimeError("R186 reader prefix interruption")
            with pytest.raises(RuntimeError, match="R186 reader prefix"):
                await engine.import_agents(pair["target"], snapshot, binding, policy, fault=crash)
            group = adapter.agent_groups(snapshot.records, policy, snapshot.dependencies)[0]
            expected = {record.source_reference: record.original_bytes for record in group.records}
            assert len(expected) == 9
            assert await engine.read_inverse(pair["target"], binding.run_id) == expected
            before = await ledger_count(pair["target"], binding.run_id)
            roster = await adapter.read_native_roster(pair["target"], binding.run_id)
            assert len(roster) == 1
            assert {item["source_reference"] for item in roster[0]["records"]} == set(expected)
            assert roster[0]["roster_policy"]
            assert await ledger_count(pair["target"], binding.run_id) == before
            result = await engine.import_agents(pair["target"], snapshot, binding, policy)
            assert result["counts"] == {"migrated": 9, "quarantined": 1}
            assert result["functional_pass"] is False
            completed = await adapter.read_native_roster(pair["target"], binding.run_id)
            assert len(completed) == 2
            assert completed[0] == roster[0]
            assert completed[1]["source_project_id"] == str(PROJECTS[2])
            assert completed[1]["records"] == [] and completed[1]["roster_policy"] == {}
            assert await pair["target"].fetchval("SELECT count(*) FROM cortex_core.import_rows WHERE run_id=$1", project_run) == 12
            await no_authority(pair)
    asyncio.run(run())


class InterruptAfterLockAcquired:
    """Interrupt after PostgreSQL grants the actual session lock, before returning."""
    def __init__(self, connection, kind):
        self.connection = connection
        self.kind = kind
        self.acquired = asyncio.Event()
        self.release = asyncio.Event()

    def __getattr__(self, name):
        return getattr(self.connection, name)

    async def execute(self, query, *args):
        result = await self.connection.execute(query, *args)
        if query == "SELECT pg_advisory_lock($1::bigint)":
            self.acquired.set()
            if self.kind == "raised":
                raise RuntimeError("R186 lock acquisition return interruption")
            await self.release.wait()
        return result


@pytest.mark.parametrize("kind", ["cancelled", "raised"])
def test_exact_session_lock_is_released_when_acquisition_return_is_interrupted(agent_cluster, kind):
    async def run():
        async with agent_cluster() as pair:
            await setup(pair)
            engine, _, snapshot, binding, policy = await prepare(pair)
            # An unrelated caller-owned lock must survive this importer invocation.
            unrelated = 314159265
            await pair["target"].execute("SELECT pg_advisory_lock($1::bigint)", unrelated)
            wrapped = InterruptAfterLockAcquired(pair["target"], kind)
            task = asyncio.create_task(engine.import_agents(wrapped, snapshot, binding, policy))
            try:
                await asyncio.wait_for(wrapped.acquired.wait(), 15)
                if kind == "cancelled":
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
                else:
                    with pytest.raises(RuntimeError, match="R186 lock acquisition"):
                        await task
                assert await ledger_count(pair["target"], binding.run_id) == (0, 0)
                assert await pair["target"].fetchval(
                    "SELECT count(*) FROM pg_locks WHERE pid=pg_backend_pid() AND locktype='advisory'"
                ) == 1
                assert await pair["target"].fetchval("SELECT pg_advisory_unlock($1::bigint)", unrelated) is True
                await no_advisory_locks(pair["target"])
                assert (await engine.import_agents(pair["target"], snapshot, binding, policy))["functional_pass"] is True
                await no_advisory_locks(pair["target"])
                await no_authority(pair)
            finally:
                wrapped.release.set()
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    asyncio.run(run())


def test_cancelled_native_lock_waiter_leaves_no_lock_or_ledger(agent_cluster):
    async def run():
        async with agent_cluster() as pair:
            await setup(pair)
            engine, _, snapshot, binding, policy = await prepare(pair)
            owner = await pair["connect"](pair["target_name"], "cortex_v2_migrator")
            lock = int.from_bytes(hashlib.sha256(b"cortex.state.import_agents.run").digest()[:8], "big", signed=True)
            task = None
            try:
                await owner.execute("SELECT pg_advisory_lock($1::bigint)", lock)
                pid = await pair["target"].fetchval("SELECT pg_backend_pid()")
                task = asyncio.create_task(engine.import_agents(pair["target"], snapshot, binding, policy))
                async def waiting():
                    while not await owner.fetchval("SELECT wait_event_type='Lock' FROM pg_stat_activity WHERE pid=$1", pid):
                        assert not task.done()
                        await asyncio.sleep(0.02)
                await asyncio.wait_for(waiting(), 15)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert await ledger_count(pair["target"], binding.run_id) == (0, 0)
                await no_advisory_locks(pair["target"])
                assert await owner.fetchval("SELECT pg_advisory_unlock($1::bigint)", lock) is True
                assert (await engine.import_agents(pair["target"], snapshot, binding, policy))["functional_pass"] is True
                await no_authority(pair)
                await no_advisory_locks(pair["target"])
            finally:
                if task is not None:
                    if not task.done():
                        task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                await owner.close()
    asyncio.run(run())
