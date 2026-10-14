"""Probe result freshness, cancellation isolation, real role and query bounds."""
import asyncio
import os
import sys
from pathlib import Path
import unittest
import psycopg
from support import PROBE_ROLE, require_probe
from cortex_core.api.c11a import GatewayError
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core_db_fixture import Fixture, REQUEST, auth_fixture
VERIFIER = auth_fixture.VERIFIER

class ControlTests(unittest.IsolatedAsyncioTestCase):
    async def test_reconnects_after_success_failure_and_non_one_result(self):
        _, make_probe = require_probe(self)
        class Cursor:
            def __init__(self, value): self.value = value
            async def fetchone(self): return self.value
        class Connection:
            def __init__(self, value): self.value = value
            async def execute(self, statement):
                if statement != 'SELECT 1': raise AssertionError('probe changed query')
                return Cursor(self.value)
            async def close(self): pass
        class Factory:
            count = 0
            async def connect(self, **kwargs):
                self.count += 1
                if self.count == 2: raise ConnectionError('public synthetic outage')
                return Connection((0,) if self.count == 4 else (1,))
        factory = Factory()
        probe = make_probe(factory, timeout_seconds=0.02)
        self.assertEqual([await probe() for _ in range(5)], [True, False, True, False, True])
        self.assertEqual(factory.count, 5)

    async def test_query_and_fetch_timeout_close_actual_connection(self):
        _, make_probe = require_probe(self)
        for phase in ('query', 'fetch'):
            with self.subTest(phase=phase):
                finished, closed = asyncio.Event(), asyncio.Event()
                class Cursor:
                    async def fetchone(self):
                        try:
                            if phase == 'fetch': await asyncio.sleep(1)
                            return (1,)
                        finally: finished.set()
                class Connection:
                    async def execute(self, statement):
                        try:
                            if phase == 'query': await asyncio.sleep(1)
                            return Cursor()
                        finally:
                            if phase == 'query': finished.set()
                    async def close(self): closed.set()
                class Factory:
                    async def connect(self, **kwargs): return Connection()
                probe = make_probe(Factory(), timeout_seconds=0.01)
                self.assertIs(await probe(), False)
                await asyncio.wait_for(closed.wait(), 0.1)
                self.assertTrue(finished.is_set())

    async def test_cancelling_one_caller_does_not_cancel_shared_observation(self):
        _, make_probe = require_probe(self)
        entered, release = asyncio.Event(), asyncio.Event()
        self.addCleanup(release.set)
        class Cursor:
            async def fetchone(self): return (1,)
        class Connection:
            async def execute(self, statement):
                entered.set()
                await release.wait()
                return Cursor()
            async def close(self): pass
        class Factory:
            count = 0
            async def connect(self, **kwargs):
                self.count += 1
                return Connection()
        factory = Factory()
        probe = make_probe(factory, timeout_seconds=0.2)
        first, second = asyncio.create_task(probe()), asyncio.create_task(probe())
        await asyncio.wait_for(entered.wait(), 0.1)
        first.cancel()
        with self.assertRaises(asyncio.CancelledError): await first
        release.set()
        self.assertIs(await second, True)
        self.assertEqual(factory.count, 1)

class IsolatedRoleTests(Fixture):
    def test_real_healthy_probe_has_no_request_membership_or_tenant_access(self):
        factory_type, make_probe = require_probe(self)
        async def run():
            factory = factory_type(os.environ['TEST_PROBE_DATABASE_URL'])
            db = await factory.connect(connect_timeout_seconds=0.2, statement_timeout_ms=100)
            try:
                identity = await (await db.execute('SELECT current_user')).fetchone()
                flags = await (await db.execute('SELECT rolsuper, rolbypassrls, rolinherit, rolcreatedb, rolcreaterole, rolreplication FROM pg_roles WHERE rolname=current_user')).fetchone()
                memberships = await (await db.execute('SELECT count(*) FROM pg_auth_members WHERE member=(SELECT oid FROM pg_roles WHERE rolname=current_user)')).fetchone()
                self.assertEqual(identity, (PROBE_ROLE,))
                self.assertEqual(flags, (False,) * 6)
                self.assertEqual(memberships, (0,))
                for sql in ('SELECT * FROM core.records', 'SELECT * FROM auth.credentials', f'SET ROLE "{REQUEST}"', f'SET ROLE "{VERIFIER}"'):
                    with self.subTest(operation=sql.split()[0]), self.assertRaises(psycopg.errors.InsufficientPrivilege):
                        await db.execute(sql)
            finally: await db.close()
            probe = make_probe(factory, timeout_seconds=0.2)
            self.assertIs(await probe(), True)
            close = getattr(probe, 'aclose', None)
            if callable(close): await close(deadline=asyncio.get_running_loop().time() + 0.1)
        asyncio.run(run())
