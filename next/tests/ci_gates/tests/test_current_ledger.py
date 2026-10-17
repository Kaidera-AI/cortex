"""Current published ledger node set; static source controls, no PostgreSQL."""
import ast
import importlib.util
import json
from pathlib import Path
import unittest
ROOT=Path(__file__).resolve().parents[4]
SUITE='next/release/ship/install/tests/test_ledger_acl_pg.py'
class CurrentLedger(unittest.TestCase):
    def test_all_eleven_actual_ledger_methods_are_required_by_the_worker(self):
        spec=importlib.util.spec_from_file_location('current_ledger_worker',ROOT/'.github/scripts/pytest_pg_worker.py')
        worker=importlib.util.module_from_spec(spec);spec.loader.exec_module(worker)
        self.assertEqual(len(worker.NATIVE_LEDGER_NODES),11)
        expected=[]
        for cls in ast.parse((ROOT/SUITE).read_text()).body:
            if isinstance(cls,ast.ClassDef):
                expected += [SUITE+'::'+cls.name+'::'+n.name for n in cls.body if isinstance(n,ast.FunctionDef) and n.name.startswith('test_')]
        self.assertEqual(set(worker.NATIVE_LEDGER_NODES),set(expected))
    def test_current_ledger_witness_pins_real_bytes_and_runs_no_native_test_offline(self):
        import hashlib
        profile=json.loads((ROOT/'.github/next-offline-suites.json').read_bytes())
        row=next(r for r in profile['excluded'] if r['path']==SUITE)
        self.assertEqual(row['evidence']['sha256'],hashlib.sha256((ROOT/SUITE).read_bytes()).hexdigest())
        self.assertNotIn(SUITE,profile['run'])
        self.assertIn('next/tests/ci_gates/tests/test_current_ledger.py',profile['run'])
if __name__=='__main__':unittest.main()
