"""C03 runner boundary must honor the expected assertion attribution rule."""
import json
import subprocess
import unittest

from test_mutation_receipts import mutator


class TeamRule(unittest.TestCase):
    expected = {'expected.test'}

    def result(self, code, failures=(), errors=(), stdout=None):
        value = {'tests_run': 1, 'failures': list(failures), 'errors': list(errors)}
        body = 'CORTEX_TEST_RESULT=' + json.dumps(value) if stdout is None else stdout
        return subprocess.CompletedProcess(['synthetic'], code, stdout=body, stderr='')

    def assertion(self, name='expected.test', phase='test'):
        return {'id': name, 'phase': phase, 'is_assertion': True, 'traceback': 'AssertionError: synthetic'}

    def test_expected_assertion_and_clean_run(self):
        self.assertEqual(mutator.mutation_status(self.result(1, [self.assertion()]), self.expected), 'killed')
        self.assertEqual(mutator.mutation_status(self.result(0), self.expected), 'survived')

    def test_runner_errors_and_missing_receipts_inconclusive(self):
        for result in (self.result(1, errors=[{'id':'setup','traceback':'ModuleNotFoundError'}]),
                       self.result(1, stdout='runner failed'), self.result(-9, stdout=''),
                       self.result(2, stdout='bad runner arguments')):
            with self.subTest(output=result.stdout, code=result.returncode):
                self.assertEqual(mutator.mutation_status(result, self.expected), 'inconclusive')

    def test_unrelated_fixture_and_malformed_failures_inconclusive(self):
        for row in (self.assertion('unrelated.test'), self.assertion(phase='fixture'), {'id':'expected.test'}):
            with self.subTest(row=row):
                self.assertEqual(mutator.mutation_status(self.result(1, [row]), self.expected), 'inconclusive')
