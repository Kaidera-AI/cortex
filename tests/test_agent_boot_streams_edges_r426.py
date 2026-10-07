"""Adversarial append edges: actual subject eligibility and safe withdrawal."""
import asyncpg
import pytest
from fixtures.agent_boot_native_r425 import boot_database
from fixtures.state_import_native import state_cluster
from fixtures.agent_boot_streams_r426 import append,seed
from test_agent_boot_metadata_sql_r425 import native

@native
@pytest.mark.parametrize('damage',('actor','membership','principal'))
async def test_entry_agent_subject_must_still_be_registered_and_active(boot_database,damage):
    async with boot_database() as f:
        await seed(f);db=f['db']
        if damage=='actor':await db.execute("UPDATE cortex_auth.actors SET status='retired' WHERE actor_id=$1",f['actor'])
        elif damage=='membership':await db.execute("UPDATE cortex_auth.memberships SET status='retired' WHERE scope_id=$1 AND actor_id=$2",f['scope'],f['actor'])
        elif damage=='principal':await db.execute("UPDATE cortex_auth.principals SET status='revoked' WHERE principal_id=$1",f['member'])
        with pytest.raises(asyncpg.CheckViolationError):await append(f,'entry')
        assert await db.fetchval('SELECT count(*) FROM cortex_context.boot_entry_binding_revisions')==0

@native
async def test_current_owner_can_withdraw_exact_publication_after_curator_revocation(boot_database):
    async with boot_database() as f:
        await seed(f);await append(f,'publication')
        await f['db'].execute('UPDATE cortex_auth.scope_grants SET can_publish=false WHERE principal_id=$1 AND scope_id=$2',f['principal'],f['catalogue'])
        retired=await append(f,'publication',revision=2,state='retired')
        assert retired['state']=='retired' and retired['revision']==2

@native
async def test_project_manager_can_withdraw_exact_entry_after_publication_withdrawal(boot_database):
    async with boot_database() as f:
        await seed(f);await append(f,'publication')
        change={'entry_scope_id':f['catalogue'],'skill_id':f['global_skill']['skill_id']}
        await append(f,'entry',**change)
        await append(f,'publication',revision=2,state='retired')
        retired=await append(f,'entry',revision=2,state='retired',**change)
        assert retired['state']=='retired' and retired['revision']==2

@native
@pytest.mark.parametrize('stream',('agent','entry','publication'))
async def test_retirement_can_only_withdraw_exact_previous_binding(boot_database,stream):
    from test_agent_boot_metadata_sql_r425 import insert,manifest
    async with boot_database() as f:
        await seed(f);await append(f,stream)
        if stream=='agent':
            other=await insert(f,'persona',manifest('persona'),audience='agent_boot')
            change={'persona_id':other['persona_id']}
        elif stream=='entry':
            other=await insert(f,'skill',manifest('skill'));change={'skill_id':other['skill_id']}
        else:
            value=manifest('skill');value['scope']='global'
            other=await insert({**f,'scope':f['catalogue']},'skill',value);change={'entry_id':other['skill_id']}
        with pytest.raises(asyncpg.CheckViolationError):await append(f,stream,revision=2,state='retired',**change)
        assert await f['db'].fetchval('SELECT max(revision) FROM cortex_context.'+{'agent':'boot_agent_binding_revisions','entry':'boot_entry_binding_revisions','publication':'boot_catalogue_publication_revisions'}[stream])==1
