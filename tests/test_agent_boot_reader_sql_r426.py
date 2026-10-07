"""Actual own-member SQL reader, no mocked repository or authentication facts."""
import json
import uuid
import pytest
from cortex_v2 import agent_boot
from cortex_v2.store import ApiProblem,Principal,Scope,ScopeContext
from fixtures.agent_boot_native_r425 import boot_database
from fixtures.state_import_native import state_cluster
from fixtures.agent_boot_streams_r426 import append,app_reader,seed
from test_agent_boot_metadata_sql_r425 import manifest,native

async def prepared(f):
    await seed(f)
    value=manifest('persona');value['name']='public-agent'
    f['persona']=await f['db'].fetchrow("""INSERT INTO cortex_context.persona_revisions
      (scope_id,persona_id,revision,body,created_by_principal,boot_manifest,audience,template_version,payload)
      VALUES($1,$2,1,'PUBLIC identity',$3,$4::jsonb,'agent_boot','cortex.persona.v2','{}') RETURNING *""",f['scope'],uuid.uuid4(),f['principal'],json.dumps(value))
    await append(f,'agent');await append(f,'entry');await append(f,'publication')
    scope=Scope('PUBLIC-project',f['scope'],'project',True,False,False)
    return ScopeContext(Principal(f['member'],f['installation']),scope,(scope,))

@native
async def test_native_reader_uses_own_actor_registered_root_and_exact_revisions(boot_database):
    assert callable(getattr(agent_boot,'load_boot_snapshot',None)), 'Own-member SQL loader missing'
    async with boot_database() as f:
        ctx=await prepared(f)
        async with app_reader(f) as app:
            snapshot=await agent_boot.load_boot_snapshot(app,ctx)
            assert snapshot['context']['actor_id']==f['actor']
            assert snapshot['context']['actor_name']=='public-agent'
            assert snapshot['context']['workspace_root']=='/fixture/public-boot'
            assert snapshot['context']['functional_roles']==['PUBLIC-role']
            assert snapshot['operational']['projection_available'] is False
            response=await agent_boot.read_boot(app,ctx,'public-agent',budget=1200,query=None,full=False,agent_label='public-agent')
            assert response['persona']['identity_text']=='PUBLIC identity'
            assert response['persona']['metadata']['boot_validation']['actor_id']==str(f['actor'])
            assert response['persona']['metadata']['boot_context']['availability']['operational']['state']=='unavailable'
            assert len(response['persona']['skills'])==1
        assert await f['db'].fetchval('SELECT count(*) FROM cortex_auth.scope_grants WHERE principal_id=$1 AND scope_id=$2',f['member'],f['catalogue'])==0

@native
@pytest.mark.parametrize('damage',('actor','membership','principal','grant','root','role','retired-head','wrong-guc'))
async def test_native_reader_refuses_revoked_missing_or_foreign_registered_facts(boot_database,damage):
    assert callable(getattr(agent_boot,'load_boot_snapshot',None)), 'Own-member SQL loader missing'
    async with boot_database() as f:
        ctx=await prepared(f);db=f['db']
        if damage=='actor':await db.execute("UPDATE cortex_auth.actors SET status='retired' WHERE actor_id=$1",f['actor'])
        elif damage=='membership':await db.execute("UPDATE cortex_auth.memberships SET status='retired' WHERE scope_id=$1 AND actor_id=$2",f['scope'],f['actor'])
        elif damage=='principal':await db.execute("UPDATE cortex_auth.principals SET status='revoked' WHERE principal_id=$1",f['member'])
        elif damage=='grant':await db.execute('UPDATE cortex_auth.scope_grants SET revoked_at=now() WHERE principal_id=$1 AND scope_id=$2',f['member'],f['scope'])
        elif damage=='root':await db.execute("UPDATE cortex_core.project_registry SET repo_root='relative-root' WHERE project_scope_id=$1",f['scope'])
        elif damage=='role':await db.execute("UPDATE cortex_core.project_identities SET original_record='{}' WHERE identity_id=$1",f['identity'])
        elif damage=='retired-head':await append(f,'agent',revision=2,state='retired')
        async with app_reader(f) as app:
            if damage=='wrong-guc':await app.execute("SELECT set_config('cortex.principal_id',$1,false)",str(f['principal']))
            with pytest.raises((ApiProblem,ValueError)):
                await agent_boot.load_boot_snapshot(app,ctx)

@native
@pytest.mark.parametrize('withdraw',('publication','owner','curator'))
async def test_snapshot_does_not_resurrect_a_withdrawn_or_ineligible_publication(boot_database,withdraw):
    assert callable(getattr(agent_boot,'load_boot_snapshot',None)), 'Own-member SQL loader missing'
    async with boot_database() as f:
        ctx=await prepared(f)
        if withdraw=='publication':await append(f,'publication',revision=2,state='retired')
        elif withdraw=='owner':await f['db'].execute('UPDATE cortex_auth.installation_owners SET revoked_at=now() WHERE installation_id=$1 AND principal_id=$2',f['installation'],f['principal'])
        else:await f['db'].execute('UPDATE cortex_auth.scope_grants SET can_publish=false WHERE principal_id=$1 AND scope_id=$2',f['principal'],f['catalogue'])
        async with app_reader(f) as app:
            snapshot=await agent_boot.load_boot_snapshot(app,ctx)
            assert not snapshot['context']['eligible_publication_heads']
            resolved=agent_boot.resolve_boot_bindings(snapshot['context'],agent_rows=snapshot['agent_rows'],entry_rows=snapshot['entry_rows'],publication_rows=snapshot['publication_rows'])
            assert resolved['publications']==[]
