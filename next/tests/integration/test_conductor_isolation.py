"""Frozen X02 controls: real PG/child processes, synthetic independent consumer."""

import asyncio
import hashlib
import os
import unittest

import httpx
import test_conductor
import test_pg_search
from cortex_core.embeddings.pg_search import CoreUnavailable
from cortex_core.conductor.metrics import MetricIdentity, Metrics
from cortex_core.gateway.health_metrics import CoreSample, TelemetryGateway
from x02_process_fixture import Child, Consumer, Manager, eventually

INSTALLATION = test_conductor.INSTALLATION


class ConductorIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Compose setup, avoiding inherited tests being counted again as X02 cases.
        self.fixture = test_conductor.ConductorTests("test_singleton_and_fence_after_expiry")
        await self.fixture.asyncSetUp()
        self.managers, self.children, self.tasks, self.ledger = [], [], [], {}
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
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        for manager in self.managers:
            await manager.close()
        for child in self.children:
            await child.close()
        await self.fixture.asyncTearDown()

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
