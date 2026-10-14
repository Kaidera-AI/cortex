"""Real ASGI/C11 algorithms with explicit synthetic Core/C04 boundaries."""
import asyncio
import json
from types import SimpleNamespace as NS
import unittest
from uuid import UUID

import test_e0 as frozen
from cortex_core.api.c11a import ConsumerGateway
from cortex_core.api.c11b2 import C11b2ReadPort
from cortex_core.auth import AuthError
from test_e0_expiry import virtual_clock

RID='00000000-0000-0000-0000-000000000001'


class KeywordGateway(unittest.IsolatedAsyncioTestCase):
    def make(self, rows=None):
        selection=frozen.surface('cortex_core.runtime.selection','AiSelection')
        gate=frozen.surface('cortex_core.runtime.selection','SelectionGate')(selection('off','synthetic-config',None))
        state=NS(rows=rows or [],events=[],allowed=True,mutate_on_final=False,mutated=False)
        principal=dict(principal_id=RID,project_id=RID,tenant_id=RID,permission_generation=1)
        class Core:
            projects={'p':UUID(RID)}
            async def principal(self,scope):
                state.events.append('auth')
                if not state.allowed:raise AuthError('forbidden')
                if state.mutate_on_final and state.events.count('read')>=3 and not state.mutated:
                    state.rows=[NS(**{**r.__dict__,'source_revision':2}) for r in state.rows];state.mutated=True
                return dict(principal)
            async def keyword_current(self,principal,scope,*,limit,deadline):
                state.events.append('read');return state.rows[:limit]
            async def keyword_target(self,principal,scope,target,*,deadline):
                state.events.append('target')
                return next((NS(revision=r.source_revision,tombstone=getattr(r,'tombstone',False)) for r in state.rows if r.record_id==str(target['record_id'])),None)
        try:port=C11b2ReadPort(Core(),None,None,None,'graph-fixture',{'p':'/fixture'},selection=gate)
        except TypeError:raise frozen.SemanticRed('OFF constructor/binding absent') from None
        async def core():state.events.append('core');return True
        async def health():return {'component':'cortex','status':'ok'}
        try:gateway=ConsumerGateway(core_probe=core,principal_resolver=port.principal,
            permission_recheck=port.permission_recheck,capability_source=port.capability_source,
            health=health,handlers=port.handlers,parse_search_body=True,search_admission=port.search_admission)
        except AttributeError:raise frozen.SemanticRed('canonical search admission absent') from None
        except TypeError:raise frozen.SemanticRed('gateway canonical admission absent') from None
        return state,port,gateway

    def row(self,rid=RID,kind='memory',content='Café match'):
        return NS(record_id=rid,kind=kind,source_revision=1,payload=json.dumps({'section':'S','content':content}).encode())

    async def call(self,gateway,method='POST',body=None,query=b'q=CAF%C3%89',scope_extra=None):
        scope={'type':'http','method':method,'path':'/search','query_string':query,'headers':[]}
        scope.update(scope_extra or {})
        payload=json.dumps(body or {'query':'CAFÉ'}).encode();messages=[]
        received=False
        async def receive():
            nonlocal received
            if received:await asyncio.Event().wait()
            received=True
            return {'type':'http.request','body':payload,'more_body':False}
        async def send(value):messages.append(value)
        await gateway.app(scope,receive,send)
        status=next(v['status'] for v in messages if v['type']=='http.response.start')
        packet=json.loads(next(v['body'] for v in messages if v['type']=='http.response.body'))
        return status,packet

    async def test_get_post_off_complete_and_empty_have_canonical_wire_no_vector_gate(self):
        for method in ('GET','POST'):
            state,port,gateway=self.make([self.row()])
            status,value=await self.call(gateway,method)
            self.assertEqual(status,200);self.assertEqual(value['state'],'ready')
            self.assertEqual(value['results'],[dict(id=RID,kind='memory',revision=1,score=1.0)])
            self.assertEqual(value['degraded'],['hosted_disabled']);self.assertEqual(value['unsupported'],[])
            self.assertEqual(value['freshness'],dict(source='canonical',state='current',complete=True,scanned_records=1))
            self.assertIsNone(port.identity);self.assertIsNone(port.provider)
            self.assertEqual((await port.capability_source('pg_search',{}))['reason'],'hosted_disabled')
        _,_,gateway=self.make()
        status,value=await self.call(gateway)
        self.assertEqual((status,value['state'],value['complete']),(200,'ready_empty',True))

    async def test_unsupported_and_overflow_are_typed_partial503_not_empty200(self):
        for rows,reason,count in (([self.row(kind='binary')],'unsupported_text',1),
                ([self.row(str(UUID(int=i+1))) for i in range(101)],'scan_limit',100)):
            with self.subTest(reason=reason):
                _,_,gateway=self.make(rows);status,value=await self.call(gateway)
                self.assertEqual((status,value['state'],value['complete']),(503,'partial',False))
                self.assertEqual(value['error'],dict(code='capability_unavailable',capability='search',state='partial',reason=reason,retryable=False))
                self.assertEqual(value['freshness']['scanned_records'],count)
                if reason=='scan_limit':self.assertEqual(value['unsupported'],[])

    async def test_source_change_during_final_auth_suppresses_hits(self):
        state,_,gateway=self.make([self.row()]);state.mutate_on_final=True
        status,value=await self.call(gateway)
        self.assertTrue(state.mutated);self.assertEqual(status,503)
        self.assertEqual(value['error']['reason'],'source_changed');self.assertNotIn('results',value)

    async def test_revoked_expired_or_fusion_request_emits_no_prepared_data(self):
        state,_,gateway=self.make([self.row()]);state.allowed=False
        status,value=await self.call(gateway);self.assertEqual(status,403);self.assertNotIn('results',value)
        _,_,gateway=self.make([self.row()])
        status,value=await self.call(gateway,scope_extra={'_cortex_deadline':asyncio.get_running_loop().time()-1})
        self.assertEqual((status,value['error']['reason']),(503,'request_timeout'));self.assertNotIn('results',value)
        status,value=await self.call(gateway,body={'query':'match','enable_graph':True})
        self.assertEqual(value['error']['reason'],'graph_fusion_unbound');self.assertNotIn('results',value)

    async def test_rerank_is_zero_call_degradation_and_canonical_after_target_is_preserved(self):
        state,_,gateway=self.make([self.row()])
        status,value=await self.call(gateway,body={'query':'match','rerank':True,'after':{'record_id':RID,'revision':1}})
        self.assertEqual(status,200);self.assertIn('target',state.events)
        self.assertEqual(value['degraded'],['hosted_disabled','rerank']);self.assertFalse(value['reranked'])

    async def test_public_keyword_shape_refuses_synthetic_non_uuid_ids(self):
        _,_,gateway=self.make([self.row('synthetic-not-public-id')])
        status,value=await self.call(gateway)
        self.assertEqual(status,503);self.assertNotIn('results',value)

    async def test_mode_change_between_response_start_and_body_drops_body(self):
        _,port,gateway=self.make([self.row()]);messages=[]
        scope=dict(type='http',method='POST',path='/search',query_string=b'',headers=[])
        async def receive():return dict(type='http.request',body=b'{"query":"match"}',more_body=False)
        async def send(message):
            messages.append(message)
            if message['type']=='http.response.start':
                cls=frozen.surface('cortex_core.runtime.selection','AiSelection')
                await port.selection.apply(cls('off','synthetic-next-config',None))
        from cortex_core.embeddings.pg_search import CapabilityUnavailable
        with self.assertRaises(CapabilityUnavailable):await gateway.app(scope,receive,send)
        self.assertFalse(any(m['type']=='http.response.body' for m in messages))

    async def test_expiry_during_core_or_principal_stops_later_dispatch(self):
        for stage in ('core','principal'):
            with self.subTest(stage=stage),virtual_clock() as clock:
                state,port,gateway=self.make([self.row()]);deadline=clock[0]+.25
                original=gateway.core_probe if stage=='core' else gateway.principal_resolver
                async def expired(*args):
                    value=await original(*args);clock[0]=deadline+1;return value
                if stage=='core':gateway.core_probe=expired
                else:gateway.principal_resolver=expired
                status,value=await self.call(gateway,scope_extra={'_cortex_deadline':deadline})
                self.assertEqual((status,value['error']['reason']),(503,'request_timeout'))
                self.assertNotIn('read',state.events)
                if stage=='core':self.assertEqual(state.events,['core'])

    async def test_canonical_targets_keep_pending_tombstone_and_not_found_masks(self):
        for kind,status,state in (('pending',503,'pending'),('tombstone',503,'unavailable'),('missing',404,None)):
            with self.subTest(kind=kind):
                row=self.row()
                if kind=='tombstone':row.tombstone=True
                _,_,gateway=self.make([row])
                target_id=str(UUID(int=2)) if kind=='missing' else RID
                code,packet=await self.call(gateway,body={'query':'match','after':{'record_id':target_id,'revision':2},'wait_ms':1})
                self.assertEqual(code,status);self.assertNotIn('results',packet)
                if state:self.assertEqual(packet['error']['state'],state)

    async def test_fractional_page_limit_is_invalid_not_silently_truncated(self):
        _,_,gateway=self.make([self.row()])
        status,value=await self.call(gateway,body={'query':'match','top_k':1.5})
        self.assertEqual(status,400);self.assertNotIn('results',value)

    async def test_off_status_never_queries_semantic_vector_or_invents_model_readiness(self):
        _,port,_=self.make()
        async def forbidden(*args):self.fail('OFF status reached vector source')
        port._core_pending=forbidden
        def ports(*args):self.fail('OFF status constructed vector/graph source')
        port._ports=ports
        principal=await port.principal({'method':'GET','path':'/degradation','headers':[]})
        value=await port.status_degradation(principal,{},None)
        self.assertEqual(value['search'],dict(state='unavailable',reason='hosted_disabled'))
