"""Mike PR30 rework: both newline probes and causal mutation classification."""
import importlib.util
import json
from pathlib import Path
import subprocess
import unittest

from test_contracts import fixture
from cortex_core.contracts import ContractError, validate_event

path=Path(__file__).resolve().parents[2]/'scripts/mutate_contracts.py'
spec=importlib.util.spec_from_file_location('contract_mutator',path)
mutator=importlib.util.module_from_spec(spec);spec.loader.exec_module(mutator)


class ReviewRegressions(unittest.TestCase):
    def test_digest_and_kind_reject_final_newline(self):
        for field in ('payload_sha256','aggregate_kind'):
            value=fixture('event-upsert.json');value[field]+='\n'
            with self.subTest(field=field),self.assertRaises(ContractError):
                validate_event(value)

    def result(self, code, failures=(), errors=(), stdout=None):
        report={'failures':[{'id':name,'traceback':'AssertionError: synthetic probe'} for name in failures],
                'errors':[{'id':'setup','traceback':error} for error in errors]}
        body='CORTEX_TEST_RESULT='+json.dumps(report) if stdout is None else stdout
        return subprocess.CompletedProcess(['synthetic-unittest'],code,stdout=body,stderr='')

    def test_expected_assertion_is_a_kill(self):
        self.assertEqual(mutator.classify(self.result(1,['expected.test']),{'expected.test'}),'killed')

    def test_setup_import_runner_and_wrong_test_are_inconclusive(self):
        probes=[self.result(1,errors=['ModuleNotFoundError']),self.result(1,errors=['setUp failed']),
                self.result(-9,stdout=''),self.result(1,stdout='runner broke'),
                self.result(1,['unrelated.test']),self.result(1,['expected.test'],['ImportError'])]
        for result in probes:
            with self.subTest(code=result.returncode,output=result.stdout):
                self.assertEqual(mutator.classify(result,{'expected.test'}),'inconclusive')

    def test_clean_run_survives(self):
        self.assertEqual(mutator.classify(self.result(0),{'expected.test'}),'survived')
