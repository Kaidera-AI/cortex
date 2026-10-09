"""Malformed and nonassertion runner failures cannot prove a mutation kill."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
from test_receipts import classify, suite


class ReceiptShape(unittest.TestCase):
    def test_missing_traceback_or_assertion_evidence_is_inconclusive(self):
        valid = {'id': 'expected.test', 'phase': 'test', 'is_assertion': True,
                 'traceback': 'AssertionError: synthetic probe'}
        for field in ('traceback', 'is_assertion'):
            row = dict(valid); row.pop(field)
            value = {'tests_run': 1, 'failures': [row], 'errors': []}
            result = subprocess.CompletedProcess(['synthetic'], 1,
                      stdout='CORTEX_TEST_RESULT=' + json.dumps(value), stderr='')
            with self.subTest(field=field):
                self.assertEqual(classify(result, {'expected.test'}), 'inconclusive')

    def test_custom_nonassertion_failure_is_inconclusive(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / 'test_probe.py').write_text('''import unittest
class Probe(unittest.TestCase):
    failureException = ValueError
    def test_expected(self):
        raise ValueError('nonassertion runner failure')
''')
            result = suite(directory)
        self.assertEqual(classify(result, {'test_probe.Probe.test_expected'}), 'inconclusive')
