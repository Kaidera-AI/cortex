"""Offline, bounded, disposable C02 contract runner; no host mounts/ports."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[2]
IMAGE = 'sha256:ce9a404c2c0138e747a43e6ea022d2f7e670ed868df35d627a663ba7fb940ea9'


def run(*args, check=True, timeout=180):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(f'{args[0]} exit {result.returncode}: {result.stderr[-1200:]}')
    return result


def preflight():
    pressure = run('memory_pressure', '-Q').stdout
    match = re.search(r'System-wide memory free percentage: (\d+)%', pressure)
    if match is None or int(match.group(1)) < 35:
        raise RuntimeError('Mac free-memory gate below 35%')
    machines = json.loads(run('podman', 'machine', 'list', '--format', 'json').stdout)
    if not any(x.get('Name') == 'kos-e020-uat' and x.get('Running') for x in machines):
        raise RuntimeError('existing Podman machine is not running')
    lines = run('podman', 'machine', 'ssh', 'kos-e020-uat', '--', 'free', '-m').stdout.splitlines()
    memory = next((row.split() for row in lines if row.startswith('Mem:')), None)
    if memory is None or int(memory[6])*100 < int(memory[1])*35:
        raise RuntimeError('Podman free-memory gate below 35%')
    if run('podman', 'image', 'exists', IMAGE, check=False).returncode:
        raise RuntimeError('pinned Python image is not cached')
    return int(match.group(1))


def main():
    mac_free = preflight()
    wheels = Path(os.environ['C02_WHEELS']).resolve()
    if not wheels.is_dir() or len(list(wheels.glob('*.whl'))) != 6:
        raise RuntimeError('six pinned offline contract wheels required')
    name = 'kaidera-test-c02-openkai-'+uuid4().hex[:10]
    label = 'cox-c02-openkai-'+name.rsplit('-', 1)[-1]
    with tempfile.TemporaryDirectory(prefix='cox-c02-openkai-stage-') as temporary:
        stage = Path(temporary)/'next'
        (stage/'src/cortex_core').mkdir(parents=True)
        (stage/'scripts').mkdir()
        for filename in ('contracts.py', 'openkai_contract.py'):
            shutil.copy2(ROOT/'src/cortex_core'/filename,
                         stage/'src/cortex_core'/filename)
        shutil.copytree(ROOT/'contracts', stage/'contracts',
                        ignore=shutil.ignore_patterns('__pycache__'))
        shutil.copytree(ROOT/'tests/contract', stage/'tests/contract',
                        ignore=shutil.ignore_patterns('__pycache__'))
        shutil.copy2(ROOT/'tests/test_receipts.py', stage/'tests/test_receipts.py')
        shutil.copy2(ROOT/'scripts/mutate_contracts.py',
                     stage/'scripts/mutate_contracts.py')
        shutil.copy2(ROOT/'requirements-test.txt', stage/'requirements-test.txt')
        try:
            run('podman', 'run', '-d', '--name', name, '--network', 'none',
                '--cpus', '1', '--memory', '512m', '--read-only',
                '--user', '10001:10001', '--cap-drop=ALL',
                '--security-opt=no-new-privileges',
                '--label', 'cortex.test='+label,
                '--tmpfs', '/tmp:rw,size=268435456,mode=1777', IMAGE,
                'sleep', '1800')
            run('podman', 'cp', str(stage), name+':/tmp/next')
            run('podman', 'cp', str(wheels), name+':/tmp/wheels')
            run('podman', 'exec', name, 'python', '-m', 'pip', 'install',
                '--no-index', '--find-links=/tmp/wheels', '--no-cache-dir',
                '--target=/tmp/deps', '-r', '/tmp/next/requirements-test.txt',
                timeout=300)
            result = run('podman', 'exec',
                         '--env', 'PYTHONPATH=/tmp/deps:/tmp/next/src',
                         '--env', 'PYTHONDONTWRITEBYTECODE=1',
                         name, 'python', '/tmp/next/tests/test_receipts.py',
                         '/tmp/next/tests/contract', check=False, timeout=300)
            print(json.dumps({'image':IMAGE, 'cpus':1, 'memory_mib':512,
                'network':'none', 'ports':[], 'host_mounts':[],
                'mac_free_percent':mac_free,
                'wheels':{p.name:hashlib.sha256(p.read_bytes()).hexdigest()
                          for p in sorted(wheels.glob('*.whl'))},
                'exit_code':result.returncode, 'stdout':result.stdout,
                'stderr':result.stderr}, sort_keys=True), flush=True)
            return result.returncode
        finally:
            run('podman', 'rm', '-f', name, check=False)
            remaining = run('podman', 'ps', '-a', '--filter',
                            'label=cortex.test='+label,
                            '--format', '{{.Names}}').stdout.strip()
            print(json.dumps({'cleanup':'pass' if not remaining else 'fail',
                              'name':name}, sort_keys=True), flush=True)
            if remaining:
                raise RuntimeError('owned disposable container cleanup failed')


if __name__ == '__main__':
    raise SystemExit(main())
