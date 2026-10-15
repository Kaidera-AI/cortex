"""E2 boundary oracles; synthetic current-read callbacks grant no authority."""
import asyncio
import json
from types import SimpleNamespace as NS
import unittest

import test_e0 as frozen
from test_e0_expiry import virtual_clock
from cortex_core.embeddings.pg_search import CapabilityUnavailable
from cortex_core.release.codec import decode_json


class CodecBoundary(unittest.TestCase):
    def project(self,kind,body):
        return frozen.surface('cortex_core.embeddings.text','project_text')(kind,body)

    def unsupported(self,body,reason,kind='memory'):
        project=frozen.surface('cortex_core.embeddings.text','project_text')
        error=frozen.surface('cortex_core.embeddings.text','UnsupportedText')
        with self.assertRaises(error) as caught:project(kind,body)
        self.assertEqual(caught.exception.reason,reason)

    def test_payload_unicode_lines_and_ignored_metadata_do_not_change_declaration_decoder(self):
        value={'section':'Cafe\u0301','content':'one\ntwo\tthree','metadata':{'weight':1.5}}
        raw=json.dumps(value,ensure_ascii=False).encode()
        self.assertEqual(self.project('memory',raw),'Cafe\u0301\none\ntwo\tthree')
        with self.assertRaises(ValueError):decode_json(raw,4096,canonical=False)

    def test_raw_limit_and_depth_are_typed(self):
        self.unsupported(b' '*(8*1024*1024+1),'text_too_large')
        nested={}
        for _ in range(34):nested={'extra':nested}
        self.unsupported(json.dumps({'section':'S','content':'x','metadata':nested}).encode(),'invalid_payload')

    def test_exact_utf8_projection_limit_and_one_byte_over(self):
        raw=lambda text:json.dumps({'section':'S','content':text},ensure_ascii=False).encode()
        self.assertEqual(len(self.project('memory',raw('é'*8191)).encode()),16384)
        self.unsupported(raw('é'*8191+'x'),'text_too_large')

    def test_empty_invalid_roles_and_bad_utf8_never_become_text(self):
        for body in (b'\xff',b'{"section":"","content":""}',b'{"section":[],"content":"x"}'):
            self.unsupported(body,'invalid_payload')
        for messages in ([],[{'role':'user','content':'x'}],[{'role':'agent','content':3}]):
            self.unsupported(json.dumps({'messages':messages}).encode(),'invalid_payload',kind='session')

    def test_declaration_strings_stay_strict_and_nonfinite_payload_metadata_refuses(self):
        for raw in (b'{"x":"a\\nb"}','{"x":"Cafe\u0301"}'.encode()):
            with self.subTest(raw=raw),self.assertRaises(ValueError):decode_json(raw,4096,canonical=False)
        self.unsupported(b'{"section":"S","content":"text","metadata":1e999}','invalid_payload')


class KeywordBoundary(unittest.IsolatedAsyncioTestCase):
    def searcher(self,rows,auth=None,read=None,**kwargs):
        cls=frozen.surface('cortex_core.embeddings.keyword','KeywordSearch')
        async def authorize(subject):
            if auth:await auth(subject)
        async def current(subject,*,limit,deadline):
            return rows[:limit] if read is None else await read(subject,limit=limit,deadline=deadline)
        return cls(read_current=current,authorize=authorize,**kwargs)

    def row(self,ident='a',content='match',**changes):
        return NS(record_id=ident,kind=changes.get('kind','memory'),source_revision=changes.get('revision',1),
                  payload=json.dumps({'section':'S','content':content}).encode())

    async def test_source_view_change_suppresses_prepared_results(self):
        calls=[]
        async def read(subject,**kwargs):
            calls.append(kwargs)
            return [self.row(revision=len(calls))]
        search=self.searcher([],read=read)
        with self.assertRaises(CapabilityUnavailable) as caught:
            await search.search('s','match',limit=1,deadline=asyncio.get_running_loop().time()+1)
        self.assertEqual(caught.exception.reason,'source_changed')
        self.assertEqual([x['limit'] for x in calls],[101,101])

    async def test_bad_limits_query_and_duplicate_rows_refuse(self):
        cls=frozen.surface('cortex_core.embeddings.keyword','KeywordSearch')
        async def auth(s):pass
        async def read(s,**kwargs):return []
        for maximum in (0,101,True):
            with self.assertRaises(ValueError):cls(read_current=read,authorize=auth,max_records=maximum)
        search=self.searcher([self.row(),self.row()])
        for query,limit in (('',1),('x',True),('x',101)):
            with self.assertRaises(ValueError):await search.search('s',query,limit=limit,deadline=asyncio.get_running_loop().time()+1)
        with self.assertRaises(CapabilityUnavailable):
            await search.search('s','match',limit=1,deadline=asyncio.get_running_loop().time()+1)

    async def test_absolute_expiry_after_each_auth_or_read_await_drops_result(self):
        for expired_stage in ('auth1','read1','read2','auth2'):
            with self.subTest(stage=expired_stage),virtual_clock() as clock:
                deadline=clock[0]+.25
                calls=[];counts={'auth':0,'read':0}
                async def step(kind):
                    counts[kind]+=1;name=kind+str(counts[kind]);calls.append(name)
                    await asyncio.sleep(0)
                    if name==expired_stage:clock[0]=deadline+1
                async def auth(subject):await step('auth')
                async def read(subject,**kwargs):await step('read');return [self.row()]
                search=self.searcher([],auth=auth,read=read)
                with self.assertRaises(CapabilityUnavailable):await search.search('s','match',limit=1,deadline=deadline)
                self.assertEqual(calls[-1],expired_stage)

    async def test_scan_bound_unsupported_and_ties_keep_honest_coverage(self):
        search=self.searcher([self.row('b'),self.row('a'),self.row('c',kind='binary')])
        result=await search.search('s','MATCH',limit=1,deadline=asyncio.get_running_loop().time()+1)
        self.assertEqual([r.record_id for r in result.hits],['a'])
        self.assertFalse(result.complete);self.assertEqual(result.reason,'unsupported_text')
        self.assertEqual(result.scanned_records,3)
        self.assertEqual([(u.record_id,u.reason) for u in result.unsupported],[('c','unknown_kind')])

    async def test_explicit_false_auth_never_reads_or_returns_and_bad_rows_refuse(self):
        for denied in (1,2):
            calls=[]
            async def auth(subject):
                calls.append('auth');return calls.count('auth')!=denied
            search=self.searcher([self.row()])
            search.authorize=auth
            with self.assertRaises(PermissionError):await search.search('s','match',limit=1,deadline=asyncio.get_running_loop().time()+1)
        for row in (self.row(revision=True),self.row(revision=0),NS(record_id='x',kind='memory',source_revision=1,payload=None)):
            with self.subTest(row=row),self.assertRaises(CapabilityUnavailable):
                await self.searcher([row]).search('s','match',limit=1,deadline=asyncio.get_running_loop().time()+1)

    async def test_overflow_witness_is_not_projected_or_disclosed_and_port_bound_is_enforced(self):
        result=await self.searcher([self.row('a',content='miss'),self.row('b',kind='binary')],max_records=1).search(
            's','match',limit=1,deadline=asyncio.get_running_loop().time()+1)
        self.assertEqual((result.hits,result.unsupported,result.scanned_records),((),(),1))
        async def unbounded(subject,**kwargs):return [self.row(str(i)) for i in range(102)]
        with self.assertRaises(CapabilityUnavailable):
            await self.searcher([],read=unbounded).search('s','match',limit=1,deadline=asyncio.get_running_loop().time()+1)

    async def test_payload_or_membership_change_without_revision_change_refuses(self):
        for changed in ('payload','membership'):
            calls=[]
            async def read(subject,**kwargs):
                calls.append(True)
                return [self.row('a' if changed=='payload' or len(calls)==1 else 'b',
                    content='match' if len(calls)==1 else 'different')]
            with self.subTest(change=changed),self.assertRaises(CapabilityUnavailable):
                await self.searcher([],read=read).search('s','match',limit=1,deadline=asyncio.get_running_loop().time()+1)
