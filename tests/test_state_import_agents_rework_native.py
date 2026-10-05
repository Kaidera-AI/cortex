"""R162 native regressions for the three sealed slice 2 author findings."""
import asyncio
import dataclasses
import uuid

import pytest

from fixtures.state_import_agents_native import AUTHORITY, agent_cluster, state_cluster
from test_state_import_agents_native import TABLES, PROJECTS, ROSTER, setup, prepare, effects, uid


async def wipe_family(pair):
    for table in TABLES:
        await pair["writer"].execute("DELETE FROM " + table)


async def no_authority(pair):
    for _, table in AUTHORITY:
        assert await pair["target"].fetchval("SELECT count(*) FROM " + table) == 0


def test_f1_empty_family_missing_roster_mapping_refuses_without_effects(agent_cluster):
    async def run():
        async with agent_cluster() as pair:
            project_run = await setup(pair)
            await wipe_family(pair)
            e, a, snapshot, binding, policy = await prepare(pair)
            assert len(snapshot.records) == 0 and len(snapshot.dependencies) == 12
            policy["roster_profiles"].clear()
            binding = dataclasses.replace(binding, policy_sha256=e.policy_digest(policy))
            before = await effects(pair, binding.run_id)
            with pytest.raises(e.ImportRefused):
                await e.import_agents(pair["target"], snapshot, binding, policy)
            assert await effects(pair, binding.run_id) == before == (0, 0, 0, 0)
            assert await pair["target"].fetchval("SELECT count(*) FROM cortex_core.import_rows WHERE run_id=$1", project_run) == 12
            await no_authority(pair)
    asyncio.run(run())


def test_f1_valid_empty_family_has_native_roster_and_inverse_drift_fence(agent_cluster):
    async def run():
        async with agent_cluster() as pair:
            project_run = await setup(pair)
            await wipe_family(pair)
            e, a, snapshot, binding, policy = await prepare(pair)
            result = await e.import_agents(pair["target"], snapshot, binding, policy)
            assert result["functional_pass"] is True and result["counts"] == {}
            assert await e.read_inverse(pair["target"], binding.run_id) == {}
            view = await a.read_native_roster(pair["target"], binding.run_id)
            alpha = next(v for v in view if v["source_project_id"] == str(PROJECTS[0]))
            assert alpha["roster_policy"] == ROSTER and alpha["records"] == []
            assert await pair["target"].fetchval("SELECT count(*) FROM cortex_core.project_profiles") == 1
            assert await pair["target"].fetchval("SELECT count(*) FROM cortex_core.import_rows WHERE run_id=$1", project_run) == 12
            await pair["target"].execute("UPDATE cortex_core.project_profiles SET agent_name='drift' WHERE profile_id=$1", uuid.UUID(uid(200)))
            with pytest.raises(e.ImportRefused):
                await e.read_inverse(pair["target"], binding.run_id)
            await no_authority(pair)
    asyncio.run(run())


@pytest.mark.parametrize("orphan", ["profile-only", "role-owner"])
def test_f2_orphan_public_profile_quarantines_exact_original_without_owner(agent_cluster, orphan):
    async def run():
        async with agent_cluster() as pair:
            await setup(pair)
            if orphan == "role-owner":
                await pair["writer"].execute("UPDATE public.agent_profiles SET agent_name='missing-agent' WHERE id=$1", uuid.UUID(uid(14)))
            e, a, snapshot, binding, policy = await prepare(pair)
            reference = "public.agent_profiles:" + uid(13 if orphan == "profile-only" else 14)
            if orphan == "profile-only":
                del policy["record_mappings"][reference]["identity_id"]
            binding = dataclasses.replace(binding, policy_sha256=e.policy_digest(policy))
            result = await e.import_agents(pair["target"], snapshot, binding, policy)
            assert result["functional_pass"] is False
            assert sum(result["counts"].values()) == len(snapshot.records) == 10
            row = await pair["target"].fetchrow("SELECT outcome,original_bytes FROM cortex_core.import_rows WHERE run_id=$1 AND source_reference=$2", binding.run_id, reference)
            assert row["outcome"] == "quarantined"
            assert bytes(row["original_bytes"]) == next(r.original_bytes for r in snapshot.records if r.source_reference == reference)
            assert await e.read_inverse(pair["target"], binding.run_id) == {r.source_reference: r.original_bytes for r in snapshot.records}
            assert await pair["target"].fetchval("SELECT count(*) FROM cortex_core.project_profiles WHERE original_profile_id=$1", reference) == 0
            owner = policy["record_mappings"][reference]["agent_name"]
            assert await pair["target"].fetchval("SELECT count(*) FROM cortex_core.project_identities WHERE project_scope_id=$1 AND identity_name=$2", PROJECTS[0], owner) == 0
            await no_authority(pair)
    asyncio.run(run())


class HoldPreflight:
    def __init__(self, target, release):
        self.target = target
        self.release = release
        self.entered = asyncio.Event()
        self.observations = []

    def __getattr__(self, name):
        return getattr(self.target, name)

    async def fetchval(self, sql, *args, **kwargs):
        value = await self.target.fetchval(sql, *args, **kwargs)
        if "SELECT EXISTS(SELECT 1 FROM cortex_core.project_identities" in sql:
            self.observations.append((value, str(args[0]), args[3]))
            self.entered.set()
            await asyncio.wait_for(self.release.wait(), 15)
        return value


def test_f3_distinct_run_race_has_one_native_name_and_typed_loser(agent_cluster):
    async def run():
        async with agent_cluster() as pair:
            await setup(pair)
            row = await pair["writer"].fetchval("SELECT to_jsonb(t)::text FROM public.agents t WHERE id=$1", uuid.UUID(uid(10)))
            await wipe_family(pair)
            await pair["writer"].execute("UPDATE public.cortex_projects SET metadata=metadata - 'roster_policy'")
            await pair["writer"].execute("INSERT INTO public.agents SELECT * FROM jsonb_populate_record(NULL::public.agents,$1::jsonb)", row)
            e, a, first_snapshot, first_binding, first_policy = await prepare(pair)
            await pair["writer"].execute("UPDATE public.agents SET id=$1", uuid.UUID(uid(999)))
            e, a, second_snapshot, second_binding, second_policy = await prepare(pair)
            assert first_binding.run_id != second_binding.run_id
            assert first_snapshot.records[0].source_reference != second_snapshot.records[0].source_reference
            other = await pair["connect"](pair["target_name"], "cortex_v2_migrator")
            release = asyncio.Event()
            first = HoldPreflight(pair["target"], release)
            second = HoldPreflight(other, release)
            tasks = []
            try:
                for connection in (pair["target"], other):
                    await connection.execute("SET default_transaction_isolation='repeatable read'")
                other_pid = await other.fetchval("SELECT pg_backend_pid()")
                tasks.append(asyncio.create_task(e.import_agents(first, first_snapshot, first_binding, first_policy)))
                await asyncio.wait_for(first.entered.wait(), 15)
                assert first.observations == [(False, uid(10), "worker")]
                tasks.append(asyncio.create_task(e.import_agents(second, second_snapshot, second_binding, second_policy)))
                async def observe_second():
                    while not second.entered.is_set():
                        waiting = await pair["writer"].fetchval("SELECT wait_event_type='Lock' FROM pg_stat_activity WHERE pid=$1", other_pid)
                        if waiting:
                            return "native-lock-wait"
                        await asyncio.sleep(0.02)
                    return "native-preflight"
                observation = await asyncio.wait_for(observe_second(), 15)
                assert observation in ("native-lock-wait", "native-preflight")
                release.set()
                results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 20)
                successes = [r for r in results if isinstance(r, dict)]
                refused = [r for r in results if isinstance(r, e.ImportRefused)]
                rows = await pair["target"].fetch("SELECT identity_id,identity_name FROM cortex_core.project_identities WHERE project_scope_id=$1", PROJECTS[0])
                assert len(rows) == 1 and rows[0]["identity_name"] == "worker"
                assert len(successes) == len(refused) == 1
                assert successes[0]["functional_pass"] is True and successes[0]["counts"] == {"migrated": 1}
                assert "collision" in str(refused[0])
                for binding, result in zip((first_binding, second_binding), results):
                    if isinstance(result, e.ImportRefused):
                        assert await pair["target"].fetchval("SELECT count(*) FROM cortex_core.import_runs WHERE run_id=$1", binding.run_id) == 0
                        assert await pair["target"].fetchval("SELECT count(*) FROM cortex_core.import_rows WHERE run_id=$1", binding.run_id) == 0
                    else:
                        assert len(await e.read_inverse(pair["target"], binding.run_id)) == 1
                await no_authority(pair)
            finally:
                release.set()
                if tasks:
                    await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 20)
                await other.close()
    asyncio.run(run())
