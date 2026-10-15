"""Additive P1-MECH-COUNT-001 oracle; prior tests/fixtures stay byte-exact."""
import unittest

from read_meter import measure
import test_planner


class ComposedReadMeter(unittest.TestCase):
    def test_same_sealed_reader_counts_include_rc2_alias_on_all_four_positives(self):
        for target in ('linux/arm64', 'linux/amd64'):
            for route in ('manual', 'kos_prerequisite'):
                with self.subTest(target=target, route=route):
                    result, counts = measure(test_planner.PlannerOracle(), target=target, route=route)
                    self.assertEqual(result.result, 'PASS')
                    self.assertEqual(counts['composed_read_count'], 32)
                    self.assertEqual(counts['request_member_count'], 31)
                    self.assertEqual(counts['direct_p1_read_count'], 31)
                    self.assertEqual(counts['rc2_config_alias_read_count'], 1)
                    self.assertEqual(counts['old_module_wrapper_count'], 31)
                    self.assertEqual(len(counts['read_member_events']), 32)
                    alias = [event for event in counts['read_member_events']
                             if event['caller_module'] == 'cortex_core.runtime.config']
                    self.assertEqual(len(alias), 1)
                    self.assertEqual(alias[0]['member_name'], 'runtime.lock.json')
                    self.assertEqual(alias[0]['caller_function'], 'admit_config')
                    self.assertEqual(result.authority_status, 'UNBOUND')
                    self.assertFalse(result.effect_authority)
                    self.assertFalse(result.native_qualified)


if __name__ == '__main__':
    unittest.main()
