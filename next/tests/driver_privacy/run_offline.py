"""Offline receipt runner; forbids live socket/process effects."""
import hashlib
import json
import os
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'tests'), str(Path(__file__).parent)]
forbidden = []
def audit(event, args):
    if event in {'socket.connect', 'subprocess.Popen', 'os.system'}:
        forbidden.append(event)
        raise AssertionError('offline oracle forbids live effects')
sys.addaudithook(audit)
from test_receipts import AssertionResult
import test_driver_logs as oracle
result = unittest.TextTestRunner(verbosity=2, resultclass=AssertionResult).run(
    unittest.defaultTestLoader.loadTestsFromModule(oracle))
value = {'python': sys.version.split()[0], 'machine': os.uname().machine,
    'tests_run': result.testsRun, 'failures': result.assertions,
    'errors': [{'id': t.id(), 'traceback': b} for t, b in result.errors],
    'skipped': result.skipped, 'forbidden_effect_events': forbidden,
    'source_sha256': hashlib.sha256(Path(oracle.c11a.__file__).read_bytes()).hexdigest(),
    'driver_fixture_sha256': hashlib.sha256(oracle.SOURCE).hexdigest(),
    'exact_method_ast_sha256': oracle.BINDINGS, 'observations': oracle.OBSERVATIONS,
    'scope': 'synthetic offline exact pinned method replay; explicit I/O adapters; no installed/native/backend/real credential exposure claim'}
Path(sys.argv[1]).write_text(json.dumps(value, indent=2) + '\n')
print(json.dumps({k: value[k] for k in ('tests_run', 'python', 'machine', 'source_sha256')}))
raise SystemExit(0 if result.wasSuccessful() else 1)
