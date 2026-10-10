"""Exact-head source control for upgrade rollback and acceptance marker."""

from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest


SOURCE = (Path(__file__).resolve().parents[2] / 'src/cortex_core/upgrade.py').read_text()
module = types.ModuleType("vera_pr83_exact_upgrade")
sys.modules[module.__name__] = module
exec(compile(SOURCE, "exact-head-upgrade.py", "exec"), module.__dict__)


class Ports:
    def __init__(self, journal):
        self.journal = journal
        self.applied = []
        self.restore_calls = 0
        self.fence_new_hook = None

    def load(self):
        return self.journal.load()

    def save(self, value):
        self.journal.save(value)

    def verified_backup(self):
        return True

    def fence_old(self):
        pass

    def fence_new(self):
        if self.fence_new_hook:
            self.fence_new_hook()

    def restore(self):
        self.restore_calls += 1

    def apply(self, *, through):
        self.applied.append(through)

    def applied_steps(self):
        return tuple(self.applied)

    def validate_preservation(self):
        return True


def coordinator(ports):
    admission = module.Admission(
        old_release="v0.1.003", new_release="v0.1.020",
        old_manifest_sha256="1" * 64, new_manifest_sha256="2" * 64,
        old_schema=1, new_schema=2, expand_steps=("expand-1",),
        readers=(("v0.1.003", 1), ("v0.1.020", 2)),
        writers=(("v0.1.003", 1), ("v0.1.020", 2)),
    )
    return module.UpgradeCoordinator(admission, ports)


class RollbackMarker(unittest.TestCase):
    def test_prepare_closes_old_writer_before_verified_backup(self):
        with tempfile.TemporaryDirectory() as temporary:
            class GapPorts(Ports):
                value = 0
                backup_value = None

                def verified_backup(self):
                    self.backup_value = self.value
                    return True

                def fence_old(self):
                    # An already admitted old-writer commit lands before fencing.
                    self.value += 1

                def restore(self):
                    self.restore_calls += 1
                    self.value = self.backup_value

            ports = GapPorts(module.AtomicUpgradeJournal(Path(temporary) / "upgrade.json"))
            upgrade = coordinator(ports)
            upgrade.prepare()
            self.assertEqual(ports.value, 1, "old writer committed during the backup/fence gap")
            upgrade.rollback()
            self.assertEqual(ports.value, 1,
                             "rollback lost an old-writer commit acknowledged before fencing")

    def test_rollback_after_one_step_completes(self):
        with tempfile.TemporaryDirectory() as temporary:
            ports = Ports(module.AtomicUpgradeJournal(Path(temporary) / "upgrade.json"))
            upgrade = coordinator(ports)
            upgrade.prepare()
            self.assertEqual(upgrade.advance(), "expand-1")
            error = None
            try:
                upgrade.rollback()
            except module.UpgradeRefusal as caught:
                error = caught.code
            self.assertIsNone(error, f"restore ran, but journal rejected rollback: {error}")
            self.assertEqual(ports.load()["phase"], "rolled_back")

    def test_accepted_marker_prevents_interleaved_restore(self):
        with tempfile.TemporaryDirectory() as temporary:
            ports = Ports(module.AtomicUpgradeJournal(Path(temporary) / "upgrade.json"))
            upgrade = coordinator(ports)
            upgrade.prepare()
            self.assertEqual(upgrade.advance(), "expand-1")
            ports.fence_new_hook = lambda: coordinator(ports).accept()
            with self.assertRaises(module.UpgradeRefusal) as seen:
                upgrade.rollback()
            self.assertEqual(seen.exception.code, "fix_forward_only")
            self.assertEqual(ports.load()["phase"], "accepted")
            self.assertEqual(ports.restore_calls, 0,
                             "restore ran after another coordinator durably accepted")


if __name__ == "__main__":
    unittest.main(verbosity=2)
