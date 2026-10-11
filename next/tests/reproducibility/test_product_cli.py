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

class PrepareCLI(unittest.TestCase):
    def test_actual_cli_admits_owner_input_and_stages_without_build(self):
        import importlib.util
        from cortex_core.release.codec import canonical_bytes, sha256
        spec = importlib.util.spec_from_file_location('cli_owner_fixture', Path(__file__).parents[1]/'release/fixture.py')
        fixture = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fixture)
        value, data = fixture.build_input()
        raw = canonical_bytes(value)
        policy_raw = canonical_bytes({'input_sha256':sha256(raw), 'policy_sha256':'e'*64, 'qualification_scope':'fixture'})
        with tempfile.TemporaryDirectory() as t:
            p = Path(t)
            for name, body in data.items():
                path = p/'inputs'/name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(body)
                path.chmod(0o644)
            (p/'input.json').write_bytes(raw)
            (p/'policy.json').write_bytes(policy_raw)
            argv = [sys.executable, '-m', 'cortex_core.release.build', 'prepare', '--input', str(p/'input.json'), '--input-sha256', sha256(raw), '--input-root', str(p/'inputs'), '--policy', str(p/'policy.json'), '--policy-record-sha256', sha256(policy_raw), '--output', str(p/'clone')]
            result = subprocess.run(argv, env=os.environ.copy(), capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report['result'], 'PREPARED')
            self.assertEqual(report['input_sha256'], sha256(raw))
            self.assertFalse(report['native_build_qualified'])
            self.assertEqual((p/'clone/source/module.py').read_bytes(), data['source/module.py'])
