"""One bounded, disposable real-PG C11b API run; no host product imports."""

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
PG = 'sha256:db676a0ed906c00f55020fb8999e4fb30c598bf5c3b5c188630aef2812d3f11d'
PY = 'sha256:ce9a404c2c0138e747a43e6ea022d2f7e670ed868df35d627a663ba7fb940ea9'


def run(*args, check=True, timeout=180):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(f'{args[0]} failed ({result.returncode}): {result.stderr[-1200:]}')
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
    memory = next((x.split() for x in lines if x.startswith('Mem:')), None)
    if memory is None or int(memory[6]) * 100 < int(memory[1]) * 35:
        raise RuntimeError('Podman-machine free-memory gate below 35%')
    for image in (PG, PY):
        if run('podman', 'image', 'exists', image, check=False).returncode:
            raise RuntimeError('pinned image is not cached')
    return int(match.group(1))


def main():
    mac_free = preflight()
    wheels = Path(os.environ['C11B_WHEELS']).resolve()
    if not wheels.is_dir() or not any(wheels.glob('psycopg*.whl')):
        raise RuntimeError('pinned offline dependency wheels absent')
    token = uuid4().hex[:10]
    pod, db, driver = (f'kaidera-test-c11b-{kind}-{token}' for kind in ('pod', 'pg', 'driver'))
    label = f'cox-c11b-{token}'
    labels = ['--label', 'cortex.test=' + label, '--label', 'cortex.worker=cox']
    output = {'mac_free_percent': mac_free, 'images': [PG, PY], 'cpus': 2,
              'memory_gib': 1, 'network': 'none', 'published_ports': [],
              'wheels_sha256': {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                for p in sorted(wheels.glob('*.whl'))}}
    try:
        run('podman', 'pod', 'create', '--name', pod, '--network', 'none',
            '--cpus', '2', '--memory', '1g', *labels)
        run('podman', 'run', '-d', '--pod', pod, '--name', db, *labels,
            '--cpus', '1', '--memory', '768m', '--read-only', '--user', '70:70',
            '--cap-drop=ALL', '--security-opt=no-new-privileges', '--image-volume=ignore',
            '--tmpfs', '/var/lib/postgresql:rw,size=536870912,mode=1777',
            '--tmpfs', '/var/run/postgresql:rw,size=16777216,mode=1777',
            '--tmpfs', '/tmp:rw,size=16777216,mode=1777',
            '--env', 'PGDATA=/var/lib/postgresql/18/data',
            '--env', 'POSTGRES_HOST_AUTH_METHOD=trust', PG,
            '-c', 'max_wal_size=64MB', '-c', 'min_wal_size=32MB')
        run('podman', 'run', '-d', '--pod', pod, '--name', driver, *labels,
            '--cpus', '1', '--memory', '256m', '--read-only', '--user', '10001:10001',
            '--cap-drop=ALL', '--security-opt=no-new-privileges',
            '--tmpfs', '/tmp:rw,size=268435456,mode=1777', PY, 'sleep', '1800')
        run('podman', 'cp', str(ROOT), driver + ':/tmp/next')
        run('podman', 'cp', str(wheels), driver + ':/tmp/wheels')
        run('podman', 'exec', driver, 'python', '-m', 'pip', 'install', '--no-index',
            '--find-links=/tmp/wheels', '--no-cache-dir', '--target=/tmp/deps',
            '-r', '/tmp/next/requirements-db-test.txt', timeout=300)
        for _ in range(30):
            if run('podman', 'exec', db, 'pg_isready', '-U', 'postgres', check=False).returncode == 0:
                break
            time.sleep(1)
        else:
            raise RuntimeError('disposable PostgreSQL did not become ready')
        output['postgres_version'] = run('podman', 'exec', db, 'psql', '-U', 'postgres',
                                          '-At', '-c', 'SHOW server_version').stdout.strip()
        command = ['podman', 'exec', '--env', 'PYTHONPATH=/tmp/deps:/tmp/next/src',
                   '--env', 'PYTHONDONTWRITEBYTECODE=1',
                   '--env', 'TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:5432/postgres',
                   driver, 'python', '/tmp/next/tests/test_receipts.py', '/tmp/next/tests/c11b']
        result = run(*command, check=False, timeout=300)
        output.update(exit_code=result.returncode, stdout=result.stdout, stderr=result.stderr)
        print(json.dumps(output, sort_keys=True), flush=True)
        return result.returncode
    finally:
        run('podman', 'pod', 'rm', '-f', pod, check=False)
        remaining = run('podman', 'ps', '-a', '--filter', 'label=cortex.test=' + label,
                        '--format', '{{.Names}}').stdout.strip()
        if remaining or run('podman', 'pod', 'exists', pod, check=False).returncode == 0:
            raise RuntimeError('owned C11b pod cleanup failed')
        print(json.dumps({'cleanup': 'pass', 'owned_pod': pod}, sort_keys=True), flush=True)


if __name__ == '__main__':
    raise SystemExit(main())
