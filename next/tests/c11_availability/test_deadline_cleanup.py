"""Vera PR #113 rework: real-factory subsecond and slow-close contracts."""
import asyncio
import logging
import os
import unittest
from unittest.mock import patch
import psycopg
from cortex_core.api.c11a import GatewayError
from support import require_probe

class SubsecondAndCloseTests(unittest.TestCase):

    def test_real_factory_connects_with_subsecond_budget(self):
        PostgresProbeFactory, postgres_core_probe = require_probe(self)
        factory = PostgresProbeFactory(os.environ['TEST_PROBE_DATABASE_URL'])

        async def run():
            try:
                connection = await factory.connect(connect_timeout_seconds=0.5, statement_timeout_ms=30)
            except GatewayError as error:
                self.fail(f'accepted subsecond Core factory budget was rejected: {error}')
            try:
                return await (await connection.execute('SHOW statement_timeout')).fetchone()
            finally:
                await connection.close()
        self.assertEqual(asyncio.run(run()), ('30ms',))

    def test_real_factory_async_connect_is_cancelled_at_outer_deadline(self):
        PostgresProbeFactory, postgres_core_probe = require_probe(self)
        started = []
        finished = []
        passed = []

        async def delayed_connect(*args, **kwargs):
            started.append(1)
            passed.append(kwargs)
            try:
                await asyncio.sleep(0.2)
                raise AssertionError('slow connect escaped the outer deadline')
            finally:
                finished.append(1)

        async def run():
            factory = PostgresProbeFactory(os.environ['TEST_PROBE_DATABASE_URL'])
            probe = postgres_core_probe(factory, timeout_seconds=0.01)
            with patch.object(psycopg.AsyncConnection, 'connect', delayed_connect):
                began = asyncio.get_running_loop().time()
                value = await probe()
                await asyncio.sleep(0)
                elapsed = asyncio.get_running_loop().time() - began
            return (value, elapsed)
        value, elapsed = asyncio.run(run())
        self.assertIs(value, False)
        self.assertLess(elapsed, 0.05)
        self.assertEqual((len(started), len(finished)), (1, 1), 'async libpq connect must be cancelled and finished')
        self.assertEqual(passed[0]['connect_timeout'], 1)
        self.assertEqual(passed[0]['options'], '-c statement_timeout=10')

    def test_psycopg_stalled_handshake_closes_after_probe_deadline(self):
        PostgresProbeFactory, postgres_core_probe = require_probe(self)

        async def run():
            accepted = asyncio.Event()
            disconnected = asyncio.Event()

            async def stalled_peer(reader, writer):
                accepted.set()
                try:
                    while await reader.read(1024):
                        pass
                finally:
                    disconnected.set()
                    writer.close()
                    await writer.wait_closed()
            server = await asyncio.start_server(stalled_peer, '127.0.0.1', 0)
            try:
                port = server.sockets[0].getsockname()[1]
                factory = PostgresProbeFactory(f'postgresql://postgres@127.0.0.1:{port}/postgres')
                probe = postgres_core_probe(factory, timeout_seconds=0.1)
                began = asyncio.get_running_loop().time()
                value = await probe()
                elapsed = asyncio.get_running_loop().time() - began
                try:
                    await asyncio.wait_for(accepted.wait(), 0.2)
                    await asyncio.wait_for(disconnected.wait(), 0.2)
                except TimeoutError as error:
                    self.fail(f'async psycopg connect lingered after cancellation: {error}')
                return (value, elapsed)
            finally:
                server.close()
                await server.wait_closed()
        value, elapsed = asyncio.run(run())
        self.assertIs(value, False)
        self.assertLess(elapsed, 0.2)

    def test_held_close_cannot_keep_success_inside_stale_flight(self):
        PostgresProbeFactory, postgres_core_probe = require_probe(self)

        class Cursor:

            async def fetchone(self):
                return (1,)

        class Connection:

            def __init__(self, owner):
                self.owner = owner

            async def execute(self, statement):
                assert statement == 'SELECT 1'
                return Cursor()

            async def close(self):
                self.owner.close_started += 1
                try:
                    await self.owner.release.wait()
                except asyncio.CancelledError:
                    self.owner.close_cancelled += 1
                    raise

        class SlowCloseFactory:

            def __init__(self):
                self.connect_started = 0
                self.close_started = 0
                self.close_cancelled = 0
                self.release = None

            async def connect(self, *, connect_timeout_seconds, statement_timeout_ms):
                self.connect_started += 1
                return Connection(self)
        factory = SlowCloseFactory()

        class Capture(logging.Handler):

            def __init__(self):
                super().__init__()
                self.messages = []

            def emit(self, record):
                self.messages.append(record.getMessage())
        capture = Capture()
        logger = logging.getLogger('cortex_core.api.c11a')
        logger.addHandler(capture)
        try:

            async def run():
                factory.release = asyncio.Event()
                probe = postgres_core_probe(factory, timeout_seconds=0.03)
                first = await probe()
                second = await probe()
                await asyncio.sleep(0.08)
                return (first, second, factory.connect_started, factory.close_cancelled)
            first, second, connections, cancelled = asyncio.run(run())
        finally:
            logger.removeHandler(capture)
        self.assertEqual((first, second), (True, True), 'query success must publish before slow cleanup')
        self.assertEqual(connections, 2, 'new probe must not reuse a close-held flight')
        self.assertEqual(cancelled, 2, 'both held closes must be cancelled on deadline')
        self.assertGreaterEqual(sum(('close timed out' in x for x in capture.messages)), 2)
if __name__ == '__main__':
    unittest.main()
