"""Qualify each PR #83 review repair with a named test-body assertion."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

TESTS = Path(__file__).resolve().parent
SOURCE = TESTS.parents[1] / 'src/cortex_core/upgrade.py'
MUTANTS = {
    'pinned_key_path': (
        [('old_signature, stable_key,', 'old_signature, trusted_public_key,')],
        ['review_key_path.py'], 'assert stable_key != trusted'),
    'backup_before_fence': (
        [("        self.ports.fence_old()\n        if self.ports.verified_backup() is not True:\n            raise UpgradeRefusal('backup_unverified')",
          "        if self.ports.verified_backup() is not True:\n            raise UpgradeRefusal('backup_unverified')\n        self.ports.fence_old()")],
        ['review_rollback_marker.py',
         'RollbackMarker.test_prepare_closes_old_writer_before_verified_backup'],
        'rollback lost an old-writer commit'),
    'effect_before_marker': (
        [("        if state['phase'] == 'prepared':\n            self.ports.save({'pair': self.pair, 'phase': 'rolling_back',",
          "        if state['phase'] == 'prepared':\n            self.ports.fence_new()\n            self.ports.save({'pair': self.pair, 'phase': 'rolling_back',")],
        ['review_rollback_marker.py',
         'RollbackMarker.test_rolling_back_marker_prevents_interleaved_acceptance'],
        'AssertionError'),
    'rollback_completion_refusal': (
        [("                if previous['phase'] == 'rolling_back':",
          "                if False:")],
        ['review_rollback_marker.py',
         'RollbackMarker.test_rollback_after_one_step_completes'],
        'restore ran, but journal rejected rollback'),
    'accept_after_rollback': (
        [("            if expected is not _UNSET and previous != expected:",
          "            if False:"),
         ("                if previous['phase'] == 'accepted':",
          "                if False:")],
        ['review_rollback_marker.py',
         'RollbackMarker.test_accepted_marker_wins_before_rollback_transition'],
        'AssertionError'),
    'stale_cas_bypass': (
        [("            if expected is not _UNSET and previous != expected:",
          "            if False:")],
        ['test_journal.JournalTests.test_stale_prepared_snapshot_cannot_advance_journal'],
        'AssertionError'),
}


def main():
    source = SOURCE.read_text()
    receipts = {}
    for name, (replacements, selector, assertion) in MUTANTS.items():
        mutated = source
        for before, after in replacements:
            if mutated.count(before) != 1:
                raise RuntimeError(f'{name}: mutation target not unique')
            mutated = mutated.replace(before, after)
        with tempfile.TemporaryDirectory(prefix='cortex-upgrade-review-mutant-') as temporary:
            mutant = Path(temporary)/'upgrade.py'
            mutant.write_text(mutated)
            env = dict(os.environ)
            if selector[0].endswith('.py'):
                env['CORE_UPGRADE_TEST_SOURCE'] = str(mutant)
                command = [sys.executable, str(TESTS/selector[0]), *selector[1:]]
            else:
                package = Path(temporary)/'cortex_core'
                package.mkdir()
                (package/'__init__.py').write_text('')
                (package/'upgrade.py').write_text(mutated)
                env['PYTHONPATH'] = os.pathsep.join((temporary, str(TESTS)))
                command = [sys.executable, '-m', 'unittest', '-v', selector[0]]
            result = subprocess.run(command, env=env, cwd=TESTS,
                                    capture_output=True, text=True)
            raw = result.stdout + result.stderr
            killed = (result.returncode == 1 and assertion in raw
                      and 'AssertionError' in raw and 'ERROR:' not in raw)
            receipts[name] = {'classification':'killed' if killed else 'inconclusive',
                              'selector':selector, 'exit_code':result.returncode,
                              'raw':raw}
    print(json.dumps(receipts, sort_keys=True))
    return 0 if all(value['classification']=='killed'
                    for value in receipts.values()) else 1


if __name__ == '__main__':
    raise SystemExit(main())
