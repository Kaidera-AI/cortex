"""Cox retry4 Option B: observed owner is pinned; no configured fresh owner.

The sole packaged validator decides initial ownership. These explicit driver
fixtures exercise actual seed/custody, with zero real SQL or installed credit.
"""
import unittest
from unittest.mock import patch

import test_seed


class SeedOwnerContractTests(unittest.TestCase):
    def snapshot(self, material):
        return (material.state, material.intent, material.credential,
                material.receipt, material.generation)

    def test_prepared_owner_drift_refuses_before_any_further_write_or_custody_change(self):
        controls = test_seed.DurableSeedTests()
        seed = controls.require_seed()
        with controls.scenario() as case:
            from cortex_core.runtime import custody_fs
            original = custody_fs.write_record
            def lost_ack(*args, **kwargs):
                result = original(*args, **kwargs)
                if args[1] == 'client':
                    raise OSError('public fixture PREPARED acknowledgement lost')
                return result
            with patch.object(custody_fs, 'write_record', side_effect=lost_ack):
                controls.refuse(lambda: controls.invoke(seed, case))
            before = controls.material(case)
            self.assertEqual(before.state, 'prepared')
            self.assertEqual(case.db.writes(), [], 'PREPARED must precede first SQL write')
            tables = case.db.snapshot()
            case.db.database_owner = 999  # Different, otherwise safe non-service OID.
            controls.refuse(lambda: controls.invoke(seed, case))
            self.assertEqual(case.db.writes(), [], 'owner drift must stop before SQL effects')
            self.assertEqual(case.db.tables, tables)
            self.assertEqual(self.snapshot(controls.material(case)), self.snapshot(before))

    def test_committed_owner_drift_refuses_without_writes_or_custody_change(self):
        controls = test_seed.DurableSeedTests()
        seed = controls.require_seed()
        with controls.scenario() as case:
            controls.invoke(seed, case)
            before = controls.material(case)
            self.assertEqual(before.state, 'committed')
            tables, count = case.db.snapshot(), len(case.db.writes())
            case.db.database_owner = 999
            controls.refuse(lambda: controls.invoke(seed, case))
            self.assertEqual(len(case.db.writes()), count)
            self.assertEqual(case.db.tables, tables)
            self.assertEqual(self.snapshot(controls.material(case)), self.snapshot(before))

    def test_fresh_nonservice_admin_owner_is_admitted_and_durably_pinned(self):
        controls = test_seed.DurableSeedTests()
        seed = controls.require_seed()
        with controls.scenario() as case:
            case.db.database_owner = 999
            first = controls.invoke(seed, case)
            before = controls.material(case)
            self.assertEqual(before.state, 'committed')
            self.assertEqual(before.intent['database']['owner_oid'], 999)
            self.assertEqual({r['database_owner_oid'] for r in
                              case.db.tables['auth.runtime_login_bindings']}, {999})
            self.assertEqual(controls.invoke(seed, case), first)
            self.assertEqual(self.snapshot(controls.material(case)), self.snapshot(before))
            count = len(case.db.writes())
            case.db.database_owner = 1000
            controls.refuse(lambda: controls.invoke(seed, case))
            self.assertEqual(len(case.db.writes()), count)
            self.assertEqual(self.snapshot(controls.material(case)), self.snapshot(before))


if __name__ == '__main__':
    unittest.main()
