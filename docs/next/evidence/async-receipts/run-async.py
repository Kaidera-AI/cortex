"""Copied disposable proof, never a host dependency installation."""
from pathlib import Path
import hashlib
import json
import subprocess
import sys

wt = Path.cwd()
out = wt.parents[1] / 'docs/plans/cortex-v2-v0.1.020/lane-A/evidence/async-receipts'
phase = sys.argv[1]
name = 'kaidera-test-contract-1'
image = 'sha256:ce9a404c2c0138e747a43e6ea022d2f7e670ed868df35d627a663ba7fb940ea9'
rows = []
created = False
head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
receipt_path = out / (phase + '.json')
assert not receipt_path.exists(), 'never overwrite a previous attempt'


def run(command):
    result = subprocess.run(command, capture_output=True, text=True)
    rows.append({'command': command, 'exit_code': result.returncode,
                 'stdout': result.stdout, 'stderr': result.stderr})
    print(json.dumps({'command': command[:4], 'exit_code': result.returncode,
                      'output': (result.stdout + result.stderr)[-2200:]}), flush=True)
    return result


def checked(command):
    result = run(command)
    assert result.returncode == 0, command
    return result


try:
    info = json.loads(subprocess.check_output(['podman', 'image', 'inspect', image], text=True))[0]
    assert info['Architecture'] == 'arm64'
    checked(['podman', 'run', '-d', '--name', name, '--label', 'owner=cox@helix',
             '--label', 'slice=async-receipts', '--cpus', '2', '--memory', '1g',
             '--read-only', '--user', '10001:10001', '--cap-drop=ALL',
             '--security-opt=no-new-privileges', '--tmpfs', '/tmp:rw,size=268435456,mode=1777',
             *(['--network=none'] if phase == 'red' else []), image, 'sleep', '1800'])
    created = True
    checked(['podman', 'cp', str(wt / 'next'), name + ':/tmp/next'])
    if phase != 'red':
        checked(['podman', 'exec', name, 'python', '-m', 'pip', 'install',
                 '--disable-pip-version-check', '--no-cache-dir', '--target', '/tmp/deps',
                 '-r', '/tmp/next/requirements-test.txt'])
        checked(['podman', 'network', 'disconnect', 'podman', name])
    env = ['podman', 'exec', '--env', 'PYTHONPATH=/tmp/deps:/tmp/next/src',
           '--env', 'PYTHONDONTWRITEBYTECODE=1', name]
    shared = run(env + ['python', '/tmp/next/tests/test_receipts.py', '/tmp/next/tests/receipt'])
    marker = 'CORTEX_TEST_RESULT='
    reports = [json.loads(line[len(marker):]) for line in shared.stdout.splitlines() if line.startswith(marker)]
    assert len(reports) == 1
    report = reports[0]
    if phase == 'red':
        expected = {
            'test_async_fixture_reuse.AsyncFixtureReuse.test_mike_unchanged_reuse_probes',
            'test_async_fixture_reuse.AsyncFixtureReuse.test_async_teardown_reuse_is_inconclusive',
            'test_async_fixture_reuse.AsyncFixtureReuse.test_async_case_sync_cleanup_reuse_is_inconclusive',
        }
        assert shared.returncode == 1 and report['tests_run'] == 20 and not report['errors']
        assert {row['id'] for row in report['failures']} == expected
        assert all(row['phase'] == 'test' and row['is_assertion'] is True for row in report['failures'])
    else:
        assert shared.returncode == 0 and report['tests_run'] == 20 and not report['errors'] and not report['failures']
        checked(env + ['python', '/tmp/next/scripts/mutate_test_receipts.py'])
        checked(env + ['python', '-m', 'unittest', 'discover', '-s', '/tmp/next/tests/contract', '-v'])
        checked(env + ['python', '/tmp/next/scripts/mutate_contracts.py'])
finally:
    if created:
        checked(['podman', 'rm', '-f', name])
    assert subprocess.run(['podman', 'container', 'exists', name], capture_output=True).returncode == 1
    assert subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip() == head
    source = {str(p.relative_to(wt)): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in sorted((wt / 'next').rglob('*')) if p.is_file() and 'evidence' not in p.parts}
    receipt = {'phase': phase, 'tree': head, 'image_id': image, 'native_arch': 'arm64',
               'limits': {'cpus': 2, 'memory': '1g'}, 'source_sha256': source,
               'bind_mounts': [], 'published_ports': [], 'results': rows, 'container_removed': True}
    receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
    print(json.dumps({'receipt': str(receipt_path), 'removed': True}), flush=True)
