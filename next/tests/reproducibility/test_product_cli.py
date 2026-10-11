import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class ProductCLI(unittest.TestCase):
    def test_actual_entrypoint_measures_whole_bytes_and_keeps_fixture_scope(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t)
            a, b = p / "a.tar", p / "b.tar"
            a.write_bytes(b"whole")
            b.write_bytes(b"whole")
            argv = [
                sys.executable,
                "-m",
                "cortex_core.release.build",
                "compare",
                "--a",
                str(a),
                "--b",
                str(b),
            ]
            good = subprocess.run(
                argv, env=os.environ.copy(), capture_output=True, timeout=10
            )
            self.assertEqual(good.returncode, 0, good.stderr)
            report = json.loads(good.stdout)
            self.assertEqual(report["qualification_scope"], "fixture")
            self.assertFalse(report["native_build_qualified"])
            b.write_bytes(b"changed")
            bad = subprocess.run(
                argv, env=os.environ.copy(), capture_output=True, timeout=10
            )
            self.assertEqual(bad.returncode, 1, bad.stderr)
            self.assertEqual(json.loads(bad.stdout)["result"], "RED")
