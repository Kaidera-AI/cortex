"""Scoped SQL admission regression; SOURCE DATA only, no native credit."""
import tempfile
import unittest
from unittest.mock import patch

from cortex_core.install import owner
from test_hold1_producer import SQLSubject
from test_hold1_publication_scope import PublicationScope


class ScopedSQLAdmission(unittest.TestCase):
    def replacement_before_barrier(self, *, recovering):
        with tempfile.TemporaryDirectory() as root:
            s = SQLSubject(root)
            with s.installed():
                effect = None
                if recovering:
                    effect = s.recover(s.dispatch(schedule=5))
                original = s.adapter._sql_barrier
                switched = []
                def barrier(conn, context, *, recovery=None):
                    # This proves B is a separately valid common-owner proposal
                    # with the same admitted deadline, not an invalid fixture.
                    PublicationScope.foreign_scope(self, s)
                    switched.append(len(s.trace))
                    return original(conn, context, recovery=recovery)
                with patch.object(s.adapter, '_sql_barrier', side_effect=barrier):
                    with self.assertRaises(owner.OwnerRefusal):
                        if recovering:
                            with s.answer(effect):
                                self.fail('foreign SQL recovery scope was accepted')
                        else:
                            s.dispatch()
                self.assertEqual(len(switched), 1, 'replacement did not reach SQL barrier')
                self.assertEqual(s.trace[switched[0]:],
                    [('reader' if recovering else 'writer', 'ROLLBACK')],
                    'HOLD1_FOREIGN_SCOPE_SQL_BEFORE_REFUSAL')

    def test_normal_SQL_barrier_binds_checked_scope_before_next_statement(self):
        self.replacement_before_barrier(recovering=False)

    def test_recovery_SQL_barrier_binds_fresh_scope_before_next_statement(self):
        self.replacement_before_barrier(recovering=True)
