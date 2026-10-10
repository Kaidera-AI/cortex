"""Stdlib verification of retained C05 evidence; no product imports or execution."""
import ast
import hashlib
import json
from pathlib import Path
import subprocess
import sys

OUT = Path(__file__).resolve().parent
WT = Path(sys.argv[1]).resolve()
RAW = OUT / 'mutation-integration-attempt-003.json'
d = json.loads(RAW.read_text())
assert d['passed'] and d['stack_removed'] and d['final_inventory_error'] is None
c = d['cleanup']
assert c['cleanup_verified'] and not c['pending'] and not c['owned']
assert c['password_discarded'] and c['lock_released']
created = {(r['kind'], r['id']) for r in c['events'] if r['event'] == 'created'}
removed = {(r['kind'], r['id']) for r in c['events'] if r['event'] == 'removed'}
assert len(created) == 3 and created == removed
clean = {}
faults = {}
inventory = None
for r in d['results']:
    cmd = r['command']
    if r['exit_code'] == 0 and any('/tmp/next/tests/test_receipts.py' == x for x in cmd):
        report = json.loads(next(x.split('=', 1)[1] for x in r['stdout'].splitlines()
                                 if x.startswith('CORTEX_TEST_RESULT=')))
        assert not report['failures'] and not report['errors']
        clean[cmd[-1].split('/')[-1]] = report['tests_run']
    if any('/tmp/next/scripts/mutate_' in x for x in cmd):
        assert r['exit_code'] == 0
        rows = [json.loads(x) for x in r['stdout'].splitlines() if x.startswith('{')]
        mutants = [x for x in rows if 'status' in x]
        for m in mutants:
            report = m['receipt']
            assert m['status'] == 'killed' and m['exit_code'] != 0 and not report['errors']
            assert all(x['phase'] == 'test' and x['is_assertion'] for x in report['failures'])
            assert m['expected_test'] in {x['id'].split(' (')[0] for x in report['failures']}
            for channel in ('stdout', 'stderr'):
                if channel + '_sha256' in m:
                    assert hashlib.sha256(m[channel].encode()).hexdigest() == m[channel + '_sha256']
        s = rows[-1]
        assert s['mutants'] == len(mutants) == s['killed']
        assert not s['survivors'] and not s['inconclusive']
        restored = s.get('restored')
        if 'restored_baseline_exit' in s:
            assert s['restored_baseline_exit'] == 0
            report = json.loads(next(x.split('=', 1)[1] for x in s['stdout'].splitlines()
                                     if x.startswith('CORTEX_TEST_RESULT=')))
            assert report['tests_run'] == 33 and not report['failures'] and not report['errors']
        suites = [restored] if restored and 'exit_code' in restored else (restored or {}).values()
        for suite in suites:
            assert suite['exit_code'] == 0 and not suite['receipt']['errors'] and not suite['receipt']['failures']
        hashes = s.get('restored_source_sha256')
        if isinstance(hashes, dict):
            for path, digest in hashes.items():
                assert d['source_sha256']['next/' + path] == digest
        elif hashes:
            assert hashes == d['source_sha256']['next/tests/test_receipts.py']
        faults[cmd[-1].split('/')[-1]] = len(mutants)
    if '-c' in cmd and 'root.rglob' in cmd[-1] and r['exit_code'] == 0:
        inventory = json.loads(r['stdout'])
assert clean == dict(schema=30, auth=40, records=14, coordination=23,
                     core_adapters=6, acceptance_guards=7, contract=33, receipt=20), clean
assert faults == {'mutate_core_adapters.py': 30, 'mutate_contracts.py': 26,
                  'mutate_test_receipts.py': 8}, faults
assert inventory == {k.removeprefix('next/'): v for k, v in d['source_sha256'].items()}
for path, digest in d['source_sha256'].items():
    assert hashlib.sha256((WT / path).read_bytes()).hexdigest() == digest, path
    data = subprocess.check_output(['git', '-C', str(WT), 'show', d['tree'] + ':' + path])
    assert hashlib.sha256(data).hexdigest() == digest, path
frozen = json.loads((OUT / 'frozen-inputs.json').read_text())
for f in frozen['fixtures']:
    data = (WT / f['path']).read_bytes()
    original = subprocess.check_output(['git', '-C', str(WT), 'show', f['commit'] + ':' + f['path']])
    assert data == original and hashlib.sha256(data).hexdigest() == f['sha256']
    tree = ast.parse(data)
    count = sum(isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef)) and x.name.startswith('test_')
                for x in ast.walk(tree))
    assert count == f['count']
assert hashlib.sha256((OUT / 'run-core-pg.py').read_bytes()).hexdigest() == d['controller_sha256']
assert hashlib.sha256((OUT.parent / 'replay-lifecycle/replay_lifecycle.py').read_bytes()).hexdigest() == d['tool_input_sha256']['shared/replay_lifecycle.py']
unchanged = subprocess.check_output(['git', '-C', str(WT), 'diff', '--name-only',
    '80ca5c3550680ad55cf9ed257bfe43441fc816da', d['tree'], '--', 'next/src/cortex_core/auth.py',
    'next/schema', 'next/tests/auth', 'next/tests/schema'], text=True)
assert not unchanged, unchanged
print(json.dumps(dict(tested_sha=d['tree'], raw_sha256=hashlib.sha256(RAW.read_bytes()).hexdigest(),
    clean=clean, actual_declared_body_mutants=faults, errors=0, survivors=0, inconclusive=0,
    copied_inputs=len(inventory), all_source_bytes_restored=True, frozen_tests_unchanged=True,
    c04_schema_auth_unchanged=True, owned_created_removed=3, cleanup_verified=True,
    password_discarded=True, lock_released=True), indent=2))
