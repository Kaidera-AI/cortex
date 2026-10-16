"""X5 S1 private sync protocol over X3's async physical-session boundary.

Source composition only: installed selection still requires X4, unique view/
process admission and the S2/S3 factory. Each use retires its physical session;
there is no idle pool or reset-by-assumption. No SQL/credential authority is added.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor, TimeoutError as WorkerTimeout
from contextlib import contextmanager
from dataclasses import dataclass, field
import math
import threading

from cortex_core.driver_logging import private_driver_logs
from .connections import ResolverConnection
from .secret_resolver import SecretRefusal, SecretResolver
from .signed_member import Refusal


class SessionRefusal(Refusal):
    """Private errors never cross this boundary; commit knowledge is not rollback."""
    def __init__(self, reason, *, hold=False, commit_known=False):
        self.result = 'HOLD' if hold else 'BLOCKED'
        self.commit_known = commit_known
        super().__init__(reason)


@dataclass(eq=False)
class _Work:
    deadline: float
    adapter: object
    connection: object = None
    handle: object = None
    used: bool = False
    leased: bool = False
    fenced: bool = False
    absent: bool = True
    commit_known: bool = False
    reconciling: bool = False
    caller_done: bool = False
    worker: object = None
    operations: set = field(default_factory=set)


class _SyncSessions:
    """One owned-loop bridge; run existing sync algorithms via run(callback, E).

    The callback receives the synchronous connection_factory protocol used by
    C11b/C04/Records. Four admitted workers is the API Psycopg allocation only,
    not the S2 global/process-view admission. A caller timeout/cancellation never
    cancels the executor Future or frees a possibly surviving worker/session.
    """
    def __init__(self, reference, installation):
        self._loop = asyncio.get_running_loop()
        self._owner_thread = threading.get_ident()
        self._reference, self._installation = reference, installation
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix='cortex-x5')
        self._slots = asyncio.Semaphore(4)
        self._owned, self._local = set(), threading.local()
        self._closed = False

    @property
    def owned_count(self):
        return len(self._owned)

    def _failure(self, work, reason='session_unavailable'):
        return SessionRefusal(reason, hold=not work.absent or bool(work.operations),
                              commit_known=work.commit_known)

    def _current(self, work):
        if self._closed or work.fenced or self._loop.time() >= work.deadline:
            raise self._failure(work, 'deadline_or_lifetime_expired')

    def _reap(self, work):
        # Only the loop mutates ownership. Completed IO alone does not prove
        # session absence; completed worker alone does not prove IO absence.
        if (work.worker.done() and not work.operations and not work.absent
                and work.fenced and not work.reconciling and work.connection is not None
                and self._loop.time() < work.deadline):
            # A cancelled caller does not strand a now-idle session. Reconcile
            # only after BOTH worker and IO finish, under the SAME original E.
            work.reconciling = True
            async def reconcile():
                try: await work.adapter.retire(work.connection, deadline=work.deadline)
                finally: work.absent = work.connection not in work.adapter._sessions
            task = self._loop.create_task(reconcile())
            work.operations.add(task)
            def reconciled(done):
                work.operations.discard(done)
                if not done.cancelled(): done.exception()
                self._reap(work)
            task.add_done_callback(reconciled)
        if work.worker.done() and not work.operations and work.absent:
            if work.handle is not None and (work.caller_done or work.fenced):
                work.handle.close()
            if work in self._owned:
                self._owned.remove(work)
                self._slots.release()

    def _fence(self, work):
        work.fenced = True
        if work.handle is not None: work.handle.close()
        for task in tuple(work.operations): task.cancel()

    async def _dispatch(self, work, operation):
        self._current(work)
        async def invoke():
            with private_driver_logs():
                return await operation()
        task = self._loop.create_task(invoke())
        work.operations.add(task)
        def finished(done):
            work.operations.discard(done)
            if not done.cancelled(): done.exception()  # observe privately
            self._reap(work)
        task.add_done_callback(finished)
        try:
            done, _ = await asyncio.wait({task}, timeout=min(2, max(0, work.deadline-self._loop.time())))
            if not done:
                self._fence(work)
                raise self._failure(work, 'deadline_or_lifetime_expired')
            result = task.result()
            self._current(work)
            return result
        except asyncio.CancelledError:
            self._fence(work)
            raise

    def _sync(self, work, operation):
        if threading.get_ident() == self._owner_thread:
            raise SessionRefusal('owner_loop_cannot_block')
        future = asyncio.run_coroutine_threadsafe(self._dispatch(work, operation), self._loop)
        try:
            return future.result(timeout=max(0, work.deadline-self._loop.time()))
        except WorkerTimeout:
            self._loop.call_soon_threadsafe(self._fence, work)
            # Cancel only the dispatch waiter; _dispatch retains its child IO.
            future.cancel()
            raise self._failure(work, 'deadline_or_lifetime_expired') from None

    async def run(self, callback, *, deadline):
        if (asyncio.get_running_loop() is not self._loop or self._closed
                or type(deadline) not in (int, float) or not math.isfinite(deadline)
                or deadline <= self._loop.time()):
            raise SessionRefusal('deadline_or_lifetime_expired')
        try:
            async with asyncio.timeout_at(deadline):
                await self._slots.acquire()
        except TimeoutError:
            raise SessionRefusal('deadline_or_lifetime_expired') from None
        if self._closed or self._loop.time() >= deadline:
            self._slots.release()
            raise SessionRefusal('deadline_or_lifetime_expired')
        work = _Work(deadline, ResolverConnection(self._reference, 'api_request',
                                                self._installation, 'api', deadline))
        self._owned.add(work)
        def worker():
            self._local.work = work
            try: return callback(self.connection_factory)
            finally: del self._local.work
        work.worker = self._loop.run_in_executor(self._executor, worker)
        work.worker.add_done_callback(lambda _: self._reap(work))
        try:
            done, _ = await asyncio.wait({work.worker}, timeout=max(0, deadline-self._loop.time()))
            if not done:
                self._fence(work)
                raise self._failure(work, 'deadline_or_lifetime_expired')
            value = work.worker.result()
            self._current(work)
            if not work.absent or work.leased or work.operations:
                self._fence(work)
                raise self._failure(work, 'cleanup_unproved')
            if work.used:
                # Keep the ORIGINAL publication handle across physical retirement.
                # Resolving a fresh handle here would accept a changed generation.
                await SecretResolver().check(work.handle, deadline)
                self._current(work)
            return value
        except asyncio.CancelledError:
            self._fence(work)
            raise
        except BaseException as error:
            if isinstance(error, SessionRefusal): raise
            reason = error.reason if isinstance(error, SecretRefusal) else 'session_unavailable'
            raise self._failure(work, reason) from None
        finally:
            work.caller_done = True
            if work.handle is not None: work.handle.close()
            # Retain/observe a detached worker's exception and never publish it.
            if not work.worker.done():
                work.worker.add_done_callback(lambda f: None if f.cancelled() else f.exception())
            self._reap(work)

    async def _open(self, work):
        work.used = True
        try:
            # This handle carries no material. It pins the existing X3 original
            # across transaction IO AND physical handle retirement/publication.
            work.handle = await SecretResolver().resolve(self._reference, 'api_request',
                self._installation, 'api', work.deadline)
            self._current(work)
            remaining = min(2, work.deadline-self._loop.time())
            work.absent = False
            work.connection = await work.adapter.connect(connect_timeout_seconds=remaining,
                statement_timeout_ms=max(1, int(remaining*1000)))
            await work.adapter.check(work.connection, deadline=work.deadline)
            await SecretResolver().check(work.handle, work.deadline)
        except SecretRefusal as error:
            if work.connection is not None and work.connection in work.adapter._sessions:
                await work.adapter.retire(work.connection, deadline=work.deadline)
            # X3 proves retirement/abort before raising any non-HOLD refusal.
            work.absent = error.result != 'HOLD'
            raise
        work.leased = True
        return _Connection(self, work)

    async def _release(self, work):
        try:
            try: await work.adapter.check(work.connection, deadline=work.deadline)
            finally:
                # X3 check may already retire on failure; its registry drops a
                # session only after the protected supplier proves absence.
                if work.connection in work.adapter._sessions:
                    await work.adapter.retire(work.connection, deadline=work.deadline)
        finally:
            work.leased = False
            work.absent = work.connection not in work.adapter._sessions

    @contextmanager
    def connection_factory(self):
        if threading.get_ident() == self._owner_thread:
            raise SessionRefusal('owner_loop_cannot_block')
        work = getattr(self._local, 'work', None)
        if work is None or work.used:
            raise SessionRefusal('unowned_or_reused_worker')
        connection = self._sync(work, lambda: self._open(work))
        try:
            yield connection
        finally:
            # A pending driver operation cannot race a second reset/close.
            # Keep it owned; X6 handles survivors under its original D.
            if work.fenced or self._loop.time() >= work.deadline:
                work.leased = False
                raise self._failure(work, 'deadline_or_lifetime_expired')
            self._sync(work, lambda: self._release(work))

    async def aclose(self, *, deadline):
        if type(deadline) not in (int, float) or not math.isfinite(deadline):
            raise SessionRefusal('invalid_deadline')
        self._closed = True
        for work in tuple(self._owned): self._fence(work)
        workers = {work.worker for work in self._owned if not work.worker.done()}
        if workers:
            await asyncio.wait(workers, timeout=max(0, deadline-self._loop.time()))
        for work in tuple(self._owned):
            if work.worker.done() and not work.operations and not work.absent and work.connection is not None:
                try: await work.adapter.retire(work.connection, deadline=min(deadline, work.deadline))
                except BaseException: pass
                work.absent = work.connection not in work.adapter._sessions
            self._reap(work)
        self._executor.shutdown(wait=False, cancel_futures=True)
        if self._owned:
            raise SessionRefusal('cleanup_unproved', hold=True,
                                 commit_known=any(w.commit_known for w in self._owned))


class _Connection:
    def __init__(self, bridge, work): self._bridge, self._work = bridge, work

    def _call(self, operation):
        async def checked():
            if not self._work.leased: raise SessionRefusal('session_not_leased')
            await SecretResolver().check(self._work.handle, self._work.deadline)
            result = await operation()
            await SecretResolver().check(self._work.handle, self._work.deadline)
            return result
        return self._bridge._sync(self._work, checked)

    def _attribute(self, name):
        async def read(): return getattr(self._work.connection, name)
        return self._call(read)

    @property
    def closed(self): return self._attribute('closed')

    @property
    def autocommit(self): return self._attribute('autocommit')

    @property
    def info(self):
        from types import SimpleNamespace
        async def status(): return self._work.connection.info.transaction_status
        return SimpleNamespace(transaction_status=self._call(status))

    def execute(self, sql, params=None):
        async def execute():
            remaining = min(2, self._work.deadline-self._bridge._loop.time())
            if remaining <= 0: raise SessionRefusal('deadline_or_lifetime_expired')
            # A queue/earlier statement consumes E. The connect-time GUC alone
            # would otherwise leave a later native statement its old full budget.
            await self._work.connection.execute(
                "SELECT set_config('statement_timeout', %s, false)",
                (str(max(1, int(remaining*1000))),))
            await SecretResolver().check(self._work.handle, self._work.deadline)
            return await self._work.connection.execute(sql, params)
        return _Cursor(self, self._call(execute))

    def transaction(self): return _Transaction(self)

    def close(self):
        return self._bridge._sync(self._work, lambda: self._bridge._release(self._work))


class _Cursor:
    def __init__(self, connection, cursor): self._connection, self._cursor = connection, cursor
    def fetchone(self): return self._connection._call(self._cursor.fetchone)
    def fetchall(self): return self._connection._call(self._cursor.fetchall)


class _Transaction:
    def __init__(self, connection): self._connection, self._transaction = connection, None
    def __enter__(self):
        async def enter():
            self._transaction = self._connection._work.connection.transaction()
            await self._transaction.__aenter__()
        self._connection._call(enter)
        return self
    def __exit__(self, kind, value, traceback):
        async def exit():
            result = await self._transaction.__aexit__(kind, value, traceback)
            if kind is None and self._connection._work.connection.info.transaction_status == 0:
                self._connection._work.commit_known = True
            return result
        return self._connection._call(exit)
