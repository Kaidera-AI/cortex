"""Named assertion-body mutants for Core's signed admission and acceptance."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[2]
TESTS = Path(__file__).resolve().parent
SOURCE = ROOT/'src/cortex_core/upgrade.py'
MUTANTS = {
    'signature_bypass': ('if result.returncode != 0:', 'if False:',
                         'test_signed_pair.SignedPair.test_wrong_signature_refuses'),
    'digest_bypass': ('if hashlib.sha256(raw).hexdigest() != pinned_digest:',
                      'if False:',
                      'test_signed_pair.SignedPair.test_changed_digest_even_if_resigned_refuses'),
    'preservation_bypass': ('if self.ports.validate_preservation() is not True:',
                            'if False:',
                            'test_coordinator.CoordinatorTests.test_validation_and_identity_guard_before_acceptance'),
    'acceptance_regression': ("if previous['phase'] == 'accepted':",
                              'if False:',
                              'test_journal.JournalTests.test_persisted_acceptance_refuses_rollback_and_regression'),
}


def main():
    source = SOURCE.read_text()
    receipts = {}
    for name, (before, after, test) in MUTANTS.items():
        if source.count(before) != 1:
            raise RuntimeError(f'{name}: mutation target not unique')
        with tempfile.TemporaryDirectory(prefix='cortex-upgrade-mutant-') as temporary:
            package = Path(temporary)/'cortex_core'
            package.mkdir()
            (package/'__init__.py').write_text('')
            (package/'upgrade.py').write_text(source.replace(before, after))
            env = dict(os.environ, PYTHONPATH=os.pathsep.join((temporary, str(TESTS))))
            result = subprocess.run([sys.executable, '-m', 'unittest', '-v', test],
                                    cwd=TESTS, env=env, capture_output=True, text=True)
            raw = result.stdout + result.stderr
            killed = (result.returncode == 1 and 'FAILED (failures=1)' in raw
                      and 'ERROR:' not in raw and f'FAIL: {test.split(".")[-1]}' in raw
                      and 'AssertionError:' in raw)
            receipts[name] = {'classification': 'killed' if killed else 'inconclusive',
                              'expected_test': test, 'exit_code': result.returncode,
                              'raw': raw}
    print(json.dumps(receipts, sort_keys=True))
    return 0 if all(x['classification'] == 'killed' for x in receipts.values()) else 1


if __name__ == '__main__':
    raise SystemExit(main())
