"""Separate frozen R148 author-verifier regressions; original RED files stay intact."""
import asyncio
import json
from decimal import Decimal

from fixtures.state_import_native import state_cluster
from test_state_import_projects_native import PROJECTS, inputs, seed


def test_concurrent_replay_owns_isolation_boundary(state_cluster):
    async def run():
        async with state_cluster() as pair:
            await seed(pair)
            engine, snapshot, binding, policy = await inputs(pair)
            other = await pair['connect'](pair['target_name'], 'cortex_v2_migrator')
            try:
                for connection in (pair['target'], other):
                    await connection.execute("SET default_transaction_isolation='repeatable read'")
                results = await asyncio.gather(
                    *(engine.import_projects(conn, snapshot, binding, policy, batch_size=1)
                      for conn in (pair['target'], other)), return_exceptions=True)
            finally:
                await other.close()
            assert all(isinstance(result, dict) for result in results), [type(r).__name__ for r in results]
            assert results[0] == results[1] and results[0]['counts'] == {'migrated': 12}
            assert await pair['target'].fetchval('SELECT count(*) FROM cortex_core.import_rows') == 12
    asyncio.run(run())


def test_precise_native_root_numbers_are_lossless(state_cluster):
    async def run():
        async with state_cluster() as pair:
            await seed(pair)
            number = '0.123456789012345678901234567890123456789'
            await pair['writer'].execute("""UPDATE public.cortex_projects
                SET metadata=jsonb_set(metadata,'{roots,0,decimal}',$1::jsonb) WHERE id=$2""", number, PROJECTS[0])
            await pair['writer'].execute("""UPDATE public.cortex_project_paths
                SET metadata=jsonb_set(metadata,'{decimal}',$1::jsonb) WHERE project_key='alpha' AND root_path='/fixture/alpha/z'""", number)
            engine, snapshot, binding, policy = await inputs(pair)
            receipt = await engine.import_projects(pair['target'], snapshot, binding, policy)
            assert receipt['functional_pass'] and receipt['counts'] == {'migrated': 12}
            actual = await pair['target'].fetchval("SELECT (roots->2->'decimal')::text FROM cortex_core.project_registry WHERE project_scope_id=$1", PROJECTS[0])
            assert actual == number, 'Native roots rounded original PostgreSQL numeric metadata'
            inverse = await engine.read_inverse(pair['target'], binding.run_id)
            original = json.loads(inverse[f'public.cortex_projects:{PROJECTS[0]}'], parse_float=Decimal)
            assert original['metadata']['roots'][0]['decimal'] == Decimal(number)
            before = await pair['target'].fetchval('SELECT count(*) FROM cortex_core.import_runs')
            assert await engine.import_projects(pair['target'], snapshot, binding, policy) == receipt
            assert await pair['target'].fetchval('SELECT count(*) FROM cortex_core.import_runs') == before
    asyncio.run(run())
