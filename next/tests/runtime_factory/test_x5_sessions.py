"""X5 S1 source controls. Scripted X3 suppliers confer no installed authority."""
import asyncio
import importlib
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_x3_secret_resolver import Inputs, REF, INSTALL
from cortex_core.runtime import secret_resolver as secrets


class Cursor:
    def __init__(self, row): self.row = row
    async def fetchone(self): return self.row
    async def fetchall(self): return [self.row] if self.row is not None else []


class Transaction:
    def __init__(self, db): self.db = db
    async def __aenter__(self):
        self.db.info.transaction_status = 2
        self.db.events.append('begin')
        return self
    async def __aexit__(self, kind, value, traceback):
        self.db.events.append('rollback' if kind else 'commit')
        self.db.info.transaction_status = 0
        return False


class AsyncDB:
    def __init__(self):
        from types import SimpleNamespace
        self.closed, self.autocommit = False, True
        self.info = SimpleNamespace(transaction_status=0)
        self.events, self.arguments = [], None
        self.hook = None
    async def execute(self, sql, params=None):
        self.events.append(sql)
        if self.hook: await self.hook(sql)
        if 'auth.resolve_scope' in sql:
            self.arguments = params
            return Cursor((UUID(INSTALL), UUID(int=103), params[2], UUID(int=105), 1, params[3]))
        if "current_setting('cortex.credential_digest'" in sql:
            d, i, p, a = self.arguments
            return Cursor((d, str(i), str(p), a))
        return Cursor((True,) if 'auth.bind_scope' in sql or 'pg_has_role' in sql else (1,))
    def transaction(self): return Transaction(self)
    async def close(self): self.closed = True; self.events.append('closed')


class SQLInputs(Inputs):
    async def connect(self, policy, kwargs, deadline, attempt):
        await super().connect(policy, kwargs, deadline, attempt)
        db = AsyncDB(); self.connections[-1] = db
        return db


class Sessions(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        try: self.module = importlib.import_module('cortex_core.runtime.service_sessions')
        except ModuleNotFoundError as error:
            if error.name != 'cortex_core.runtime.service_sessions': raise
            self.fail('X5_S1_SURFACE_ABSENT: bounded synchronous session bridge')
        self.inputs = SQLInputs(secrets)
        self.patch = patch.object(secrets, '_installed_inputs', return_value=self.inputs)
        self.patch.start(); self.addCleanup(self.patch.stop)
        self.bridge = self.module._SyncSessions(REF, INSTALL)
        self.addAsyncCleanup(self.cleanup_bridge)

    async def cleanup_bridge(self):
        try: await self.bridge.aclose(deadline=self.deadline(.1))
        except self.module.SessionRefusal as error:
            self.assertEqual(error.result, 'HOLD')
            # Test-owned fake resources only; unresolved native ownership stays HOLD.
            for db in self.inputs.connections:
                if not db.closed: await db.close()

    def deadline(self, seconds=1): return asyncio.get_running_loop().time() + seconds

    async def settled(self, predicate):
        for _ in range(100):
            if predicate(): return
            await asyncio.sleep(.005)
        self.fail('owned work did not settle')

    async def test_c04_original_algorithm_and_outer_retirement_before_publication(self):
        from cortex_core.auth import authorized
        def operation(factory):
            with factory() as db:
                with authorized(db, b'fixture-credential', UUID(INSTALL), UUID(int=104)) as scope:
                    self.assertEqual(db.execute('SELECT 1').fetchall(), [(1,)])
                    self.assertEqual(scope.principal_id, UUID(int=105))
                self.assertFalse(db.closed)
                self.assertTrue(db.autocommit)
            return 'private-result'
        self.assertEqual(await self.bridge.run(operation, deadline=self.deadline()), 'private-result')
        db = self.inputs.connections[0]
        self.assertIn('commit', db.events)
        self.assertEqual(db.events[-1], 'closed')
        self.assertEqual(self.bridge.owned_count, 0)

    async def test_rollback_does_not_become_commit_or_retry(self):
        def operation(factory):
            with factory() as db:
                with db.transaction():
                    db.execute('SELECT 1')
                    raise LookupError('fixture-private-error')
        with self.assertRaises(self.module.SessionRefusal) as caught:
            await self.bridge.run(operation, deadline=self.deadline())
        self.assertNotIn('fixture-private-error', str(caught.exception))
        self.assertFalse(caught.exception.commit_known)
        self.assertEqual(self.inputs.connections[0].events.count('rollback'), 1)
        self.assertNotIn('commit', self.inputs.connections[0].events)
        self.assertEqual(self.inputs.events.count('connect'), 1)

    async def test_known_commit_failed_retirement_is_hold_never_retried(self):
        self.inputs.cleanup_fault = True
        def operation(factory):
            with factory() as db:
                with db.transaction(): db.execute('SELECT 1')
            return 'must-not-publish'
        with self.assertRaises(self.module.SessionRefusal) as caught:
            await self.bridge.run(operation, deadline=self.deadline())
        self.assertTrue(caught.exception.commit_known)
        self.assertEqual(caught.exception.result, 'HOLD')
        self.assertEqual(self.bridge.owned_count, 1)
        self.assertEqual(self.inputs.events.count('connect'), 1)
        self.inputs.cleanup_fault = None

    async def test_queue_expiry_never_dispatches_or_connects(self):
        entered, release = threading.Event(), threading.Event()
        def blocked(factory):
            entered.set(); release.wait(1)
        jobs = [asyncio.create_task(self.bridge.run(blocked, deadline=self.deadline(1))) for _ in range(4)]
        await self.settled(entered.is_set)
        with self.assertRaises(self.module.SessionRefusal):
            await self.bridge.run(lambda factory: self.fail('expired work dispatched'), deadline=self.deadline(.02))
        self.assertEqual(self.inputs.events, [])
        release.set(); await asyncio.gather(*jobs)
        self.assertEqual(self.bridge.owned_count, 0)

    async def test_cancelled_native_worker_keeps_capacity_until_worker_is_absent(self):
        entered, release = threading.Event(), threading.Event()
        def blocked(factory):
            entered.set(); release.wait(1)
            return 'late-private-result'
        job = asyncio.create_task(self.bridge.run(blocked, deadline=self.deadline()))
        await self.settled(entered.is_set)
        job.cancel()
        with self.assertRaises(asyncio.CancelledError): await job
        self.assertEqual(self.bridge.owned_count, 1)
        release.set(); await self.settled(lambda: self.bridge.owned_count == 0)

    async def test_cancellation_swallowing_driver_keeps_session_and_no_late_publication(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def swallow(sql):
            entered.set()
            while not release.is_set():
                try: await release.wait()
                except asyncio.CancelledError: pass
        original = self.inputs.connect
        async def connect(*args):
            db = await original(*args); db.hook = swallow; return db
        self.inputs.connect = connect
        def operation(factory):
            with factory() as db: db.execute('SELECT 1')
            return 'late-private-result'
        start = time.monotonic()
        with self.assertRaises(self.module.SessionRefusal):
            await self.bridge.run(operation, deadline=self.deadline(.08))
        self.assertTrue(entered.is_set())
        self.assertLess(time.monotonic() - start, .4)
        self.assertEqual(self.bridge.owned_count, 1)
        self.assertFalse(self.inputs.connections[0].closed)
        release.set(); await asyncio.sleep(.02)
        # Original E has expired: this survivor is explicit until X6/owner cleanup.
        self.assertEqual(self.bridge.owned_count, 1)
        await self.inputs.connections[0].close()  # fixture teardown, not bridge credit

    async def test_revocation_during_driver_await_fences_outer_result(self):
        original = self.inputs.connect
        async def connect(*args):
            db = await original(*args)
            async def revoke(sql): self.inputs.active = False; await asyncio.sleep(0)
            db.hook = revoke; return db
        self.inputs.connect = connect
        def operation(factory):
            with factory() as db: db.execute('SELECT 1')
            return 'revoked-result'
        with self.assertRaises(self.module.SessionRefusal):
            await self.bridge.run(operation, deadline=self.deadline())
        self.assertTrue(self.inputs.connections[0].closed)
        self.assertEqual(self.bridge.owned_count, 0)

    async def test_physical_session_is_not_reused_even_if_caller_leaves_dirty_state(self):
        def operation(factory):
            with factory() as db: db.execute('LISTEN fixture_dirty')
        await self.bridge.run(operation, deadline=self.deadline())
        await self.bridge.run(operation, deadline=self.deadline())
        self.assertEqual(len(self.inputs.connections), 2)
        self.assertTrue(all(db.closed for db in self.inputs.connections))

    async def test_escaped_connection_and_cursor_cannot_be_reused(self):
        escaped = []
        def operation(factory):
            with factory() as db:
                escaped.extend([db, db.execute('SELECT 1')])
        await self.bridge.run(operation, deadline=self.deadline())
        for use in (lambda: escaped[0].execute('SELECT 1'), escaped[1].fetchone):
            with self.assertRaises(self.module.SessionRefusal): await asyncio.to_thread(use)

    async def test_connect_fault_retires_partial_session(self):
        self.inputs.session_fault = {'login_oid': 45}
        def operation(factory):
            with factory(): self.fail('bad session reached C04')
        with self.assertRaises(self.module.SessionRefusal):
            await self.bridge.run(operation, deadline=self.deadline())
        self.assertTrue(self.inputs.connections[0].closed)
        self.assertEqual(self.bridge.owned_count, 0)

    async def test_sync_context_on_owner_loop_refuses_without_io(self):
        with self.assertRaises(self.module.SessionRefusal):
            with self.bridge.connection_factory(): pass
        self.assertEqual(self.inputs.events, [])

    async def test_shutdown_fences_inflight_worker_and_reports_survivor(self):
        entered, release = threading.Event(), threading.Event()
        def blocked(factory): entered.set(); release.wait(1); return 'private'
        job = asyncio.create_task(self.bridge.run(blocked, deadline=self.deadline()))
        await self.settled(entered.is_set)
        with self.assertRaises(self.module.SessionRefusal) as caught:
            await self.bridge.aclose(deadline=self.deadline(.02))
        self.assertEqual(caught.exception.result, 'HOLD')
        self.assertEqual(self.bridge.owned_count, 1)
        release.set()
        with self.assertRaises(self.module.SessionRefusal): await job
        await self.settled(lambda: self.bridge.owned_count == 0)
        with self.assertRaises(self.module.SessionRefusal):
            await self.bridge.run(lambda _: None, deadline=self.deadline())

    async def test_installed_supplier_remains_unbound(self):
        self.patch.stop()
        def operation(factory):
            with factory(): self.fail('unbound connection published')
        with self.assertRaises(self.module.SessionRefusal) as caught:
            await self.bridge.run(operation, deadline=self.deadline())
        self.assertEqual(caught.exception.reason, 'required_binding_gap')
        self.assertEqual(self.bridge.owned_count, 0)

