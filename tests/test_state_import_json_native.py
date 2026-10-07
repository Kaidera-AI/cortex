"""Frozen R148 regressions for policy-number stability and typed native JSON drift."""
import asyncio
import dataclasses

import pytest

from fixtures.state_import_native import state_cluster
from test_state_import_projects_native import PROJECTS, inputs, seed


def test_numeric_policy_hash_survives_freeze_and_replay(state_cluster):
    async def run():
        async with state_cluster() as pair:
            await seed(pair)
            engine, snapshot, binding, policy = await inputs(pair)
            policy = {**policy, 'approved_number': 1e-7}
            binding = dataclasses.replace(binding, policy_sha256=engine.policy_digest(policy))
            receipt = await engine.import_projects(pair['target'], snapshot, binding, policy)
            assert receipt['functional_pass'] and receipt['counts'] == {'migrated': 12}
            events = await pair['target'].fetch('SELECT to_jsonb(t)::text FROM cortex_core.import_runs t ORDER BY event_seq')
            assert await engine.import_projects(pair['target'], snapshot, binding, policy) == receipt
            assert await pair['target'].fetch('SELECT to_jsonb(t)::text FROM cortex_core.import_runs t ORDER BY event_seq') == events
    asyncio.run(run())


@pytest.mark.parametrize('boolean,number', [('true', '1'), ('false', '0')])
def test_native_root_boolean_number_drift_refuses(state_cluster, boolean, number):
    async def run():
        async with state_cluster() as pair:
            await seed(pair)
            await pair['writer'].execute("""UPDATE public.cortex_projects
                SET metadata=jsonb_set(metadata,'{roots,0,covered}',$1::jsonb) WHERE id=$2""", boolean, PROJECTS[0])
            await pair['writer'].execute("""UPDATE public.cortex_project_paths
                SET metadata=jsonb_set(metadata,'{covered}',$1::jsonb) WHERE project_key='alpha' AND root_path='/fixture/alpha/z'""", boolean)
            engine, snapshot, binding, policy = await inputs(pair)
            await engine.import_projects(pair['target'], snapshot, binding, policy)
            rows = await pair['target'].fetch('SELECT to_jsonb(t)::text FROM cortex_core.import_rows t ORDER BY ordinal')
            await pair['target'].execute("""UPDATE cortex_core.project_registry
                SET roots=jsonb_set(roots,'{2,covered}',$1::jsonb) WHERE project_scope_id=$2""", number, PROJECTS[0])
            with pytest.raises(engine.ImportRefused):
                await engine.read_inverse(pair['target'], binding.run_id)
            with pytest.raises(engine.ImportRefused):
                await engine.import_projects(pair['target'], snapshot, binding, policy)
            assert await pair['target'].fetch('SELECT to_jsonb(t)::text FROM cortex_core.import_rows t ORDER BY ordinal') == rows
    asyncio.run(run())
