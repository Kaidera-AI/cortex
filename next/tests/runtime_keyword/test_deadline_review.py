"""Independent reviewer contract probes; synthetic ports and local ASGI only."""
import asyncio,json,unittest
from types import SimpleNamespace as NS
from cortex_core.api.c11a import ConsumerGateway
from cortex_core.embeddings.keyword import KeywordSearch

class ReviewEdges(unittest.IsolatedAsyncioTestCase):
    async def test_literal_metacharacters_casefold_and_metadata_exclusion(self):
        raw=lambda section,content,meta:json.dumps(dict(section=section,content=content,metadata=meta)).encode()
        rows=[NS(record_id='a',kind='memory',source_revision=1,payload=raw('Topic','Straße [a.*] café','ONLY-METADATA'))]
        async def auth(subject):return True
        async def read(subject,**kwargs):return rows
        search=KeywordSearch(read_current=read,authorize=auth)
        deadline=asyncio.get_running_loop().time()+1
        for q,wanted in [('STRASSE',1),('[a.*]',1),('aZZ',0),('ONLY-METADATA',0)]:
            with self.subTest(query=q):
                result=await search.search('s',q,deadline=deadline)
                self.assertEqual(len(result.hits),wanted)
                self.assertTrue(result.complete)

    def gateway(self,*,slow_receive=False):
        async def core():return True
        async def principal(scope):
            return dict(principal_id='00000000-0000-0000-0000-000000000001',
                project_id='00000000-0000-0000-0000-000000000002',
                tenant_id='00000000-0000-0000-0000-000000000003',permission_generation=1)
        async def recheck(*args):return True
        async def capability(*args):return dict(state='unavailable')
        async def health():return dict(component='cortex',status='ok')
        return ConsumerGateway(core_probe=core,principal_resolver=principal,
            permission_recheck=recheck,capability_source=capability,health=health,
            handlers={},parse_search_body=True)

    async def test_expired_request_cannot_wait_forever_on_timeout_response_send(self):
        app=self.gateway();events=[]
        async def receive():
            await asyncio.Event().wait()  # admitted body read expires first
        async def send(message):
            events.append(message['type'])
            await asyncio.Event().wait()  # client backpressure on timeout packet
        deadline=asyncio.get_running_loop().time()+.01
        task=asyncio.create_task(app.app(dict(type='http',method='POST',path='/search',
            headers=[],query_string=b'',_cortex_deadline=deadline),receive,send))
        try:
            await asyncio.sleep(.08)
            self.assertTrue(task.done(),'request exceeded original absolute deadline: timeout fallback is still awaiting raw ASGI send')
        finally:
            task.cancel();await asyncio.gather(task,return_exceptions=True)
        self.assertLessEqual(len(events),1)

    async def test_original_deadline_also_bounds_backpressure_after_normal_headers(self):
        app=self.gateway();events=[]
        async def receive():return dict(type='http.request',body=b'{}',more_body=False)
        async def send(message):
            events.append(message['type'])
            await asyncio.Event().wait()
        task=asyncio.create_task(app.app(dict(type='http',method='POST',path='/search',
            headers=[],query_string=b'',_cortex_deadline=asyncio.get_running_loop().time()+.01),receive,send))
        try:
            await asyncio.sleep(.08)
            self.assertTrue(task.done(),'normal response must remain under the original deadline')
        finally:
            task.cancel();await asyncio.gather(task,return_exceptions=True)
        self.assertEqual(events,['http.response.start'])
