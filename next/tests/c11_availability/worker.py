"""Public-source receipt driver; never emits environment or DSNs."""
import ast
import hashlib
import importlib.metadata
import json
from pathlib import Path
import runpy
import sys
import unittest
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))
from cortex_core.api import c11a
source = Path(c11a.__file__).resolve()
executed = set()
def trace(frame, event, arg):
    if event == 'call' and frame.f_code.co_filename == str(source):
        executed.add(frame.f_code.co_qualname)
    return trace
print('CORTEX_LOADED=' + json.dumps({'file': str(source), 'sha256': hashlib.sha256(source.read_bytes()).hexdigest(), 'python': sys.version.split()[0], 'psycopg': importlib.metadata.version('psycopg'), 'suite': sys.argv[1]}), flush=True)
original_run = unittest.TextTestRunner.run
def measured_run(runner, tests):
    result = original_run(runner, tests)
    print('CORTEX_SKIPS=' + json.dumps({'tests_run': result.testsRun, 'skipped': result.skipped}), flush=True)
    return result
unittest.TextTestRunner.run = measured_run
if len(sys.argv) > 2:
    names = sys.argv[2:]
    if not all(name.startswith('test_') and len(name.split('.')) == 3 for name in names):
        raise ValueError('explicit test method selector required')
    sys.path.insert(0, str(Path(sys.argv[1])))
    unittest.defaultTestLoader.discover = lambda directory: unittest.defaultTestLoader.loadTestsFromNames(names)
sys.settrace(trace)
try:
    sys.argv = [str(ROOT / 'tests/test_receipts.py'), sys.argv[1]]
    runpy.run_path(sys.argv[0], run_name='__main__')
finally:
    sys.settrace(None)
    print('CORTEX_EXECUTED=' + json.dumps(sorted(executed)), flush=True)
