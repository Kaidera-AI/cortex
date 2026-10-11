"""Frozen report controls: malformed offer statuses refuse without exceptions."""
import unittest

from test_b02_offer_accounting import diagnostic, warm_run


class StatusInventoryTests(unittest.TestCase):
    def assert_invalid_without_exception(self, value):
        try:
            result = diagnostic(value)
        except Exception as error:
            self.fail(f"malformed offer inventory raised {type(error).__name__}: {error}")
        self.assertFalse(result["accounting_valid"])
        self.assertFalse(result["harness_valid"])
        self.assertEqual(result["verdict"], "INVALID_HARNESS")
        for key in ("throughput_status", "latency_status", "recall_status"):
            self.assertEqual(result[key], "NOT_RUN")

    def test_missing_warmup_status_refuses_without_key_error(self):
        value = warm_run()
        value["warmup_records"][3].pop("status")
        self.assert_invalid_without_exception(value)

    def test_non_string_warmup_status_refuses_without_type_error(self):
        for status in (None, False, 1, [], {}):
            with self.subTest(status=status):
                value = warm_run()
                value["warmup_records"][3]["status"] = status
                self.assert_invalid_without_exception(value)

    def test_missing_or_non_string_measured_status_also_refuses(self):
        for status in ("missing", [], {}):
            with self.subTest(status=status):
                value = warm_run()
                row = value["records"][3]
                if status == "missing":
                    row.pop("status")
                else:
                    row["status"] = status
                self.assert_invalid_without_exception(value)

    def test_invalid_inventory_short_circuits_unavailable_latency_fields(self):
        value = warm_run()
        value["warmup_records"][3].pop("status")
        value["records"][0].pop("latency_seconds")
        self.assert_invalid_without_exception(value)

    def test_status_classification_precedes_missing_identity_fields(self):
        value = warm_run()
        value["warmup_records"][3] = {"status": []}
        self.assert_invalid_without_exception(value)

    def test_complete_valid_inventory_retains_pass(self):
        result = diagnostic(warm_run())
        self.assertTrue(result["accounting_valid"])
        self.assertEqual(result["verdict"], "PASS")
