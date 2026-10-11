"""Desired original behavior assertions over fresh exact-AST probe results."""
import json
from pathlib import Path
import unittest

heads = json.loads((Path(__file__).parent/'scoped-head-result.json').read_text())['heads']
prior = {r['case']:r for r in heads[0]['cases']}
current = {r['case']:r for r in heads[1]['cases']}


class ScopedHead(unittest.TestCase):
    def check_continues(self,row):
        self.assertIsNone(row['repeated_cycle_error'], 'valid empty scoped page after global head must not strand consumer')
        self.assertIsNone(row['new_local_cycle_error'], 'new local event beyond old foreign-only fence must become eligible')
        self.assertEqual(row['final']['state']['applied_cursor'],row['new_local_cursor'])

    def test_prior_scoped_gap_positive(self):
        for row in prior.values():
            with self.subTest(case=row['case']):self.check_continues(row)

    def test_current_empty_project_global_gap(self):
        self.assertIsNone(current['empty_project_foreign_head']['repeated_cycle_error'])

    def test_current_completed_local_foreign_tail(self):
        self.assertIsNone(current['completed_local_foreign_tail']['repeated_cycle_error'])

    def test_current_new_local_after_empty_foreign_fence(self):
        row=current['empty_project_foreign_head']
        self.assertIsNone(row['new_local_cycle_error'])
        self.assertEqual(row['final']['state']['applied_cursor'],row['new_local_cursor'])

    def test_current_new_local_after_completed_foreign_fence(self):
        row=current['completed_local_foreign_tail']
        self.assertIsNone(row['new_local_cycle_error'])
        self.assertEqual(row['final']['state']['applied_cursor'],row['new_local_cursor'])

    def test_current_original_durable_cases_positive(self):
        self.assertEqual(len(heads[1]['durable_positives']),3)
        for row in heads[1]['durable_positives']:
            with self.subTest(case=row['case']):self.assertEqual(row['final']['state']['applied_cursor'],2)


if __name__=='__main__':unittest.main(verbosity=2)
