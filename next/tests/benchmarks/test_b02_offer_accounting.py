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
