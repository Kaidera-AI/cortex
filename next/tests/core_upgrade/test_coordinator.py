"""Upgrade orchestration failures must stay on the safe side of acceptance."""
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace


class FakePorts:
    def __init__(self):
        self.calls = []
        self.applied = []
        self.state = None
        self.fail_after = None
        self.valid = True

    def verified_backup(self):
        self.calls.append('backup')
        return True

    def fence_old(self):
        self.calls.append('fence_old')

    def fence_new(self):
        self.calls.append('fence_new')

    def restore(self):
        self.calls.append('restore')

    def apply(self, through):
        self.calls.append('apply:'+through)
        if through not in self.applied:
            self.applied.append(through)
        if self.fail_after == through:
            self.fail_after = None
            raise InterruptedError(through)

    def applied_steps(self):
        return tuple(self.applied)

    def validate_preservation(self):
        self.calls.append('validate')
        return self.valid

    def load(self):
        return self.state

    def save(self, state, *, expected=None):
        if self.state != expected:
            raise ValueError('journal_conflict')
        self.state = state


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.ports = FakePorts()
        self.ports.operation_lock_path = Path(directory.name)/'upgrade.op.lock'
        self.admission = SimpleNamespace(old_manifest_sha256='1'*64,
            new_manifest_sha256='2'*64, old_release='v0.1.002',
            new_release='v0.1.020', old_schema=1, new_schema=2,
            expand_steps=('expand-1', 'expand-2'),
            writer_allowed=lambda release, schema: release == 'v0.1.020' and schema == 2)

    def coordinator(self):
        UpgradeCoordinator = self.api()
        return UpgradeCoordinator(self.admission, self.ports)

    def api(self):
        try:
            from cortex_core.upgrade import UpgradeCoordinator
        except ImportError:
            self.fail('Core upgrade coordinator is absent')
        return UpgradeCoordinator

    def test_interruption_after_each_step_reenters_without_duplicate_work(self):
        c = self.coordinator()
        c.prepare()
        self.assertEqual(self.ports.calls[:2], ['fence_old', 'backup'])
        for step in self.admission.expand_steps:
            self.ports.fail_after = step
            with self.assertRaises(InterruptedError):
                c.advance()
            c.advance()
        self.assertEqual(self.ports.applied, ['expand-1', 'expand-2'])
        self.assertEqual(self.ports.calls.count('apply:expand-1'), 1)
        c.accept()
        self.assertEqual(self.ports.state['phase'], 'accepted')
        with self.assertRaisesRegex(ValueError, 'fix_forward_only'):
            self.coordinator().rollback()
        self.assertNotIn('restore', self.ports.calls)

    def test_preacceptance_rollback_fences_new_writer_and_restores(self):
        c = self.coordinator()
        c.prepare()
        c.advance()
        c.rollback()
        self.assertEqual(self.ports.calls[-2:], ['fence_new', 'restore'])
        self.assertEqual(self.ports.state['phase'], 'rolled_back')

    def test_validation_and_identity_guard_before_acceptance(self):
        c = self.coordinator()
        c.prepare()
        c.advance()
        c.advance()
        self.ports.valid = False
        with self.assertRaisesRegex(ValueError, 'preservation_failed'):
            c.accept()
        self.assertEqual(self.ports.state['phase'], 'prepared')
        other = SimpleNamespace(**vars(self.admission))
        other.new_manifest_sha256 = '3'*64
        UpgradeCoordinator = self.api()
        with self.assertRaisesRegex(ValueError, 'pair_mismatch'):
            UpgradeCoordinator(other, self.ports).advance()

    def test_no_db_or_backup_port_before_signed_admission(self):
        UpgradeCoordinator = self.api()
        with self.assertRaisesRegex(ValueError, 'admission_required'):
            UpgradeCoordinator(None, self.ports).prepare()
        self.assertEqual(self.ports.calls, [])


if __name__ == '__main__':
    unittest.main()
