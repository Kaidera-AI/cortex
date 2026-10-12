"""Desired recovery assertions over the freshly executed exact-source probe."""
import json
from pathlib import Path
import unittest

rows = {r['case']: r for r in json.loads((Path(__file__).parent/'consumer/pagination-crash-result.json').read_text())['cases']}

class ResumeRepair(unittest.TestCase):
    def test_positive_uninterrupted_repair(self):
        self.assertEqual(rows['uninterrupted_original_1001']['applied_cursor'], 1001)

    def test_resume_after_root_checkpoint_interruption(self):
        self.assertEqual(rows['after_root_checkpoint_interruption']['after_resumed_cycle']['applied_cursor'], 1001)

    def test_resume_after_successor_transient_failure(self):
        self.assertEqual(rows['transient_successor_failure_then_restoration']['status']['applied_cursor'], 1001)

if __name__ == '__main__':
    unittest.main(verbosity=2)
