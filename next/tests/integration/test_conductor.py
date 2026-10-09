"""Synthetic control fixture; real PG lease and Design59 metric serialization."""

import asyncio
import time
import unittest
from uuid import UUID

import asyncpg
import test_pg_search
from cortex_core.conductor.metrics import MetricIdentity, Metrics
from cortex_core.conductor.supervisor import LeaseBusy, StaleLease, Supervisor
from prometheus_client.parser import text_string_to_metric_families

INSTALLATION = UUID("00000000-0000-0000-0000-000000000101")


class ConductorTests(test_pg_search.SearchTests):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.admin.execute(
            "DROP SCHEMA IF EXISTS coordination CASCADE; DROP SCHEMA IF EXISTS core CASCADE; CREATE SCHEMA core; CREATE TABLE core.installations(id uuid PRIMARY KEY)"
        )
        await self.admin.execute(
            "INSERT INTO core.installations VALUES($1)", INSTALLATION
        )
        await self.admin.execute(
            test_pg_search.Path(__file__)
            .resolve()
            .parents[2]
            .joinpath("schema/coordination/c08-supervisor-leases.sql")
            .read_text()
        )
        if not await self.admin.fetchval(
            "SELECT 1 FROM pg_roles WHERE rolname='conductor_runtime'"
        ):
            await self.admin.execute(
                "CREATE ROLE conductor_runtime LOGIN NOSUPERUSER NOBYPASSRLS"
            )
        await self.admin.execute(
            "GRANT USAGE ON SCHEMA coordination TO conductor_runtime; GRANT SELECT,INSERT,UPDATE ON coordination.supervisor_leases TO conductor_runtime; DROP TABLE IF EXISTS public.test_controls; CREATE TABLE public.test_controls(fence bigint); GRANT SELECT,INSERT ON public.test_controls TO conductor_runtime"
        )
        self.control_pool = await asyncpg.create_pool(
            test_pg_search.os.environ["SEARCH_TEST_DSN"].replace(
                "postgres@", "conductor_runtime@"
            ),
            min_size=1,
            max_size=2,
        )
        self.supervisor = Supervisor(
            self.control_pool, INSTALLATION, lease_seconds=0.15
        )

    async def asyncTearDown(self):
        await self.control_pool.close()
        await super().asyncTearDown()

    async def test_singleton_and_fence_after_expiry(self):
        first = await self.supervisor.acquire()
        other = Supervisor(self.control_pool, INSTALLATION, lease_seconds=0.2)
        with self.assertRaises(LeaseBusy):
            await other.acquire()
        await asyncio.sleep(0.18)
        second = await other.acquire()
        self.assertGreater(second, first)
        with self.assertRaises(StaleLease):
            await self.supervisor.renew()
        with self.assertRaises(StaleLease):
            await self.supervisor.release()

        async def action(conn, fence):
            await conn.execute("INSERT INTO public.test_controls VALUES($1)", fence)

        with self.assertRaises(StaleLease):
            await self.supervisor.guarded(action)
        await other.guarded(action)
        self.assertEqual(
            await self.admin.fetchval("SELECT fence FROM public.test_controls"), second
        )

    async def test_expired_during_control_rolls_back(self):
        supervisor = Supervisor(self.control_pool, INSTALLATION, lease_seconds=0.03)
        await supervisor.acquire()

        async def action(conn, fence):
            await conn.execute("INSERT INTO public.test_controls VALUES($1)", fence)
            await asyncio.sleep(0.05)

        with self.assertRaises(StaleLease):
            await supervisor.guarded(action)
        self.assertEqual(
            await self.admin.fetchval("SELECT count(*) FROM public.test_controls"), 0
        )

    async def test_release_reclaim_never_reuses_fence(self):
        first = await self.supervisor.acquire()
        await self.supervisor.release()
        self.assertEqual((await self.supervisor.health())["state"], "supervisor_down")
        other = Supervisor(self.control_pool, INSTALLATION, lease_seconds=0.2)
        self.assertGreater(await other.acquire(), first)

    async def test_stalled_control_cannot_hold_lease_lock_indefinitely(self):
        supervisor = Supervisor(self.control_pool, INSTALLATION, lease_seconds=0.05)
        first = await supervisor.acquire()
        entered = asyncio.Event()

        async def stalled(conn, fence):
            await conn.execute("INSERT INTO public.test_controls VALUES($1)", fence)
            entered.set()
            await asyncio.Event().wait()

        task = asyncio.create_task(supervisor.guarded(stalled))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            with self.assertRaises(StaleLease):
                await asyncio.wait_for(task, 0.5)
            self.assertEqual(
                await self.admin.fetchval("SELECT count(*) FROM public.test_controls"),
                0,
            )
            other = Supervisor(self.control_pool, INSTALLATION, lease_seconds=0.2)
            self.assertGreater(await asyncio.wait_for(other.acquire(), 0.5), first)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def queued_expiry_operation(self, method):
        supervisor = Supervisor(self.control_pool, INSTALLATION, lease_seconds=1)
        fence = await supervisor.acquire()
        entered = asyncio.Event()

        async def stalled(conn, token):
            await conn.execute("INSERT INTO public.test_controls VALUES($1)", token)
            entered.set()
            await asyncio.Event().wait()

        guard = asyncio.create_task(supervisor.guarded(stalled))
        queued = None
        try:
            await asyncio.wait_for(entered.wait(), 1)
            queued = asyncio.create_task(getattr(supervisor, method)())
            deadline = asyncio.get_running_loop().time() + 0.5
            while not await self.admin.fetchval(
                "SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE "
                "usename='conductor_runtime' AND wait_event_type='Lock' AND state='active')"
            ):
                self.assertLess(asyncio.get_running_loop().time(), deadline,
                                "Operation never queued on the guarded row lock")
                await asyncio.sleep(0.002)
            self.assertFalse(queued.done())
            self.assertTrue(await self.admin.fetchval(
                "SELECT expires_at > clock_timestamp() FROM coordination.supervisor_leases "
                "WHERE installation_id=$1", INSTALLATION
            ), "Operation must enter the lock wait before lease expiry")
            with self.assertRaises(StaleLease):
                await asyncio.wait_for(guard, 2)
            with self.assertRaises(StaleLease):
                await asyncio.wait_for(queued, 2)
            row = await self.admin.fetchrow(
                "SELECT fence,expires_at > clock_timestamp() AS live FROM "
                "coordination.supervisor_leases WHERE installation_id=$1", INSTALLATION
            )
            self.assertEqual(row["fence"], fence)
            self.assertFalse(row["live"])
            self.assertEqual(await self.admin.fetchval(
                "SELECT count(*) FROM public.test_controls"), 0)
        finally:
            tasks = [guard] + ([queued] if queued is not None else [])
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def test_renew_waiting_behind_guard_refuses_expired_lease(self):
        await self.queued_expiry_operation("renew")

    async def test_release_waiting_behind_guard_refuses_expired_lease(self):
        await self.queued_expiry_operation("release")

    async def test_heartbeat_stops_and_data_path_continues(self):
        stop = asyncio.Event()
        running = asyncio.create_task(self.supervisor.run(stop))
        await asyncio.sleep(0.25)
        self.assertEqual((await self.supervisor.health())["state"], "healthy")
        stop.set()
        await running
        self.assertEqual((await self.supervisor.health())["state"], "supervisor_down")
        await self.record()
        result = await self.store.search(
            "alice", test_pg_search.IDENTITY, test_pg_search.VECTOR
        )
        self.assertEqual(len(result.hits), 1)

    async def test_health_core_outage(self):
        self.assertEqual((await self.supervisor.health())["state"], "supervisor_down")
        await self.control_pool.close()
        health = await self.supervisor.health()
        self.assertFalse(health["core_available"])
        self.assertEqual(health["state"], "core_unavailable")

    async def test_private_control_table_and_installation_binding(self):
        flags = await self.admin.fetchrow(
            "SELECT relrowsecurity,relforcerowsecurity FROM pg_class WHERE oid='coordination.supervisor_leases'::regclass"
        )
        self.assertTrue(flags[0] and flags[1])
        async with self.pool.acquire() as conn:
            with self.assertRaises(asyncpg.InsufficientPrivilegeError):
                await conn.fetchval(
                    "SELECT count(*) FROM coordination.supervisor_leases"
                )
        unknown = Supervisor(self.control_pool, UUID(int=999), lease_seconds=0.2)
        with self.assertRaises(asyncpg.ForeignKeyViolationError):
            await unknown.acquire()
        async with self.control_pool.acquire() as conn:
            self.assertEqual(
                await conn.fetchval(
                    "SELECT count(*) FROM coordination.supervisor_leases"
                ),
                0,
            )

    async def test_direct_fence_regression_refused(self):
        await self.supervisor.acquire()
        await self.supervisor.release()
        await self.supervisor.acquire()
        with self.assertRaises(asyncpg.PostgresError):
            await self.admin.execute(
                "UPDATE coordination.supervisor_leases SET fence=fence-1"
            )

    async def test_metrics_real_bytes_and_supplied_backlog(self):
        metrics = Metrics(MetricIdentity(INSTALLATION, "test", "0.1.020"))

        async def backlog():
            return 7

        await self.supervisor.sample_metrics(metrics, backlog)
        text = metrics.export()
        families = {
            f.name: list(f.samples)[0].value
            for f in text_string_to_metric_families(text)
        }
        self.assertEqual(families["cortex_embed_backlog"], 7)
        self.assertEqual(
            families["cortex_db_bytes"],
            await self.admin.fetchval("SELECT pg_database_size(current_database())"),
        )


class MetricTests(unittest.TestCase):
    def test_closed_families_labels_and_actual_timing(self):
        metrics = Metrics(MetricIdentity(INSTALLATION, "test", "0.1.020"))
        self.assertEqual(metrics.export(), "")
        with metrics.timer("api"):
            time.sleep(0.002)
        metrics.observe_search(0.4)
        metrics.update_core(1024, 3)
        families = list(text_string_to_metric_families(metrics.export()))
        self.assertEqual(
            {f.name for f in families},
            {
                "cortex_api_latency_seconds",
                "cortex_search_latency_seconds",
                "cortex_db_bytes",
                "cortex_embed_backlog",
            },
        )
        for family in families:
            sample = family.samples[0]
            self.assertEqual(
                set(sample.labels),
                {"deployment_id", "environment", "component", "release"},
            )
            self.assertEqual(sample.labels["deployment_id"], str(INSTALLATION))
            self.assertEqual(sample.labels["component"], "cortex")
        self.assertGreater(
            next(
                f.samples[0].value
                for f in families
                if f.name == "cortex_api_latency_seconds"
            ),
            0,
        )

    def test_unsafe_labels_and_nonfinite_values_refused(self):
        for environment in ['test"}\nmalicious_metric', "", "x" * 65]:
            with self.assertRaises(ValueError):
                Metrics(MetricIdentity(INSTALLATION, environment, "0.1.020"))
        metrics = Metrics(MetricIdentity(INSTALLATION, "test", "0.1.020"))
        for value in [float("nan"), float("inf"), -1]:
            with self.assertRaises(ValueError):
                metrics.observe_api(value)
        with self.assertRaises(ValueError):
            metrics.update_core(-1, 3)
