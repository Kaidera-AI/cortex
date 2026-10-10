"""Artifact qualification: a wrapped dependency failure is never a semantic kill."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import unittest


class OutboxCaller(unittest.TestCase):
    def caller(self):
        path = Path(__file__).resolve().parents[2]/'scripts/mutate_outbox.py'
        self.assertTrue(path.is_file(), 'C06 caller is missing')
        spec = importlib.util.spec_from_file_location('c06_mutation_caller', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def result(self, traceback, code=1, errors=()):
        value = dict(tests_run=1, failures=[dict(id='expected', traceback=traceback,
            phase='test', is_assertion=True)], errors=list(errors))
        return SimpleNamespace(returncode=code, stdout='CORTEX_TEST_RESULT='+json.dumps(value), stderr='')

    def test_wrapped_import_failure_is_inconclusive(self):
        caller = self.caller()
        for name in ('ImportError', 'ModuleNotFoundError'):
            result = self.result(name+': missing dependency\n\nDuring handling of the above exception:\nAssertionError: missing port')
            self.assertEqual(caller.mutation_status(result, {'expected'}), 'inconclusive')

    def test_only_expected_body_assertion_qualifies(self):
        caller = self.caller()
        self.assertEqual(caller.mutation_status(self.result('AssertionError: semantic fault'), {'expected'}), 'killed')
        self.assertEqual(caller.mutation_status(self.result('AssertionError: wrong body'), {'other'}), 'inconclusive')
        self.assertEqual(caller.mutation_status(self.result('AssertionError: signal', -9), {'expected'}), 'inconclusive')
        self.assertEqual(caller.mutation_status(self.result('AssertionError: body', errors=[dict(id='other',traceback='RuntimeError: fixture')]), {'expected'}), 'inconclusive')
