"""D1 mutants count only named real-PG test-body assertion failures."""
import json
import os
from pathlib import Path
import subprocess
import sys

TESTS = Path(__file__).resolve().parent
EXPECTED = {
    'd1_403_leak': 'test_memory_api.MemoryAPI.test_d1_authenticated_out_of_scope_matches_unknown_id',
    'd1_tombstone_skipped': 'test_memory_api.MemoryAPI.test_d1_authorized_tombstone_is_gone',
    'd1_digest_dropped': 'test_memory_api.MemoryAPI.test_d1_committed_read_exposes_exact_revision_and_digest',
}


def main():
    if not os.environ.get('C11B_WHEELS'):
        raise RuntimeError('offline C11B_WHEELS is required')
    receipts = {}
    for name, expected in EXPECTED.items():
        env = dict(os.environ)
        env.pop('CORE_UPGRADE_TEST_SOURCE', None)
        env['C11B_MUTATION'] = name
        result = subprocess.run([sys.executable, str(TESTS/'run_pg.py')], env=env,
                                capture_output=True, text=True, timeout=300)
        raw = result.stdout + result.stderr
        try:
            rows = [json.loads(line) for line in result.stdout.splitlines()]
            run = next(row for row in rows if row.get('suite') == 'c11b')
            cleanup = next(row for row in rows if row.get('cleanup'))
            marker = json.loads(run['stdout'].split('CORTEX_TEST_RESULT=', 1)[1])
            failures = marker['failures']
            killed = (result.returncode == 1 and run['exit_code'] == 1
                      and run['mutation'] == name and cleanup['cleanup'] == 'pass'
                      and not marker['errors'] and failures
                      and all(row['phase'] == 'test' and row['is_assertion'] is True
                              for row in failures)
                      and any(row['id'].split(' (')[0] == expected for row in failures))
        except (KeyError, ValueError, StopIteration, IndexError, TypeError):
            killed = False
        receipts[name] = {'classification': 'killed' if killed else 'inconclusive',
                          'expected_test': expected, 'exit_code': result.returncode,
                          'raw': raw}
    print(json.dumps(receipts, sort_keys=True))
    return 0 if all(row['classification'] == 'killed' for row in receipts.values()) else 1


if __name__ == '__main__':
    raise SystemExit(main())
