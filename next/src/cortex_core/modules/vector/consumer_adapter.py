"""Bounded synchronous C07 bridge to the one async private client.

One retained worker per sink bridges C07's sync call and HTTPX's async lifecycle.
Timed-out/noncooperative work remains owned and blocks overlap; no READY/native
claim. Runtime shutdown must call close and treat unverified drain as unavailable.
"""
import asyncio
import math
import threading
import time

from .projection import ProjectionBinding, _event
from .qdrant import ProtocolFailure
from .qdrant_client import PrivateClient


class QdrantSink:
    def __init__(self, binding, client_factory, embed):
        if type(binding) is not ProjectionBinding or not callable(client_factory) or not callable(embed):
            raise ValueError('invalid_projection_sink')
        self.binding, self.client_factory, self.embed = binding, client_factory, embed
        self._lock, self._workers, self._closed = threading.Lock(), [], False

    def status(self):
        with self._lock:
            self._workers = [j for j in self._workers if j['thread'].is_alive()]
            return {'state':'unavailable','pending_workers':len(self._workers)}

    @staticmethod
    def _deadline(deadline):
        now = time.monotonic()
        if deadline is None:
            return now+5
        if type(deadline) not in (int,float) or not math.isfinite(deadline) or deadline <= now:
            raise ProtocolFailure('qdrant_projection_unavailable')
        return min(now+5,deadline)

    @staticmethod
    def _cancel(job):
        loop, task = job.get('loop'), job.get('task')
        if loop is not None and task is not None:
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                pass  # Closed loop: thread liveness, not this race, owns retirement.

    def apply(self, event, generation, *, deadline=None):
        try:
            end = self._deadline(deadline)
            if type(generation) is not int or generation != self.binding.consumer_generation:
                raise ValueError()
            _event(self.binding,event)
            # Never block an API event loop with the synchronous consumer API.
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                pass
            else:
                raise ValueError()
            job = {'done':threading.Event(),'result':None,'error':False}
            def run():
                try:
                    job['result'] = asyncio.run(self._apply(event,end,job))
                except BaseException:
                    job['error'] = True  # No dependency exception text crosses the worker boundary.
                finally:
                    job['done'].set()
            with self._lock:
                self._workers = [j for j in self._workers if j['thread'].is_alive()]
                if self._closed or self._workers:
                    raise ValueError()
                job['thread'] = threading.Thread(target=run,name='cortex-qdrant-effect',daemon=True)
                self._workers.append(job)
                job['thread'].start()
            if not job['done'].wait(max(0,end-time.monotonic())):
                self._cancel(job)
                raise TimeoutError()
            job['thread'].join(max(0,end-time.monotonic()))
            if job['thread'].is_alive() or job['error'] or time.monotonic() >= end:
                raise ValueError()
            return job['result']
        except Exception:
            raise ProtocolFailure('qdrant_projection_unavailable') from None

    async def _apply(self, event, deadline, job):
        job['loop'],job['task'] = asyncio.get_running_loop(),asyncio.current_task()
        if time.monotonic() >= deadline:
            raise TimeoutError()
        client = self.client_factory()
        if type(client) is not PrivateClient:
            raise ValueError()
        async with client:
            async def operation(end):
                vector = await self.embed(self.binding.identity,event.payload)
                from .projection import build_point
                desired = build_point(self.binding,event,vector)
                return await client._apply_projection(self.binding,event,desired,end)
            return await client._owned(operation,deadline)

    def close(self, *, deadline=None):
        end = self._deadline(deadline)
        with self._lock:
            self._closed = True
            jobs = tuple(self._workers)
        for job in jobs:
            self._cancel(job)
        for job in jobs:
            job['thread'].join(max(0,end-time.monotonic()))
        if self.status()['pending_workers']:
            raise ProtocolFailure('qdrant_cleanup_unverified')
