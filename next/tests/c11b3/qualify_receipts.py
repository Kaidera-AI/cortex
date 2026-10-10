"""Require clean C11b3 PG baseline and named test-body kills, never fixture errors."""
import json
from pathlib import Path
import sys

EXPECTED = {
    'provider_bypass': 'test_read_api.ReadAPI.test_p01_provider_identity_cache_and_unavailable_refusal',
    'session_claim_hook': 'test_session_api.SessionAPI.test_source_path_is_unique_across_authorized_projects',
    'session_iso': 'test_session_api.SessionAPI.test_iso_timestamps_and_bounded_large_batch',
    'session_size': 'test_session_api.SessionAPI.test_iso_timestamps_and_bounded_large_batch',
    'manifest_ledger': 'test_read_api.ReadAPI.test_installer_ledger_contains_nemo_retrieval_schemas',
    'query_cache_rls': 'test_read_api.ReadAPI.test_installer_ledger_contains_nemo_retrieval_schemas',
    'source_claim_sql': 'test_session_api.SessionAPI.test_source_path_is_unique_across_authorized_projects',
}


def receipt(path):
    values = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(values) == 2 and values[1]['cleanup'] == 'pass', path
    run = values[0]
    assert run['network'] == 'none' and run['published_ports'] == []
    assert (run['cpus'], run['memory_gib']) == (2, 1)
    assert run['mac_free_percent'] >= 35 and run['postgres_version'].startswith('18.')
    result = next(line.removeprefix('CORTEX_TEST_RESULT=') for line in run['stdout'].splitlines()
                  if line.startswith('CORTEX_TEST_RESULT='))
    return run, json.loads(result)


def qualify(root):
    green, clean = receipt(root / 'green.jsonl')
    assert green['mutation'] is None and green['exit_code'] == 0
    assert clean == {'tests_run': 14, 'failures': [], 'errors': []}
    killed = {}
    for name, expected_id in EXPECTED.items():
        run, result = receipt(root / f'mutant-{name}.jsonl')
        assert run['mutation'] == name and run['exit_code'] == 1
        assert result['tests_run'] == 14 and result['errors'] == []
        failures = result['failures']
        assert failures and all(f['phase'] == 'test' and f['is_assertion'] is True
                                for f in failures), name
        assert expected_id in {f['id'] for f in failures}, name
        killed[name] = [f['id'] for f in failures]
    print(json.dumps({'green_tests': 14, 'mutants_killed': killed,
                      'cleanup': 'pass', 'limits': '2cpu/1GiB/no-network'}, sort_keys=True))


if __name__ == '__main__':
    if len(sys.argv) != 2:
        raise SystemExit('usage: qualify_receipts.py RECEIPT_DIR')
    qualify(Path(sys.argv[1]))
