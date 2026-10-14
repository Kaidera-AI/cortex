"""Frozen synthetic exception canaries: no traceback/DSN/value reaches logs."""
import asyncio
import logging
import unittest
from support import require_probe

SENTINEL = 'SYNTHETIC_RC1_SECRET_DO_NOT_LOG'

class Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []
    def emit(self, record):
        self.lines.append(logging.Formatter().format(record))

class PrivacyTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, phase):
        _, make_probe = require_probe(self)
        class Cursor:
            async def fetchone(self):
                if phase == 'fetch': raise RuntimeError(SENTINEL)
                return (1,)
        class Connection:
            async def execute(self, statement):
                if phase == 'query': raise RuntimeError(SENTINEL)
                return Cursor()
            async def close(self):
                if phase == 'close': raise RuntimeError(SENTINEL)
        class Factory:
            async def connect(self, **kwargs):
                if phase == 'connect': raise RuntimeError(SENTINEL)
                return Connection()
        capture = Capture()
        logger = logging.getLogger('cortex_core.api.c11a')
        logger.addHandler(capture)
        try:
            probe = make_probe(Factory(), timeout_seconds=0.02)
            result = await probe()
            await asyncio.sleep(0.06)
            close = getattr(probe, 'aclose', None)
            if callable(close): await close(deadline=asyncio.get_running_loop().time() + 0.1)
        finally:
            logger.removeHandler(capture)
        return result, capture.lines

    async def test_close_exception_never_reaches_formatted_log(self):
        result, lines = await self.exercise('close')
        self.assertIs(result, True)
        self.assertTrue(lines, 'close failure must retain fixed operational telemetry')
        self.assertNotIn(SENTINEL, '\n'.join(lines))
        self.assertFalse(any('Traceback' in line for line in lines))
        self.assertEqual(lines, ['Core probe connection close failed'])

    async def test_connect_query_fetch_canaries_refuse_without_logging(self):
        for phase in ('connect', 'query', 'fetch'):
            with self.subTest(phase=phase):
                result, lines = await self.exercise(phase)
                self.assertIs(result, False)
                self.assertNotIn(SENTINEL, '\n'.join(lines))
                self.assertEqual(lines, [])
