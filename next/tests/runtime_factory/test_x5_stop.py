"""Stop/lifetime residuals: no new cleanup budget and no stale health result."""
import asyncio
import threading
import unittest
from unittest.mock import patch
from uuid import UUID

from test_x5_sessions import SQLInputs, REF, INSTALL
from cortex_core.runtime import secret_resolver as secrets, factory, service_sessions


class Stop(unittest.IsolatedAsyncioTestCase):
    async def test_stop_D_prevents_later_reconciliation_after_detached_worker_exits(self):
        inputs = SQLInputs(secrets)
        entered, release = threading.Event(), threading.Event()
        with patch.object(secrets, '_installed_inputs', return_value=inputs):
            bridge = service_sessions._SyncSessions(REF, INSTALL)
            def operation(connection_factory):
                with connection_factory() as db:
                    db.execute('SELECT 1'); entered.set(); release.wait(1)
            job = asyncio.create_task(bridge.run(operation, deadline=asyncio.get_running_loop().time()+1))
            for _ in range(100):
                if entered.is_set(): break
                await asyncio.sleep(.005)
            self.assertTrue(entered.is_set())
            try:
                with self.assertRaises(service_sessions.SessionRefusal):
                    await bridge.aclose(deadline=asyncio.get_running_loop().time()+.01)
                release.set()
                with self.assertRaises(service_sessions.SessionRefusal): await job
                await asyncio.sleep(.02)
                self.assertFalse(inputs.connections[0].closed, 'new retirement dispatched after stop D')
                self.assertEqual(bridge.owned_count, 1)
            finally:
                release.set()
                await inputs.connections[0].close()  # fixture teardown, no absence credit

    async def test_executor_dispatch_failure_returns_explicit_hold_with_owned_ticket(self):
        bridge = service_sessions._SyncSessions(REF, INSTALL)
        loop = asyncio.get_running_loop()
        with patch.object(loop, 'run_in_executor', side_effect=OSError('private executor failure')):
            with self.assertRaises(service_sessions.SessionRefusal) as caught:
                await bridge.run(lambda _: self.fail('failed dispatch ran callback'), deadline=loop.time()+1)
        self.assertNotIn('private executor failure', str(caught.exception))
        self.assertEqual(caught.exception.result, 'HOLD')
        self.assertEqual(bridge.owned_count, 1)
        with self.assertRaises(service_sessions.SessionRefusal):
            await bridge.aclose(deadline=loop.time()+.01)

    async def test_supervisor_loss_during_enrollment_await_fences_readiness(self):
        state = 'healthy'
        async def core(): return {'core_available':True, 'state':state}
        async def enrollment(_):
            nonlocal state
            state = 'supervisor_down'
            await asyncio.sleep(0)
            return 'ready'
        result = await factory._installed_health(core, enrollment, UUID(INSTALL),
            deadline=asyncio.get_running_loop().time()+1)
        self.assertEqual(result['readiness'], 'unavailable')
