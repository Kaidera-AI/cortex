"""Mounted registry writes validate typed input and carry replay headers.

Storage handlers are a declared seam here; native SQL tests qualify their effects.
"""
from types import SimpleNamespace
import uuid
import pytest
from fastapi.testclient import TestClient
from cortex_v2.store import Principal,Scope,ScopeContext
from cortex_v2 import app as app_module
from cortex_v2.interface import operations

IDS={'agent':('boot.agent.bind','/v1/boot/agent-bindings'),'entry':('boot.entry.bind','/v1/boot/entry-bindings'),'publication':('boot.catalogue.publish','/v1/boot/catalogue-publications')}

@pytest.mark.parametrize('stream',IDS)
@pytest.mark.parametrize('case',('success','replay','missing-key','forged-author','bool-revision'))
def test_mounted_enactor_validates_body_key_and_exact_replay_envelope(stream,case,monkeypatch):
    operation,path=IDS[stream]
    entry=next((row for row in operations.OPERATIONS if row['operation_id']==operation),None)
    assert entry is not None,'Typed boot operation missing'
    scope=Scope('PUBLIC-project',uuid.uuid4(),'project',True,True,False)
    ctx=ScopeContext(Principal(uuid.uuid4(),uuid.uuid4()),scope,(scope,));calls=[]
    class Transaction:
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
    class Connection:
        def transaction(self):return Transaction()
    connection=Connection()
    class Acquisition:
        async def __aenter__(self):return connection
        async def __aexit__(self,*args):pass
    async def auth(*args,**kwargs):return ctx.principal
    async def scopes(conn,principal,alias,read_scopes,*,write):
        assert conn is connection and principal is ctx.principal
        assert alias=='PUBLIC-project' and read_scopes==['PUBLIC-project'] and write is True
        return ctx
    async def handler(conn,context,idempotency_key,payload,path_params):
        assert conn is connection and context is ctx and idempotency_key=='PUBLIC-key'
        assert payload.expected_revision==0 and path_params=={}
        calls.append(payload)
        return (200 if case=='replay' else 201),{'state':'committed','revision':1},case=='replay'
    monkeypatch.setitem(entry,'handler',handler)
    monkeypatch.setattr(app_module,'authenticate',auth);monkeypatch.setattr(app_module,'resolve_scopes',scopes)
    app=app_module.create_app();app.state.pool=SimpleNamespace(acquire=lambda:Acquisition())
    app.state.profile=SimpleNamespace(instance_id='PUBLIC fixture',contract_version='PUBLIC test contract');app.state.settings=SimpleNamespace(token_pepper=b'PUBLIC fixture pepper')
    data={'expected_revision':0,'state':'active','source_reference':'PUBLIC route'}
    if stream=='agent':data.update(actor_id=str(uuid.uuid4()),identity_id=str(uuid.uuid4()),persona_id=str(uuid.uuid4()),persona_revision=1,functional_roles=['PUBLIC-role'])
    elif stream=='entry':data.update(subject_kind='project',entry_kind='skill',entry_scope_id=str(scope.scope_id),skill_id=str(uuid.uuid4()),bound_revision=1,priority=0)
    else:data.update(catalogue_scope_id=str(uuid.uuid4()),entry_kind='skill',entry_id=str(uuid.uuid4()),entry_revision=1)
    headers={'Authorization':'Bearer '+'A'*43,'X-Cortex-Scope':'PUBLIC-project','Idempotency-Key':'PUBLIC-key'}
    if case=='missing-key':headers.pop('Idempotency-Key')
    elif case=='forged-author':data['enacted_by_principal']=str(uuid.uuid4())
    elif case=='bool-revision':data['expected_revision']=False
    response=TestClient(app,raise_server_exceptions=False).post(path,json=data,headers=headers)
    if case in ('success','replay'):
        assert response.status_code==(200 if case=='replay' else 201)
        assert response.json()['data']=={'state':'committed','revision':1}
        assert len(calls)==1
        assert response.headers.get('Idempotent-Replay')==('true' if case=='replay' else None)
    else:
        assert response.status_code==(400 if case=='missing-key' else 422)
        assert calls==[]
    assert 'Traceback' not in response.text and 'Bearer' not in response.text
