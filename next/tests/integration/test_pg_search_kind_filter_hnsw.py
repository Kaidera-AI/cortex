"""Supplemental real-index witness; original frozen kind tests stay byte-identical."""
from contextlib import asynccontextmanager
import os
from pathlib import Path
import unittest
import test_pg_search_kind_filter as frozen

class TestHnswCompanion(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        if os.environ.get('NEMO_KIND_FILTER_OWNED_FIXTURE')!='1':
            raise RuntimeError('explicit owned disposable fixture authority required')
        await frozen.existing.SearchTests.asyncSetUp(self)
    asyncTearDown = frozen.existing.SearchTests.asyncTearDown
    seed_kind = frozen.TestNativeKindFilter.seed_kind
    selected = frozen.TestNativeKindFilter.selected
    async def test_native_real_hnsw_index_path_keeps_the_kind_restriction(self):
        # Nontrivial synthetic corpus and explicit index-path fixture: this is
        # one query witness, never real-data ANN recall/benchmark qualification.
        for i in range(256):
            await self.seed_kind('alice','wrong-'+str(i),'memory',[1.0,(i+1)/1000.0]+[0.0]*766)
        await self.seed_kind('alice','wanted-index','decision',[0.8,0.6]+[0.0]*766)
        await self.admin.execute('ANALYZE retrieval.search_vectors; ANALYZE retrieval.search_sources')
        plans=[]; original=self.store.pool
        class ConnectionProxy:
            def __init__(self,conn):self.conn=conn
            def __getattr__(self,name):return getattr(self.conn,name)
            async def execute(self,query,*args):
                value=await self.conn.execute(query,*args)
                if "hnsw.iterative_scan" in query: await self.conn.execute('SET LOCAL enable_seqscan=off; SET LOCAL enable_sort=off; SET LOCAL enable_bitmapscan=off')
                return value
            async def fetch(self,query,*args):
                values=await self.conn.fetch(query,*args)
                if 'FROM retrieval.search_vectors v' in query:
                    plans.append(await self.conn.fetchval('EXPLAIN (ANALYZE,FORMAT JSON) '+query,*args))
                    receipt = os.environ.get('REN_CX_REPORT')
                    if receipt: Path(receipt).with_suffix('.hnsw-plan.json').write_text(plans[-1]+'\n')
                return values
        class PoolProxy:
            @asynccontextmanager
            async def acquire(self,**kwargs):
                async with original.acquire(**kwargs) as conn:yield ConnectionProxy(conn)
        self.store.pool=PoolProxy()
        try:result=await self.selected('decision',1)
        finally:self.store.pool=original
        assert result is not None and [h.record_id for h in result.hits]==['wanted-index']
        assert plans and 'search_vectors_hnsw' in plans[0], 'real product SQL did not traverse the forced HNSW fixture path'

    async def test_native_limit_applies_to_ranked_matching_kinds(self):
        await self.seed_kind('alice','closer-wrong','memory')
        await self.seed_kind('alice','match-first','decision',[0.8,0.6]+[0.0]*766)
        await self.seed_kind('alice','match-second','decision',[0.6,0.8]+[0.0]*766)
        result=await self.selected('decision',1)
        assert result is not None and [h.record_id for h in result.hits]==['match-first']
