"""Causal probes for the exact shared helper; every output is retained."""
from pathlib import Path
import hashlib
import json
import sys

sys.path.insert(0, '/tmp/next/tests')
from test_receipts import classify, report, suite

path = Path('/tmp/next/tests/test_receipts.py')
original = path.read_bytes()
prefix = 'test_classifier.Classifier.'
mutations = [
    ('fixture assertions admitted',
     'phase = "test" if code is not None and code in frames and not fixture else "fixture"',
     'phase = "test"', 'test_nemo_async_fixture_assertions_are_inconclusive'),
    ('unrelated assertion admitted', 'failed.intersection(expected)', 'failed',
     'test_unrelated_assertion_is_inconclusive'),
    ('nonassertion failure admitted', ' or row.get("is_assertion") is not True', '',
     'test_nonassertion_failure_exception_is_inconclusive'),
    ('test errors ignored', ' or value["errors"]', '',
     'test_error_alongside_expected_assertion_is_inconclusive'),
    ('operational exits admitted', 'not in (0, 1)', 'not in (0, 1, -9, -15, 2, 137)',
     'test_operational_exits_signals_and_exit_mismatch_are_inconclusive'),
    ('subtest assertion receipt omitted', 'self.record_assertion(subtest, err)', 'pass',
     'test_expected_subtest_assertion_is_killed'),
    ('zero test report admitted', 'value["tests_run"] < 1', 'value["tests_run"] < 0',
     'test_invalid_report_count_rows_or_assertion_evidence_are_inconclusive'),
]


def capture(result):
    return {'exit_code': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr,
            'receipt': report(result),
            'stdout_sha256': hashlib.sha256(result.stdout.encode()).hexdigest(),
            'stderr_sha256': hashlib.sha256(result.stderr.encode()).hexdigest()}


baseline = suite('/tmp/next/tests/receipt')
print(json.dumps({'baseline': capture(baseline)}), flush=True)
assert classify(baseline, set()) == 'survived'
results = []
try:
    for name, before, after, test in mutations:
        assert original.decode().count(before) == 1, name
        mutated = original.decode().replace(before, after, 1).encode()
        path.write_bytes(mutated)
        result = suite('/tmp/next/tests/receipt')
        expected = prefix + test
        status = classify(result, {expected})
        row = {'mutation': name, 'recipe': {'before': before, 'after': after},
               'expected_test': expected, 'status': status,
               'original_sha256': hashlib.sha256(original).hexdigest(),
               'mutated_sha256': hashlib.sha256(mutated).hexdigest(), **capture(result)}
        results.append(row)
        print(json.dumps(row), flush=True)
        path.write_bytes(original)
finally:
    path.write_bytes(original)
restored = suite('/tmp/next/tests/receipt')
print(json.dumps({'mutants': len(results), 'killed': sum(r['status'] == 'killed' for r in results),
                  'survivors': [r['mutation'] for r in results if r['status'] == 'survived'],
                  'inconclusive': [r['mutation'] for r in results if r['status'] == 'inconclusive'],
                  'restored_source_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                  'restored': capture(restored)}), flush=True)
assert len(results) == len(mutations) and all(r['status'] == 'killed' for r in results)
assert classify(restored, set()) == 'survived' and path.read_bytes() == original
