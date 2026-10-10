"""The acceptance marker survives process re-entry and never regresses."""
from pathlib import Path
import tempfile
import unittest


class JournalTests(unittest.TestCase):
    def test_persisted_acceptance_refuses_rollback_and_regression(self):
        try:
            from cortex_core.upgrade import AtomicUpgradeJournal, UpgradeRefusal
        except ImportError:
            self.fail('Durable upgrade journal is absent')
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)/'upgrade.json'
            first = AtomicUpgradeJournal(path)
            prepared = {'pair':('1'*64,'2'*64), 'phase':'prepared', 'steps':()}
            first.save(prepared)
            self.assertEqual(AtomicUpgradeJournal(path).load()['phase'], 'prepared')
            first.save({**prepared, 'phase':'accepted', 'steps':('expand-1',)})
            second = AtomicUpgradeJournal(path)
            self.assertEqual(second.load()['steps'], ('expand-1',))
            with self.assertRaises(UpgradeRefusal) as seen:
                second.save(prepared)
            self.assertEqual(seen.exception.code, 'fix_forward_only')
            self.assertEqual(AtomicUpgradeJournal(path).load()['phase'], 'accepted')


if __name__ == '__main__':
    unittest.main()
