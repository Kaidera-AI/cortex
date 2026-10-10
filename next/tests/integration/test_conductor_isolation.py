"""Frozen X02 controls: real PG/child processes, synthetic independent consumer."""

import asyncio
import hashlib
import os
import unittest
from uuid import UUID

import httpx
import test_conductor
import test_pg_search
from cortex_core.embeddings.pg_search import CoreUnavailable
from cortex_core.conductor.metrics import MetricIdentity, Metrics
from cortex_core.gateway.health_metrics import CoreSample, TelemetryGateway
from x02_process_fixture import Child, Consumer, FixtureError, Manager, eventually

INSTALLATION = test_conductor.INSTALLATION


class ConductorIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Compose setup, avoiding inherited tests being counted again as X02 cases.
        self.managers, self.children, self.tasks, self.ledger = [], [], [], {}
        self.cleanup_done = False
        self.fixture = test_conductor.ConductorTests("test_singleton_and_fence_after_expiry")
        self.addAsyncCleanup(self.asyncTearDown)
        await self.fixture.asyncSetUp()
        self.dsn = os.environ["SEARCH_TEST_DSN"].replace("postgres@", "conductor_runtime@")
        await self.fixture.admin.execute(
            "DROP TABLE IF EXISTS public.x02_sink,public.x02_feed,public.x02_checkpoint;"
            "CREATE TABLE public.x02_feed(seq bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,"
            "record_id text UNIQUE NOT NULL,digest text NOT NULL);"
            "CREATE TABLE public.x02_sink(seq bigint PRIMARY KEY,record_id text UNIQUE NOT NULL,digest text NOT NULL);"
            "CREATE TABLE public.x02_checkpoint(id integer PRIMARY KEY,seq bigint NOT NULL);"
            "INSERT INTO public.x02_checkpoint VALUES(1,0);"
            "GRANT SELECT ON public.x02_feed TO search_runtime;"
            "GRANT SELECT,INSERT ON public.x02_sink TO search_runtime;"
            "GRANT SELECT,UPDATE ON public.x02_checkpoint TO search_runtime"
        )

    async def asyncTearDown(self):
        if getattr(self, "cleanup_done", False):
            return
        errors = []
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        for resource in [*self.managers, *self.children]:
            try:
                await resource.close()
            except BaseException as error:
                errors.append(error)
        try:
            await self.fixture.asyncTearDown()
        except BaseException as error:
            errors.append(error)
            # Composed fixture teardown can fail on its first pool or partial setup.
            for name in ("control_pool", "pool", "admin"):
                resource = getattr(self.fixture, name, None)
                if resource is not None:
                    try:
                        await resource.close()
                    except BaseException as error:
                        errors.append(error)
        self.cleanup_done = True
        if errors:
            raise FixtureError("Owned fixture cleanup failed after all cleanup attempts") from None

    def manager(self, **options):
        manager = Manager(self.dsn, INSTALLATION, **options)
        self.managers.append(manager)
        return manager

    async def child(self, **options):
        child = await Child.spawn(self.dsn, INSTALLATION, **options)
        self.children.append(child)
        return child

    async def produce(self, rid):
        await self.fixture.record(rid=rid)
        digest = hashlib.sha256(rid.encode()).hexdigest()
        await self.fixture.admin.execute(
            "INSERT INTO public.x02_feed(record_id,digest) VALUES($1,$2)", rid, digest
        )
        self.ledger[rid] = digest
        result = await self.fixture.store.search("alice", test_pg_search.IDENTITY,
                                                 test_pg_search.VECTOR, limit=16)
        self.assertIn(rid, [hit.record_id for hit in result.hits])

    async def assert_ledger(self):
        rows = await self.fixture.admin.fetch("SELECT seq,record_id,digest FROM public.x02_sink ORDER BY seq")
        self.assertEqual({row["record_id"]: row["digest"] for row in rows}, self.ledger)
        self.assertEqual(len(rows), len(self.ledger))
        self.assertEqual(await self.fixture.admin.fetchval("SELECT seq FROM public.x02_checkpoint"),
                         max((row["seq"] for row in rows), default=0))

    async def test_external_restart_uses_new_pid_and_fence(self):
        manager = self.manager()
        self.assertEqual((await manager.start())["kind"], "ready")
        before = manager.child
        await before.kill()
        self.assertTrue(await eventually(lambda: len(manager.children) >= 2 and manager.state == "ready"))
        self.assertNotEqual(manager.child.pid, before.pid)
        self.assertGreater(manager.child.fence, before.fence)
        self.assertIsNotNone(before.process.returncode)
        self.assertTrue(any(event["kind"] == "exit" and event["pid"] == before.pid for event in manager.events))
        await self.produce("after-restart")

    async def test_live_overlap_is_refused_without_affecting_core(self):
        first = await self.child(mode="heartbeat")
        self.assertEqual(first.receipt["kind"], "ready")
        second = await self.child(mode="heartbeat")
        self.assertEqual(second.receipt["kind"], "lease_busy")
        self.assertIsNotNone(second.process.returncode)
        self.assertEqual(await self.fixture.admin.fetchval(
            "SELECT fence FROM coordination.supervisor_leases WHERE installation_id=$1", INSTALLATION), first.fence)
        await self.produce("while-overlapping")

    async def test_stale_control_is_refused_after_takeover(self):
        old = await self.child()
        self.assertEqual(old.receipt["kind"], "ready")
        self.assertTrue(await eventually(lambda: self.lease_dead()))
        new = await self.child(lease_seconds=2)
        self.assertEqual(new.receipt["kind"], "ready")
        self.assertGreater(new.fence, old.fence)
        self.assertEqual((await old.request("intent"))["kind"], "stale")
        self.assertEqual(await self.fixture.admin.fetchval("SELECT count(*) FROM public.test_controls"), 0)
        self.assertEqual((await new.request("intent"))["kind"], "intent")
        self.assertEqual(await self.fixture.admin.fetchval("SELECT fence FROM public.test_controls"), new.fence)

    async def lease_dead(self):
        return (await self.fixture.supervisor.health())["state"] == "supervisor_down"

    async def test_expiring_child_control_rolls_back(self):
        child = await self.child()
        self.assertEqual(child.receipt["kind"], "ready")
        self.assertEqual((await child.request("intent", delay=0.8))["kind"], "stale")
        self.assertEqual(await self.fixture.admin.fetchval("SELECT count(*) FROM public.test_controls"), 0)
        replacement = await self.child(lease_seconds=2)
        self.assertEqual(replacement.receipt["kind"], "ready")
        self.assertGreater(replacement.fence, child.fence)

    async def test_consumer_continues_without_conductor(self):
        for rid in ("independent-a", "independent-b", "independent-c"):
            await self.produce(rid)
        consumer = Consumer(self.fixture.pool)
        self.assertEqual(await consumer.advance(), 3)
        self.assertEqual(await consumer.advance(), 0)
        await self.assert_ledger()
        self.assertTrue(await self.lease_dead())

    async def test_consumer_progresses_through_failure_barriers(self):
        manager = self.manager()
        self.assertEqual((await manager.start())["kind"], "ready")
        for phase in ("normal", "shadow", "pre_checkpoint"):
            await self.produce(phase + "-before")
            consumer = Consumer(self.fixture.pool, pause_at=phase)
            task = asyncio.create_task(consumer.advance())
            self.tasks.append(task)
            self.assertTrue(await eventually(lambda: consumer.entered.is_set(), 2), phase)
            old = manager.child
            await old.kill()
            await self.produce(phase + "-after")
            consumer.resume.set()
            self.assertEqual(await asyncio.wait_for(task, 2), 1)
            self.assertEqual(await consumer.advance(), 1)
            await self.assert_ledger()
            self.assertTrue(await eventually(lambda: manager.child is not old and manager.state == "ready"))

    async def test_external_health_survives_dead_child(self):
        child = await self.child()
        self.assertEqual(child.receipt["kind"], "ready")
        async def sample():
            return CoreSample(await self.fixture.admin.fetchval("SELECT pg_database_size(current_database())"), 0)
        gateway = TelemetryGateway(Metrics(MetricIdentity(INSTALLATION, "test", "0.1.020")),
                                   self.fixture.supervisor.health, sample)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway.app, client=("127.0.0.1", 12345)),
                                     base_url="http://fixture.invalid") as client:
            self.assertEqual((await client.get("/health")).json()["status"], "ok")
            await child.kill()
            self.assertIsNotNone(child.process.returncode)
            self.assertTrue(await eventually(lambda: self.lease_dead(), 2))
            response = await client.get("/health")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {"component": "cortex", "status": "degraded",
                "core_available": True, "conductor": "supervisor_down", "reason": None})
            async def unavailable():
                raise CoreUnavailable("synthetic reader refusal")
            gateway.read_health = unavailable
            response = await client.get("/health")
            self.assertEqual(response.status_code, 503)
            self.assertFalse(response.json()["core_available"])
        await self.produce("after-observer")

    async def test_restart_exhaustion_is_explicit_and_bounded(self):
        manager = self.manager(max_restarts=2, crash=True)
        self.assertEqual((await manager.start())["kind"], "deliberate_crash")
        self.assertTrue(await eventually(lambda: manager.state == "exhausted"))
        self.assertEqual(len(manager.children), 3)
        self.assertTrue(all(child.process.returncode is not None for child in manager.children))
        await self.produce("after-exhaustion")

    async def test_requested_stop_does_not_restart(self):
        manager = self.manager()
        self.assertEqual((await manager.start())["kind"], "ready")
        await manager.request_stop()
        self.assertTrue(await eventually(lambda: manager.state == "stopped"))
        self.assertEqual(len(manager.children), 1)
        self.assertTrue(manager.task.done())
        self.assertTrue(await self.lease_dead())
        await self.produce("after-stop")

    async def test_close_reaps_children_and_manager_task(self):
        manager = self.manager()
        self.assertEqual((await manager.start())["kind"], "ready")
        await manager.child.kill()
        self.assertTrue(await eventually(lambda: len(manager.children) >= 2 and manager.state == "ready"))
        await manager.close()
        self.assertTrue(all(child.process.returncode is not None for child in manager.children))
        self.assertTrue(manager.task.done())

    async def test_expiry_receipt_proves_callback_entered_before_rollback(self):
        child = await self.child()
        self.assertEqual(child.receipt["kind"], "ready")
        receipt = await child.request("intent", delay=0.8)
        self.assertEqual(receipt["kind"], "stale")
        self.assertTrue(receipt["entered"])
        self.assertTrue(receipt["lease_expired"])
        self.assertEqual(await self.fixture.admin.fetchval("SELECT count(*) FROM public.test_controls"), 0)

    async def test_background_fixture_failure_surfaces_after_cleanup(self):
        from unittest.mock import patch
        from x02_process_fixture import FixtureError

        manager = self.manager()
        self.assertEqual((await manager.start())["kind"], "ready")
        with patch.object(manager, "spawn", side_effect=FixtureError("synthetic restart failure")):
            await manager.child.kill()
            self.assertTrue(await eventually(lambda: manager.task.done()))
            with self.assertRaises(FixtureError):
                await manager.close()
        self.assertTrue(all(child.process.returncode is not None for child in manager.children))


# Reviewer controls: bodies copied unchanged from Mike's frozen probe.
def Cases(method):
    # A factory preserves reviewer bodies without unittest discovering a class alias.
    return ConductorIsolationTests(method)


class Process:
    def __init__(self, code=None):
        self.pid = 424242  # A synthetic object identifier, never an OS process.
        self.returncode = code
        self.exited = asyncio.Event()
        self.stdin = self
        if code is not None:
            self.exited.set()

    async def wait(self):
        await self.exited.wait()
        return self.returncode

    def kill(self):
        self.returncode = -9
        self.exited.set()

    def close(self):
        pass

    def write(self, data):
        self.returncode = 0
        self.exited.set()

    async def drain(self):
        pass


class FixtureLifecycle(unittest.IsolatedAsyncioTestCase):
    async def test_stop_during_replacement_spawn_closes_replacement(self):
        manager = Manager('synthetic-not-connected', UUID(int=1), lease_seconds=0.001)
        old, replacement = Child(Process(0)), Child(Process())
        entered, resume = asyncio.Event(), asyncio.Event()
        manager.child, manager.children, manager.state = old, [old], 'ready'

        async def delayed_spawn():
            entered.set()
            await resume.wait()
            manager.child = replacement
            manager.children.append(replacement)
            manager.state = 'ready'
            return {'kind': 'ready'}

        manager.spawn = delayed_spawn
        manager.task = asyncio.create_task(manager.watch())
        stop = None
        try:
            await asyncio.wait_for(entered.wait(), 1)
            stop = asyncio.create_task(manager.request_stop())
            while not manager.requested_stop:
                await asyncio.sleep(0)
            resume.set()
            error = None
            try:
                await asyncio.wait_for(asyncio.shield(stop), 5.5)
            except TimeoutError:
                error = 'TimeoutError'
            live = replacement.process.returncode is None
            self.assertEqual((error, live), (None, False),
                             f'accepted stop during spawn: error={error}, replacement_still_live={live}')
        finally:
            resume.set()
            if stop:
                stop.cancel()
                await asyncio.gather(stop, return_exceptions=True)
            await manager.close()

    async def test_fixture_error_exit_cannot_be_silent_successful_close(self):
        child = Child(Process(80))
        caught = None
        try:
            await child.close()
        except FixtureError:
            caught = 'FixtureError'
        self.assertEqual(caught, 'FixtureError', 'fixture_error exit80 was reported reaped without surfacing failure')

    async def test_teardown_attempts_all_owned_cleanup_after_one_failure(self):
        closed = []

        class Spy:
            def __init__(self, name, fail=False):
                self.name, self.fail = name, fail

            async def close(self):
                closed.append(self.name)
                if self.fail:
                    raise FixtureError('controlled manager failure')

            async def asyncTearDown(self):
                closed.append(self.name)

        subject = Cases('test_requested_stop_does_not_restart')
        subject.tasks = []
        subject.managers = [Spy('first', True), Spy('second')]
        subject.children = [Spy('standalone-child')]
        subject.fixture = Spy('PG-fixture')
        error = None
        try:
            await subject.asyncTearDown()
        except FixtureError:
            error = 'FixtureError'
        self.assertEqual(error, 'FixtureError', 'failure should remain visible after cleanup')
        self.assertEqual(closed, ['first', 'second', 'standalone-child', 'PG-fixture'],
                         'one manager failure short-circuited later ownership cleanup')


    async def test_watcher_refuses_runtime_fixture_error_before_restart(self):
        manager = Manager('synthetic-not-connected', UUID(int=1), max_restarts=0)
        manager.child = Child(Process(80))
        caught = None
        try:
            await manager.watch()
        except FixtureError:
            caught = 'FixtureError'
        self.assertEqual(caught, 'FixtureError', 'exit80 must not become restart exhaustion')
        self.assertEqual(manager.children, [])

    async def test_late_heartbeat_failure_survives_pool_cleanup(self):
        from unittest.mock import AsyncMock, patch
        import x02_process_fixture as fixture_module

        class Pool:
            closed = False
            async def close(self):
                self.closed = True
        pool = Pool()
        class SupervisorDouble:
            fence = 1
            def __init__(self, *args, **kwargs):
                pass
            async def run(self, stop):
                await stop.wait()
                raise RuntimeError('late synthetic heartbeat failure')
        async def commands(supervisor, stop, heartbeat):
            return
        loop = asyncio.get_running_loop()
        with patch.dict(os.environ, {'SEARCH_TEST_DSN': 'postgresql://conductor_runtime@127.0.0.1:12345/search_test'}), \
             patch.object(fixture_module.asyncpg, 'create_pool', new=AsyncMock(return_value=pool)), \
             patch.object(fixture_module, 'Supervisor', SupervisorDouble), \
             patch.object(fixture_module, 'commands', commands), \
             patch.object(fixture_module, 'emit'), \
             patch.object(loop, 'add_signal_handler'), patch.object(loop, 'remove_signal_handler'):
            caught = None
            try:
                await fixture_module.worker(UUID(int=1), .5, 'heartbeat', False)
            except RuntimeError:
                caught = 'RuntimeError'
        self.assertEqual(caught, 'RuntimeError', 'late heartbeat failure was swallowed')
        self.assertTrue(pool.closed, 'pool must close before heartbeat error propagates')

    async def test_partial_setup_has_registered_owned_cleanup(self):
        from unittest.mock import patch
        class Partial:
            closed = False
            async def asyncSetUp(self):
                raise FixtureError('controlled partial setup')
            async def asyncTearDown(self):
                self.closed = True
        partial = Partial()
        subject = Cases('test_requested_stop_does_not_restart')
        with patch.object(test_conductor, 'ConductorTests', return_value=partial):
            with self.assertRaises(FixtureError):
                await subject.asyncSetUp()
        self.assertEqual(len(subject._cleanups), 1, 'owned cleanup must be registered before setup')
        cleanup, args, kwargs = subject._cleanups[0]
        await cleanup(*args, **kwargs)
        self.assertTrue(partial.closed)

    async def test_failed_fixture_teardown_still_closes_remaining_pools(self):
        closed = []
        class Resource:
            def __init__(self, name):
                self.name = name
            async def close(self):
                closed.append(self.name)
        class Partial:
            control_pool = Resource('control')
            pool = Resource('data')
            admin = Resource('admin')
            async def asyncTearDown(self):
                raise FixtureError('controlled pool close failure')
        subject = Cases('test_requested_stop_does_not_restart')
        subject.tasks, subject.managers, subject.children = [], [], []
        subject.fixture = Partial()
        with self.assertRaises(FixtureError):
            await subject.asyncTearDown()
        self.assertEqual(closed, ['control', 'data', 'admin'], 'PG cleanup must not stop at first pool')

    async def test_declared_lease_busy_keeps_75_after_reaping(self):
        from unittest.mock import AsyncMock, patch
        import x02_process_fixture as fixture_module
        class Pool:
            closed = False
            async def close(self):
                self.closed = True
        pool = Pool()
        class SupervisorDouble:
            fence = None
            def __init__(self, *args, **kwargs):
                pass
            async def run(self, stop):
                raise fixture_module.LeaseBusy('declared initial refusal')
        loop = asyncio.get_running_loop()
        caught, code = None, None
        with patch.dict(os.environ, {'SEARCH_TEST_DSN': 'postgresql://conductor_runtime@127.0.0.1:12345/search_test'}), \
             patch.object(fixture_module.asyncpg, 'create_pool', new=AsyncMock(return_value=pool)), \
             patch.object(fixture_module, 'Supervisor', SupervisorDouble), patch.object(fixture_module, 'emit'), \
             patch.object(loop, 'add_signal_handler'), patch.object(loop, 'remove_signal_handler'):
            try:
                code = await fixture_module.worker(UUID(int=1), .5, 'heartbeat', False)
            except fixture_module.LeaseBusy:
                caught = 'LeaseBusy'
        self.assertEqual((caught, code), (None, 75), 'declared lease refusal must survive heartbeat cleanup')
        self.assertTrue(pool.closed)
