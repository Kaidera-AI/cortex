"""Frozen PR68 counterexamples for every offered warmup and queue timing."""
import asyncio
import copy
import unittest

from vector_baseline import load, report
from test_b02_report import fixture


def warm_run():
    value = fixture()
    value["config"]["warmup_seconds"] = 1
    value["warmup_records"] = copy.deepcopy(value["records"])
    for row in value["warmup_records"]:
        row["phase"] = "warmup"
    return value


def diagnostic(value):
    return report.summarize(value, latency_budget_ms=100,
                            bindings={"dataset": "synthetic", "heldout_query_ids": ["q"]})["diagnostic"]


class OfferAccountingTests(unittest.TestCase):
    def assert_invalid(self, value):
        result = diagnostic(value)
        self.assertFalse(result["accounting_valid"])
        self.assertFalse(result["harness_valid"])
        self.assertEqual(result["verdict"], "INVALID_HARNESS")
        for key in ("throughput_status", "latency_status", "recall_status"):
            self.assertEqual(result[key], "NOT_RUN")

    def test_complete_warmup_and_configured_zero_are_valid(self):
        for value in (warm_run(), fixture()):
            with self.subTest(warmup=value["config"]["warmup_seconds"]):
                result = diagnostic(value)
                self.assertTrue(result["accounting_valid"])
                self.assertTrue(result["harness_valid"])
                self.assertEqual(result["verdict"], "PASS")
                self.assertEqual(result["successful_rps"], 40)
                self.assertEqual(result["offered"], 40)

    def test_missing_warmup_offer_invalidates_before_decision(self):
        value = warm_run()
        value["warmup_records"].pop(3)
        self.assert_invalid(value)

    def test_duplicate_warmup_offer_invalidates_before_decision(self):
        value = warm_run()
        value["warmup_records"][3] = copy.deepcopy(value["warmup_records"][2])
        self.assert_invalid(value)

    def test_empty_configured_warmup_invalidates_before_decision(self):
        value = warm_run()
        value["warmup_records"] = []
        self.assert_invalid(value)

    def test_wrong_phase_warmup_invalidates_before_decision(self):
        value = warm_run()
        value["warmup_records"][3]["phase"] = "measured"
        self.assert_invalid(value)

    def test_wrong_scheduled_position_invalidates_before_decision(self):
        value = warm_run()
        value["warmup_records"][3]["scheduled_offset_seconds"] += .025
        self.assert_invalid(value)

    def test_extra_offer_even_with_complete_id_set_is_invalid(self):
        value = warm_run()
        value["warmup_records"].append(copy.deepcopy(value["warmup_records"][0]))
        self.assert_invalid(value)

    def test_wrong_client_warmup_invalidates_before_decision(self):
        value = warm_run()
        value["warmup_records"][3]["client"] = 4
        self.assert_invalid(value)

    def test_boolean_id_is_not_a_configured_integer_offer(self):
        value = warm_run()
        value["warmup_records"][0]["offer_id"] = False
        self.assert_invalid(value)

    def test_invalid_status_warmup_is_invalid_harness(self):
        value = warm_run()
        value["warmup_records"][3]["status"] = "UNKNOWN"
        self.assert_invalid(value)

    def test_warmup_count_is_bound_to_configured_duration(self):
        value = warm_run()
        value["config"]["warmup_seconds"] = 2
        self.assert_invalid(value)

    def test_nonintegral_configured_warmup_cannot_round_into_pass(self):
        value = warm_run()
        value["config"]["warmup_seconds"] = 1.001
        self.assert_invalid(value)

    def test_negative_warmup_and_unconfigured_extra_warmup_are_invalid(self):
        for duration in (-1, 0):
            with self.subTest(duration=duration):
                value = warm_run()
                value["config"]["warmup_seconds"] = duration
                self.assert_invalid(value)

    def test_complete_legacy_warmup_timestamps_derive_lag(self):
        value = warm_run()
        for row in value["warmup_records"]:
            row.pop("dispatcher_lag_seconds")
        self.assertEqual(diagnostic(value)["verdict"], "PASS")

    def test_missing_or_incoherent_warmup_lag_cannot_qualify(self):
        for kind in ("missing", "incoherent", "negative", "bool"):
            with self.subTest(kind=kind):
                value = warm_run()
                row = value["warmup_records"][3]
                if kind == "missing":
                    row.pop("dispatcher_lag_seconds")
                    row.pop("dispatched_at")
                else:
                    row["dispatcher_lag_seconds"] = {"incoherent": .01, "negative": -.01,
                                                     "bool": False}[kind]
                result = diagnostic(value)
                self.assertEqual(result["verdict"], "INVALID_HARNESS")
                self.assertEqual(result["dispatcher_lag"]["warmup"]["invalid_lag_offer_ids"], [3])


class QueueTimingTests(unittest.IsolatedAsyncioTestCase):
    async def test_dispatcher_lag_is_excluded_from_worker_queue_wait(self):
        class Clock:
            value = 100.0

            def now(self):
                return self.value

            async def sleep(self, delay):
                self.value += delay + .01
                await asyncio.sleep(0)

        class Client:
            async def open(self):
                pass

            async def request(self, query):
                await asyncio.sleep(0)
                return {"ids": [query["id"]]}

            async def aclose(self):
                pass

        value = await load.run([{"id": "q"}], lambda _: Client(),
                               load.RunConfig(duration_seconds=.2, warmup_seconds=0), clock=Clock())
        row = next(r for r in value["records"] if r["status"] == "OK" and r["dispatcher_lag_seconds"] > 0)
        self.assertAlmostEqual(row["dispatcher_lag_seconds"], .01)
        self.assertAlmostEqual(row["started_at"] - row["dispatched_at"], .025)
        self.assertAlmostEqual(row["queue_seconds"], .025)
        self.assertAlmostEqual(row["latency_seconds"], row["completed_at"] - row["scheduled_at"])


class MeasuredPreparationTests(unittest.IsolatedAsyncioTestCase):
    """S4b deciding controls: a drained warmup, live idle clients and a new t0."""
    def require_hook(self):
        import inspect
        self.assertIn("before_measured", inspect.signature(load.run).parameters,
                      "S4b requires an optional after-warmup preparation hook")

    async def test_preparation_runs_after_drain_before_new_origin_with_all_clients_open(self):
        self.require_hook()
        import time
        from test_b02_load import Client
        clients = [Client(i, delay=.04) for i in range(8)]
        marks = []
        async def prepare(open_clients, warmup, config):
            self.assertEqual(tuple(clients), open_clients)
            self.assertTrue(all(c.opened and not c.closed for c in clients))
            self.assertEqual(len(warmup), 1)
            self.assertEqual(warmup[0]["status"], "OK")
            self.assertEqual(sum(c.calls for c in clients), 1)
            await asyncio.sleep(.04)
            marks.append(time.monotonic())
            return {"status": "fixture-preparation-only"}
        value = await load.run([{"id": "q"}], lambda i: clients[i],
                               load.RunConfig(duration_seconds=.025, warmup_seconds=.025),
                               before_measured=prepare)
        self.assertEqual(len(marks), 1)
        self.assertGreaterEqual(value["measured_start"], marks[0])
        self.assertEqual(value["preparation"], {"status": "fixture-preparation-only"})
        self.assertEqual((len(value["records"]), sum(c.calls for c in clients)), (1, 2))
        self.assertTrue(all(c.closed for c in clients))

    async def test_preparation_receives_a_private_warmup_copy(self):
        self.require_hook()
        from test_b02_load import Client
        async def prepare(clients, warmup, config):
            warmup[0]["status"] = "ERROR"
            warmup[0]["response"]["ids"].append("foreign")
            warmup[0]["scheduled_at"] = -1
        value = await load.run([{"id": "q"}], lambda i: Client(i),
                               load.RunConfig(duration_seconds=.025, warmup_seconds=.025),
                               before_measured=prepare)
        row = value["warmup_records"][0]
        self.assertEqual((row["status"], row["response"]["ids"]), ("OK", ["q"]))
        self.assertGreater(row["scheduled_at"], 0)

    async def test_preparation_failure_closes_clients_and_never_offers_measured_work(self):
        self.require_hook()
        from test_b02_load import Client
        clients = [Client(i) for i in range(8)]
        async def prepare(*args):
            raise RuntimeError("fixture-preparation-failed")
        with self.assertRaises(RuntimeError):
            await load.run([{"id": "q"}], lambda i: clients[i],
                           load.RunConfig(duration_seconds=.025, warmup_seconds=.025),
                           before_measured=prepare)
        self.assertEqual(sum(c.calls for c in clients), 1)
        self.assertTrue(all(c.closed for c in clients))

    async def test_cancellation_during_preparation_closes_all_clients(self):
        self.require_hook()
        from test_b02_load import Client
        clients = [Client(i) for i in range(8)]
        entered = asyncio.Event()
        async def prepare(*args):
            entered.set()
            await asyncio.Event().wait()
        task = asyncio.create_task(load.run([{"id": "q"}], lambda i: clients[i],
            load.RunConfig(duration_seconds=.025, warmup_seconds=0), before_measured=prepare))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(sum(c.calls for c in clients), 0)
        self.assertTrue(all(c.closed for c in clients))
