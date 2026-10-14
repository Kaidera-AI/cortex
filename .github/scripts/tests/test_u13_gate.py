"""Frozen exact-inventory CI oracle; these unit fixtures are not PG evidence."""
import importlib.util
from pathlib import Path
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'pytest_u13_worker.py'

class ExactU13Gate(unittest.TestCase):
    def gate(self):
        self.assertTrue(SCRIPT.is_file(), 'U13 exact-node outcome gate is absent')
        spec = importlib.util.spec_from_file_location('u13_gate_oracle', SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.valid

    def packet(self):
        nodes = ['fixture::test_' + str(i) for i in range(8)]
        return nodes, {'selected': nodes[:], 'deselected': [], 'reports': [
            {'nodeid': node, 'when': phase, 'outcome': 'passed', 'wasxfail': False}
            for node in nodes for phase in ('setup', 'call', 'teardown')], 'pytest_exit': 0}

    def test_complete_eight_actual_call_phases_pass(self):
        nodes, packet = self.packet()
        self.assertTrue(self.gate()(nodes, packet))

    def test_removed_or_zero_selected_never_pass(self):
        for count in (7, 0):
            nodes, packet = self.packet()
            packet['selected'] = nodes[:count]
            packet['reports'] = [r for r in packet['reports'] if r['nodeid'] in packet['selected']]
            self.assertFalse(self.gate()(nodes, packet))

    def test_skip_and_xfail_and_xpass_never_pass(self):
        for outcome, xfail in [('skipped', False), ('skipped', True), ('passed', True)]:
            nodes, packet = self.packet()
            packet['reports'][1].update(outcome=outcome, wasxfail=xfail)
            self.assertFalse(self.gate()(nodes, packet))

    def test_deselection_fails_even_when_eight_nodes_pass(self):
        nodes, packet = self.packet()
        packet['deselected'] = ['unexpected::test_extra']
        self.assertFalse(self.gate()(nodes, packet))

    def test_body_or_setup_or_teardown_failure_never_pass(self):
        for index in (0, 1, 2):
            nodes, packet = self.packet()
            packet['reports'][index]['outcome'] = 'failed'
            self.assertFalse(self.gate()(nodes, packet))

    def test_missing_duplicate_or_foreign_phase_refuses(self):
        for change in ('missing', 'duplicate', 'foreign'):
            nodes, packet = self.packet()
            if change == 'missing': packet['reports'].pop()
            elif change == 'duplicate': packet['reports'].append(packet['reports'][0].copy())
            else: packet['reports'][0]['nodeid'] = 'foreign::test_extra'
            self.assertFalse(self.gate()(nodes, packet))

    def test_nonzero_exit_or_duplicate_inventory_refuses(self):
        nodes, packet = self.packet()
        packet['pytest_exit'] = 3
        self.assertFalse(self.gate()(nodes, packet))
        nodes, packet = self.packet()
        packet['selected'][-1] = packet['selected'][0]
        self.assertFalse(self.gate()(nodes, packet))

if __name__ == '__main__': unittest.main()
