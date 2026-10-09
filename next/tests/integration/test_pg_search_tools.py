"""Deterministic RED-first tool checks; subprocess responses are synthetic."""

import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import run_pg_search
import mutate_pg_search


class ToolTests(unittest.TestCase):
    def test_removal_failure_is_not_success_with_empty_inventory(self):
        def run(command, **kwargs):
            return subprocess.CompletedProcess(
                command, 1 if command[:2] == ["podman", "rm"] else 0, "", ""
            )

        def output(command, **kwargs):
            return "127.0.0.1:51234" if command[:2] == ["podman", "port"] else ""

        with (
            patch.object(run_pg_search.subprocess, "run", side_effect=run),
            patch.object(run_pg_search.subprocess, "check_output", side_effect=output),
        ):
            with self.assertRaises(RuntimeError):
                run_pg_search.main()

    def test_mutation_setup_errors_are_not_semantic_kills(self):
        bad = subprocess.CompletedProcess(
            [],
            1,
            "cleanup: PASS\n",
            "ERROR: test_setup (test_pg_search.SearchTests.test_setup)\nRan 10 tests\nFAILED (errors=1)\n",
        )
        with (
            tempfile.TemporaryDirectory() as scratch,
            patch.object(sys, "argv", ["mutate_pg_search.py", scratch]),
            patch.object(mutate_pg_search.subprocess, "run", return_value=bad),
        ):
            with self.assertRaises(RuntimeError):
                mutate_pg_search.run()

    def test_mutation_clean_baseline_required_before_any_edit(self):
        before = mutate_pg_search.SOURCE.read_bytes()
        bad = subprocess.CompletedProcess(
            [], 1, "cleanup: PASS\n", "Ran 10 tests\nFAILED (errors=1)\n"
        )

        def failed_baseline(command, **kwargs):
            self.assertEqual(
                mutate_pg_search.SOURCE.read_bytes(),
                before,
                "Source mutated before baseline was admitted",
            )
            return bad

        with (
            tempfile.TemporaryDirectory() as scratch,
            patch.object(sys, "argv", ["mutate_pg_search.py", scratch]),
            patch.object(
                mutate_pg_search.subprocess, "run", side_effect=failed_baseline
            ),
        ):
            with self.assertRaises(RuntimeError):
                mutate_pg_search.run()

    def test_only_expected_behavioral_assertion_is_a_semantic_kill(self):
        target = "test_pg_search_review.ReviewTests.test_case"
        good = (
            "FAIL: test_case ("
            + target
            + ")\nAssertionError: behavior differs\nRan 1 test\nFAILED (failures=1)\ncleanup: PASS\n"
        )
        self.assertEqual(mutate_pg_search.classify_kill(1, good, target), "KILLED")
        for bad in [
            good.replace("FAIL:", "ERROR:"),
            good.replace(target, "unrelated.Tests.test_case"),
            good.replace("cleanup: PASS", "cleanup: FAIL"),
            good.replace("AssertionError:", "RuntimeError:"),
        ]:
            self.assertEqual(
                mutate_pg_search.classify_kill(1, bad, target), "INCONCLUSIVE"
            )
        self.assertEqual(
            mutate_pg_search.classify_kill(
                0, "Ran 1 test\nOK\ncleanup: PASS\n", target
            ),
            "SURVIVED",
        )
