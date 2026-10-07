"""Actual v2 command composition and HTTP route; database/auth seams are synthetic."""
import asyncio
import copy
from datetime import datetime,timezone
import importlib
import importlib.util
import json
from types import SimpleNamespace
import uuid

import pytest
from fastapi.testclient import TestClient
from cortex_v2.models import CreateContentRequest
from cortex_v2.store import ApiProblem,Principal,Scope,ScopeContext

TYPES=['commit','decision','lesson','started','stopped','blocked','unblocked','bug','handoff','question']
PID=uuid.UUID('10000000-0000-4000-8000-000000000001');SID=uuid.UUID('20000000-0000-4000-8000-000000000002')
SUMMARY='PUBLIC résumé ☃\n'+'exact original '*400


def required():
    assert importlib.util.find_spec('cortex_v2.agent_log') is not None,'v2 log service missing'
    module=importlib.import_module('cortex_v2.agent_log')
    assert callable(getattr(module,'write_log',None)) and callable(getattr(module,'read_log',None)),'v2 log service missing'
    return module


class Memory:
    def __init__(self):self.rows={};self.receipts={};self.effects=[];self.agent='worker';self.defect=None
    async def fetchval(self,sql,*args):
        assert PID in args and SID in args and 'actor' in sql and 'membership' in sql
        return self.agent
    async def execute(self,*args):raise AssertionError('facade bypassed existing canonical services')


def setup(monkeypatch,event_type='commit'):
    module=required();store=Memory()
    context=ScopeContext(Principal(PID,PID),Scope('fixture-project',SID,'project',True,True,True),(Scope('fixture-project',SID,'project',True,True,True),))
    async def begin(connection,**kwargs):
        key=(kwargs['operation'],kwargs['idempotency_key']);previous=store.receipts.get(key)
        if previous:
            if previous[0]!=kwargs['digest']:raise ApiProblem(409,'idempotency_key_reused','Use a new key.')
            return copy.deepcopy(previous[1]),True
        return None,False
    async def commit(connection,**kwargs):store.receipts[(kwargs['operation'],kwargs['idempotency_key'])]=(kwargs['digest'],copy.deepcopy(kwargs['receipt']))
    async def create(connection,ctx,payload,key):
        assert connection is store and ctx is context and isinstance(payload,CreateContentRequest)
        store.effects.append((payload.content_class,payload.body,copy.deepcopy(payload.payload),key))
        cid=uuid.uuid4();store.rows[cid]={'content_id':str(cid),'content_class':payload.content_class,'scope_id':str(SID),'body':payload.body,'payload':copy.deepcopy(payload.payload),'revision':1,'created_by_principal':str(PID),'created_at':datetime.now(timezone.utc).isoformat()}
        return 201,{'state':'committed','content_id':str(cid),'scope_id':str(SID),'revision':1},False
    async def read(connection,cid,revision,history):
        assert connection is store and revision==1 and history is False
        if cid not in store.rows:raise ApiProblem(404,'content_not_found','Unavailable.')
        row=copy.deepcopy(store.rows[cid])
        if store.defect=='summary':row['body']='PUBLIC changed'
        if store.defect=='scope':row['scope_id']=str(PID)
        if store.defect=='files':row['payload']['files']=['PUBLIC wrong']
        if store.defect=='class':row['content_class']='knowledge'
        return row
    monkeypatch.setattr(module,'begin_command',begin);monkeypatch.setattr(module,'commit_receipt',commit)
    monkeypatch.setattr(module,'create_content',create);monkeypatch.setattr(module,'get_content',read)
    payload=module.LogRequest(event_type=event_type,summary=SUMMARY,files_affected=['/PUBLIC/a b.py','/PUBLIC/é.md'],metadata={'parent_goal_id':'PUBLIC-goal','goal_ancestry':['PUBLIC-goal']})
    return module,store,context,payload


@pytest.mark.parametrize('event_type',TYPES)
def test_canonical_command_exact_payload_projection_and_replay(event_type,monkeypatch):
    module,store,context,payload=setup(monkeypatch,event_type)
    status,response,replayed=asyncio.run(module.write_log(store,context,payload,'PUBLIC-key','worker'))
    assert status in [200,201] and response['verified'] is True and not replayed
    kind=event_type if event_type in ['decision','lesson'] else 'team_event'
    result=asyncio.run(module.read_log(store,context,kind,response['id']))
    assert result['verified'] is True and result['kind']==kind and result['id']==response['id']
    assert result['row']['summary']==SUMMARY and result['row']['agent_name']=='worker@fixture-project'
    assert result['row']['project']=='fixture-project'
    if kind=='team_event':assert result['row']['event_type']==event_type and result['row']['files']==payload.files_affected
    else:
        assert response['embedded'] is False
        companion=asyncio.run(module.read_log(store,context,'team_event',response['team_event_id']))
        assert companion['row']['summary']==SUMMARY and companion['row']['files']==payload.files_affected and companion['row']['event_type']==event_type
        assert result['row']['metadata']==payload.metadata
    before=copy.deepcopy(store.effects)
    second=asyncio.run(module.write_log(store,context,payload,'PUBLIC-key','worker'))
    assert second[1]==response and second[2] is True and store.effects==before
    changed=payload.model_copy(update={'summary':'PUBLIC changed request'})
    with pytest.raises(ApiProblem) as error:asyncio.run(module.write_log(store,context,changed,'PUBLIC-key','worker'))
    assert error.value.code=='idempotency_key_reused' and store.effects==before
    assert len(store.effects)==(2 if kind!='team_event' else 1)


@pytest.mark.parametrize('defect',['summary','scope','files','class'])
def test_no_verified_success_when_actual_canonical_readback_differs(defect,monkeypatch):
    module,store,context,payload=setup(monkeypatch,'decision');store.defect=defect
    with pytest.raises(ApiProblem):asyncio.run(module.write_log(store,context,payload,'PUBLIC-key','worker'))
    assert ('agent.log','PUBLIC-key') not in store.receipts


@pytest.mark.parametrize('kind,id_value',[('handoff',str(PID)),('team_event','../../outside'),('decision','prefix'),('foreign',str(PID))])
def test_unmapped_confirmation_kind_or_unsafe_id_refuses(kind,id_value,monkeypatch):
    module,store,context,payload=setup(monkeypatch)
    with pytest.raises(ApiProblem):asyncio.run(module.read_log(store,context,kind,id_value))
    assert store.effects==[]


@pytest.mark.parametrize('case',['valid','wrong-agent','unregistered','scope-denied','readback-failure','missing-key','missing-scope','extra-field','wrong-kind'])
def test_mounted_http_log_and_confirmation_keep_scope_and_transaction_boundary(case,monkeypatch):
    module,store,context,payload=setup(monkeypatch,'decision')
    app_module=importlib.import_module('cortex_v2.app')
    class Transaction:
        async def __aenter__(self):self.before=copy.deepcopy((store.rows,store.receipts,store.effects));return self
        async def __aexit__(self,kind,*args):
            if kind:store.rows,store.receipts,store.effects=self.before
    class Acquisition:
        async def __aenter__(self):return store
        async def __aexit__(self,*args):pass
    store.transaction=lambda:Transaction()
    app=app_module.create_app();app.state.pool=SimpleNamespace(acquire=lambda:Acquisition())
    app.state.profile=SimpleNamespace(instance_id='PUBLIC-synthetic-profile');app.state.settings=SimpleNamespace(token_pepper=b'PUBLIC synthetic test pepper')
    async def auth(*args,**kwargs):return context.principal
    async def resolve(connection,principal,alias,read_scopes,*,write):
        assert connection is store and principal is context.principal and read_scopes==[alias]
        if alias!='fixture-project' or case=='scope-denied':raise ApiProblem(403,'scope_write_denied','Denied.')
        return context
    monkeypatch.setattr(app_module,'authenticate',auth);monkeypatch.setattr(app_module,'resolve_scopes',resolve)
    headers={'Authorization':'Bearer '+'A'*43,'X-Cortex-Scope':'fixture-project','X-Agent-Name':'owner' if case=='wrong-agent' else 'worker','Idempotency-Key':'PUBLIC-key'}
    if case=='missing-key':headers.pop('Idempotency-Key')
    if case=='missing-scope':headers.pop('X-Cortex-Scope')
    if case=='unregistered':store.agent=None
    if case=='readback-failure':store.defect='summary'
    body=payload.model_dump(exclude_none=True)
    if case=='extra-field':body['scope_id']=str(PID)
    client=TestClient(app,raise_server_exceptions=False)
    response=client.post('/log',headers=headers,json=body)
    if case in ['valid','wrong-kind']:
        assert response.status_code==200 and response.json()['verified'] is True
        rid=response.json()['id'];get=client.get('/verify/write',params={'kind':'handoff' if case=='wrong-kind' else 'decision','id':rid},headers=headers)
        if case=='wrong-kind':assert get.status_code==422
        else:
            assert get.status_code==200 and get.json()['row']['summary']==SUMMARY
            assert len(store.effects)==2 and store.effects[0][0]=='decision' and store.effects[1][0]=='progress'
    else:
        assert response.status_code in [400,403,422,500] and response.status_code!=404
        assert store.rows=={} and store.receipts=={} and store.effects==[]
    assert 'Bearer' not in response.text and 'Traceback' not in response.text
