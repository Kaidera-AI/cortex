"""Independent Git-bound C06 forward-matrix, host boundary and publication check."""
import ast
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile

WT = Path(sys.argv[1]).resolve()
OUT = WT / 'docs/next/evidence/c06-source'
sha = lambda value: hashlib.sha256(value).hexdigest()


def git(*args):
    return subprocess.check_output(['git', '-C', str(WT), *args])


def inventory(revision):
    archive = tarfile.open(fileobj=io.BytesIO(git('archive', revision, 'next')))
    return {m.name: sha(archive.extractfile(m).read()) for m in archive.getmembers() if m.isfile()}


binding = json.loads((OUT / 'forward-source-binding.json').read_text())
tested = binding['tested_commit']
raw = OUT / binding['raw_receipt']
data = json.loads(raw.read_text())
assert sha(raw.read_bytes()) == binding['raw_sha256']
assert data['tree'] == tested and data['passed'] and data['stack_removed']
assert not data['mutation_errors'] and data['final_inventory_error'] is None
assert inventory(tested) == inventory('HEAD') == data['source_sha256']
for name, digest in binding['controllers'].items():
    relative = 'docs/next/evidence/c06-source/' + name
    assert sha(git('show', tested + ':' + relative)) == digest
    assert sha(git('show', 'HEAD:' + relative)) == sha((WT / relative).read_bytes()) == digest
assert data['controller_sha256'] == binding['controllers']['run-core-pg.py']
assert data['tool_input_sha256']['run-core-pg.py'] == binding['controllers']['run-core-pg.py']
assert data['tool_input_sha256']['shared/replay_lifecycle.py'] == binding['controllers']['replay_lifecycle.py']
assert data['native_arch'] == 'arm64' and not data['published_ports'] and not data['bind_mounts']
assert data['network'] == 'none; private loopback'
assert data['limits'] == {'cpus': 2, 'memory': '1g', 'pg_memory': '768m', 'driver_memory': '256m'}
cleanup = data['cleanup']
assert cleanup['cleanup_verified'] and cleanup['password_discarded'] and cleanup['lock_released']
assert not cleanup['pending'] and not cleanup['owned']
created = {(r['kind'], r['id']) for r in cleanup['events'] if r['event'] == 'created'}
removed = {(r['kind'], r['id']) for r in cleanup['events'] if r['event'] == 'removed'}
assert len(created) == 3 and created == removed
wheels = json.loads((OUT / 'offline-wheel-inputs.json').read_text())['wheels']
assert data['tool_input_sha256']['actual_wheels'] == wheels == data['tool_input_sha256']['copied_wheels']

clean = {}
matrices = []
body = 0
discovery = []
product_steps = []
copied = None
preflight = None
for position, item in enumerate(data['results']):
    command = item['command']
    lines = item.get('stdout', '').splitlines()
    if any(part.endswith('verify-replay-inputs.py') for part in command):
        preflight = json.loads(item['stdout'])
        assert item['exit_code'] == 0 and preflight['passed']
        assert preflight['controller_input_sha256'] == binding['controllers']
    for line in lines:
        if line.startswith('CORTEX_RECEIPT_DISCOVERY='):
            assert command[:2] == ['podman', 'exec'] and item['exit_code'] == 0
            assert json.loads(line.split('=', 1)[1]) == {'tests_run': 41}
            discovery.append(position)
    if any('from cortex_core.migrations import apply_migrations' in part for part in command):
        product_steps.append(position)
    if '/tmp/next/tests/test_receipts.py' in command:
        product_steps.append(position)
        reports = [json.loads(line.split('=', 1)[1]) for line in lines if line.startswith('CORTEX_TEST_RESULT=')]
        assert item['exit_code'] == 0 and len(reports) == 1
        value = reports[0]
        assert not value['failures'] and not value['errors']
        name = Path(command[-1]).name
        assert name not in clean
        clean[name] = value['tests_run']
    if any('/tmp/next/scripts/mutate_' in part for part in command):
        product_steps.append(position)
        assert item['exit_code'] == 0
        rows = [json.loads(line) for line in lines if line.startswith('{')]
        faults = [row for row in rows if 'status' in row]
        summary = rows[-1]
        assert len(faults) == summary['mutants'] == summary['killed']
        assert not summary['survivors'] and not summary['inconclusive']
        for fault in faults:
            receipt = fault['receipt']
            assert fault['status'] == 'killed' and fault['exit_code'] != 0
            assert receipt['failures'] and not receipt['errors']
            assert fault['expected_test'] in {r['id'].split(' (')[0] for r in receipt['failures']}
            assert all(r['phase'] == 'test' and r['is_assertion'] and
                       r['phase_attribution'] == 'traceback+active-unittest-v2' for r in receipt['failures'])
            body += len(receipt['failures'])
            if fault['mutation'] in ('inventory checker admits unknown writer', 'quoted and unqualified writer forms ignored'):
                assert fault['auxiliary_control']['qualified']
                assert fault['auxiliary_control']['expected_boundary_unchanged']
        matrices.append(len(faults))
    if '-c' in command and 'root.rglob' in command[-1] and item.get('exit_code') == 0:
        copied = json.loads(item['stdout'])
assert preflight and len(discovery) == 1 and discovery[0] < min(product_steps)
controller_tree = ast.parse((OUT / 'run-core-pg.py').read_text())
declared = next(n.value for n in controller_tree.body if isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == 'SUITES' for t in n.targets))
expected = dict(ast.literal_eval(declared))
expected.update(outbox_write_only=2, outbox_private_namespace=2, outbox=32, outbox_caller=2,
                outbox_guards=23, outbox_retention=5, outbox_inventory=17, outbox_late_publication=1)
assert clean == expected and len(clean) == 26 and sum(clean.values()) == 344
assert matrices == [43, 32, 30, 11, 3, 1, 26, 8] and sum(matrices) == 154
assert copied == {name.removeprefix('next/'): digest for name, digest in data['source_sha256'].items()}

red = json.loads((OUT / 'host-boundary-red-001.json').read_text())
green = json.loads((OUT / 'host-boundary-green-001.json').read_text())
assert red['test_sha256'] == green['test_sha256'] == binding['controllers']['test_host_no_product_import.py']
assert red['tests_run'] == green['tests_run'] == 2 and len(red['failures']) == 2
assert not red['errors'] and not green['failures'] and not green['errors']
expected_red = {
    '__main__.HostBoundary.test_host_preflight_does_not_import_receipt_module': 'receipt module executed on host',
    '__main__.HostBoundary.test_host_preflight_does_not_call_receipt_load_tests': 'receipt load_tests executed on host',
}
assert {r['id'] for r in red['failures']} == set(expected_red)
assert all('AssertionError' in r['traceback'] and expected_red[r['id']] in r['traceback'] for r in red['failures'])
assert green['controller_sha256'] == binding['controllers']['verify-replay-inputs.py']
assert all(r['import_marker'] and r['load_tests_marker'] for r in red['observations'])
assert all(not r['import_marker'] and not r['load_tests_marker'] for r in green['observations'])
preflight_tree = ast.parse((OUT / 'verify-replay-inputs.py').read_text())
assert not any(isinstance(n, ast.Attribute) and n.attr == 'discover' for n in ast.walk(preflight_tree))

publication = json.loads((OUT / 'forward-publication-index.json').read_text())
members = publication['members']
expected_paths = {str(p.relative_to(WT)) for root in (WT / 'next', OUT) for p in root.rglob('*') if p.is_file()}
expected_paths -= {'docs/next/evidence/c06-source/forward-publication-index.json'}
assert set(members) == expected_paths
for name, digest in members.items():
    assert sha((WT / name).read_bytes()) == digest
    assert sha(git('show', 'HEAD:' + name)) == digest
assert publication['tested_commit'] == tested and publication['raw_sha256'] == sha(raw.read_bytes())
print(json.dumps({'passed': True, 'tested_commit': tested,
                  'publication_head': git('rev-parse', 'HEAD').decode().strip(),
                  'source_files': len(data['source_sha256']), 'controllers': len(binding['controllers']),
                  'clean_suites': len(clean), 'clean_tests': sum(clean.values()),
                  'primary_faults': sum(matrices), 'body_assertions': body,
                  'survivors': 0, 'inconclusive': 0, 'errors': 0,
                  'discovery': 'pod first product action', 'receipt_count': 41,
                  'host_controls': '2 RED assertions -> 2 GREEN; no host markers',
                  'owned_removed': len(removed), 'restored': True,
                  'publication_members': len(members), 'raw_sha256': sha(raw.read_bytes())}, indent=2))
