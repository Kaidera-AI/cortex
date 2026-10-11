"""Run synthetic signed admission with isolated HOME and explicit minisign path."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[2]
SUITE = Path(__file__).resolve().parent


def main():
    if len(sys.argv) != 3 or sys.argv[1] not in ('red', 'green'):
        raise RuntimeError('usage: run_host.py red|green /absolute/minisign')
    mode = sys.argv[1]
    binary = Path(sys.argv[2])
    if not binary.is_absolute() or not binary.is_file() or not os.access(binary, os.X_OK):
        raise RuntimeError('host fixture binary must be an explicit absolute executable')
    source_head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
    output = {'mode': mode, 'source_head': source_head,
              'minisign_binary': str(binary),
              'minisign_sha256': hashlib.sha256(binary.read_bytes()).hexdigest(),
              'key_paths': [], 'home_files': [], 'cleanup': 'not_run'}
    with tempfile.TemporaryDirectory(prefix='cox-signed-fixture-') as temporary:
        root = Path(temporary)
        home = root / 'home'
        cases = root / 'cases'
        home.mkdir()
        cases.mkdir()
        key_log = root / 'key-paths.jsonl'
        env = os.environ.copy()
        env['HOME'] = str(home)
        env['SIGNED_CASE_ROOT'] = str(cases)
        env['SIGNED_KEY_PATH_LOG'] = str(key_log)
        env['SIGNED_MINISIGN_BIN'] = str(binary)
        env['PYTHONPATH'] = str(ROOT / 'src')
        env['PYTHONDONTWRITEBYTECODE'] = '1'
        result = subprocess.run([sys.executable, str(ROOT / 'tests/test_receipts.py'),
                                 str(SUITE)], capture_output=True, text=True,
                                env=env, timeout=180)
        records = [json.loads(line) for line in key_log.read_text().splitlines()]
        if not records or any(
            row['home'] != str(home) or row['binary'] != str(binary)
            or not Path(row['public']).is_relative_to(cases)
            or not Path(row['secret']).is_relative_to(cases)
            for row in records
        ):
            raise RuntimeError('signed fixture key paths escaped temporary custody')
        output.update({'test_exit': result.returncode, 'stdout': result.stdout,
                       'stderr': result.stderr, 'home': str(home),
                       'key_paths': records,
                       'home_files': [str(path.relative_to(home))
                                      for path in home.rglob('*')],
                       'temporary_root': str(root)})
    output['cleanup'] = 'pass' if not root.exists() else 'failed'
    print(json.dumps(output, sort_keys=True), flush=True)
    if output['cleanup'] != 'pass' or output['home_files']:
        return 2
    return result.returncode


if __name__ == '__main__':
    raise SystemExit(main())
