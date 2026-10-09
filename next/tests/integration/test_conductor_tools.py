"""Proof must refuse a result if its source changes during the child check."""

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import mutate_conductor


class ConductorToolTests(unittest.TestCase):
    def test_source_drift_during_check_is_inconclusive(self):
        original = mutate_conductor.SUPERVISOR.read_bytes()

        def drift(*args, **kwargs):
            mutate_conductor.SUPERVISOR.write_bytes(original + b"\n# synthetic receipt drift\n")
            return subprocess.CompletedProcess(args[0], 0, "Ran 1 test\nOK\ncleanup: PASS\n", "")

        try:
            with tempfile.TemporaryDirectory() as directory:
                with patch.object(mutate_conductor.subprocess, "run", side_effect=drift):
                    with self.assertRaisesRegex(RuntimeError, "INCONCLUSIVE.*source"):
                        mutate_conductor.execute(Path(directory), "drift")
        finally:
            mutate_conductor.SUPERVISOR.write_bytes(original)
