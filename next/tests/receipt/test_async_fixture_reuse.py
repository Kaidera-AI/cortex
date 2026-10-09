"""Fixture reuse must not turn an expected method's assertion into a body kill."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_receipts import classify, report, suite

HERE = Path(__file__).resolve().parent
HELPER = HERE.parent / 'test_receipts.py'
EXPECTED = 'test_probe.Probe.test_expected'


class AsyncFixtureReuse(unittest.TestCase):
    def probe(self, source):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / 'test_probe.py').write_text(source)
            result = suite(directory)
        print('CORTEX_PROBE_RESULT=' + json.dumps({
            'expected': EXPECTED, 'exit_code': result.returncode,
            'stdout': result.stdout, 'stderr': result.stderr,
            'report': report(result), 'classification': classify(result, {EXPECTED}),
        }), flush=True)
        return result

    def test_mike_unchanged_reuse_probes(self):
        # Copy only the location/context; Mike's executable/assertion bytes are unchanged.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            helper = root / 'head/next/tests/test_receipts.py'
            helper.parent.mkdir(parents=True)
            shutil.copyfile(HELPER, helper)
            probe = root / 'fixture-reuse-probe.py'
            shutil.copyfile(HERE / 'fixture-reuse-probe.py', probe)
            result = subprocess.run([sys.executable, str(probe)], capture_output=True, text=True)
            rows = json.loads((root / 'fixture-reuse-probe.json').read_text())
        print('CORTEX_REUSE_RESULT=' + json.dumps({
            'probe_sha256': hashlib.sha256((HERE / 'fixture-reuse-probe.py').read_bytes()).hexdigest(),
            'exit_code': result.returncode, 'stdout': result.stdout,
            'stderr': result.stderr, 'rows': rows,
        }), flush=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(rows), 5)
        self.assertTrue(all(row['classified'] == row['expected'] for row in rows))

    def test_async_teardown_reuse_is_inconclusive(self):
        result = self.probe('''import unittest
class Probe(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = False
    async def asyncTearDown(self):
        self.fixture = True
        await self.test_expected()
    async def test_expected(self):
        if self.fixture:
            self.fail('teardown reused expected method')
        print('BODY_EXECUTED', flush=True)
''')
        self.assertEqual(result.returncode, 1)
        self.assertIn('BODY_EXECUTED', result.stdout)
        self.assertEqual(classify(result, {EXPECTED}), 'inconclusive')
        self.assertEqual(report(result)['failures'][0]['phase'], 'fixture')

    def test_async_case_sync_cleanup_reuse_is_inconclusive(self):
        result = self.probe('''import unittest
class Probe(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = False
        self.addCleanup(self.cleanup)
    def cleanup(self):
        self.fixture = True
        self.test_expected()
    def test_expected(self):
        if self.fixture:
            self.fail('sync cleanup reused expected method')
        print('BODY_EXECUTED', flush=True)
''')
        self.assertEqual(result.returncode, 1)
        self.assertIn('BODY_EXECUTED', result.stdout)
        self.assertEqual(classify(result, {EXPECTED}), 'inconclusive')
        self.assertEqual(report(result)['failures'][0]['phase'], 'fixture')

    def test_genuine_async_body_helper_assertion_is_killed(self):
        result = self.probe('''import unittest
class Probe(unittest.IsolatedAsyncioTestCase):
    async def helper(self):
        self.fail('genuine body through helper')
    async def test_expected(self):
        await self.helper()
''')
        self.assertEqual(result.returncode, 1)
        self.assertEqual(classify(result, {EXPECTED}), 'killed')
        self.assertEqual(report(result)['failures'][0]['phase'], 'test')
