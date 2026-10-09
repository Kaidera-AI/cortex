"""Mutation proof must not confuse infrastructure failure with an assertion."""
import importlib.util
import json
from pathlib import Path
import subprocess
import unittest

script=Path(__file__).resolve().parents[2]/'scripts/mutate_schema.py'
spec=importlib.util.spec_from_file_location('c03_mutation_verifier',script)
mutator=importlib.util.module_from_spec(spec)
spec.loader.exec_module(mutator)


class MutationReceipts(unittest.TestCase):
    def result(self, code, stderr=''):
        failures = [{'id':'expected.test','phase':'test','is_assertion':True,'traceback':stderr}] if 'AssertionError' in stderr else []
        errors = [{'id':'setup','traceback':stderr}] if stderr and not failures else []
        value={'tests_run':1,'failures':failures,'errors':errors}
        return subprocess.CompletedProcess(['synthetic-unittest'],code,stdout='CORTEX_TEST_RESULT='+json.dumps(value),stderr=stderr)

    def test_signal_failure_invalidates_proof(self):
        for code in (-9,-15):
            with self.subTest(code=code), self.assertRaises(SystemExit):
                mutator.mutation_killed(self.result(code))

    def test_postgres_failure_invalidates_proof(self):
        with self.assertRaises(SystemExit):
            mutator.mutation_killed(self.result(1,'psycopg.OperationalError'))

    def test_assertion_and_success_are_distinguished(self):
        self.assertTrue(mutator.mutation_killed(self.result(1,'AssertionError: regression\nFAILED (failures=1)'), {'expected.test'}))
        self.assertFalse(mutator.mutation_killed(self.result(0)))
