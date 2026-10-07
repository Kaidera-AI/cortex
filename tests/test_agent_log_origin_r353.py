"""Actual facade/native HTTP forging path; server/auth/database seams are explicit."""
import asyncio
import copy
import importlib
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from cortex_v2.store import ApiProblem
from test_agent_log_service_r335 import setup,PID,SID,SUMMARY


def client(monkeypatch):
    module,store,context,payload=setup(monkeypatch)
    base_fetch=store.fetchval;queries=[]
    async def fetch(sql,*args):
        if 'command_receipts' in sql:
            queries.append(sql)
            creator,scope,record_id,kind=args
            if creator!=PID or scope!=SID:return None
            for (operation,key),(digest,receipt) in store.receipts.items():
                if operation=='agent.log' and (receipt.get('id')==str(record_id) or receipt.get('team_event_id')==str(record_id)):
                    return 'worker'
            return None
        return await base_fetch(sql,*args)
    store.fetchval=fetch
    app_module=importlib.import_module('cortex_v2.app');app=app_module.create_app()
    class Transaction:
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
    class Acquisition:
        async def __aenter__(self):return store
        async def __aexit__(self,*args):pass
    store.transaction=lambda:Transaction()
    app.state.pool=SimpleNamespace(acquire=lambda:Acquisition())
    app.state.settings=SimpleNamespace(token_pepper=b'PUBLIC synthetic pepper')
    app.state.profile=SimpleNamespace(instance_id='PUBLIC-synthetic-profile',contract_version='PUBLIC-contract')
    async def auth(*a,**kw):return context.principal
    async def resolve(connection,principal,alias,scopes,*,write):
        assert connection is store and principal is context.principal and alias=='fixture-project' and scopes==[alias]
        return context
    monkeypatch.setattr(app_module,'authenticate',auth);monkeypatch.setattr(app_module,'resolve_scopes',resolve)
    monkeypatch.setattr(app_module,'create_content',module.create_content)
    headers={'Authorization':'Bearer '+'A'*43,'X-Cortex-Scope':'fixture-project','X-Agent-Name':'worker','Idempotency-Key':'PUBLIC-key'}
    return module,store,context,payload,TestClient(app,raise_server_exceptions=False),headers,queries


@pytest.mark.parametrize('label',['victim@fixture-project','worker@fixture-project'])
def test_generic_same_scope_content_marker_has_no_trusted_log_origin_even_with_exact_fields(label,monkeypatch):
    module,store,context,payload,http,headers,queries=client(monkeypatch)
    ordinary={'content_class':'progress','body':SUMMARY,'payload':{'agent_log_schema':'cortex.agent-log.v1','project':'fixture-project','agent_name':label,'summary':SUMMARY,'event_type':'commit','files':payload.files_affected,'metadata':payload.metadata}}
    created=http.post('/v1/content',json=ordinary,headers=headers)
    assert created.status_code==201
    cid=created.json()['data']['content_id']
    assert store.rows[uuid.UUID(cid)]['created_by_principal']==str(PID)
    result=http.get('/verify/write',params={'kind':'team_event','id':cid},headers=headers)
    assert result.status_code==404 and result.json()['error']['code']=='log_record_not_found'
    assert 'verified' not in result.json() and 'victim' not in result.text
    assert len(store.effects)==1 and store.receipts=={} and queries


@pytest.mark.parametrize('tamper',['creator','label','valid'])
def test_real_log_origin_and_canonical_creator_label_are_bound_before_verified_success(tamper,monkeypatch):
    module,store,context,payload,http,headers,queries=client(monkeypatch)
    written=http.post('/log',json=payload.model_dump(exclude_none=True),headers=headers)
    assert written.status_code==200 and written.json()['verified'] is True
    cid=written.json()['id'];row=store.rows[uuid.UUID(cid)]
    if tamper=='creator':row['created_by_principal']=str(SID)
    if tamper=='label':row['payload']['agent_name']='victim@fixture-project'
    result=http.get('/verify/write',params={'kind':'team_event','id':cid},headers=headers)
    if tamper=='valid':
        assert result.status_code==200 and result.json()['verified'] is True and result.json()['row']['agent_name']=='worker@fixture-project'
        assert queries and all('agent.log' in q for q in queries)
    else:
        assert result.status_code==404 and result.json()['error']['code']=='log_record_not_found' and 'victim' not in result.text
    assert len(store.effects)==1
