"""The C04 mutation caller must reject operational and fixture failure receipts."""
import importlib.util
import json
from pathlib import Path
import subprocess
import unittest

path = Path(__file__).resolve().parents[2] / 'scripts/mutate_auth.py'
spec = importlib.util.spec_from_file_location('auth_mutation_verifier', path)
mutator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mutator)


class AuthMutationReceipts(unittest.TestCase):
    def result(self, code=1, phase='test', errors=()):
        value = {'tests_run': 1, 'failures': [{'id': 'expected.test', 'phase': phase,
                 'is_assertion': True, 'traceback': 'AssertionError: synthetic'}],
                 'errors': list(errors)}
        return subprocess.CompletedProcess([], code, 'CORTEX_TEST_RESULT=' + json.dumps(value), '')

    def test_operational_and_fixture_failures_are_inconclusive(self):
        values = [self.result(code=-9), self.result(phase='fixture'),
                  self.result(errors=[{'id': 'setup', 'traceback': 'RuntimeError: fixture'}]),
                  subprocess.CompletedProcess([], 1, 'missing report', '')]
        for value in values:
            with self.subTest(code=value.returncode, output=value.stdout):
                self.assertEqual(mutator.mutation_status(value, {'expected.test'}), 'inconclusive')

    def test_expected_body_and_wrong_assertion_remain_distinct(self):
        value = self.result()
        self.assertEqual(mutator.mutation_status(value, {'expected.test'}), 'killed')
        self.assertEqual(mutator.mutation_status(value, {'other.test'}), 'inconclusive')
