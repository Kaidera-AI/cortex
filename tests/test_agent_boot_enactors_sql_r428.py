"""Actual transactional typed enactors: authority, optimistic heads, receipts."""
import asyncio
import importlib
import importlib.util
import uuid
import pytest
from cortex_v2.agent_boot_models import BootAgentBindRequest,BootEntryBindRequest,BootPublicationRequest
from cortex_v2.store import ApiProblem,Principal,Scope,ScopeContext
from fixtures.agent_boot_native_r425 import boot_database
from fixtures.state_import_native import state_cluster
from fixtures.agent_boot_streams_r426 import TABLES,app_reader
from test_agent_boot_reader_sql_r426 import prepared
from test_agent_boot_metadata_sql_r425 import native

STREAMS=('agent','entry','publication')
HANDLERS=dict(agent='enact_boot_agent',entry='enact_boot_entry',publication='publish_boot_catalogue')

def module():
    name='cortex_v2.interface.agent_boot_enact'
    assert importlib.util.find_spec(name) is not None,'Typed boot enactors missing'
    return importlib.import_module(name)

def context(f, principal=None):
    scope=Scope('PUBLIC-project',f['scope'],'project',True,True,False)
    return ScopeContext(Principal(principal or f['principal'],f['installation']),scope,(scope,))

def payload(f,stream):
    common=dict(expected_revision=1,state='active',source_reference='PUBLIC typed enactor')
    if stream=='agent':return BootAgentBindRequest(**common,actor_id=f['actor'],identity_id=f['identity'],persona_id=f['persona']['persona_id'],persona_revision=1,functional_roles=['PUBLIC-role'])
    if stream=='entry':return BootEntryBindRequest(**common,binding_id=f['binding'],subject_kind='agent',actor_id=f['actor'],entry_kind='skill',entry_scope_id=f['scope'],skill_id=f['skill']['skill_id'],bound_revision=1,priority=0)
    return BootPublicationRequest(**common,publication_id=f['publication'],catalogue_scope_id=f['catalogue'],entry_kind='skill',entry_id=f['global_skill']['skill_id'],entry_revision=1)

async def count(f,stream):
    return await f['db'].fetchval('SELECT count(*) FROM cortex_context.'+TABLES[stream])

async def receipts(f):
    return await f['db'].fetchval('SELECT count(*) FROM cortex_core.command_receipts')

async def call(app,ctx,key,stream,request):
    return await getattr(module(),HANDLERS[stream])(app,ctx,key,request,{})

@native
@pytest.mark.parametrize('stream',STREAMS)
async def test_enact_and_exact_replay_have_one_append_and_one_committed_receipt(boot_database,stream):
    async with boot_database() as f:
        await prepared(f);request=payload(f,stream);ctx=context(f)
        async with app_reader(f,f['principal']) as app:
            first=await call(app,ctx,'PUBLIC-first',stream,request)
            replay=await call(app,ctx,'PUBLIC-first',stream,request)
            assert first[0]==201 and first[2] is False
            assert replay[0]==200 and replay[2] is True and replay[1]==first[1]
            assert first[1]['state']=='committed' and first[1]['revision']==2
            assert first[1]['schema_version']=='cortex.boot-enactment-receipt.v1'
        assert await count(f,stream)==2 and await receipts(f)==1

@native
@pytest.mark.parametrize('stream',STREAMS)
async def test_changed_payload_reuse_is_typed_conflict_without_append(boot_database,stream):
    async with boot_database() as f:
        await prepared(f);request=payload(f,stream);ctx=context(f)
        async with app_reader(f,f['principal']) as app:
            await call(app,ctx,'PUBLIC-first',stream,request)
            with pytest.raises(ApiProblem) as error:await call(app,ctx,'PUBLIC-first',stream,request.model_copy(update={'source_reference':'PUBLIC changed'}))
            assert error.value.status==409 and error.value.code=='idempotency_key_reused'
        assert await count(f,stream)==2 and await receipts(f)==1

@native
@pytest.mark.parametrize('stream',STREAMS)
async def test_stale_expected_head_has_no_row_or_receipt_effect(boot_database,stream):
    async with boot_database() as f:
        await prepared(f);request=payload(f,stream);ctx=context(f)
        async with app_reader(f,f['principal']) as app:
            await call(app,ctx,'PUBLIC-first',stream,request)
            with pytest.raises(ApiProblem) as error:await call(app,ctx,'PUBLIC-stale',stream,request)
            assert error.value.status==409 and error.value.code=='boot_revision_conflict'
        assert await count(f,stream)==2 and await receipts(f)==1

@native
@pytest.mark.parametrize('stream',STREAMS)
async def test_member_write_grant_is_not_enactment_authority(boot_database,stream):
    async with boot_database() as f:
        await prepared(f);request=payload(f,stream);ctx=context(f,f['member'])
        async with app_reader(f) as app:
            with pytest.raises(ApiProblem) as error:await call(app,ctx,'PUBLIC-denied',stream,request)
            assert error.value.status==403
        assert await count(f,stream)==1 and await receipts(f)==0

@native
@pytest.mark.parametrize('stream',STREAMS)
async def test_receipt_failure_rolls_back_the_actual_append(boot_database,stream,monkeypatch):
    async with boot_database() as f:
        await prepared(f);request=payload(f,stream);ctx=context(f)
        async def refuse(*args,**kwargs):raise RuntimeError('PUBLIC receipt failure')
        async with app_reader(f,f['principal']) as app:
            monkeypatch.setattr(module(),'commit_receipt',refuse)
            with pytest.raises(RuntimeError,match='PUBLIC receipt failure'):await call(app,ctx,'PUBLIC-rollback',stream,request)
        assert await count(f,stream)==1 and await receipts(f)==0

@native
@pytest.mark.parametrize('stream',STREAMS)
async def test_two_actual_connections_same_expected_revision_commit_once(boot_database,stream):
    async with boot_database() as f:
        await prepared(f);request=payload(f,stream);ctx=context(f)
        async def enact(app,key):
            try:return await call(app,ctx,key,stream,request)
            except ApiProblem as error:return error
        async with app_reader(f,f['principal']) as left,app_reader(f,f['principal']) as right:
            results=await asyncio.gather(enact(left,'PUBLIC-left'),enact(right,'PUBLIC-right'))
        assert sum(isinstance(r,ApiProblem) and r.code=='boot_revision_conflict' for r in results)==1
        assert sum(isinstance(r,tuple) and r[0]==201 for r in results)==1
        assert await count(f,stream)==2 and await receipts(f)==1

@native
@pytest.mark.parametrize('stream',('entry','publication'))
async def test_generated_stream_identifier_is_preserved_by_receipt_replay(boot_database,stream):
    async with boot_database() as f:
        await prepared(f);ctx=context(f);field='binding_id' if stream=='entry' else 'publication_id'
        request=payload(f,stream).model_copy(update={field:None,'expected_revision':0})
        async with app_reader(f,f['principal']) as app:
            first=await call(app,ctx,'PUBLIC-generated',stream,request)
            replay=await call(app,ctx,'PUBLIC-generated',stream,request)
            assert first[1]==replay[1] and uuid.UUID(first[1]['stream_id'])
            assert first[1]['revision']==1
        assert await count(f,stream)==2 and await receipts(f)==1

@native
@pytest.mark.parametrize('stream',STREAMS)
async def test_revoked_owner_cannot_replay_a_committed_enactment(boot_database,stream):
    async with boot_database() as f:
        await prepared(f);request=payload(f,stream);ctx=context(f)
        async with app_reader(f,f['principal']) as app:
            await call(app,ctx,'PUBLIC-first',stream,request)
            await f['db'].execute('UPDATE cortex_auth.installation_owners SET revoked_at=now() WHERE installation_id=$1 AND principal_id=$2',f['installation'],f['principal'])
            with pytest.raises(ApiProblem) as error:await call(app,ctx,'PUBLIC-first',stream,request)
            assert error.value.status==403
        assert await count(f,stream)==2 and await receipts(f)==1

@native
@pytest.mark.parametrize('stream',STREAMS)
@pytest.mark.parametrize('damage',('installation','scope','principal'))
async def test_context_cannot_forge_authenticated_installation_scope_or_author(boot_database,stream,damage):
    async with boot_database() as f:
        await prepared(f);request=payload(f,stream);ctx=context(f)
        if damage=='installation':ctx=ScopeContext(Principal(f['principal'],uuid.uuid4()),ctx.selected,ctx.read_scopes)
        elif damage=='principal':ctx=context(f,f['member'])
        else:
            scope=Scope('PUBLIC-foreign',uuid.uuid4(),'project',True,True,False)
            ctx=ScopeContext(ctx.principal,scope,(scope,))
        async with app_reader(f,f['principal']) as app:
            with pytest.raises(ApiProblem) as error:await call(app,ctx,'PUBLIC-forged',stream,request)
            assert error.value.status==403
        assert await count(f,stream)==1 and await receipts(f)==0
