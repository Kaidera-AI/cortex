"""Stdlib host controls: preflight must not execute a receipt module or load_tests."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

WT = Path.cwd().resolve()
OUT = Path(__file__).resolve().parent
OBSERVATIONS = []


class HostBoundary(unittest.TestCase):
    def probe(self):
        scratch = WT / 'tmp'
        scratch.mkdir(exist_ok=True)
        fixture = WT / 'next/tests/receipt/test_c06_host_boundary_probe.py'
        self.assertFalse(fixture.exists(), 'never overwrite an existing fixture')
        with tempfile.TemporaryDirectory(prefix='cox-host-boundary-', dir=scratch) as folder:
            import_marker = Path(folder) / 'module-imported'
            load_marker = Path(folder) / 'load-tests-called'
            code = (
                'from pathlib import Path\nimport unittest\n'
                f'Path({str(import_marker)!r}).write_text("module imported")\n'
                'class SyntheticReceipt(unittest.TestCase):\n'
                ' def test_fixture(self): pass\n'
                'def load_tests(loader, tests, pattern):\n'
                f' Path({str(load_marker)!r}).write_text("load_tests called")\n'
                ' return loader.loadTestsFromTestCase(SyntheticReceipt)\n'
            )
            fixture.write_text(code)
            try:
                result = subprocess.run(
                    [sys.executable, str(OUT / 'verify-replay-inputs.py'), str(WT)],
                    capture_output=True, text=True,
                    env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'}, timeout=120,
                )
                value = json.loads(result.stdout)
                observed = {'test': self.id(), 'exit_code': result.returncode,
                            'import_marker': import_marker.exists(),
                            'load_tests_marker': load_marker.exists(),
                            'preflight': value, 'stderr': result.stderr,
                            'fixture_sha256': hashlib.sha256(code.encode()).hexdigest()}
                OBSERVATIONS.append(observed)
                self.assertEqual(result.returncode, 1, 'uncommitted fixture must refuse')
                self.assertFalse(value['checks']['product_committed']['passed'])
                return observed
            finally:
                fixture.unlink()

    def test_host_preflight_does_not_import_receipt_module(self):
        observed = self.probe()
        self.assertFalse(observed['import_marker'], 'receipt module executed on host')

    def test_host_preflight_does_not_call_receipt_load_tests(self):
        observed = self.probe()
        self.assertFalse(observed['load_tests_marker'], 'receipt load_tests executed on host')


if __name__ == '__main__':
    tests = unittest.defaultTestLoader.loadTestsFromTestCase(HostBoundary)
    result = unittest.TextTestRunner(verbosity=2).run(tests)
    value = {'tree': subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),
             'tests_run': result.testsRun,
             'failures': [{'id': case.id(), 'traceback': trace} for case, trace in result.failures],
             'errors': [{'id': case.id(), 'traceback': trace} for case, trace in result.errors],
             'observations': OBSERVATIONS,
             'controller_sha256': hashlib.sha256((OUT / 'verify-replay-inputs.py').read_bytes()).hexdigest(),
             'test_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    Path(sys.argv[1]).write_text(json.dumps(value, indent=2) + '\n')
    raise SystemExit(0 if result.wasSuccessful() else 1)
