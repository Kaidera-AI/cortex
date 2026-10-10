"""The acceptance marker survives process re-entry and never regresses."""
from pathlib import Path
import tempfile
import unittest


class JournalTests(unittest.TestCase):
    def test_stale_prepared_snapshot_cannot_advance_journal(self):
        from cortex_core.upgrade import AtomicUpgradeJournal, UpgradeRefusal
        with tempfile.TemporaryDirectory() as temporary:
            journal = AtomicUpgradeJournal(Path(temporary)/'upgrade.json')
            initial = {'pair':('1'*64, '2'*64), 'phase':'prepared', 'steps':()}
            one = {**initial, 'steps':('expand-1',)}
            two = {**initial, 'steps':('expand-1', 'expand-2')}
            journal.save(initial, expected=None)
            journal.save(one, expected=initial)
            with self.assertRaises(UpgradeRefusal) as seen:
                journal.save(two, expected=initial)
            self.assertEqual(seen.exception.code, 'journal_conflict')
            self.assertEqual(journal.load()['steps'], ('expand-1',))

    def test_compare_and_set_serializes_terminal_choice_after_every_prefix(self):
        from cortex_core.upgrade import AtomicUpgradeJournal, UpgradeRefusal
        pair = ('1'*64, '2'*64)
        for steps in ((), ('expand-1',), ('expand-1', 'expand-2')):
            with self.subTest(steps=steps), tempfile.TemporaryDirectory() as temporary:
                journal = AtomicUpgradeJournal(Path(temporary)/'upgrade.json')
                initial = {'pair':pair, 'phase':'prepared', 'steps':()}
                journal.save(initial, expected=None)
                previous = initial
                for count in range(1, len(steps) + 1):
                    current = {'pair':pair, 'phase':'prepared', 'steps':steps[:count]}
                    journal.save(current, expected=previous)
                    previous = current
                rolling = {**previous, 'phase':'rolling_back'}
                journal.save(rolling, expected=previous)
                with self.assertRaises(UpgradeRefusal) as seen:
                    journal.save({**previous, 'phase':'accepted'}, expected=previous)
                self.assertEqual(seen.exception.code, 'rollback_in_progress')
                journal.save({**rolling, 'phase':'rolled_back'}, expected=rolling)
                self.assertEqual(AtomicUpgradeJournal(journal.path).load()['phase'],
                                 'rolled_back')

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
