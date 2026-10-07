"""R151 preflight: late fixture operations must work before missing-surface RED."""
import asyncio
import uuid
import pytest
from fixtures.state_import_agents_native import agent_cluster, state_cluster
from test_state_import_agents_native import setup, effects, PROJECTS
from test_state_import_projects_native import inputs as project_inputs

def test_effects_helper_returns_materialized_native_counts(agent_cluster):
    async def run():
        async with agent_cluster() as pair:
            await setup(pair)
            counts=await effects(pair,uuid.uuid4())
            assert type(counts) is tuple and counts==(0,0,0,0)
    asyncio.run(run())

def test_legal_root_drift_reaches_project_readback(agent_cluster):
    async def run():
        async with agent_cluster() as pair:
            project_run=await setup(pair)
            engine,_,_,_=await project_inputs(pair)
            await pair["target"].execute("UPDATE cortex_core.project_registry SET roots=jsonb_set(roots,'{0,path}','\"/fixture/drift\"'::jsonb) WHERE project_scope_id=$1",PROJECTS[0])
            assert await pair["target"].fetchval("SELECT roots->0->>'path' FROM cortex_core.project_registry WHERE project_scope_id=$1",PROJECTS[0])=="/fixture/drift"
            with pytest.raises(engine.ImportRefused,match="roots"):
                await engine.read_inverse(pair["target"],project_run)
    asyncio.run(run())
