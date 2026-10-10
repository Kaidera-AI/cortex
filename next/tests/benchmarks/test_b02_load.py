"""Frozen offered-load controls: actual async work, no product/native claim."""
import asyncio
import importlib
from pathlib import Path
import unittest


class Client:
    def __init__(self, number, *, delay=0, error=False, open_error=False):
        self.number, self.delay, self.error, self.open_error = number, delay, error, open_error
        self.opened = self.closed = False
        self.calls = 0

    async def open(self):
        self.opened = True
        if self.open_error:
            raise RuntimeError("synthetic-secret")

    async def request(self, query):
        self.calls += 1
        await asyncio.sleep(self.delay)
        if self.error:
            raise RuntimeError("synthetic-secret")
        return {"ids": [query["id"]]}

    async def aclose(self):
        self.closed = True


class LoadTests(unittest.IsolatedAsyncioTestCase):
    def surface(self):
        path = Path(__file__).resolve().parents[2] / "src/vector_baseline/load.py"
        self.assertTrue(path.is_file(), "B02 load surface absent")
        return importlib.import_module("vector_baseline.load")

    async def test_fixed_schedule_and_eight_persistent_clients(self):
        load = self.surface()
        clients = []

        def factory(i):
            clients.append(Client(i, delay=.08))
            return clients[-1]

        run = await load.run([{"id": "q1"}, {"id": "q2"}], factory,
                             load.RunConfig(duration_seconds=.5, warmup_seconds=0))
        self.assertEqual(len(clients), 8)
        self.assertTrue(all(c.opened and c.closed for c in clients))
        self.assertEqual(len(run["records"]), 20)
        for i, row in enumerate(run["records"]):
            self.assertAlmostEqual(row["scheduled_offset_seconds"], i / 40, places=8)
            self.assertEqual(row["client"], i % 8)
            self.assertEqual(row["query_id"], "q1" if i % 2 == 0 else "q2")
        self.assertEqual(sum(c.calls for c in clients), 20)
        self.assertEqual(run["clients_opened"], 8)
        self.assertEqual(run["clients_closed"], 8)

    async def test_queue_wait_is_part_of_scheduled_latency(self):
        load = self.surface()
        run = await load.run([{"id": "q"}], lambda i: Client(i, delay=.3 if i == 0 else 0),
                             load.RunConfig(duration_seconds=.425, warmup_seconds=0))
        row = run["records"][8]
        self.assertGreater(row["queue_seconds"], .05)
        self.assertAlmostEqual(row["latency_seconds"], row["completed_at"] - row["scheduled_at"], places=8)
        self.assertGreater(row["latency_seconds"], row["completed_at"] - row["started_at"] + .05)

    async def test_deadlines_and_errors_keep_every_offer_and_scrub_messages(self):
        load = self.surface()
        run = await load.run([{"id": "q"}], lambda i: Client(i, delay=.1 if i % 2 == 0 else 0,
                                                              error=i % 2 == 1),
                             load.RunConfig(duration_seconds=.2, warmup_seconds=0, deadline_seconds=.025))
        self.assertEqual(len(run["records"]), 8)
        self.assertEqual({r["status"] for r in run["records"]}, {"TIMEOUT", "ERROR"})
        self.assertTrue(all(r["latency_seconds"] >= .025 for r in run["records"] if r["status"] == "TIMEOUT"))
        self.assertNotIn("synthetic-secret", str(run))
        self.assertTrue(all(r.get("error_class") == "RuntimeError" for r in run["records"] if r["status"] == "ERROR"))

    async def test_scheduler_stall_records_missed_arrivals_without_retiming(self):
        load = self.surface()

        class StallClock:
            value = 100.0

            def now(self):
                return self.value

            async def sleep(self, delay):
                self.value += delay + .1
                await asyncio.sleep(0)

        run = await load.run([{"id": "q"}], lambda i: Client(i),
                             load.RunConfig(duration_seconds=.25, warmup_seconds=0), clock=StallClock())
        self.assertEqual(len(run["records"]), 10)
        self.assertTrue(any(r["status"] == "MISSED" for r in run["records"]))
        for i, row in enumerate(run["records"]):
            self.assertAlmostEqual(row["scheduled_offset_seconds"], i / 40)

    async def test_warmup_is_separate_and_uses_same_sessions(self):
        load = self.surface()
        clients = [Client(i) for i in range(8)]
        run = await load.run([{"id": "q"}], lambda i: clients[i],
                             load.RunConfig(duration_seconds=.2, warmup_seconds=.2))
        self.assertEqual(len(run["records"]), 8)
        self.assertEqual(len(run["warmup_records"]), 8)
        self.assertEqual(sum(c.calls for c in clients), 16)
        self.assertTrue(all(r["phase"] == "measured" for r in run["records"]))

    async def test_partial_open_side_effect_is_closed_before_error_propagates(self):
        load = self.surface()
        clients = []

        def factory(i):
            clients.append(Client(i, open_error=i == 2))
            return clients[-1]

        with self.assertRaises(RuntimeError):
            await load.run([{"id": "q"}], factory, load.RunConfig(duration_seconds=.2, warmup_seconds=0))
        self.assertEqual(len(clients), 3)
        self.assertTrue(all(c.closed for c in clients))

    async def test_caller_cancellation_closes_all_created_clients(self):
        load = self.surface()
        clients = [Client(i, delay=2) for i in range(8)]
        task = asyncio.create_task(load.run([{"id": "q"}], lambda i: clients[i],
                                           load.RunConfig(duration_seconds=1, warmup_seconds=0)))
        await asyncio.sleep(.05)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(all(c.closed for c in clients))

    async def test_observation_failure_is_visible_and_cleaned(self):
        load = self.surface()

        async def observer():
            raise RuntimeError("synthetic-observer-secret")

        run = await load.run([{"id": "q"}], lambda i: Client(i),
                             load.RunConfig(duration_seconds=.2, warmup_seconds=0), observer=observer)
        self.assertTrue(run["observation_errors"])
        self.assertEqual(run["clients_closed"], 8)
        self.assertNotIn("synthetic-observer-secret", str(run))

    async def test_invalid_configuration_refuses_before_opening_clients(self):
        load = self.surface()
        calls = []
        for config in [load.RunConfig(clients=7), load.RunConfig(rps=20),
                       load.RunConfig(duration_seconds=True), load.RunConfig(deadline_seconds=float("nan"))]:
            with self.subTest(config=config):
                with self.assertRaises(ValueError):
                    await load.run([{"id": "q"}], lambda i: calls.append(i), config)
        self.assertEqual(calls, [])
