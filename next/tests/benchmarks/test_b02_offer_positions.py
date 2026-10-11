"""Absolute timestamps must agree with the configured phase offer positions."""
import unittest

from test_b02_offer_accounting import diagnostic, warm_run


class OfferPositionTests(unittest.TestCase):
    def test_coherent_lag_cannot_hide_a_retimed_warmup_offer(self):
        value = warm_run()
        row = value["warmup_records"][3]
        row["scheduled_at"] += .025
        row["dispatched_at"] += .025
        # Derived and reported lag remain coherent; the offered series is wrong.
        result = diagnostic(value)
        self.assertFalse(result["accounting_valid"])
        self.assertFalse(result["harness_valid"])
        self.assertEqual(result["verdict"], "INVALID_HARNESS")
        for key in ("throughput_status", "latency_status", "recall_status"):
            self.assertEqual(result[key], "NOT_RUN")

    def test_reordered_complete_phase_is_still_accounted(self):
        value = warm_run()
        value["warmup_records"].reverse()
        self.assertTrue(diagnostic(value)["accounting_valid"])
        self.assertEqual(diagnostic(value)["verdict"], "PASS")
