"""Reject green or fault receipts lacking test-body assertions and pod cleanup."""
import json
from pathlib import Path
import sys

EXPECTED = {'credential_required', 'search_lag', 'core_gap', 'graph_lag',
            'final_recheck', 'search_rls', 'session_writer', 'session_replay'}


def receipt(path):
    lines = [json.loads(line) for line in Path(path).read_text().splitlines()]
    assert len(lines) == 2 and lines[1]['cleanup'] == 'pass', path
    run = lines[0]
    assert run['network'] == 'none' and run['published_ports'] == [], path
    assert run['cpus'] == 2 and run['memory_gib'] == 1, path
    assert run['postgres_version'].startswith('18.'), path
    prefix = 'CORTEX_TEST_RESULT='
    result_line = next(line for line in run['stdout'].splitlines() if line.startswith(prefix))
    return run, json.loads(result_line.removeprefix(prefix))


def qualify(root):
    green, result = receipt(root / 'green.jsonl')
    assert green['exit_code'] == 0 and green['mutation'] is None
    assert result == {'tests_run': 9, 'failures': [], 'errors': []}
    killed = {}
    for name in sorted(EXPECTED):
        run, result = receipt(root / f'mutant-{name}.jsonl')
        assert run['mutation'] == name and run['exit_code'] == 1
        assert result['tests_run'] == 9 and result['errors'] == []
        failures = result['failures']
        assert failures and all(f['id'].startswith(('test_read_api.ReadAPI.test_',
                                                    'test_session_api.SessionAPI.test_',
                                                    'test_credential.CredentialGateway.test_'))
            and f['phase'] == 'test' and f['is_assertion'] is True for f in failures), name
        killed[name] = [f['id'] for f in failures]
    print(json.dumps({'green_tests': result['tests_run'], 'mutants_killed': killed,
                      'cleanup': 'pass', 'resource_limits': '2cpu/1GiB/no-network'}, sort_keys=True))


if __name__ == '__main__':
    if len(sys.argv) != 2:
        raise SystemExit('usage: qualify_receipts.py RECEIPT_DIR')
    qualify(Path(sys.argv[1]))
