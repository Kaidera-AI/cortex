"""Run one O01 executable-path mutant against a disposable source copy."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[2]
MUTANTS = {
    'age_path_fallback': (
        'cortex_core/backup.py',
        '    if not isinstance(binary, (str, os.PathLike)):\n',
        "    if binary == 'age':\n        return 'age'\n"
        '    if not isinstance(binary, (str, os.PathLike)):\n',
        'test_path_substitution_cannot_receive_synthetic_backup_plaintext',
    ),
    'native_verifier_path_fallback': (
        'cortex_core/backup_validation.py',
        '    verifybackup = _backup_binary(pg_verifybackup_binary)\n'
        '    waldump = _backup_binary(pg_waldump_binary)\n'
        '    commands = [\n',
        "    verifybackup = 'pg_verifybackup'\n"
        '    waldump = _backup_binary(pg_waldump_binary)\n'
        '    commands = [\n',
        'test_explicit_native_verifiers_ignore_path_substitution',
    ),
}


def git(*args):
    return subprocess.check_output(['git', '-C', str(ROOT.parent), *args], text=True).strip()


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in MUTANTS:
        raise SystemExit('select one named O01 mutant')
    name = sys.argv[1]
    source, old, new, expected = MUTANTS[name]
    head = git('rev-parse', 'HEAD')
    dirty = bool(git('status', '--porcelain'))
    with tempfile.TemporaryDirectory(prefix='cox-o01-mutant-') as temporary:
        copied = Path(temporary)/'src'
        shutil.copytree(ROOT/'src', copied)
        target = copied/source
        value = target.read_text()
        if value.count(old) != 1:
            raise RuntimeError('mutation anchor is not unique')
        target.write_text(value.replace(old, new))
        env = {**os.environ, 'PYTHONPATH': str(copied),
               'PYTHONDONTWRITEBYTECODE': '1'}
        result = subprocess.run([sys.executable, str(ROOT/'tests/test_receipts.py'),
                                 str(ROOT/'tests/backup_validation')],
                                env=env, capture_output=True, text=True, timeout=120)
    print(json.dumps({'source_head': head, 'source_dirty': dirty,
                      'mutation': name, 'expected_test': expected,
                      'exit_code': result.returncode, 'stdout': result.stdout,
                      'stderr': result.stderr, 'cleanup': 'pass'}, sort_keys=True))
    return result.returncode


if __name__ == '__main__':
    raise SystemExit(main())
