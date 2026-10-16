"""An absent SQL session does not prove an admitted native worker absent."""
import asyncio
import threading
import unittest

from test_x5_sessions import REF, INSTALL
from cortex_core.runtime import service_sessions


class WorkerHold(unittest.IsolatedAsyncioTestCase):
    async def test_timed_out_worker_without_sql_session_reports_hold_until_absent(self):
        bridge = service_sessions._SyncSessions(REF, INSTALL)
        entered, release = threading.Event(), threading.Event()
        loop = asyncio.get_running_loop()

        def operation(_):
            entered.set()
            release.wait(1)
            return 'late-private-result'

        try:
            with self.assertRaises(service_sessions.SessionRefusal) as caught:
                await bridge.run(operation, deadline=loop.time()+.08)
            self.assertTrue(entered.is_set())
            self.assertEqual(bridge.owned_count, 1)
            self.assertEqual(caught.exception.result, 'HOLD')
        finally:
            release.set()
            for _ in range(100):
                if bridge.owned_count == 0: break
                await asyncio.sleep(.005)
            await bridge.aclose(deadline=loop.time()+.1)
        self.assertEqual(bridge.owned_count, 0)
