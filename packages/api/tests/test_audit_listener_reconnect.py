"""Frozen readiness assertions; termination-compatible asyncpg double, no DB."""
import ast,asyncio,copy,os,time,unittest
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace
WT=Path(__file__).resolve().parents[3]
SOURCE=WT/'packages/api/main.py'
def namespace():
    tree=ast.parse(SOURCE.read_text())
    n=copy.deepcopy(next(x for x in tree.body if getattr(x,'name','')=='listen_for_team_events'))
    n.decorator_list=[]
    future=ast.ImportFrom(module='__future__',names=[ast.alias(name='annotations')],level=0)
    ns={'asyncio':asyncio,'suppress':suppress,'__file__':str(SOURCE)}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[future,n],type_ignores=[])),str(SOURCE),'exec'),ns)
    return ns

class ActualAPIControls(unittest.IsolatedAsyncioTestCase):
    async def test_ready_listener_disconnect_reconnects(self):
            ns=namespace();connections=[]
            class Connection:
                def __init__(self):self.closed=False;self.termination=set()
                def add_termination_listener(self, callback):self.termination.add(callback)
                def remove_termination_listener(self, callback):self.termination.discard(callback)
                async def add_listener(self,*args):pass
                async def remove_listener(self,*args):pass
                async def close(self):self.closed=True
                def drop(self):
                    self.closed=True
                    for callback in tuple(self.termination):callback(self)
            async def connect(*args,**kwargs):
                connection=Connection();connections.append(connection);return connection
            ns.update(asyncpg=SimpleNamespace(connect=connect),PG_DSN_ADMIN='synthetic-unused',
                database_connection_kwargs=lambda *args:{},EVENT_WAKE_CHANNEL='test',
                event_notification_callback=lambda *args:None,event_listener_conn=None,
                event_listener_ready=False,event_listener_error=None)
            task=asyncio.create_task(ns['listen_for_team_events']())
            try:
                for _ in range(10):await asyncio.sleep(0)
                self.assertEqual(len(connections),1)
                connections[0].drop()
                for _ in range(50):await asyncio.sleep(0)
                self.assertFalse(ns['event_listener_ready'] and connections[0].closed,
                    'connection drop leaves readiness true and independent Future permanently pending')
            finally:
                task.cancel();await asyncio.gather(task,return_exceptions=True)
            self.assertFalse(ns['event_listener_ready'])

    async def test_reconnect_and_shutdown_remove_owned_callbacks(self):
        ns=namespace(); connections=[]; retries=[]
        class Connection:
            def __init__(self):self.closed=False;self.callbacks=set();self.notify=set();self.saved=[]
            def add_termination_listener(self,callback):self.callbacks.add(callback);self.saved.append(callback)
            def remove_termination_listener(self,callback):self.callbacks.discard(callback)
            async def add_listener(self,channel,callback):self.notify.add(callback)
            async def remove_listener(self,channel,callback):self.notify.discard(callback)
            async def close(self):self.closed=True
            def drop(self):
                self.closed=True
                for callback in tuple(self.callbacks):callback(self)
        async def connect(*args,**kwargs):
            c=Connection();connections.append(c);return c
        async def sleep(delay):
            if delay:retries.append(delay)
            await asyncio.sleep(0)
        runtime=SimpleNamespace(connect=connect)
        ns.update(asyncpg=runtime,PG_DSN_ADMIN='synthetic-unused',database_connection_kwargs=lambda *args:{},
            EVENT_WAKE_CHANNEL='test',event_notification_callback=lambda *args:None,event_listener_conn=None,event_listener_ready=False,event_listener_error=None,
            asyncio=SimpleNamespace(Event=asyncio.Event,Future=asyncio.Future,CancelledError=asyncio.CancelledError,sleep=sleep))
        task=asyncio.create_task(ns['listen_for_team_events']())
        try:
            for _ in range(10):await asyncio.sleep(0)
            self.assertEqual(len(connections),1)
            self.assertTrue(ns['event_listener_ready'])
            old=connections[0];old.drop()
            self.assertFalse(ns['event_listener_ready'])
            for _ in range(20):await asyncio.sleep(0)
            self.assertEqual(len(connections),2)
            self.assertEqual(retries,[2])
            self.assertIs(ns['event_listener_conn'],connections[1])
            self.assertTrue(ns['event_listener_ready'])
            self.assertFalse(old.callbacks or old.notify)
            # Late callback from the previous connection cannot invalidate the new one.
            old.saved[0](old)
            self.assertTrue(ns['event_listener_ready'])
            self.assertFalse(task.done())
        finally:
            task.cancel();await asyncio.gather(task,return_exceptions=True)
        self.assertFalse(ns['event_listener_ready'])
        self.assertIsNone(ns['event_listener_conn'])
        self.assertTrue(all(c.closed and not c.callbacks and not c.notify for c in connections))
