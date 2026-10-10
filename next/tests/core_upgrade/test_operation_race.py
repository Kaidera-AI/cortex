"""Vera's advance/rollback schedule: no migration may land after restore."""
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import types
import unittest


SOURCE = Path(os.environ.get('CORE_UPGRADE_TEST_SOURCE',
    Path(__file__).resolve().parents[2] / 'src/cortex_core/upgrade.py')).read_text()
module = types.ModuleType('cox_upgrade_operation_race')
sys.modules[module.__name__] = module
exec(compile(SOURCE, 'operation-race-upgrade.py', 'exec'), module.__dict__)


class Ports:
    def __init__(self, journal, entered_apply, release_apply):
        self.journal = journal
        self.operation_lock_path = journal.path.with_name('upgrade.op.lock')
        self.entered_apply = entered_apply
        self.release_apply = release_apply
        self.data = 0
        self.backup = None
        self.ledger = []
        self.restore_calls = 0

    def load(self):
        return self.journal.load()

    def save(self, state, *, expected):
        self.journal.save(state, expected=expected)

    def fence_old(self):
        pass

    def verified_backup(self):
        self.backup = self.data
        return True

    def apply(self, *, through):
        self.entered_apply.set()
        if not self.release_apply.wait(10):
            raise AssertionError('timed out waiting to release apply')
        if through not in self.ledger:
            self.data = 1
            self.ledger.append(through)

    def applied_steps(self):
        return tuple(self.ledger)

    def fence_new(self):
        pass

    def restore(self):
        self.restore_calls += 1
        self.data = self.backup
        self.ledger.clear()

    def validate_preservation(self):
        return True


def coordinator(ports):
    admission = module.Admission(
        old_release='v0.1.003', new_release='v0.1.020',
        old_manifest_sha256='1'*64, new_manifest_sha256='2'*64,
        old_schema=1, new_schema=2, expand_steps=('expand-1',),
        readers=(('v0.1.003', 1), ('v0.1.020', 2)),
        writers=(('v0.1.003', 1), ('v0.1.020', 2)),
    )
    return module.UpgradeCoordinator(admission, ports)


class OperationRace(unittest.TestCase):
    def test_paused_advance_drains_before_rollback_restores(self):
        with tempfile.TemporaryDirectory() as temporary:
            entered, release = threading.Event(), threading.Event()
            ports = Ports(module.AtomicUpgradeJournal(Path(temporary)/'journal.json'),
                          entered, release)
            upgrade = coordinator(ports)
            upgrade.prepare()
            order, errors = [], []
            rollback_started = threading.Event()

            def run_advance():
                try:
                    upgrade.advance()
                    order.append('advance')
                except Exception as error:
                    errors.append(error)

            def run_rollback():
                rollback_started.set()
                try:
                    coordinator(ports).rollback()
                    order.append('rollback')
                except Exception as error:
                    errors.append(error)

            advance = threading.Thread(target=run_advance)
            rollback = threading.Thread(target=run_rollback)
            advance.start()
            self.assertTrue(entered.wait(10), 'advance did not enter apply')
            rollback.start()
            self.assertTrue(rollback_started.wait(10), 'rollback did not start')
            time.sleep(0.2)
            completed_before_release = not rollback.is_alive()
            release.set()
            advance.join(10)
            rollback.join(10)
            self.assertFalse(advance.is_alive() or rollback.is_alive())
            self.assertFalse(errors, [str(error) for error in errors])
            self.assertFalse(completed_before_release,
                             'rollback completed while apply was still in flight')
            self.assertEqual(order, ['advance', 'rollback'])
            self.assertEqual(ports.load()['phase'], 'rolled_back')
            self.assertEqual((ports.data, ports.ledger, ports.restore_calls),
                             (ports.backup, [], 1))

    def test_rollback_first_refuses_later_advance(self):
        with tempfile.TemporaryDirectory() as temporary:
            ports = Ports(module.AtomicUpgradeJournal(Path(temporary)/'journal.json'),
                          threading.Event(), threading.Event())
            upgrade = coordinator(ports)
            upgrade.prepare()
            upgrade.rollback()
            with self.assertRaises(module.UpgradeRefusal):
                coordinator(ports).advance()
            self.assertEqual(ports.load()['phase'], 'rolled_back')
            self.assertEqual((ports.data, ports.ledger, ports.restore_calls),
                             (ports.backup, [], 1))

    def test_paused_rollback_excludes_accept_until_restore_completes(self):
        with tempfile.TemporaryDirectory() as temporary:
            entered_fence, release_fence = threading.Event(), threading.Event()

            class PausedFence(Ports):
                def fence_new(self):
                    entered_fence.set()
                    if not release_fence.wait(10):
                        raise AssertionError('timed out waiting to release fence')

            apply_release = threading.Event()
            apply_release.set()
            ports = PausedFence(
                module.AtomicUpgradeJournal(Path(temporary)/'journal.json'),
                threading.Event(), apply_release)
            upgrade = coordinator(ports)
            upgrade.prepare()
            upgrade.advance()
            rollback_errors, accept_errors = [], []
            accept_started, accept_finished = threading.Event(), threading.Event()

            def run_rollback():
                try:
                    coordinator(ports).rollback()
                except Exception as error:
                    rollback_errors.append(error)

            def run_accept():
                accept_started.set()
                try:
                    coordinator(ports).accept()
                except Exception as error:
                    accept_errors.append(error)
                finally:
                    accept_finished.set()

            rollback = threading.Thread(target=run_rollback)
            accept = threading.Thread(target=run_accept)
            rollback.start()
            self.assertTrue(entered_fence.wait(10), 'rollback did not enter fence')
            accept.start()
            self.assertTrue(accept_started.wait(10), 'accept did not start')
            time.sleep(0.2)
            completed_while_paused = accept_finished.is_set()
            release_fence.set()
            rollback.join(10)
            accept.join(10)
            self.assertFalse(rollback.is_alive() or accept.is_alive())
            self.assertFalse(rollback_errors, [str(error) for error in rollback_errors])
            self.assertFalse(completed_while_paused,
                             'accept completed while rollback was in flight')
            self.assertEqual([error.code for error in accept_errors], ['not_prepared'])
            self.assertEqual(ports.load()['phase'], 'rolled_back')
            self.assertEqual((ports.data, ports.ledger, ports.restore_calls),
                             (ports.backup, [], 1))


if __name__ == '__main__':
    unittest.main(verbosity=2)
