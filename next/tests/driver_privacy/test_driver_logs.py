"""New frozen oracle: exact pinned methods, inert I/O, synthetic data only."""
import ast
import asyncio
import hashlib
import importlib
import logging
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

from cortex_core.api import c11a

SECRET = 'COX_PUBLIC_SYNTHETIC_RAW_EXCEPTION'
PASSWORD = 'COX_PUBLIC_SYNTHETIC_PASSWORD'
HOST = 'cox-public-synthetic-host'
DSN = f'postgresql://public:{PASSWORD}@{HOST}/public'
RAW = f'{SECRET} password={PASSWORD} host={HOST} dsn={DSN}'
FIXTURE = Path(__file__).with_name('fixtures') / 'psycopg-3.2.9-connection_async.py'
SOURCE = FIXTURE.read_bytes()
PIN = '88a0216cb2adae48d8b739e35d29e3426dc049e70a05099c6d754c7d98dd79f7'
assert hashlib.sha256(SOURCE).hexdigest() == PIN
OWNER = next(n for n in ast.parse(SOURCE).body
             if isinstance(n, ast.ClassDef) and n.name == 'AsyncConnection')
METHODS = [n for n in OWNER.body if isinstance(n, ast.AsyncFunctionDef)
           and n.name in {'connect', 'execute', 'wait', '_try_cancel', 'close'}]
assert len(METHODS) == 5
BINDINGS = {n.name: hashlib.sha256(ast.dump(n, include_attributes=False).encode()).hexdigest()
            for n in METHODS}
PROJECTION = ast.Module(body=[ast.ImportFrom(module='__future__',
    names=[ast.alias(name='annotations')], level=0),
    ast.ClassDef(name='AsyncConnection', bases=[], keywords=[], body=METHODS,
                 decorator_list=[])], type_ignores=[])
ast.fix_missing_locations(PROJECTION)
OBSERVATIONS = {}


class DriverError(Exception):
    pass


class QueryCanceled(DriverError):
    pass


class Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.rows = []

    def emit(self, record):
        self.rows.append({'logger': record.name, 'level': record.levelno,
            'message': record.getMessage(), 'formatted': logging.Formatter().format(record),
            'args': repr(record.args), 'exc_info': record.exc_info is not None,
            'exc_text': record.exc_text, 'stack_info': record.stack_info})


def driver_projection(mode):
    finished = []
    async def attempts(params):
        return [params]

    async def wait_conn(generator, **kwargs):
        if mode == 'connect':
            raise DriverError(RAW)
        return generator

    async def wait_query(generator, socket, **kwargs):
        if not generator['interrupted']:
            generator['interrupted'] = True
            await asyncio.Event().wait()
        raise QueryCanceled('public-synthetic-query-cancelled')

    namespace = {'sys': sys, 'asyncio': asyncio,
        'e': types.SimpleNamespace(Error=DriverError, QueryCanceled=QueryCanceled,
                                  _NO_TRACEBACK=DriverError),
        'waiting': types.SimpleNamespace(wait_conn_async=wait_conn, wait_async=wait_query),
        'logger': logging.getLogger('psycopg'), '_WAIT_INTERVAL': 0.1,
        '_INTERRUPTED': (asyncio.CancelledError, KeyboardInterrupt), 'ACTIVE': 1,
        'timeout_from_conninfo': lambda params: params['connect_timeout'],
        'conninfo_attempts_async': attempts, 'make_conninfo': lambda *a, **k: DSN}
    exec(compile(PROJECTION, str(FIXTURE), 'exec'), namespace)
    cls = namespace['AsyncConnection']
    async def params(cls, conninfo, **kwargs):
        return dict(kwargs, host=HOST, port='5432', hostaddr=HOST)

    def connect_gen(cls, conninfo, timeout):
        conn = cls()
        conn._closed = False
        conn.pgconn = types.SimpleNamespace(socket=0, transaction_status=1,
                                           finish=lambda: finished.append('finish'))
        async def cancel_safe(**kwargs):
            raise DriverError(RAW)
        conn.cancel_safe = cancel_safe
        class Cursor:
            async def execute(self, *args, **kwargs):
                return await conn.wait({'interrupted': False})
        conn.cursor = lambda: Cursor()
        return conn

    cls._get_connection_params = classmethod(params)
    cls._connect_gen = classmethod(connect_gen)
    cls.closed = property(lambda self: self._closed)
    module = types.ModuleType('psycopg')
    module.AsyncConnection = cls
    return module, finished


class DriverPrivacyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.before_factory = logging.getLogRecordFactory()
        self.loggers = [logging.getLogger(n) for n in
                        ('', 'psycopg', 'psycopg.child', 'asyncpg', 'asyncpg.child')]
        self.before = [(x.level, x.disabled, x.propagate) for x in self.loggers]
        for logger in self.loggers:
            logger.setLevel(logging.DEBUG)
            logger.disabled = False
            logger.propagate = True
        self.root = Capture()
        self.direct = Capture()
        logging.getLogger().addHandler(self.root)
        logging.getLogger('psycopg').addHandler(self.direct)

    def tearDown(self):
        OBSERVATIONS[self._testMethodName] = {'root': self.root.rows, 'direct': self.direct.rows}
        logging.getLogger().removeHandler(self.root)
        logging.getLogger('psycopg').removeHandler(self.direct)
        for logger, state in zip(self.loggers, self.before):
            logger.setLevel(state[0])
            logger.disabled, logger.propagate = state[1:]
        logging.setLogRecordFactory(self.before_factory)

    def assert_private(self, rows):
        self.assertTrue(rows, 'fixed driver telemetry must remain observable')
        for row in rows:
            with self.subTest(logger=row['logger'], level=row['level']):
                text = repr(row)
                for value in (SECRET, PASSWORD, HOST, DSN):
                    self.assertNotIn(value, text, 'raw driver/private data reached a handler')
                self.assertFalse(row['exc_info'])
                self.assertIsNone(row['exc_text'])
                self.assertIsNone(row['stack_info'])

    async def real_methods(self, mode, direct=False):
        driver, finished = driver_projection(mode)
        with patch.dict(sys.modules, {'psycopg': driver}):
            factory = c11a.PostgresProbeFactory(DSN)
            if direct:
                with self.assertRaises(DriverError):
                    await factory.connect(connect_timeout_seconds=0.02, statement_timeout_ms=20)
            else:
                probe = c11a.postgres_core_probe(factory, timeout_seconds=0.02)
                self.assertIs(await probe(), False)
                await probe.aclose(deadline=asyncio.get_running_loop().time() + 0.2)
        return finished

    async def test_exact_failed_connect_debug_is_private_at_root_and_driver(self):
        self.assertEqual(await self.real_methods('connect'), [])
        self.assertEqual([x['level'] for x in self.root.rows], [logging.DEBUG] * 2)
        self.assert_private(self.root.rows)
        self.assert_private(self.direct.rows)

    async def test_exact_query_cancel_warning_is_private_and_finishes_connection(self):
        self.assertEqual(await self.real_methods('cancel'), ['finish'])
        self.assertEqual([x['level'] for x in self.root.rows],
                         [logging.DEBUG, logging.DEBUG, logging.WARNING])
        self.assert_private(self.root.rows)
        self.assert_private(self.direct.rows)

    async def test_direct_factory_connect_cannot_forget_boundary(self):
        await self.real_methods('connect', direct=True)
        self.assert_private(self.root.rows)

    async def test_all_driver_descendants_and_root_levels_strip_exception_and_stack(self):
        class Factory:
            async def connect(inner, **kwargs):
                for name in ('psycopg', 'psycopg.child', 'asyncpg', 'asyncpg.child', ''):
                    for level in (10, 20, 30, 40, 50):
                        try:
                            raise DriverError(RAW)
                        except DriverError:
                            logging.getLogger(name).log(level, 'unsafe %s', RAW,
                                                       exc_info=True, stack_info=True)
                raise DriverError(RAW)
        probe = c11a.postgres_core_probe(Factory(), timeout_seconds=0.2)
        self.assertIs(await probe(), False)
        await probe.aclose(deadline=asyncio.get_running_loop().time() + 0.2)
        self.assertEqual(len(self.root.rows), 25)
        self.assert_private(self.root.rows)
        self.assert_private(self.direct.rows)

    async def test_delayed_close_child_task_keeps_boundary_and_fixed_api_warning(self):
        class Connection:
            async def execute(inner, statement):
                return types.SimpleNamespace(fetchone=fetchone)
            async def close(inner):
                await asyncio.sleep(0.01)
                logging.getLogger('asyncpg.child').warning(RAW)
                raise DriverError(RAW)
        async def fetchone():
            return (1,)
        class Factory:
            async def connect(inner, **kwargs):
                return Connection()
        probe = c11a.postgres_core_probe(Factory(), timeout_seconds=0.05)
        self.assertIs(await probe(), True)
        await probe.aclose(deadline=asyncio.get_running_loop().time() + 0.2)
        self.assert_private(self.root.rows)
        self.assertEqual([r['message'] for r in self.root.rows
                          if r['logger'] == 'cortex_core.api.c11a'],
                         ['Core probe connection close failed'])

    async def test_concurrent_unrelated_driver_task_retains_its_original_telemetry(self):
        entered, release = asyncio.Event(), asyncio.Event()
        class Factory:
            async def connect(inner, **kwargs):
                entered.set()
                await release.wait()
                logging.getLogger('psycopg').warning(RAW)
                raise DriverError(RAW)
        probe = c11a.postgres_core_probe(Factory(), timeout_seconds=0.2)
        waiter = asyncio.create_task(probe())
        await asyncio.wait_for(entered.wait(), 0.1)
        logging.getLogger('psycopg').warning('outside %s', RAW)
        release.set()
        self.assertIs(await waiter, False)
        await probe.aclose(deadline=asyncio.get_running_loop().time() + 0.2)
        self.assertEqual(self.root.rows[0]['message'], 'outside ' + RAW)
        self.assert_private(self.root.rows[1:])
        logging.getLogger('asyncpg').debug('after %s', RAW)
        self.assertEqual(self.root.rows[-1]['message'], 'after ' + RAW)

    def shared_boundary(self):
        path = Path(c11a.__file__).parents[1] / 'driver_logging.py'
        self.assertTrue(path.is_file(), 'shared RC3 redaction boundary is absent')
        module = importlib.import_module('cortex_core.driver_logging')
        boundary = getattr(module, 'private_driver_logs', None)
        self.assertTrue(callable(boundary), 'RC3 reusable boundary is absent')
        return boundary

    async def test_shared_boundary_is_nested_and_exception_safe(self):
        boundary = self.shared_boundary()
        with boundary():
            logging.getLogger().error(RAW)
            with self.assertRaises(DriverError), boundary():
                logging.getLogger('asyncpg').debug(RAW)
                raise DriverError(RAW)
            logging.getLogger('psycopg').warning(RAW)
        self.assert_private(self.root.rows)
        logging.getLogger().warning(RAW)
        self.assertEqual(self.root.rows[-1]['message'], RAW)

    async def test_existing_record_factory_is_chained_and_reentry_installs_after_reconfigure(self):
        boundary = self.shared_boundary()
        calls = []
        prior = self.before_factory
        def external(*args, **kwargs):
            calls.append(1)
            return prior(*args, **kwargs)
        logging.setLogRecordFactory(external)
        with boundary():
            first = logging.getLogRecordFactory()
            logging.getLogger('asyncpg.child').warning(RAW)
            with boundary():
                self.assertIs(logging.getLogRecordFactory(), first)
                logging.getLogger().error(RAW)
        self.assertEqual(len(calls), 2)
        logging.setLogRecordFactory(external)
        with boundary():
            logging.getLogger('psycopg').debug(RAW)
        self.assertEqual(len(calls), 3)
        self.assert_private(self.root.rows)
