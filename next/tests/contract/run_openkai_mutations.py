"""C02 named mutants: only expected test-body assertions count as kills."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT/'src/cortex_core/openkai_contract.py'
TESTS = Path(__file__).resolve().parent
MUTANTS = {
    'source_pin': ("and source['commit'] == SOURCE_COMMIT", 'and True',
                   'test_observed_inventory_and_release_pin'),
    'commit_ack': ("and value.get('durability') == 'canonical_committed'", 'and True',
                   'test_committed_write_read_retry_conflict_and_unknown_timeout'),
    'same_retry': ("same['receipt'] == first['receipt']", 'True',
                   'test_committed_write_read_retry_conflict_and_unknown_timeout'),
    'lag_results': ("and case['complete'] is False\n              and case['freshness'] == 'lagging' and 'results' not in case\n              and case['error']",
                    "and case['complete'] is False\n              and case['freshness'] == 'lagging' and True\n              and case['error']",
                    'test_search_states_never_turn_lag_into_zero_hits'),
    'off_remote_call': ("case['selected'] is False and case['calls'] == []",
                        "case['selected'] is False and True",
                        'test_openkai_alone_scope_off_mirror_and_stale_recall'),
    'mirror_binding': ("and request['source_path'] == case['source_path']",
                       'and True',
                       'test_openkai_alone_scope_off_mirror_and_stale_recall'),
    'stale_recall': ("and case['decision'] == 'discarded'", 'and True',
                     'test_openkai_alone_scope_off_mirror_and_stale_recall'),
    'typed_route_error': ("and error['body']['error']['code']\n          and type(",
                          'and True\n          and type(',
                          'test_each_observed_route_has_a_proposed_exchange_and_typed_error'),
    'd1_unauth_403': ("and read['unauthorized_status'] == 404",
                      "and read['unauthorized_status'] in (403, 404)",
                      'test_committed_write_read_retry_conflict_and_unknown_timeout'),
    'd2_wait_cap': ("and 0 <= parameters['wait_ms'] <= 10000",
                    "and parameters['wait_ms'] >= 0",
                    'test_each_observed_route_has_a_proposed_exchange_and_typed_error'),
    'd3_control_bound': ("and case['binding'] == 'ruled_unimplemented'\n              and case['future_scope']",
                         "and case['binding'] in ('ruled_unimplemented', 'ruled_bound')\n              and case['future_scope']",
                         'test_openkai_alone_scope_off_mirror_and_stale_recall'),
    'd4_partial_206': ("case['status'] == 503\n              and case['complete'] is False\n              and case['wire_state'] == 'ruled_unimplemented'",
                       "case['status'] in (206, 503)\n              and case['complete'] is False\n              and case['wire_state'] == 'ruled_unimplemented'",
                       'test_search_states_never_turn_lag_into_zero_hits'),
}


def main():
    source = SOURCE.read_text()
    receipts = {}
    for name, (before, after, method) in MUTANTS.items():
        if source.count(before) != 1:
            raise RuntimeError(f'{name}: non-unique mutation')
        with tempfile.TemporaryDirectory(prefix='c02-openkai-mutant-') as temporary:
            next_root = Path(temporary)/'next'
            package = next_root/'src/cortex_core'
            package.mkdir(parents=True)
            (package/'__init__.py').write_text('')
            (package/'openkai_contract.py').write_text(source.replace(before, after))
            contracts = next_root/'contracts'
            contracts.mkdir()
            (contracts/'openapi.json').write_bytes((ROOT/'contracts/openapi.json').read_bytes())
            env = dict(os.environ, PYTHONPATH=os.pathsep.join((str(next_root/'src'), str(TESTS))))
            test = 'test_openkai_consumer.OpenKaiConsumer.'+method
            result = subprocess.run([sys.executable, '-m', 'unittest', '-v', test],
                                    cwd=TESTS, env=env, capture_output=True, text=True)
            raw = result.stdout + result.stderr
            killed = (result.returncode == 1 and 'FAILED (failures=1)' in raw
                      and 'ERROR:' not in raw and f'FAIL: {method}' in raw
                      and 'AssertionError:' in raw)
            receipts[name] = {'classification':'killed' if killed else 'inconclusive',
                              'expected_test':test, 'exit_code':result.returncode,
                              'raw':raw}
    print(json.dumps(receipts, sort_keys=True))
    return 0 if all(row['classification']=='killed' for row in receipts.values()) else 1


if __name__ == '__main__':
    raise SystemExit(main())
