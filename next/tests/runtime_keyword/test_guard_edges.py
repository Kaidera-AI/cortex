"""Independent edge cases for the new guards; no original-member authority."""
import asyncio
import copy
import json
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

import test_core as core
import test_gateway as gateway
import test_wire as wire
import test_e0 as frozen
from test_e0_expiry import virtual_clock
from cortex_core.api.c11a import KeywordResponse,GatewayError
from cortex_core.api.c11b2 import C11b2ReadPort
from cortex_core.embeddings import keyword
from cortex_core.embeddings.pg_search import CapabilityUnavailable
from cortex_core.runtime.selection import AiSelection,SelectionGate


class CodecEdges(unittest.TestCase):
    project=core.CodecBoundary.project
    unsupported=core.CodecBoundary.unsupported

    def test_payload_type_and_root_shape_are_not_silently_coerced(self):
        for raw in (None,'text',b'[]',b'null',b'"text"'):
            with self.subTest(raw=raw):self.unsupported(raw,'invalid_payload')
        self.unsupported(b'{"messages":{}}','invalid_payload',kind='session')


class KeywordEdges(unittest.IsolatedAsyncioTestCase):
    searcher=core.KeywordBoundary.searcher
    row=core.KeywordBoundary.row

    async def test_invalid_deadline_never_authorizes_and_long_budget_is_clamped(self):
        calls=[]
        async def auth(subject):calls.append('auth')
        for deadline in (None,True,float('inf'),float('nan'),-1):
            with self.subTest(deadline=deadline),self.assertRaises(CapabilityUnavailable):
                await self.searcher([self.row()],auth=auth).search('s','match',deadline=deadline)
        self.assertEqual(calls,[])
        with virtual_clock() as clock:
            start=clock[0];seen=[]
            async def read(subject,**kwargs):seen.append(kwargs['deadline']);return [self.row()]
            await self.searcher([],read=read).search('s','match',deadline=start+100)
            self.assertEqual(seen,[start+10,start+10])

    async def test_expired_revalidation_does_not_start_source_hash_work(self):
        with virtual_clock() as clock:
            deadline=clock[0]+.25;reads=[];hashes=[]
            async def read(subject,**kwargs):
                reads.append(True)
                if len(reads)==2:clock[0]=deadline+1
                return [self.row()]
            original=keyword.source_state
            def observed(rows):hashes.append(True);return original(rows)
            with patch.object(keyword,'source_state',observed),self.assertRaises(CapabilityUnavailable):
                await self.searcher([],read=read).search('s','match',deadline=deadline)
            self.assertEqual(hashes,[])

    async def test_projection_cpu_expiry_stops_next_record_and_next_read(self):
        for count in (1,2):
            with self.subTest(records=count),virtual_clock() as clock:
                deadline=clock[0]+.25;projected=[];reads=[]
                async def read(subject,**kwargs):reads.append(True);return [self.row(str(i)) for i in range(count)]
                original=keyword.project_text
                def project(kind,payload):
                    projected.append(True);value=original(kind,payload);clock[0]=deadline+1;return value
                with patch.object(keyword,'project_text',project),self.assertRaises(CapabilityUnavailable):
                    await self.searcher([],read=read).search('s','match',deadline=deadline)
                self.assertEqual(projected,[True]);self.assertEqual(reads,[True])

    async def test_hash_cpu_expiry_stops_final_auth_dispatch(self):
        with virtual_clock() as clock:
            deadline=clock[0]+.25;auths=[]
            async def auth(subject):auths.append(True)
            original=keyword.source_state
            def state(rows):
                value=original(rows);clock[0]=deadline+1;return value
            with patch.object(keyword,'source_state',state),self.assertRaises(CapabilityUnavailable):
                await self.searcher([self.row()],auth=auth).search('s','match',deadline=deadline)
            self.assertEqual(auths,[True])

    async def test_each_source_identity_field_is_revalidated_independently(self):
        changes=dict(record_id='b',kind='session',source_revision=2,
                     payload=b'{"section":"S","content":"different"}')
        for name,value in changes.items():
            with self.subTest(field=name):
                reads=[]
                async def read(subject,**kwargs):
                    row=self.row();reads.append(True)
                    if len(reads)>1:setattr(row,name,value)
                    return [row]
                with self.assertRaises(CapabilityUnavailable):await self.searcher([],read=read).search(
                    's','match',deadline=asyncio.get_running_loop().time()+1)


class GatewayEdges(unittest.IsolatedAsyncioTestCase):
    make=gateway.KeywordGateway.make
    row=gateway.KeywordGateway.row
    call=gateway.KeywordGateway.call

    async def test_missing_hosted_or_canonical_ports_fail_at_construction(self):
        ports=NS(projects={})
        off=SelectionGate(AiSelection('off','fixture',None))
        for selection,search in ((None,None),(off,None),(off,NS(search=None))):
            with self.subTest(selection=selection,search=search),self.assertRaises(ValueError):
                C11b2ReadPort(ports,None,None,None,'fixture',{},selection=selection,keyword=search)

    async def test_plain_dictionary_cannot_select_a_success_or_partial_http_status(self):
        _,_,app=self.make([self.row()])
        async def forged(*args):return dict(results=[],complete=False,status=200)
        app.handlers['C01-R022']=forged
        status,value=await self.call(app)
        self.assertEqual(status,503);self.assertNotIn('results',value)

    async def test_keyword_packet_cannot_borrow_a_semantic_ready_gate(self):
        _,_,app=self.make()
        async def ready(*args):return dict(state='ready',model_id='fixture',generation='fixture',freshness=dict(complete=True,applied_cursor='fixture'))
        async def packet(*args):
            value=wire.KeywordWire().packet();value.pop('error')
            value.update(complete=True,state='ready_empty',unsupported=[])
            value['freshness'].update(complete=True,scanned_records=0)
            return KeywordResponse(value)
        app.search_admission=None;app.capability_source=ready;app.handlers['C01-R022']=packet
        status,value=await self.call(app)
        self.assertEqual(status,503);self.assertNotIn('results',value)

    async def test_optional_fusion_is_a_nonretryable_closed_refusal(self):
        _,_,app=self.make([self.row()])
        status,value=await self.call(app,body=dict(query='match',enable_graph=True))
        self.assertEqual(status,503);self.assertIs(value['error']['retryable'],False)

    async def test_canonical_wait_can_observe_progress_without_semantic_work(self):
        state,_,app=self.make([self.row()])
        async def progress():await asyncio.sleep(0);state.rows[0].source_revision=2
        task=asyncio.create_task(progress())
        try:status,value=await self.call(app,body=dict(query='match',after=dict(record_id=gateway.RID,revision=2),wait_ms=100))
        finally:await task
        self.assertEqual(status,200);self.assertEqual(value['results'][0]['revision'],2)


class WireEdges(unittest.TestCase):
    packet=wire.KeywordWire.packet

    def test_duplicate_ids_and_inconsistent_complete_state_refuse(self):
        value=self.packet();value['unsupported']*=2;value['freshness']['scanned_records']=2
        with self.assertRaises(GatewayError):KeywordResponse(value).transport_status()
        value=self.packet();value.pop('error');value.update(complete=True,state='ready',unsupported=[])
        value['freshness']['complete']=True
        with self.assertRaises(GatewayError):KeywordResponse(value).transport_status()


class GateEdges(unittest.IsolatedAsyncioTestCase):
    async def test_bad_selection_data_cannot_construct_or_replace_a_gate(self):
        for value in (None,AiSelection('unknown','fixture',None),AiSelection('off','',None)):
            with self.subTest(value=value),self.assertRaises(ValueError):SelectionGate(value)
