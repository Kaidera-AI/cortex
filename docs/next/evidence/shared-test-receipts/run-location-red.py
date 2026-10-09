from pathlib import Path
import hashlib
import json
import subprocess
import sys

wt = Path.cwd()
out = wt.parents[1] / 'docs/plans/cortex-v2-v0.1.020/lane-A/evidence/shared-test-receipts'
out.mkdir(parents=True, exist_ok=True)
phase = sys.argv[1]
name = 'kaidera-test-receipts-1'
image = 'sha256:ce9a404c2c0138e747a43e6ea022d2f7e670ed868df35d627a663ba7fb940ea9'
results = []
created = False


def run(command):
    result = subprocess.run(command, capture_output=True, text=True)
    results.append({'command': command, 'exit_code': result.returncode,
                    'stdout': result.stdout, 'stderr': result.stderr})
    print(json.dumps({'command': command[:4], 'exit_code': result.returncode,
                      'output': (result.stdout + result.stderr)[-1000:]}), flush=True)
    return result


def checked(command):
    result = run(command)
    assert result.returncode == 0
    return result


try:
    assert subprocess.run(['podman', 'container', 'exists', name], capture_output=True).returncode == 1
    checked(['podman', 'run', '-d', '--name', name, '--network', 'none',
             '--label', 'owner=cox@helix', '--label', 'slice=shared-test-receipts',
             '--cpus', '1', '--memory', '256m', '--read-only', '--user', '10001:10001',
             '--cap-drop=ALL', '--security-opt=no-new-privileges',
             '--tmpfs', '/tmp:rw,size=134217728,mode=1777', image, 'sleep', '1800'])
    created = True
    checked(['podman', 'cp', str(wt / 'next'), name + ':/tmp/next'])
    env = ['podman', 'exec', '--env', 'PYTHONDONTWRITEBYTECODE=1', name]
    if phase == 'missing-red':
        result = run(env + ['python', '-m', 'unittest', 'discover', '-s', '/tmp/next/tests/receipt', '-v'])
        assert result.returncode == 1 and "No module named 'test_receipts'" in result.stderr
    else:
        if phase == 'fixture-red':
            prior = (wt / 'tmp/prior-helper.py').read_text()
            before = 'phase = "test" if code is not None and code in frames and not fixture else "fixture"'
            assert prior.count(before) == 1
            seeded = wt / 'tmp/fault-helper.py'
            seeded.write_text(prior.replace(before, 'phase = "test"', 1))
            checked(['podman', 'cp', str(seeded), name + ':/tmp/next/tests/test_receipts.py'])
        result = run(env + ['python', '/tmp/next/tests/test_receipts.py', '/tmp/next/tests/receipt'])
        if phase == 'move-red':
            assert result.returncode == 1
            value = json.loads(next(s.removeprefix('CORTEX_TEST_RESULT=') for s in result.stdout.splitlines() if s.startswith('CORTEX_TEST_RESULT=')))
            assert value['tests_run'] == 16 and not value['errors'] and len(value['failures']) == 2
            assert all(s['id'].startswith('test_shared_location.SharedLocation.') for s in value['failures'])
        elif phase == 'fixture-red':
            assert result.returncode == 1
            lines = [s.removeprefix('CORTEX_TEST_RESULT=') for s in result.stdout.splitlines()
                     if s.startswith('CORTEX_TEST_RESULT=')]
            assert len(lines) == 1
            receipt = json.loads(lines[0])
            assert receipt['tests_run'] == 14 and receipt['failures'] and not receipt['errors']
            assert any(s['id'].startswith('test_classifier.Classifier.test_nemo_async_fixture_assertions_are_inconclusive')
                       for s in receipt['failures'])
        else:
            assert phase in ('green', 'move-green') and result.returncode == 0
            checked(['podman', 'cp', str(wt / 'tmp/mutate-receipts.py'), name + ':/tmp/mutate-receipts.py'])
            checked(env + ['python', '/tmp/mutate-receipts.py'])
finally:
    if created:
        checked(['podman', 'rm', '-f', name])
    assert subprocess.run(['podman', 'container', 'exists', name], capture_output=True).returncode == 1
    source_files = {str(p.relative_to(wt)): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in sorted((wt / 'next').rglob('*')) if p.is_file()}
    receipt = {'phase': phase, 'source_tree': subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),
               'source_sha256': source_files, 'image_id': image, 'network': 'none', 'ports': [], 'bind_mounts': [],
               'limits': {'cpus': 1, 'memory': '256m', 'tmpfs': '128m'}, 'container_removed': True,
               'fault_seed': 'phase always test in a disposable prior-helper copy; not branch source' if phase == 'fixture-red' else None,
               'results': results}
    (out / (phase + '.json')).write_text(json.dumps(receipt, indent=2) + '\n')
