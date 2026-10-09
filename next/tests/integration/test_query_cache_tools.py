"""P01 mutation admission regression checks, with synthetic subprocess results."""

import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import mutate_query_cache


class CacheToolTests(unittest.TestCase):
    def test_setup_errors_cannot_count_as_semantic_kills(self):
        bad = subprocess.CompletedProcess(
            [],
            1,
            "cleanup: PASS\n",
            "ERROR: test_setup (test_query_cache.CacheTests.test_setup)\nRan 34 tests\nFAILED (errors=1)\n",
        )
        with (
            tempfile.TemporaryDirectory() as scratch,
            patch.object(sys, "argv", ["mutate_query_cache.py", scratch]),
            patch.object(mutate_query_cache.subprocess, "run", return_value=bad),
        ):
            with self.assertRaises(RuntimeError):
                mutate_query_cache.run()

    def test_clean_baseline_precedes_every_source_or_sql_edit(self):
        originals = {m[0]: m[0].read_bytes() for m in mutate_query_cache.MUTATIONS}

        def failed_baseline(command, **kwargs):
            for path, original in originals.items():
                self.assertEqual(
                    path.read_bytes(), original, "Edited before clean baseline"
                )
            return subprocess.CompletedProcess(
                [], 1, "cleanup: PASS\n", "FAILED (errors=1)\n"
            )

        with (
            tempfile.TemporaryDirectory() as scratch,
            patch.object(sys, "argv", ["mutate_query_cache.py", scratch]),
            patch.object(
                mutate_query_cache.subprocess, "run", side_effect=failed_baseline
            ),
        ):
            with self.assertRaises(RuntimeError):
                mutate_query_cache.run()
