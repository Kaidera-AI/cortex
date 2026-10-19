"""Strict causal outcome controls and retained inventory; no PG pass claim."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))
from next.tests.floor import release_profile as floor

NODE = "test_probe.Probe.test_expected"


def process(code=1, phase="test", ids=None, tests_run=1, errors=None):
    ids = [NODE] if ids is None else ids
    value = dict(tests_run=tests_run, selected=[NODE], skipped=[], failures=[
        dict(id=node, phase=phase, is_assertion=True, traceback="AssertionError: synthetic",
             exception_type="builtins.AssertionError") for node in ids], errors=errors or [])
    return subprocess.CompletedProcess(["synthetic-control"], code,
        "CORTEX_TEST_RESULT=" + json.dumps(value), "")


class Admission(unittest.TestCase):
    def test_oracle_controls(self):
        self.assertEqual(floor.oracle_controls(process(), {NODE}), "killed")

    def test_only_exact_expected_body_kills(self):
        for value in (process(3), process(124), process(-9), process(phase="fixture"),
                      process(ids=["other"]), process(ids=[NODE, "other"]),
                      process(tests_run=0), process(errors=[dict(id=NODE, traceback="import error")]),
                      subprocess.CompletedProcess([], 1, "", "")):
            with self.subTest(value=value):
                self.assertEqual(floor.oracle_controls(value, {NODE}), "inconclusive")


    def test_required_skip_xfail_and_duplicate_json_are_inconclusive(self):
        for key in ("skipped", "expected_failures", "unexpected_successes"):
            value = json.loads(process().stdout.split("=", 1)[1])
            value[key] = [NODE]
            result = subprocess.CompletedProcess([], 1, "CORTEX_TEST_RESULT=" + json.dumps(value), "")
            self.assertEqual(floor.oracle_controls(result, {NODE}), "inconclusive")
        result = process()
        result.stdout = result.stdout.replace('"tests_run": 1', '"tests_run": 1, "tests_run": 1')
        self.assertEqual(floor.oracle_controls(result, {NODE}), "inconclusive")

    def test_missing_extra_skipped_or_duplicate_nodes_refuse(self):
        self.assertEqual(floor.oracle_controls(process(tests_run=2), {NODE}), "inconclusive")
        self.assertEqual(floor.oracle_controls(process(ids=[NODE, NODE]), {NODE}), "inconclusive")
        self.assertEqual(floor.oracle_controls(process(), set()), "inconclusive")
        self.assertEqual(floor.oracle_controls(process(0, ids=[]), {NODE}), "survived")
        self.assertEqual(floor.oracle_controls(process(0, ids=[], tests_run=2), {NODE}), "inconclusive")

    def test_all_prior_admissions_and_107_plus_32_retained(self):
        result = floor.profile(ROOT, ROOT / ".github/next-offline-suites.json")
        baseline = json.loads((ROOT / ".github/next-offline-suites.json").read_text())
        self.assertEqual(len(result["floor_nodes"]), 107)
        self.assertEqual(len(result["crypto_nodes"]), 32)
        self.assertEqual(len(result["real_pg_required"]), 81)
        self.assertEqual(result["prior_run"], baseline["run"])
        self.assertEqual(result["prior_excluded"], baseline["excluded"])
        self.assertEqual(result["passed"], 0)
        self.assertEqual(result["qualification"], "inventory_only")


    def test_pinned_retained_profile_is_admitted(self):
        try:
            result = floor.profile(ROOT, ROOT / ".github/next-offline-suites.json")
        except floor.ReceiptRefusal as error:
            self.fail("BODY: accepted retained inventory refused: " + error.code)
        self.assertEqual(len(result["floor_nodes"]), 107)
        self.assertEqual(len(set(result["crypto_nodes"])), 32)

    def test_profile_detects_source_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(floor.ReceiptRefusal):
                floor.profile(directory, ROOT / ".github/next-offline-suites.json")


if __name__ == "__main__":
    unittest.main()
