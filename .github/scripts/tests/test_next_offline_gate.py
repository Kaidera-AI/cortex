"""Frozen desired inventory/outcome contract, written before the runner."""
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'pytest_next_offline.py'


class InventoryOracle(unittest.TestCase):
    def setUp(self):
        self.assertTrue(SCRIPT.is_file(), 'BODY RED: next offline inventory gate is absent')
        spec = importlib.util.spec_from_file_location('next_gate_oracle_subject', SCRIPT)
        self.gate = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.gate)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for name in ('next/tests/test_a.py', 'next/other/b_test.py',
                     'next/docs/runtime/rc0/test_oracle.py'):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('def test_body(): assert True\n')
        self.manifest = {'run': ['next/tests/test_a.py', 'next/docs/runtime/rc0/test_oracle.py'],
                         'excluded': [{'path': 'next/other/b_test.py', 'reason': 'Needs real PostgreSQL.'}]}

    def check(self):
        return self.gate.inventory(self.root, self.manifest)

    def test_complete_partition_including_rc0_oracle(self):
        result = self.check()
        self.assertTrue(result['ok'], result)
        self.assertEqual(len(result['found']), 3)

    def test_new_unlisted_suite_is_red(self):
        (self.root / 'next/tests/test_new.py').write_text('def test_new(): pass\n')
        result = self.check()
        self.assertFalse(result['ok'])
        self.assertEqual(result['missing'], ['next/tests/test_new.py'])

    def test_removed_run_entry_is_red(self):
        self.manifest['run'].pop(0)
        self.assertEqual(self.check()['missing'], ['next/tests/test_a.py'])
        self.assertFalse(self.check()['ok'])

    def test_removed_exclusion_is_red(self):
        self.manifest['excluded'].clear()
        self.assertFalse(self.check()['ok'])

    def test_stale_run_entry_is_red(self):
        self.manifest['run'].append('next/test_gone.py')
        self.assertEqual(self.check()['stale'], ['next/test_gone.py'])
        self.assertFalse(self.check()['ok'])

    def test_duplicate_or_overlap_is_red(self):
        self.manifest['run'].append(self.manifest['run'][0])
        self.assertFalse(self.check()['ok'])
        self.manifest['run'].pop()
        self.manifest['excluded'].append({'path': self.manifest['run'][0], 'reason': 'Overlap.'})
        self.assertFalse(self.check()['ok'])

    def test_blank_or_multiline_reason_is_red(self):
        for reason in ('', '  ', 'First line\nsecond line'):
            with self.subTest(reason=reason):
                self.manifest['excluded'][0]['reason'] = reason
                self.assertFalse(self.check()['ok'])

    def test_glob_escape_non_suite_and_wrong_types_are_red(self):
        for name in ('next/tests/*.py', '../next/test_a.py', '/next/test_a.py', 'next/helper.py', 42):
            with self.subTest(name=name):
                self.manifest['run'].append(name)
                self.assertFalse(self.check()['ok'])
                self.manifest['run'].pop()
        for value in ({'run': 'next/tests/test_a.py', 'excluded': []},
                      {'run': [], 'excluded': {}}, {'run': [], 'excluded': [42]}):
            self.assertFalse(self.gate.inventory(self.root, value)['ok'])

    def test_empty_run_is_red(self):
        self.manifest = {'run': [], 'excluded': [
            {'path': str(p.relative_to(self.root)), 'reason': 'Excluded.'}
            for p in self.root.rglob('*.py')]}
        self.assertFalse(self.check()['ok'])

    def test_symlink_suite_is_red(self):
        (self.root / 'outside.py').write_text('def test_body(): pass\n')
        (self.root / 'next/test_link.py').symlink_to(self.root / 'outside.py')
        self.manifest['run'].append('next/test_link.py')
        self.assertFalse(self.check()['ok'])


class OutcomeOracle(unittest.TestCase):
    def setUp(self):
        self.assertTrue(SCRIPT.is_file(), 'BODY RED: next offline outcome gate is absent')
        spec = importlib.util.spec_from_file_location('next_gate_oracle_subject', SCRIPT)
        self.gate = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.gate)
        self.packet = {'expected': ['next/test_a.py::test_a'], 'selected': ['next/test_a.py::test_a'],
                       'deselected': [], 'pytest_exit': 0, 'gate_exit': 0,
                       'reports': [{'nodeid': 'next/test_a.py::test_a', 'when': when,
                                    'outcome': 'passed', 'wasxfail': False}
                                   for when in ('setup', 'call', 'teardown')]}

    def test_complete_positive(self):
        self.assertTrue(self.gate.receipt_valid(self.packet, 0))

    def test_zero_tests_is_red(self):
        self.packet.update(expected=[], selected=[], reports=[])
        self.assertFalse(self.gate.receipt_valid(self.packet, 0))

    def test_skip_xfail_xpass_and_each_failed_phase_are_red(self):
        for phase in range(3):
            for outcome, wasxfail in [('failed', False), ('skipped', False), ('passed', True)]:
                packet = copy.deepcopy(self.packet)
                packet['reports'][phase].update(outcome=outcome, wasxfail=wasxfail)
                self.assertFalse(self.gate.receipt_valid(packet, 0))

    def test_raw_exit_gate_exit_process_exit_must_agree(self):
        for key in ('pytest_exit', 'gate_exit'):
            packet = {**self.packet, key: 1}
            self.assertFalse(self.gate.receipt_valid(packet, 0))
        self.assertFalse(self.gate.receipt_valid(self.packet, 1))
        self.assertFalse(self.gate.receipt_valid(self.packet, -9))

    def test_missing_duplicate_foreign_phases_and_deselection_are_red(self):
        for reports in (self.packet['reports'][:-1], self.packet['reports'] * 2,
                        self.packet['reports'] + [{'nodeid': 'foreign', 'when': 'call',
                                                  'outcome': 'passed', 'wasxfail': False}]):
            self.assertFalse(self.gate.receipt_valid({**self.packet, 'reports': reports}, 0))
        self.assertFalse(self.gate.receipt_valid({**self.packet, 'deselected': ['hidden']}, 0))

    def test_missing_or_malformed_receipt_is_red(self):
        for packet in (None, {}, {'expected': []}, {'expected': [], 'reports': None}):
            self.assertFalse(self.gate.receipt_valid(packet, 0))


if __name__ == '__main__':
    unittest.main(verbosity=2)
