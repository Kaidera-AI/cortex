"""Shared receipt contract, exercised by real subprocesses and bounded bad reports."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_receipts import MARKER, classify, report, suite

EXPECTED = "test_probe.Probe.test_expected"


class Classifier(unittest.TestCase):
    def probe(self, source, expected=EXPECTED):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "test_probe.py").write_text(source)
            result = suite(directory)
        print("CORTEX_PROBE_RESULT=" + json.dumps({
            "expected": expected, "exit_code": result.returncode,
            "stdout": result.stdout, "stderr": result.stderr,
            "report": report(result), "classification": classify(result, {expected}),
        }), flush=True)
        return result

    def synthetic(self, value, exit_code=1):
        return subprocess.CompletedProcess(["synthetic"], exit_code,
                                           MARKER + json.dumps(value), "")

    def receipt(self):
        return {"tests_run": 1, "failures": [{"id": EXPECTED, "phase": "test",
                "is_assertion": True, "traceback": "AssertionError: synthetic",
                "exception_type": "builtins.AssertionError"}], "errors": []}

    def test_expected_body_assertion_is_killed(self):
        result = self.probe("import unittest\nclass Probe(unittest.TestCase):\n"
                            "    def test_expected(self):\n        self.fail('body assertion')\n")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(classify(result, {EXPECTED}), "killed")
        row = report(result)["failures"][0]
        self.assertEqual(row["phase"], "test")
        self.assertIs(row["is_assertion"], True)

    def test_unrelated_assertion_is_inconclusive(self):
        result = self.probe("import unittest\nclass Probe(unittest.TestCase):\n"
                            "    def test_other(self):\n        self.fail('wrong test')\n")
        self.assertEqual(classify(result, {EXPECTED}), "inconclusive")

    def test_clean_suite_survives(self):
        result = self.probe("import unittest\nclass Probe(unittest.TestCase):\n"
                            "    def test_expected(self):\n        self.assertTrue(True)\n")
        self.assertEqual(classify(result, {EXPECTED}), "survived")
        self.assertEqual(report(result), {"tests_run": 1, "failures": [], "errors": []})

    def test_sync_fixture_assertions_are_inconclusive(self):
        for phase in ("setUp", "tearDown"):
            with self.subTest(phase=phase):
                result = self.probe("import unittest\nclass Probe(unittest.TestCase):\n"
                    f"    def {phase}(self):\n        self.fail('fixture assertion')\n"
                    "    def test_expected(self):\n        print('BODY_EXECUTED', flush=True)\n")
                self.assertEqual(result.returncode, 1)
                self.assertEqual("BODY_EXECUTED" in result.stdout, phase == "tearDown")
                self.assertEqual(classify(result, {EXPECTED}), "inconclusive")
                self.assertEqual(report(result)["failures"][0]["phase"], "fixture")

    def test_nemo_async_fixture_assertions_are_inconclusive(self):
        # Nemo's real IsolatedAsyncioTestCase probe; only the shared runner changes.
        for phase in ("asyncSetUp", "asyncTearDown"):
            with self.subTest(phase=phase):
                result = self.probe("import unittest\nclass Probe(unittest.IsolatedAsyncioTestCase):\n"
                    f"    async def {phase}(self):\n        self.fail('fixture assertion')\n"
                    "    async def test_expected(self):\n"
                    "        print('BODY_EXECUTED', flush=True)\n        self.assertTrue(True)\n")
                self.assertEqual(result.returncode, 1)
                self.assertEqual("BODY_EXECUTED" in result.stdout, phase == "asyncTearDown")
                self.assertEqual(classify(result, {EXPECTED}), "inconclusive")
                self.assertEqual(report(result)["failures"][0]["phase"], "fixture")

    def test_async_body_assertion_is_killed(self):
        result = self.probe("import unittest\nclass Probe(unittest.IsolatedAsyncioTestCase):\n"
                            "    async def test_expected(self):\n        self.fail('async body')\n")
        self.assertEqual(classify(result, {EXPECTED}), "killed")

    def test_cleanup_and_class_fixture_assertions_are_inconclusive(self):
        sources = [
            "    def test_expected(self):\n        self.addCleanup(self.fail, 'cleanup assertion')\n",
            "    @classmethod\n    def setUpClass(cls):\n        raise AssertionError('class setup')\n"
            "    def test_expected(self):\n        pass\n",
            "    @classmethod\n    def tearDownClass(cls):\n        raise AssertionError('class teardown')\n"
            "    def test_expected(self):\n        pass\n",
        ]
        for source in sources:
            with self.subTest(source=source):
                result = self.probe("import unittest\nclass Probe(unittest.TestCase):\n" + source)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(classify(result, {EXPECTED}), "inconclusive")

    def test_import_body_and_runner_errors_are_inconclusive(self):
        sources = ["raise ImportError('probe import failure')\n",
                   "import unittest\nclass Probe(unittest.TestCase):\n"
                   "    def test_expected(self):\n        raise RuntimeError('body error')\n",
                   "import os\nos._exit(2)\n"]
        for source in sources:
            with self.subTest(source=source):
                result = self.probe(source)
                self.assertEqual(classify(result, {EXPECTED}), "inconclusive")

    def test_nonassertion_failure_exception_is_inconclusive(self):
        result = self.probe("import unittest\nclass Probe(unittest.TestCase):\n"
                            "    failureException = ValueError\n"
                            "    def test_expected(self):\n        raise ValueError('custom failure')\n")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(report(result)["failures"][0]["phase"], "test")
        self.assertIs(report(result)["failures"][0]["is_assertion"], False)
        self.assertEqual(classify(result, {EXPECTED}), "inconclusive")

    def test_expected_subtest_assertion_is_killed(self):
        result = self.probe("import unittest\nclass Probe(unittest.TestCase):\n"
                            "    def test_expected(self):\n        with self.subTest(value=1):\n"
                            "            self.assertEqual(1, 2)\n")
        self.assertEqual(classify(result, {EXPECTED}), "killed")
        self.assertTrue(report(result)["failures"][0]["id"].startswith(EXPECTED + " ("))

    def test_error_alongside_expected_assertion_is_inconclusive(self):
        result = self.probe("import unittest\nclass Probe(unittest.TestCase):\n"
                            "    def test_expected(self):\n        self.fail('body assertion')\n"
                            "    def test_error(self):\n        raise RuntimeError('other error')\n")
        self.assertEqual(classify(result, {EXPECTED}), "inconclusive")
        self.assertEqual(len(report(result)["failures"]), 1)
        self.assertEqual(len(report(result)["errors"]), 1)

    def test_operational_exits_signals_and_exit_mismatch_are_inconclusive(self):
        for exit_code in (-9, -15, 2, 137, 0):
            with self.subTest(exit_code=exit_code):
                self.assertEqual(classify(self.synthetic(self.receipt(), exit_code), {EXPECTED}), "inconclusive")
        result = self.probe("import os,signal\nos.kill(os.getpid(), signal.SIGTERM)\n")
        self.assertLess(result.returncode, 0)
        self.assertEqual(classify(result, {EXPECTED}), "inconclusive")

    def test_malformed_missing_or_duplicate_reports_are_inconclusive(self):
        for output in ("", MARKER + "not JSON", MARKER + "{}",
                       MARKER + json.dumps(self.receipt()) + "\n" + MARKER + json.dumps(self.receipt())):
            with self.subTest(output=output):
                result = subprocess.CompletedProcess(["synthetic"], 1, output, "AssertionError")
                self.assertIsNone(report(result))
                self.assertEqual(classify(result, {EXPECTED}), "inconclusive")

    def test_invalid_report_count_rows_or_assertion_evidence_are_inconclusive(self):
        variants = []
        for count in (True, 0, -1, "1"):
            value = self.receipt(); value["tests_run"] = count; variants.append(value)
        for field in ("id", "traceback", "phase", "is_assertion"):
            value = self.receipt(); value["failures"][0].pop(field); variants.append(value)
        for collection in ("failures", "errors"):
            value = self.receipt(); value[collection] = "invalid"; variants.append(value)
        for value in variants:
            with self.subTest(value=value):
                self.assertEqual(classify(self.synthetic(value), {EXPECTED}), "inconclusive")
