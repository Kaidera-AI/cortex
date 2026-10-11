"""Guard removal must fail at the original desired refusal, not an import error."""

import unittest
from unittest.mock import patch
import test_attempt_lessons
from cortex_core.release.build import audit, compare, receipts


class Mutations(unittest.TestCase):
    def test_named_guard_removals_are_killed_by_frozen_assertions(self):
        cases = [
            (
                audit,
                "audit_cache",
                "test_A09_body_only_mutation_refuses_with_same_header_filename",
            ),
            (
                audit,
                "compare_metadata",
                "test_A11_account_log_and_unknown_acl_drift_refuses",
            ),
            (compare, "compare_archives", "test_whole_archive_inequality_is_RED"),
            (
                compare,
                "require_independent",
                "test_two_exports_of_one_build_are_not_independent",
            ),
            (receipts, "require_complete", "test_missing_cleanup_never_admits_receipt"),
            (
                receipts,
                "verify_checked_bytes",
                "test_changed_module_checked_before_any_execution",
            ),
        ]
        for module, name, test in cases:
            with (
                self.subTest(guard=name),
                patch.object(module, name, return_value=True),
            ):
                result = unittest.TestResult()
                test_attempt_lessons.Attempts(test).run(result)
                self.assertTrue(result.failures)
                self.assertEqual(result.errors, [])
