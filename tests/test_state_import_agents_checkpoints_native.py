"""R186 native checkpoint regressions; all earlier fixture/test bytes stay frozen."""
import asyncio
import json
import uuid
from contextlib import asynccontextmanager

import pytest

from fixtures.state_import_agents_native import agent_cluster, state_cluster
from test_state_import_agents_native import PROJECTS, ROSTER, setup, prepare, uid
from test_state_import_agents_rework_native import wipe_family, no_authority


async def later_quarantine(pair):
    project_run = await setup(pair)
    await pair["writer"].execute(
        "UPDATE public.cortex_projects SET metadata=metadata || jsonb_build_object('roster_policy',$1::jsonb) WHERE id=$2",
        json.dumps(ROSTER), PROJECTS[1],
    )
    engine, adapter, snapshot, binding, policy = await prepare(pair)
    assert str(PROJECTS[1]) not in policy["roster_profiles"]
    groups = adapter.agent_groups(snapshot.records, policy, snapshot.dependencies)
    assert groups[0].reason is None and len(groups[0].records) == 9
    assert groups[1].reason is not None and len(groups[1].records) == 1
    return project_run, engine, adapter, snapshot, binding, policy


async def quarantine_readback(pair, engine, snapshot, binding, project_run):
    expected = {r.source_reference: r.original_bytes for r in snapshot.records}
    assert await engine.read_inverse(pair["target"], binding.run_id) == expected
    rows = await pair["target"].fetch(
        "SELECT source_reference,original_bytes,outcome,reason FROM cortex_core.import_rows WHERE run_id=$1 ORDER BY ordinal",
        binding.run_id,
    )
    assert len(rows) == 10
    assert sum(row["outcome"] == "migrated" for row in rows) == 9
    bad = next(row for row in rows if row["source_reference"] == "public.agents:" + uid(12))
    assert bad["outcome"] == "quarantined" and bad["reason"]
    assert bytes(bad["original_bytes"]) == expected[bad["source_reference"]]
    assert await pair["target"].fetchval("SELECT count(*) FROM cortex_core.project_identities WHERE project_scope_id=$1", PROJECTS[1]) == 0
    assert await pair["target"].fetchval("SELECT count(*) FROM cortex_core.import_rows WHERE run_id=$1", project_run) == 12
    await no_authority(pair)


@pytest.mark.parametrize("batch", ["default", "all"])
def test_ocr001_later_nonempty_quarantine_survives_batching(agent_cluster, batch):
    async def run():
        async with agent_cluster() as pair:
            project_run, e, a, s, b, p = await later_quarantine(pair)
            kwargs = {} if batch == "default" else {"batch_size": 256}
            result = await e.import_agents(pair["target"], s, b, p, **kwargs)
            assert result["functional_pass"] is False
            assert result["counts"] == {"migrated": 9, "quarantined": 1}
            await quarantine_readback(pair, e, s, b, project_run)
            assert await e.import_agents(pair["target"], s, b, p, **kwargs) == result
    asyncio.run(run())


def test_ocr001_partial_prefix_readback_then_resume_preserves_later_quarantine(agent_cluster):
    async def run():
        async with agent_cluster() as pair:
            project_run, e, a, s, b, p = await later_quarantine(pair)
            def crash(stage, checkpoint):
                if stage == "after_commit" and checkpoint == 1:
                    raise RuntimeError("R186 checkpoint interruption")
            with pytest.raises(RuntimeError, match="R186 checkpoint"):
                await e.import_agents(pair["target"], s, b, p, fault=crash)
            prefix = {r.source_reference: r.original_bytes for r in a.agent_groups(s.records, p, s.dependencies)[0].records}
            assert len(prefix) == 9
            assert await e.read_inverse(pair["target"], b.run_id) == prefix
            assert await pair["target"].fetchval("SELECT max(checkpoint) FROM cortex_core.import_runs WHERE run_id=$1", b.run_id) == 1
            result = await e.import_agents(pair["target"], s, b, p)
            assert result["functional_pass"] is False and result["counts"] == {"migrated": 9, "quarantined": 1}
            await quarantine_readback(pair, e, s, b, project_run)
    asyncio.run(run())


async def late_contenders(pair):
    project_run = await setup(pair)
    row = await pair["writer"].fetchval("SELECT to_jsonb(t)::text FROM public.agents t WHERE id=$1", uuid.UUID(uid(12)))
    await wipe_family(pair)
    await pair["writer"].execute("UPDATE public.cortex_projects SET metadata=metadata - 'roster_policy'")
    await pair["writer"].execute("INSERT INTO public.agents SELECT * FROM jsonb_populate_record(NULL::public.agents,$1::jsonb)", row)
    await pair["writer"].execute("UPDATE public.agents SET name='late-contest'")
    e, a, first_snapshot, first_binding, first_policy = await prepare(pair)
    groups = a.agent_groups(first_snapshot.records, first_policy, first_snapshot.dependencies)
    assert len(groups) == 3 and groups[0].records == ()
    assert json.loads(groups[0].projection_json)["items"] == []
    assert json.loads(groups[0].projection_json)["roster"] is None
    assert len(groups[1].records) == 1
    await pair["writer"].execute("UPDATE public.agents SET id=$1", uuid.UUID(uid(999)))
    e, a, second_snapshot, second_binding, second_policy = await prepare(pair)
    assert first_binding.run_id != second_binding.run_id
    assert first_snapshot.records[0].source_reference != second_snapshot.records[0].source_reference
    return project_run, e, a, (first_snapshot, first_binding, first_policy), (second_snapshot, second_binding, second_policy)


class PauseAfterFirstCommit:
    """Pause after a real successful checkpoint COMMIT, never inside a SQL stub."""
    def __init__(self, connection):
        self.connection = connection
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.commits = 0

    def __getattr__(self, name):
        return getattr(self.connection, name)

    @asynccontextmanager
    async def transaction(self, *args, **kwargs):
        outermost = not self.connection.is_in_transaction()
        async with self.connection.transaction(*args, **kwargs):
            yield
        if outermost:
            self.commits += 1
            if self.commits == 1:
                self.entered.set()
                await asyncio.wait_for(self.release.wait(), 20)


async def ledger_count(target, run_id):
    return (
        await target.fetchval("SELECT count(*) FROM cortex_core.import_runs WHERE run_id=$1", run_id),
        await target.fetchval("SELECT count(*) FROM cortex_core.import_rows WHERE run_id=$1", run_id),
    )


async def no_advisory_locks(target):
    assert await target.fetchval("SELECT count(*) FROM pg_locks WHERE pid=pg_backend_pid() AND locktype='advisory'") == 0


def test_ocr002_later_name_race_has_zero_losing_events(agent_cluster):
    async def run():
        async with agent_cluster() as pair:
            project_run, e, a, first_input, second_input = await late_contenders(pair)
            other = await pair["connect"](pair["target_name"], "cortex_v2_migrator")
            first, second = PauseAfterFirstCommit(pair["target"]), PauseAfterFirstCommit(other)
            tasks = []
            try:
                for connection in (pair["target"], other):
                    await connection.execute("SET default_transaction_isolation='repeatable read'")
                pid = await other.fetchval("SELECT pg_backend_pid()")
                tasks.append(asyncio.create_task(e.import_agents(first, *first_input)))
                await asyncio.wait_for(first.entered.wait(), 15)
                assert await ledger_count(pair["target"], first_input[1].run_id) == (2, 0)
                tasks.append(asyncio.create_task(e.import_agents(second, *second_input)))
                async def observe():
                    while not second.entered.is_set():
                        if await pair["writer"].fetchval("SELECT wait_event_type='Lock' FROM pg_stat_activity WHERE pid=$1", pid):
                            return "native-lock-wait"
                        if tasks[1].done():
                            return "early-refusal"
                        await asyncio.sleep(0.02)
                    return "second-empty-checkpoint-committed"
                observation = await asyncio.wait_for(observe(), 15)
                assert observation in ("native-lock-wait", "second-empty-checkpoint-committed")
                first.release.set()
                winner = await asyncio.wait_for(tasks[0], 15)
                second.release.set()
                results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 20)
                assert winner["functional_pass"] is True and winner["counts"] == {"migrated": 1}
                assert isinstance(results[1], e.ImportRefused) and "collision" in str(results[1])
                assert await ledger_count(other, second_input[1].run_id) == (0, 0)
                rows = await pair["target"].fetch("SELECT identity_name FROM cortex_core.project_identities WHERE project_scope_id=$1", PROJECTS[1])
                assert [row["identity_name"] for row in rows] == ["late-contest"]
                assert await e.read_inverse(pair["target"], first_input[1].run_id) == {r.source_reference: r.original_bytes for r in first_input[0].records}
                assert await pair["target"].fetchval("SELECT count(*) FROM cortex_core.import_rows WHERE run_id=$1", project_run) == 12
                await no_authority(pair)
                await no_advisory_locks(pair["target"])
                await no_advisory_locks(other)
            finally:
                first.release.set(); second.release.set()
                if tasks:
                    await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 20)
                await other.close()
    asyncio.run(run())


def test_ocr002_interrupted_empty_prefix_reserves_its_later_name(agent_cluster):
    async def run():
        async with agent_cluster() as pair:
            project_run, e, a, first_input, second_input = await late_contenders(pair)
            def crash(stage, checkpoint):
                if stage == "after_commit" and checkpoint == 1:
                    raise RuntimeError("R186 empty-prefix interruption")
            with pytest.raises(RuntimeError, match="R186 empty-prefix"):
                await e.import_agents(pair["target"], *first_input, fault=crash)
            assert await ledger_count(pair["target"], first_input[1].run_id) == (2, 0)
            await no_advisory_locks(pair["target"])
            with pytest.raises(e.ImportRefused, match="collision"):
                await e.import_agents(pair["target"], *second_input, batch_size=256)
            assert await ledger_count(pair["target"], second_input[1].run_id) == (0, 0)
            assert await pair["target"].fetchval("SELECT count(*) FROM cortex_core.project_identities") == 0
            resumed = await e.import_agents(pair["target"], *first_input)
            assert resumed["counts"] == {"migrated": 1} and resumed["functional_pass"] is True
            assert await e.read_inverse(pair["target"], first_input[1].run_id) == {r.source_reference: r.original_bytes for r in first_input[0].records}
            await no_authority(pair)
            await no_advisory_locks(pair["target"])
    asyncio.run(run())


@pytest.mark.parametrize("stage", ["normal", "before_commit", "after_commit", "cancelled"])
def test_run_session_lock_released_on_every_exit(agent_cluster, stage):
    async def run():
        async with agent_cluster() as pair:
            await setup(pair); e, a, s, b, p = await prepare(pair)
            if stage == "normal":
                assert (await e.import_agents(pair["target"], s, b, p))["functional_pass"] is True
            elif stage == "cancelled":
                paused = PauseAfterFirstCommit(pair["target"])
                task = asyncio.create_task(e.import_agents(paused, s, b, p))
                try:
                    await asyncio.wait_for(paused.entered.wait(), 15)
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
                finally:
                    paused.release.set()
                    if not task.done(): task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            else:
                def crash(point, checkpoint):
                    if point == stage and checkpoint == 1:
                        raise RuntimeError("R186 exit interruption")
                with pytest.raises(RuntimeError, match="R186 exit"):
                    await e.import_agents(pair["target"], s, b, p, fault=crash)
            await no_advisory_locks(pair["target"])
            await no_authority(pair)
    asyncio.run(run())
