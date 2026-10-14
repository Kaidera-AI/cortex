"""Constructor and request-race joins; frozen before E0 policy/proposal files."""
import asyncio
from types import SimpleNamespace as NS
import unittest

from cortex_core.api.c11b2 import C11b2ReadPort
from cortex_core.embeddings.pg_search import CapabilityUnavailable
from test_e0 import (admitted_ai, read_port, require_parameter, surface, SemanticRed)


class OffJoinOracle(unittest.IsolatedAsyncioTestCase):
    async def test_reusing_identical_hosted_bytes_never_revalidates_old_work(self):
        cls = surface('cortex_core.runtime.selection', 'SelectionGate')
        original = admitted_ai()
        gate = cls(original)
        old = gate.snapshot.revision
        await gate.apply(admitted_ai('off'))
        try:
            await gate.apply(original)
        except CapabilityUnavailable:
            pass  # rejecting a reused owner/config revision is also safe
        calls = []
        async def old_work(deadline):
            calls.append(True)
        with self.assertRaises(CapabilityUnavailable):
            await gate.run(old_work, revision=old,
                           deadline=asyncio.get_running_loop().time()+1)
        self.assertEqual(calls, [])

    async def test_off_constructor_has_no_fake_embedding_or_provider_dependency(self):
        require_parameter(C11b2ReadPort, 'selection')
        require_parameter(C11b2ReadPort, 'keyword')
        gate = surface('cortex_core.runtime.selection', 'SelectionGate')(admitted_ai('off'))
        obj = C11b2ReadPort(NS(projects={}), None, None, None, 'local-fact-generation', {},
                          selection=gate, keyword=NS(search=lambda: None))
        self.assertIsNone(obj.identity)
        self.assertIsNone(obj.provider)

    async def test_off_during_warm_cache_await_drops_result_before_vector_search(self):
        require_parameter(C11b2ReadPort, 'selection')
        obj = read_port()
        gate = surface('cortex_core.runtime.selection', 'SelectionGate')(admitted_ai())
        obj.selection = gate
        entered, release = asyncio.Event(), asyncio.Event()
        class WarmCache:
            async def get(self, *args, **kwargs):
                entered.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    await release.wait()  # deliberate late result after cancellation
                return NS(vector=tuple([1.0]+[0.0]*767))
        obj._cache = lambda search: WarmCache()
        replies = []
        async def request():
            replies.append(await obj.search({'principal_id':'synthetic'}, {'method':'POST',
                '_c11b_body':{'query':'known text','rerank':False},
                '_cortex_deadline':asyncio.get_running_loop().time()+1}, None))
        task = asyncio.create_task(request())
        try:
            try:
                await asyncio.wait_for(entered.wait(), .2)
                await asyncio.wait_for(gate.apply(admitted_ai('off')), .2)
                release.set()
                try:
                    await asyncio.wait_for(task, .2)
                except (CapabilityUnavailable, asyncio.CancelledError):
                    pass
            except TimeoutError:
                raise SemanticRed('OFF transition/request exceeded bounded race oracle') from None
            self.assertEqual(replies, [])
            self.assertFalse(any(row[0]=='vector' for row in obj.calls))
        finally:
            release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
