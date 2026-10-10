"""Independent stdlib verifier for C05's fixed-classifier native replay."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys

wt = Path(sys.argv[1]).resolve()
out = wt / 'docs/next/evidence/c05-rework'
raw = out / 'mutation-reclassified-002.json'
d = json.loads(raw.read_bytes())
sha = lambda body: hashlib.sha256(body).hexdigest()
assert d['passed'] and d['stack_removed'] and not d['mutation_errors']
assert d['final_inventory_error'] is None
c = d['cleanup']
assert c['cleanup_verified'] and c['password_discarded'] and c['lock_released']
assert not c['pending'] and not c['owned']
created = {(e['kind'], e['id']) for e in c['events'] if e['event'] == 'created'}
removed = {(e['kind'], e['id']) for e in c['events'] if e['event'] == 'removed'}
assert len(created) == 3 and created == removed
assert d['limits'] == {'cpus': 2, 'memory': '1g', 'pg_memory': '768m', 'driver_memory': '256m'}
assert d['native_arch'] == 'arm64' and not d['published_ports'] and not d['bind_mounts']
assert len(d['source_sha256']) == 406
for relative, digest in d['source_sha256'].items():
    assert sha((wt / relative).read_bytes()) == digest, relative
    assert sha(subprocess.check_output(['git', '-C', str(wt), 'show', d['tree'] + ':' + relative])) == digest, relative
classifier = 'next/tests/test_receipts.py'
fixed = subprocess.check_output(['git', '-C', str(wt), 'show', 'fdd04d7b8b8f977e3c3b94f7badc9a17c87d787f:' + classifier])
assert sha(fixed) == d['source_sha256'][classifier]
wheels = json.loads((out / 'offline-wheel-inputs.json').read_text())['wheels']
assert d['tool_input_sha256']['actual_wheels'] == wheels == d['tool_input_sha256']['copied_wheels']
assert sha((out / 'run-core-pg.py').read_bytes()) == d['controller_sha256']
assert sha((out / 'replay_lifecycle.py').read_bytes()) == d['tool_input_sha256']['shared/replay_lifecycle.py']
expected_clean = {'schema':30, 'auth':40, 'records':14, 'coordination':23, 'core_adapters':6,
                  'acceptance_guards':7, 'contract':33, 'receipt':29, 'c05_review':8, 'c05_private':5}
clean = {}
matrices = []
body = 0
attributions = set()
inventory = None
for result in d['results']:
    cmd = result['command']
    if '/tmp/next/tests/test_receipts.py' in cmd:
        report = json.loads(next(line.split('=', 1)[1] for line in result['stdout'].splitlines()
                                 if line.startswith('CORTEX_TEST_RESULT=')))
        assert result['exit_code'] == 0 and not report['errors'] and not report['failures']
        clean[Path(cmd[-1]).name] = report['tests_run']
    if any('/tmp/next/scripts/mutate_' in part for part in cmd):
        assert result['exit_code'] == 0
        rows = [json.loads(line) for line in result['stdout'].splitlines() if line.startswith('{')]
        faults = [row for row in rows if 'status' in row]
        summary = rows[-1]
        assert len(faults) == summary['mutants'] == summary['killed']
        assert not summary['survivors'] and not summary['inconclusive']
        for fault in faults:
            report = fault['receipt']
            assert fault['status'] == 'killed' and fault['exit_code'] != 0
            assert report['failures'] and not report['errors']
            actual = {failure['id'].split(' (')[0] for failure in report['failures']}
            assert set(fault.get('expected_tests', [fault['expected_test']])) <= actual
            for failure in report['failures']:
                assert failure['phase'] == 'test' and failure['is_assertion']
                assert failure['phase_attribution'] == 'traceback+active-unittest-v2'
                attributions.add(failure['phase_attribution'])
                body += 1
            for channel in ('stdout', 'stderr'):
                if channel + '_sha256' in fault:
                    assert sha(fault[channel].encode()) == fault[channel + '_sha256']
        restored = summary.get('restored')
        if isinstance(restored, dict):
            suites = [restored] if 'exit_code' in restored else restored.values()
            for suite in suites:
                assert suite['exit_code'] == 0 and not suite['receipt']['errors'] and not suite['receipt']['failures']
        restored_hashes = summary.get('restored_source_sha256')
        if isinstance(restored_hashes, dict):
            for path, digest in restored_hashes.items():
                assert d['source_sha256']['next/' + path] == digest
        elif restored_hashes:
            assert restored_hashes == d['source_sha256']['next/tests/test_receipts.py']
        matrices.append(len(faults))
    if '-c' in cmd and 'root.rglob' in cmd[-1] and result['exit_code'] == 0:
        inventory = json.loads(result['stdout'])
assert clean == expected_clean, clean
assert matrices == [30, 11, 26, 8], matrices
assert body == 234 and attributions == {'traceback+active-unittest-v2'}
assert inventory == {name.removeprefix('next/'): digest for name, digest in d['source_sha256'].items()}
failed = json.loads((out / 'mutation-reclassified-001.json').read_text())
assert not failed['passed'] and failed['stack_removed'] and failed['cleanup']['cleanup_verified']
assert any('test_receipts.py' in part for result in failed['results'] for part in result['command'])
print(json.dumps({'tested_commit': d['tree'], 'receipt_sha256': sha(raw.read_bytes()),
                  'clean_suites': clean, 'clean_tests': sum(clean.values()), 'mutants': matrices,
                  'expected_body_failures': body, 'inconclusive': 0, 'source_files': len(inventory),
                  'classifier_sha256': sha(fixed), 'owned_resources_removed': 3,
                  'wheels_match': True, 'restored': True}, indent=2))
