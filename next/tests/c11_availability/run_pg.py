"""One owned network-none PG18.4 pod for C11 outage and inherited suites."""

import hashlib
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[2]
PG = 'docker.io/pgvector/pgvector@sha256:42e7f6b4e1eceb02ff14e3e6bc6108bbe259abbe83879dc1845d0da1ddeb555d'
PY = 'sha256:ce9a404c2c0138e747a43e6ea022d2f7e670ed868df35d627a663ba7fb940ea9'
SUITES = ('c11a', 'c11b2', 'auth', 'records', 'outbox', 'schema', 'module_consumer')


def run(*argv, check=True, timeout=300):
    value = subprocess.run(argv, text=True, capture_output=True, timeout=timeout)
    if check and value.returncode:
        raise RuntimeError(f'{argv[:3]} failed {value.returncode}: {value.stderr[-600:]}')
    return value


def preflight(wheels):
    pressure = run('memory_pressure', '-Q').stdout
    value = re.search(r'System-wide memory free percentage: (\d+)%', pressure)
    if value is None or int(value.group(1)) < 35:
        raise RuntimeError('Mac free memory below 35%')
    machine = run('podman', 'machine', 'ssh', 'kos-e020-uat', '--', 'free', '-m').stdout
    row = next((line.split() for line in machine.splitlines() if line.startswith('Mem:')), None)
    if row is None or int(row[6]) * 100 < int(row[1]) * 35:
        raise RuntimeError('Podman VM free memory below 35%')
    if not wheels.is_dir() or not any(wheels.glob('psycopg*.whl')):
        raise RuntimeError('offline wheel set is absent')
    for image in (PG, PY):
        if run('podman', 'image', 'exists', image, check=False).returncode:
            raise RuntimeError('pinned local image absent; no pull allowed')
    return int(value.group(1)), int(row[6])


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else 'red'
    if mode not in ('red', 'focus', 'green') and not mode.startswith('mutant-'):
        raise RuntimeError('invalid C11 availability mode')
    wheels = Path(os.environ['C11_AVAIL_WHEELS']).resolve()
    mac_free, vm_free_mib = preflight(wheels)
    token = uuid4().hex[:10]
    pod, db, driver = (f'kaidera-test-c11avail-{kind}-{token}'
                       for kind in ('pod', 'pg', 'driver'))
    label = f'cox-c11avail-{token}'
    labels = ['--label', 'cortex.test=' + label, '--label', 'cortex.worker=cox']
    output = {'mode': mode, 'source_head': run('git', 'rev-parse', 'HEAD').stdout.strip(),
              'images': [PG, PY], 'network': 'none', 'published_ports': [],
              'cpus': 2, 'memory_gib': 1, 'mac_free_percent': mac_free,
              'vm_free_mib': vm_free_mib, 'pod': pod,
              'wheels': {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                         for p in sorted(wheels.glob('*.whl'))},
              'suites': {}, 'control': [], 'cleanup': 'not_run'}
    exit_code = 1
    try:
        run('podman', 'pod', 'create', '--name', pod, '--network', 'none',
            '--cpus', '2', '--memory', '1g', *labels)
        run('podman', 'run', '-d', '--pod', pod, '--name', db, *labels,
            '--cpus', '1', '--memory', '768m', '--read-only', '--user', '70:70',
            '--cap-drop=ALL', '--security-opt=no-new-privileges', '--image-volume=ignore',
            '--tmpfs', '/var/lib/postgresql:rw,size=536870912,mode=1777',
            '--tmpfs', '/var/run/postgresql:rw,size=16777216,mode=1777',
            '--tmpfs', '/tmp:rw,size=16777216,mode=1777', PG, 'sleep', '1800')
        run('podman', 'run', '-d', '--pod', pod, '--name', driver, *labels,
            '--cpus', '1', '--memory', '256m', '--read-only', '--user', '10001:10001',
            '--cap-drop=ALL', '--security-opt=no-new-privileges',
            '--tmpfs', '/tmp:rw,size=268435456,mode=1777', PY, 'sleep', '1800')
        run('podman', 'exec', db, 'initdb', '-D', '/var/lib/postgresql/18/data',
            '--auth=trust', '-U', 'postgres', timeout=120)
        run('podman', 'exec', db, 'pg_ctl', '-D', '/var/lib/postgresql/18/data',
            '-l', '/tmp/postgres.log', '-o', '-c listen_addresses=127.0.0.1 '
            '-c max_wal_size=64MB -c min_wal_size=32MB', 'start', timeout=120)
        for _ in range(30):
            if run('podman', 'exec', db, 'pg_isready', '-U', 'postgres',
                   check=False).returncode == 0:
                break
            time.sleep(0.2)
        else:
            raise RuntimeError('owned PostgreSQL failed readiness')
        output['postgres_version'] = run('podman', 'exec', db, 'psql', '-U',
                                         'postgres', '-At', '-c',
                                         'SHOW server_version').stdout.strip()
        run('podman', 'cp', str(ROOT), driver + ':/tmp/next')
        run('podman', 'cp', str(wheels), driver + ':/tmp/wheels')
        run('podman', 'exec', driver, 'python', '-m', 'pip', 'install', '--no-index',
            '--find-links=/tmp/wheels', '--no-cache-dir', '--target=/tmp/deps',
            '-r', '/tmp/next/requirements-db-test.txt', timeout=300)
        run('podman', 'exec', driver, 'python', '-m', 'pip', 'install', '--no-index',
            '--find-links=/tmp/wheels', '--no-cache-dir', '--target=/tmp/deps',
            '-r', '/tmp/next/requirements-test.txt', timeout=300)
        run('podman', 'exec', driver, 'python', '-m', 'pip', 'install', '--no-index',
            '--find-links=/tmp/wheels', '--no-cache-dir', '--target=/tmp/deps',
            'asyncpg==0.30.0', 'httpx==0.28.1', timeout=300)
        if mode.startswith('mutant-'):
            mutate = Path(__file__).with_name('mutants.py')
            run('podman', 'cp', str(mutate), driver + ':/tmp/mutants.py')
            run('podman', 'exec', driver, 'python', '/tmp/mutants.py', mode[7:])
        env = ['podman', 'exec', '--env', 'PYTHONPATH=/tmp/deps:/tmp/next/src',
               '--env', 'PYTHONDONTWRITEBYTECODE=1',
               '--env', 'TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:5432/postgres',
               driver, 'python', '-u', '/tmp/next/tests/test_receipts.py']
        with tempfile.TemporaryFile(mode='w+t') as errors:
            child = subprocess.Popen(env + ['/tmp/next/tests/c11_availability'],
                                     stdout=subprocess.PIPE, stderr=errors, text=True,
                                     bufsize=1)
            lines = []
            messages = queue.Queue()

            def copy_output():
                for value in child.stdout:
                    messages.put(value)
                messages.put(None)

            threading.Thread(target=copy_output, daemon=True).start()
            deadline = time.monotonic() + 300
            while True:
                try:
                    line = messages.get(timeout=max(0, deadline - time.monotonic()))
                except queue.Empty:
                    child.kill()
                    child.wait(timeout=10)
                    raise RuntimeError('outage suite exceeded five-minute watchdog')
                if line is None:
                    break
                lines.append(line)
                if line.startswith('CORTEX_CONTROL='):
                    action = line.strip().split('=', 1)[1]
                    if action == 'STOP':
                        cmd = ('pg_ctl', '-D', '/var/lib/postgresql/18/data',
                               '-m', 'immediate', 'stop')
                    elif action == 'START':
                        cmd = ('pg_ctl', '-D', '/var/lib/postgresql/18/data',
                               '-l', '/tmp/postgres.log', '-o',
                               '-c listen_addresses=127.0.0.1 -c max_wal_size=64MB '
                               '-c min_wal_size=32MB', 'start')
                    else:
                        raise RuntimeError('unknown outage control')
                    event = run('podman', 'exec', db, *cmd, check=False, timeout=120)
                    output['control'].append({'action': action, 'exit_code': event.returncode,
                                              'stdout': event.stdout, 'stderr': event.stderr})
                    if event.returncode:
                        raise RuntimeError('owned PostgreSQL control failed')
            child.wait(timeout=300)
            errors.seek(0)
            result = {'exit_code': child.returncode, 'stdout': ''.join(lines),
                      'stderr': errors.read()}
        output['suites']['c11_availability'] = result
        if mode == 'green' and result['exit_code'] == 0:
            for name in SUITES:
                path = '/tmp/next/tests/' + name
                receipt = run(*(env + [path]), check=False, timeout=600)
                output['suites'][name] = {'exit_code': receipt.returncode,
                                          'stdout': receipt.stdout,
                                          'stderr': receipt.stderr}
            exit_code = 0 if all(row['exit_code'] == 0 for row in output['suites'].values()) else 1
        else:
            exit_code = result['exit_code']
        return exit_code
    finally:
        run('podman', 'pod', 'rm', '-f', pod, check=False)
        remaining = run('podman', 'ps', '-a', '--filter', 'label=cortex.test=' + label,
                        '--format', '{{.Names}}').stdout.strip()
        if remaining or run('podman', 'pod', 'exists', pod, check=False).returncode == 0:
            output['cleanup'] = 'failed'
        else:
            output['cleanup'] = 'pass'
        print(json.dumps(output, sort_keys=True), flush=True)
        if output['cleanup'] != 'pass':
            raise RuntimeError('owned pod cleanup failed')


if __name__ == '__main__':
    raise SystemExit(main())
