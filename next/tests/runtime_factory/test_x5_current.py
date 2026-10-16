"""Additional body REDs found during S1 review, frozen before their fixes."""
import asyncio
from collections import Counter
import threading
import time
import unittest
from unittest.mock import patch

from test_x5_sessions import SQLInputs, Transaction, REF, INSTALL
from cortex_core.runtime import secret_resolver as secrets, factory, service_sessions


class Currentness(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.inputs = SQLInputs(secrets)
        self.patch = patch.object(secrets, '_installed_inputs', return_value=self.inputs)
        self.patch.start(); self.addCleanup(self.patch.stop)
        self.bridge = service_sessions._SyncSessions(REF, INSTALL)
        self.addAsyncCleanup(self.bridge.aclose, deadline=asyncio.get_running_loop().time()+2)

    async def test_revocation_inside_transaction_prevents_next_driver_dispatch(self):
        original = self.inputs.connect
        async def connect(*args):
            db = await original(*args)
            async def revoke(sql):
                if sql == 'SELECT 1': self.inputs.active = False
            db.hook = revoke
            return db
        self.inputs.connect = connect
        def operation(connection_factory):
            with connection_factory() as db:
                with db.transaction():
                    db.execute('SELECT 1')
                    db.execute('MUST_NOT_EXECUTE')
        with self.assertRaises(service_sessions.SessionRefusal):
            await self.bridge.run(operation, deadline=asyncio.get_running_loop().time()+1)
        self.assertNotIn('MUST_NOT_EXECUTE', self.inputs.connections[0].events)
        self.assertNotIn('commit', self.inputs.connections[0].events)

    async def test_revocation_at_commit_prevents_post_commit_sql_but_preserves_commit(self):
        original = self.inputs.connect
        async def connect(*args):
            db = await original(*args)
            inputs = self.inputs
            class RevokeAtCommit(Transaction):
                async def __aexit__(self, *args):
                    result = await super().__aexit__(*args)
                    inputs.active = False
                    return result
            db.transaction = lambda: RevokeAtCommit(db)
            return db
        self.inputs.connect = connect
        def operation(connection_factory):
            with connection_factory() as db:
                with db.transaction(): db.execute('SELECT 1')
                db.execute('MUST_NOT_EXECUTE')
        with self.assertRaises(service_sessions.SessionRefusal) as caught:
            await self.bridge.run(operation, deadline=asyncio.get_running_loop().time()+1)
        self.assertTrue(caught.exception.commit_known)
        self.assertNotIn('MUST_NOT_EXECUTE', self.inputs.connections[0].events)

    async def test_caller_cancellation_reconciles_session_under_original_remaining_E(self):
        entered, release = threading.Event(), threading.Event()
        def operation(connection_factory):
            with connection_factory() as db:
                db.execute('SELECT 1'); entered.set(); release.wait(1)
        task = asyncio.create_task(self.bridge.run(operation, deadline=asyncio.get_running_loop().time()+1))
        for _ in range(100):
            if entered.is_set(): break
            await asyncio.sleep(.005)
        self.assertTrue(entered.is_set())
        task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        self.assertEqual(self.bridge.owned_count, 1)
        release.set()
        for _ in range(100):
            if not self.bridge.owned_count: break
            await asyncio.sleep(.005)
        self.assertEqual(self.bridge.owned_count, 0)
        self.assertTrue(self.inputs.connections[0].closed)

    async def test_route_gap_categories_preserve_accepted_full_inventory(self):
        rows = factory.route_inventory()
        self.assertEqual(Counter(row.binding_gap for row in rows), {
            'X6_SEPARATE_GUARD':1, 'NAMED_JOIN_REQUIRED':14,
            'READ_POSITIVE_GAP':34, 'WRITE_POSITIVE_GAP':22,
            'OWNER_TRANSITION_GAP':8, 'MEDIA_REFUSAL_PROPOSED':2,
            'CONTRACT_UNAVAILABLE':3, 'STREAM_CONTRACT_GAP':2, 'RETIRED_410':2})
        categories = {row.route_id:row.binding_gap for row in rows}
        self.assertEqual(categories['C01-R015'], 'WRITE_POSITIVE_GAP')
        self.assertEqual(categories['C01-R082'], 'READ_POSITIVE_GAP')

    async def test_generation_change_during_outer_retirement_cannot_publish(self):
        original = self.inputs.retire
        async def changed(*args):
            absent = await original(*args)
            self.inputs.generation += 1
            return absent
        self.inputs.retire = changed
        def operation(connection_factory):
            with connection_factory() as db: db.execute('SELECT 1')
            return 'stale-generation-result'
        with self.assertRaises(service_sessions.SessionRefusal):
            await self.bridge.run(operation, deadline=asyncio.get_running_loop().time()+1)
        self.assertTrue(self.inputs.connections[0].closed)

    async def test_driver_statement_budget_uses_remaining_original_E(self):
        original = self.inputs.connect
        observations = []
        loop = asyncio.get_running_loop(); deadline = loop.time()+.3
        async def connect(*args):
            db = await original(*args)
            execute = db.execute
            async def recorded(sql, params=None):
                if 'statement_timeout' in sql:
                    observations.append((int(params[0]), (deadline-loop.time())*1000))
                return await execute(sql, params)
            db.execute = recorded
            return db
        self.inputs.connect = connect
        def operation(connection_factory):
            with connection_factory() as db:
                time.sleep(.03)
                db.execute('SELECT 1')
        await self.bridge.run(operation, deadline=deadline)
        self.assertTrue(observations, 'remaining-E driver statement cap absent')
        self.assertTrue(all(0 < budget <= min(2000, remaining+5) for budget, remaining in observations))
