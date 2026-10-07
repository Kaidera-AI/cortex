"""Valid owner succession and preserved compatibility label."""
import uuid
import pytest
from cortex_v2 import agent_boot
from fixtures.agent_boot_native_r425 import boot_database
from fixtures.state_import_native import state_cluster
from fixtures.agent_boot_streams_r426 import append,app_reader,caller
from test_agent_boot_reader_sql_r426 import prepared
from test_agent_boot_metadata_sql_r425 import native

@native
async def test_native_reader_preserves_ratified_compatibility_surface_label(boot_database):
    async with boot_database() as f:
        ctx=await prepared(f)
        async with app_reader(f) as app:
            response=await agent_boot.read_boot(app,ctx,'public-agent',budget=1200,query=None,full=False)
            assert response['surface_version']=='kaidera-os-e009-clean-baseline-2026-06-24'

@native
@pytest.mark.parametrize('successor_publishes',(False,True))
async def test_owner_succession_never_blesses_predecessor_publication(successor_publishes,boot_database):
    async with boot_database() as f:
        ctx=await prepared(f);db=f['db'];owner=uuid.uuid4();publication=uuid.uuid4()
        await db.execute('UPDATE cortex_auth.installation_owners SET revoked_at=now() WHERE installation_id=$1 AND principal_id=$2',f['installation'],f['principal'])
        await db.execute("INSERT INTO cortex_auth.principals(principal_id,installation_id,principal_name,status) VALUES($1,$2,'public-successor','active')",owner,f['installation'])
        await db.execute('INSERT INTO cortex_auth.installation_owners(installation_id,principal_id) VALUES($1,$2)',f['installation'],owner)
        await db.execute('INSERT INTO cortex_auth.scope_grants(principal_id,scope_id,can_read,can_write,can_publish) VALUES($1,$2,true,true,true)',owner,f['catalogue'])
        await caller(f,owner)
        if successor_publishes:await append(f,'publication',publication_id=publication,published_by_principal=owner)
        async with app_reader(f) as app:
            snapshot=await agent_boot.load_boot_snapshot(app,ctx)
            resolved=agent_boot.resolve_boot_bindings(snapshot['context'],agent_rows=snapshot['agent_rows'],entry_rows=snapshot['entry_rows'],publication_rows=snapshot['publication_rows'])
            assert {r['publication_id'] for r in resolved['publications']}==({publication} if successor_publishes else set())
            assert all(r['publication_id']!=f['publication'] for r in resolved['publications'])
