"""PROBE-BOUND-001: concurrent slow-Core probes have one bounded operation."""

import asyncio
import os
import time
import unittest

import psycopg

from cortex_core.api.c11a import PostgresProbeFactory, postgres_core_probe


class BlockingFactory:
    """Old callable blocks threads; new async connect honors cancellation."""

    def __init__(self):
        self.started = 0
        self.finished = 0
        self.deadlines = []

    def __call__(self):
        self.started += 1
        try:
            time.sleep(0.15)
            raise TimeoutError('synthetic blocked connect')
        finally:
            self.finished += 1

    async def connect(self, *, connect_timeout_seconds, statement_timeout_ms):
        self.deadlines.append((connect_timeout_seconds, statement_timeout_ms))
        self.started += 1
        try:
            await asyncio.sleep(0.15)
            raise TimeoutError('synthetic blocked async connect')
        finally:
            self.finished += 1


class ProbeBoundTests(unittest.TestCase):
    def test_real_pg_factory_enforces_connect_and_statement_deadlines(self):
        factory = PostgresProbeFactory(os.environ['TEST_DATABASE_URL'])

        async def run():
            connection = await factory.connect(connect_timeout_seconds=1,
                                               statement_timeout_ms=30)
            try:
                setting = await (await connection.execute('SHOW statement_timeout')).fetchone()
                started = asyncio.get_running_loop().time()
                with self.assertRaises(psycopg.errors.QueryCanceled):
                    await connection.execute('SELECT pg_sleep(0.2)')
                elapsed = asyncio.get_running_loop().time() - started
                return setting, elapsed
            finally:
                await connection.close()

        setting, elapsed = asyncio.run(run())
        self.assertEqual(setting, ('30ms',))
        self.assertLess(elapsed, 0.15, 'server query deadline must interrupt pg_sleep')

    def test_twenty_slow_callers_share_one_deadline_and_leave_no_thread(self):
        factory = BlockingFactory()
        probe = postgres_core_probe(factory, timeout_seconds=0.01)

        async def run():
            start = asyncio.get_running_loop().time()
            values = await asyncio.gather(*(probe() for _ in range(20)))
            return values, asyncio.get_running_loop().time() - start, (
                factory.started, factory.finished, tuple(factory.deadlines))

        values, elapsed, (started, finished, deadlines) = asyncio.run(run())
        self.assertEqual(values, [False] * 20)
        self.assertLess(elapsed, 0.05, 'all callers must fail within the 10 ms deadline')
        self.assertLessEqual(started, 1, 'single flight must start at most one factory')
        self.assertEqual((started, finished), (1, 1),
                         'no factory operation may linger after its deadline')
        self.assertEqual(len(deadlines), 1)
        connect_seconds, statement_ms = deadlines[0]
        self.assertGreater(connect_seconds, 0)
        self.assertLessEqual(connect_seconds, 0.01)
        self.assertGreater(statement_ms, 0)
        self.assertLessEqual(statement_ms, 10)
