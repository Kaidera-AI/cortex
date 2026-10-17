"""Actual descriptor/resources/lifespan; policy and SQL are explicit doubles."""
import asyncio
from contextlib import contextmanager
import io
import json
import os
import signal
import time
from types import ModuleType
import unittest
from unittest.mock import patch

from cortex_core.runtime import resources
from cortex_core.runtime.bootstrap_config import decode_config
from cortex_core.runtime.tests import test_services as fixtures
from cortex_core.migrations import prepare_migrations
try:
    from cortex_core.runtime import lifecycle
except ImportError:
    lifecycle=None


class LifecycleTests(unittest.TestCase):
    setUp=fixtures.ServiceTests.setUp
    vault=fixtures.ServiceTests.vault
    data=fixtures.ServiceTests.data
    driver=fixtures.ServiceTests.driver

    def require_source(self):
        self.assertTrue(lifecycle is not None and hasattr(resources.CoreResources,'begin_shutdown'),
            'actual fixed runtime lifespan/server owner is missing')

    @contextmanager
    def app(self):
        self.config=decode_config(json.dumps(self.value).encode(),mode='serve');self.rows=self.data()
        self.rows.pop('FROM auth.runtime_login_bindings')
        self.rows.update({'auth.runtime_request_installation':[(True,)],
            'auth.enrollment_readiness':[('ready',)],'SELECT 1':[(1,)],
            'FROM core.schema_migrations':[(i,h) for i,h,_ in prepare_migrations()]})
        with self.vault(self.config):pass
        read,write=os.pipe();os.write(write,bytes(range(32)));os.close(write)
        module=ModuleType('cortex_ship.install.roles')
        module.validate_catalog=lambda *a,**kw:dict(source_only=True)
        try:
            with patch.object(resources,'_driver_connect',side_effect=self.driver), \
                 patch.dict('sys.modules',{'cortex_ship.install.roles':module}):
                yield lifecycle.RuntimeApp(self.config,unlock_fd=read,key_id='unit-public-key')
        finally:os.close(read)

    async def start(self,app):
        incoming=asyncio.Queue();outgoing=asyncio.Queue()
        task=asyncio.create_task(app(dict(type='lifespan',state={}),incoming.get,outgoing.put))
        await incoming.put(dict(type='lifespan.startup'))
        message=await asyncio.wait_for(outgoing.get(),2)
        return incoming,outgoing,task,message

    def test_actual_lifespan_admits_before_start_and_closes_only_two_roles_in_reverse(self):
        self.require_source()
        with self.app() as app:
            async def check():
                incoming,outgoing,task,message=await self.start(app)
                self.assertEqual(message,dict(type='lifespan.startup.complete'))
                self.assertTrue(app.started);self.assertFalse(app.closed)
                self.assertEqual([c.name for c in self.calls],['cortex_api_request','cortex_probe'])
                await incoming.put(dict(type='lifespan.shutdown'))
                self.assertEqual(await outgoing.get(),dict(type='lifespan.shutdown.complete'))
                await task;self.assertTrue(app.closed);self.assertIsNone(app.failure)
            asyncio.run(check())
        self.assertEqual(self.events[-2:],['close:cortex_probe','close:cortex_api_request'])

    def test_schema_failure_never_admits_and_closes_partial_owner_without_secret_trace(self):
        self.require_source()
        with self.app() as app:
            self.rows['FROM core.schema_migrations'].pop()
            async def check():
                incoming,outgoing,task,message=await self.start(app)
                self.assertEqual(message['type'],'lifespan.startup.failed')
                self.assertNotIn('public-test-password',message['message'])
                with self.assertRaises(ValueError):await task
                self.assertFalse(app.started);self.assertTrue(app.closed)
                self.assertIsNotNone(app.failure)
            asyncio.run(check())
        self.assertEqual(self.events[-2:],['close:cortex_probe','close:cortex_api_request'])

    def test_shutdown_stops_http_admission_before_probe_auth_or_write(self):
        self.require_source()
        with self.app() as app:
            async def check():
                incoming,outgoing,task,message=await self.start(app)
                app.begin_shutdown();queries=sum(len(c.queries) for c in self.calls);messages=[]
                async def receive():self.fail('draining app consumed a body')
                async def send(m):messages.append(m)
                await app(dict(type='http',method='POST',path='/memory',headers=[]),receive,send)
                self.assertEqual(messages[0]['status'],503)
                self.assertEqual(sum(len(c.queries) for c in self.calls),queries)
                await incoming.put(dict(type='lifespan.shutdown'));await outgoing.get();await task
            asyncio.run(check())

    def test_cleanup_deadline_is_absolute_no_renewal_and_unresolved_is_failure(self):
        self.require_source()
        with self.app() as app:
            async def check():
                incoming,outgoing,task,message=await self.start(app)
                first=app.begin_shutdown();self.assertEqual(app.begin_shutdown(),first)
                owned=app.resources
                with self.assertRaisesRegex(ValueError,'resources_shutdown'):
                    owned.begin_shutdown(time.monotonic()+self.config.limits['shutdown_seconds']+10)
                self.calls[1].close=lambda:time.sleep(2)
                object.__setattr__(owned,'_shutdown_deadline',time.monotonic()+0.12)
                began=time.monotonic();await incoming.put(dict(type='lifespan.shutdown'))
                self.assertEqual((await outgoing.get())['type'],'lifespan.shutdown.failed')
                with self.assertRaises(ValueError):await task
                self.assertLess(time.monotonic()-began,0.7)
                self.assertIsNotNone(app.failure)
            asyncio.run(check())

    def test_existing_driver_timer_is_shortened_on_shutdown_without_deadline_extension(self):
        self.require_source()
        with self.app() as app:
            async def check():
                incoming,outgoing,task,message=await self.start(app)
                with resources._driver_deadline(time.monotonic()+5):
                    app.begin_shutdown(deadline=time.monotonic()+0.5)
                    self.assertLessEqual(signal.getitimer(signal.ITIMER_REAL)[0],0.5)
                await incoming.put(dict(type='lifespan.shutdown'));await outgoing.get();await task
            asyncio.run(check())

    def test_uvicorn_is_one_explicit_server_with_fixed_bind_and_no_proxy_or_reload(self):
        self.require_source()
        with self.app() as app:
            server=lifecycle.runtime_server(app)
            self.assertEqual((server.config.host,server.config.port),
                (self.config.bind['host'],self.config.bind['port']))
            self.assertEqual(server.config.workers,1);self.assertFalse(server.config.reload)
            self.assertFalse(server.config.proxy_headers);self.assertFalse(server.config.access_log)
            self.assertEqual(server.config.http,'h11');self.assertEqual(server.config.lifespan,'on')

    def test_nonactual_or_wrong_mode_config_never_reaches_server_or_custody(self):
        self.require_source()
        for config in (None,self.value,decode_config(json.dumps(self.value).encode(),mode='seed')):
            with self.assertRaisesRegex(ValueError,'resources_mode'):
                lifecycle.RuntimeApp(config,unlock_fd=9999,key_id='unit-public-key')

    def test_already_admitted_handler_cannot_open_sql_after_shutdown(self):
        self.require_source()
        with self.app() as app:
            async def check():
                incoming,outgoing,task,message=await self.start(app)
                app.begin_shutdown();queries=sum(len(c.queries) for c in self.calls)
                with self.assertRaisesRegex(ValueError,'resources_draining'):
                    with app.resources.request_connection('api_request',deadline=time.monotonic()+1):pass
                self.assertEqual(sum(len(c.queries) for c in self.calls),queries)
                await incoming.put(dict(type='lifespan.shutdown'));await outgoing.get();await task
            asyncio.run(check())

    def test_operator_serve_calls_actual_fixed_composition_with_same_config_and_descriptors(self):
        self.require_source()
        from cortex_core.runtime import operator
        path=self.root/'config.json';path.write_text(json.dumps(self.value));path.chmod(0o600)
        calls=[]
        async def serve(config,*,unlock_fd,key_id):calls.append((config,unlock_fd,key_id));return 0
        out,err=io.StringIO(),io.StringIO()
        with patch.object(lifecycle,'serve_runtime',side_effect=serve),patch('sys.stdout',out),patch('sys.stderr',err):
            result=operator.main(['serve','--config',str(path),'--unlock-fd','11','--key-id','unit-public-key'])
        self.assertEqual(result,0,err.getvalue());self.assertEqual(out.getvalue(),'');self.assertEqual(err.getvalue(),'')
        self.assertEqual(len(calls),1);self.assertEqual(calls[0][0].mode,'serve')
        self.assertEqual(calls[0][1:],(11,'unit-public-key'))

    def test_signal_during_partial_open_cannot_renew_cleanup_budget(self):
        self.require_source()
        with self.app() as app:
            def driver(**kw):
                if self.calls:
                    app.begin_shutdown(deadline=time.monotonic()+0.06)
                    raise RuntimeError('public-fixture-connect-failure')
                connection=self.driver(**kw)
                connection.close=lambda:time.sleep(0.25)
                return connection
            async def check():
                began=time.monotonic()
                with patch.object(resources,'_driver_connect',side_effect=driver):
                    incoming,outgoing,task,message=await self.start(app)
                    with self.assertRaises(ValueError):await task
                self.assertEqual(message['type'],'lifespan.startup.failed')
                self.assertLess(time.monotonic()-began,0.18,
                    'partial-open cleanup renewed the first shutdown budget')
                self.assertFalse(app.started);self.assertIsNotNone(app.failure)
            asyncio.run(check())

    def test_signal_during_first_open_stops_second_connection_and_startup_sql(self):
        self.require_source()
        with self.app() as app:
            def driver(**kw):
                connection=self.driver(**kw)
                if len(self.calls)==1:app.begin_shutdown(deadline=time.monotonic()+0.2)
                return connection
            async def check():
                with patch.object(resources,'_driver_connect',side_effect=driver):
                    incoming,outgoing,task,message=await self.start(app)
                    with self.assertRaises(ValueError):await task
                self.assertEqual(message['type'],'lifespan.startup.failed')
                self.assertEqual(len(self.calls),1,'draining startup opened another role')
                self.assertEqual(self.calls[0].queries,[],'startup SQL ran after first shutdown')
                self.assertFalse(app.started);self.assertIsNotNone(app.failure)
            asyncio.run(check())

    def test_signal_during_ledger_gate_prevents_next_startup_transaction(self):
        self.require_source()
        from cortex_core.schema.readiness import check_schema_ready
        with self.app() as app:
            at_signal=[]
            def ledger(connection):
                ready=check_schema_ready(connection)
                at_signal.append(sum(len(c.queries) for c in self.calls))
                app.begin_shutdown(deadline=time.monotonic()+0.2)
                return ready
            async def check():
                with patch('cortex_core.schema.readiness.check_schema_ready',side_effect=ledger):
                    incoming,outgoing,task,message=await self.start(app)
                    with self.assertRaises(ValueError):await task
                self.assertEqual(message['type'],'lifespan.startup.failed')
                self.assertEqual(sum(len(c.queries) for c in self.calls),at_signal[0],
                    'catalog/probe startup SQL continued after the first shutdown')
                self.assertFalse(app.started);self.assertIsNotNone(app.failure)
            asyncio.run(check())


if __name__=='__main__':unittest.main()
