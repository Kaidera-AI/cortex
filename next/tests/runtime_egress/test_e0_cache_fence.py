"""Exercise the actual cache algorithm with synthetic storage/effect seams."""
import asyncio
import unittest

from cortex_core.embeddings.query_cache import QueryEmbeddingCache
from cortex_core.embeddings.pg_search import CapabilityUnavailable
from test_e0 import surface, admitted_ai, IDENTITY, SemanticRed


class CachePublicationOracle(unittest.IsolatedAsyncioTestCase):
    async def test_off_blocks_late_cache_publication_and_release_work(self):
        gate = surface('cortex_core.runtime.selection', 'SelectionGate')(admitted_ai())
        cache = object.__new__(QueryEmbeddingCache)
        cache.wait_seconds = 12
        entered, release = asyncio.Event(), asyncio.Event()
        effects = []
        async def lookup(*args):
            return ('synthetic-key', None, 1)
        async def embed(*args):
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()  # subordinate returns despite cancellation
            return [1.0]+[0.0]*767
        async def publish(*args):
            effects.append(('publish', gate.snapshot.mode))
            return True
        async def abandon(*args):
            effects.append(('release', gate.snapshot.mode))
        cache._lookup_claim, cache.embed = lookup, embed
        cache._publish, cache._release = publish, abandon
        async def request(deadline):
            return await cache.get('subject', IDENTITY, 'synthetic query', deadline=deadline)
        replies = []
        async def run():
            replies.append(await gate.run(request, revision=gate.snapshot.revision,
                                         deadline=asyncio.get_running_loop().time()+1))
        task = asyncio.create_task(run())
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
                raise SemanticRed('cache/OFF race exceeded bounded oracle') from None
            self.assertEqual(replies, [])
            self.assertFalse(any(mode=='off' for operation,mode in effects), effects)
        finally:
            release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
