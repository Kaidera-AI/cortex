"""E0-DEADLINE-002: deterministic elapsed-time controls, no wall-clock sleep."""
import asyncio
from contextlib import contextmanager
from types import SimpleNamespace as NS
from unittest.mock import patch
import unittest

import test_e0 as original
from cortex_core.embeddings.pg_search import CapabilityUnavailable


@contextmanager
def virtual_clock():
    loop=asyncio.get_running_loop()
    clock=[loop.time()]
    debug=loop.get_debug()
    loop.set_debug(False)
    try:
        with patch.object(loop,'time',lambda:clock[0]):
            yield clock
    finally:
        loop.set_debug(debug)


class GateExpiryOracle(unittest.IsolatedAsyncioTestCase):
    async def _after_callback(self, budget):
        cls=original.surface('cortex_core.runtime.selection','SelectionGate')
        gate=cls(original.admitted_ai())
        published=[]
        with virtual_clock() as clock:
            start=clock[0]
            requested=start+budget
            observed=[]
            async def work(deadline):
                observed.append(deadline)
                await asyncio.sleep(0)
                clock[0]=deadline+1
                return 'late-sensitive-result'
            try:
                published.append(await gate.run(work,revision=gate.snapshot.revision,
                                                deadline=requested))
            except CapabilityUnavailable:
                pass
            self.assertEqual(len(observed),1,'valid work refused before callback')
            self.assertGreater(observed[0],start)
            self.assertLessEqual(observed[0],min(requested,start+10))
            self.assertEqual(published,[],'expired callback result escaped selection gate')

    async def test_post_callback_expiry_uses_original_short_deadline(self):
        await self._after_callback(.25)

    async def test_post_callback_expiry_uses_clamped_ten_second_deadline(self):
        await self._after_callback(100)


class ReadExpiryOracle(unittest.IsolatedAsyncioTestCase):
    async def _during(self, stage):
        obj=original.read_port()
        events=[]
        published=[]
        with virtual_clock() as clock:
            deadline=clock[0]+.25
            async def boundary(name):
                events.append(name)
                await asyncio.sleep(0)
                if name==stage:
                    clock[0]=deadline+1
            class Cache:
                async def get(self,*args,**kwargs):
                    await boundary('provider' if stage=='provider' else 'cache')
                    return NS(vector=tuple([1.0]+[0.0]*767))
            search,graph=obj._ports(None)
            search_method=search.search
            freshness=obj._post_read_freshness
            current=obj._current
            async def read(*args,**kwargs):
                await boundary('vector')
                return await search_method(*args,**kwargs)
            async def fresh(*args,**kwargs):
                await boundary('freshness')
                return await freshness(*args,**kwargs)
            async def auth(*args,**kwargs):
                await boundary('auth')
                return await current(*args,**kwargs)
            search.search=read
            obj._ports=lambda principal:(search,graph)
            obj._cache=lambda search:Cache()
            obj._post_read_freshness=fresh
            obj._current=auth
            try:
                published.append(await obj.search({'principal_id':'synthetic'},
                    {'method':'POST','_c11b_body':{'query':'known text','rerank':False},
                     '_cortex_deadline':deadline},None))
            except CapabilityUnavailable:
                pass
            self.assertIn(stage,events,'valid request refused before expiring await')
            self.assertEqual(published,[],'expired read returned successful response')
            self.assertEqual(events[events.index(stage)+1:],[],
                             'expired await dispatched another stage before refusal')

    async def test_cache_await_expiry_drops_vector_before_dispatch(self):
        await self._during('cache')

    async def test_provider_await_expiry_drops_result_before_vector_dispatch(self):
        await self._during('provider')

    async def test_vector_await_expiry_drops_hits_before_freshness(self):
        await self._during('vector')

    async def test_freshness_await_expiry_stops_final_auth_dispatch(self):
        await self._during('freshness')

    async def test_final_auth_await_expiry_cannot_publish_success(self):
        await self._during('auth')
