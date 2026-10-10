"""Independent stdlib check of C06-PR51-002 RED/GREEN/mutant/native custody."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys

wt = Path(sys.argv[1]).resolve()
out = wt / 'docs/next/evidence/c06-source'
sha = lambda body: hashlib.sha256(body).hexdigest()
raw = out / 'mutation-pr51-002-full-001.json'
d = json.loads(raw.read_bytes())
assert d['passed'] and d['stack_removed'] and not d['mutation_errors']
assert d['final_inventory_error'] is None
c = d['cleanup']
assert c['cleanup_verified'] and c['password_discarded'] and c['lock_released']
assert not c['pending'] and not c['owned']
created = {(e['kind'], e['id']) for e in c['events'] if e['event'] == 'created'}
removed = {(e['kind'], e['id']) for e in c['events'] if e['event'] == 'removed'}
assert len(created) == 3 and created == removed
assert d['native_arch'] == 'arm64' and not d['published_ports'] and not d['bind_mounts']
assert d['limits']['cpus'] == 2 and d['limits']['memory'] == '1g'
assert len(d['source_sha256']) == 426
for relative, digest in d['source_sha256'].items():
    assert sha((wt / relative).read_bytes()) == digest, relative
    assert sha(subprocess.check_output(['git', '-C', str(wt), 'show', d['tree'] + ':' + relative])) == digest, relative
fixed = subprocess.check_output(['git', '-C', str(wt), 'show', 'fdd04d7b8b8f977e3c3b94f7badc9a17c87d787f:next/tests/test_receipts.py'])
assert sha(fixed) == d['source_sha256']['next/tests/test_receipts.py']
expected_wheels = json.loads((out / 'offline-wheel-inputs.json').read_text())['wheels']
assert d['tool_input_sha256']['actual_wheels'] == expected_wheels == d['tool_input_sha256']['copied_wheels']
clean = []
matrices = []
body = 0
inventory = None
for result in d['results']:
    cmd = result['command']
    if '/tmp/next/tests/test_receipts.py' in cmd and result.get('exit_code') == 0:
        rows = [json.loads(x.split('=', 1)[1]) for x in result['stdout'].splitlines() if x.startswith('CORTEX_TEST_RESULT=')]
        if rows:
            assert not rows[-1]['errors'] and not rows[-1]['failures']
            clean.append((Path(cmd[-1]).name, rows[-1]['tests_run']))
    if any('/tmp/next/scripts/mutate_' in x for x in cmd):
        assert result['exit_code'] == 0
        rows = [json.loads(x) for x in result['stdout'].splitlines() if x.startswith('{')]
        mutants = [x for x in rows if 'status' in x]
        summary = rows[-1]
        assert len(mutants) == summary['mutants'] == summary['killed']
        assert not summary['survivors'] and not summary['inconclusive']
        for mutant in mutants:
            report = mutant['receipt']
            assert mutant['status'] == 'killed' and mutant['exit_code'] != 0 and not report['errors'] and report['failures']
            ids = {f['id'].split(' (')[0] for f in report['failures']}
            assert mutant['expected_test'] in ids
            for failure in report['failures']:
                assert failure['phase'] == 'test' and failure['is_assertion']
                assert failure['phase_attribution'] == 'traceback+active-unittest-v2'
                body += 1
        matrices.append(len(mutants))
    if '-c' in cmd and 'root.rglob' in cmd[-1] and result.get('exit_code') == 0:
        inventory = json.loads(result['stdout'])
assert len(clean) == 26 and sum(n for _, n in clean) == 316
assert ('outbox_inventory', 10) in clean
assert matrices == [43, 32, 30, 11, 3, 1, 26, 8]
assert body == 367
assert inventory == {name.removeprefix('next/'): digest for name, digest in d['source_sha256'].items()}
red = json.loads((out / 'pr51-002-red.json').read_text())
expected_ids = {'test_outbox_inventory.WriterInventory.test_assigned_variable_dynamic_writer_fails_closed',
                'test_outbox_inventory.WriterInventory.test_helper_return_dynamic_writer_fails_closed'}
assert red['exit_code'] == 1 and not red['receipt']['errors']
assert {f['id'] for f in red['receipt']['failures']} == expected_ids
assert all(f['phase'] == 'test' and f['is_assertion'] for f in red['receipt']['failures'])
green = json.loads((out / 'pr51-002-green.json').read_text())
assert green['suite_exit_code'] == 0 and green['suite_report'] == {'tests_run':10,'failures':[],'errors':[]}
assert green['writer_audit'] == {'passed':True,'writer_count':62,'unclassified':[],'missing':[],'stale':[]}
mutant = json.loads((out / 'pr51-002-mutant.json').read_text())
assert mutant['baseline']['exit_code'] == mutant['restored']['exit_code'] == 0
assert mutant['mutant']['exit_code'] == 1 and mutant['source_restored']
assert expected_ids <= {f['id'] for f in mutant['mutant']['receipt']['failures']}
assert not mutant['mutant']['receipt']['errors']
assert all(f['phase'] == 'test' and f['is_assertion'] for f in mutant['mutant']['receipt']['failures'])
print(json.dumps({'tested_commit':d['tree'],'raw_sha256':sha(raw.read_bytes()),'clean_suites':len(clean),
                  'clean_tests':316,'primary_faults':sum(matrices),'body_assertions':body,
                  'source_files':426,'writer_sites':62,'red_tests':2,'guard_removal_body_failures':len(mutant['mutant']['receipt']['failures']),
                  'classifier_sha256':sha(fixed),'owned_removed':3,'restored':True},indent=2))
