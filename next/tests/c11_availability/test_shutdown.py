"""Explicit successful shutdown is quiescent; incomplete drain refuses."""
import asyncio
import unittest
from cortex_core.api.c11a import GatewayError
from support import require_probe, require_shutdown

class ShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_shutdown_cancels_held_close_and_blocks_new_admissions(self):
        _, make_probe = require_probe(self)
        entered, finished, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        self.addCleanup(release.set)
        class Cursor:
            async def fetchone(self): return (1,)
        class Connection:
            async def execute(self, statement): return Cursor()
            async def close(self):
                entered.set()
                try: await release.wait()
                finally: finished.set()
        class Factory:
            count = 0
            async def connect(self, **kwargs):
                self.count += 1
                return Connection()
        factory = Factory()
        probe = make_probe(factory, timeout_seconds=1)
        close = require_shutdown(self, probe)
        baseline = asyncio.all_tasks()
        self.assertIs(await probe(), True)
        await asyncio.wait_for(entered.wait(), 0.1)
        started = asyncio.get_running_loop().time()
        await close(deadline=started + 0.1)
        self.assertLess(asyncio.get_running_loop().time() - started, 0.15)
        self.assertTrue(finished.is_set())
        self.assertEqual(asyncio.all_tasks() - baseline, set())
        self.assertIs(await probe(), False)
        self.assertEqual(factory.count, 1)
        await close(deadline=asyncio.get_running_loop().time() + 0.1)

    async def test_shutdown_cancels_connect_and_drains_all_waiters(self):
        _, make_probe = require_probe(self)
        entered, finished, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        self.addCleanup(release.set)
        class Factory:
            count = 0
            async def connect(self, **kwargs):
                self.count += 1
                entered.set()
                try: await release.wait()
                finally: finished.set()
                raise AssertionError('a cancelled connect cannot publish healthy')
        factory = Factory()
        probe = make_probe(factory, timeout_seconds=1)
        close = require_shutdown(self, probe)
        baseline = asyncio.all_tasks()
        callers = [asyncio.create_task(probe()) for _ in range(20)]
        await asyncio.wait_for(entered.wait(), 0.1)
        await close(deadline=asyncio.get_running_loop().time() + 0.1)
        self.assertEqual(await asyncio.gather(*callers), [False] * 20)
        self.assertTrue(finished.is_set())
        self.assertEqual(factory.count, 1)
        self.assertEqual(asyncio.all_tasks() - baseline, set())

    async def test_cancellation_resistant_close_refuses_then_can_be_drained(self):
        _, make_probe = require_probe(self)
        entered, cancelled, finished, release = (asyncio.Event() for _ in range(4))
        self.addCleanup(release.set)
        class Cursor:
            async def fetchone(self): return (1,)
        class Connection:
            async def execute(self, statement): return Cursor()
            async def close(self):
                entered.set()
                try:
                    try: await release.wait()
                    except asyncio.CancelledError:
                        cancelled.set()
                        await release.wait()
                finally: finished.set()
        class Factory:
            async def connect(self, **kwargs): return Connection()
        probe = make_probe(Factory(), timeout_seconds=1)
        close = require_shutdown(self, probe)
        baseline = asyncio.all_tasks()
        self.assertIs(await probe(), True)
        await asyncio.wait_for(entered.wait(), 0.1)
        with self.assertRaises(GatewayError):
            await close(deadline=asyncio.get_running_loop().time() + 0.02)
        self.assertTrue(cancelled.is_set())
        self.assertFalse(finished.is_set(), 'incomplete drain cannot report success')
        self.assertIs(await probe(), False)
        release.set()
        await asyncio.wait_for(finished.wait(), 0.1)
        await close(deadline=asyncio.get_running_loop().time() + 0.1)
        self.assertEqual(asyncio.all_tasks() - baseline, set())

    async def test_shutdown_requires_finite_absolute_deadline(self):
        _, make_probe = require_probe(self)
        class Factory:
            async def connect(self, **kwargs): raise ConnectionError('public synthetic')
        probe = make_probe(Factory(), timeout_seconds=0.01)
        close = require_shutdown(self, probe)
        for deadline in (True, None, float('inf'), float('nan')):
            with self.subTest(kind=type(deadline).__name__), self.assertRaises(GatewayError):
                await close(deadline=deadline)
        await close(deadline=asyncio.get_running_loop().time() + 0.1)
