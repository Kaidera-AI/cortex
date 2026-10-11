"""One owned PG18.4 pod: real primary/paused-recovery O01 verdict controls."""

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
PG = 'docker.io/pgvector/pgvector@sha256:42e7f6b4e1eceb02ff14e3e6bc6108bbe259abbe83879dc1845d0da1ddeb555d'
UID = '00000000-0000-4000-8000-000000000001'


def run(*argv, check=True, timeout=180, input=None):
    result = subprocess.run(argv, text=True, input=input, capture_output=True,
                            timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(f'{argv[:4]} failed {result.returncode}: {result.stderr[-500:]}')
    return result


def psql(db, sql, *, port=5432):
    return run('podman', 'exec', db, 'psql', '-X', '-A', '-t',
               '-v', 'ON_ERROR_STOP=1', '-U', 'postgres', '-p', str(port), '-c', sql).stdout.strip()


def preflight():
    pressure = run('memory_pressure', '-Q').stdout
    match = re.search(r'System-wide memory free percentage: (\d+)%', pressure)
    if match is None or int(match.group(1)) < 35:
        raise RuntimeError('Mac free memory below 35%')
    machine = run('podman', 'machine', 'ssh', 'kos-e020-uat', '--', 'free', '-m').stdout
    row = next((line.split() for line in machine.splitlines() if line.startswith('Mem:')), None)
    if row is None or int(row[6]) * 100 < int(row[1]) * 35:
        raise RuntimeError('Podman VM free memory below 35%')
    if run('podman', 'image', 'exists', PG, check=False).returncode:
        raise RuntimeError('pinned PG image is absent; no pull allowed')
    return int(match.group(1)), int(row[6])


def wait_sql(db, sql, expected, *, port=5432, attempts=80):
    for _ in range(attempts):
        result = run('podman', 'exec', db, 'psql', '-X', '-A', '-t',
                     '-U', 'postgres', '-p', str(port), '-c', sql, check=False)
        if result.returncode == 0 and result.stdout.strip() == expected:
            return
        time.sleep(0.25)
    raise RuntimeError(f'owned PostgreSQL did not reach {expected} on port {port}')


def apply_schema(db):
    manifest = json.loads((ROOT/'schema/manifest.json').read_text())
    run('podman', 'cp', str(ROOT/'schema'), db + ':/tmp/schema')
    psql(db, 'CREATE SCHEMA IF NOT EXISTS core; CREATE TABLE IF NOT EXISTS '
         'core.schema_migrations (migration_id text PRIMARY KEY, sha256 text NOT NULL, '
         'applied_at timestamptz NOT NULL DEFAULT now())')
    for entry in manifest['migrations']:
        local = ROOT/'schema'/entry['file']
        if hashlib.sha256(local.read_bytes()).hexdigest() != entry['sha256']:
            raise RuntimeError('source migration digest differs')
        run('podman', 'exec', db, 'psql', '-X', '-v', 'ON_ERROR_STOP=1',
            '-U', 'postgres', '-f', '/tmp/schema/' + entry['file'])
        psql(db, "INSERT INTO core.schema_migrations (migration_id,sha256) VALUES ("
             + "'" + entry['id'] + "','" + entry['sha256'] + "')")
    psql(db, "INSERT INTO core.installations (id) VALUES ('" + UID + "')")
    return len(manifest['migrations'])


def main():
    mac_free, vm_free_mib = preflight()
    token = uuid4().hex[:10]
    pod = f'kaidera-test-o01recovery-pod-{token}'
    db = f'kaidera-test-o01recovery-pg-{token}'
    label = f'cox-o01recovery-{token}'
    labels = ['--label', 'cortex.test=' + label, '--label', 'cortex.worker=cox']
    output = {'source_head': run('git', 'rev-parse', 'HEAD').stdout.strip(),
              'image': PG, 'pod': pod, 'network': 'none', 'published_ports': [],
              'cpus': 2, 'memory_gib': 1, 'mac_free_percent': mac_free,
              'vm_free_mib': vm_free_mib, 'setup': {}, 'suite': None,
              'cleanup': 'not_run'}
    exit_code = 1
    with tempfile.TemporaryDirectory(prefix='cox-o01recovery-fixture-') as root_name:
        root = Path(root_name)
        try:
            run('podman', 'pod', 'create', '--name', pod, '--network', 'none',
                '--cpus', '2', '--memory', '1g', *labels)
            run('podman', 'run', '-d', '--pod', pod, '--name', db, *labels,
                '--cpus', '2', '--memory', '1g', '--read-only', '--user', '70:70',
                '--cap-drop=ALL', '--security-opt=no-new-privileges', '--image-volume=ignore',
                '--tmpfs', '/var/lib/postgresql:rw,size=805306368,mode=1777',
                '--tmpfs', '/var/run/postgresql:rw,size=16777216,mode=1777',
                '--tmpfs', '/tmp:rw,size=33554432,mode=1777', PG, 'sleep', '1800')
            run('podman', 'exec', db, 'initdb', '-D', '/var/lib/postgresql/primary',
                '--auth=trust', '--wal-segsize=1', '-U', 'postgres', timeout=120)
            run('podman', 'exec', db, 'mkdir', '/var/lib/postgresql/archive')
            primary_options = ("-c listen_addresses=127.0.0.1 -c max_wal_size=64MB "
                               "-c min_wal_size=32MB -c archive_mode=on "
                               "-c archive_command='cp %p /var/lib/postgresql/archive/%f'")
            run('podman', 'exec', db, 'pg_ctl', '-D', '/var/lib/postgresql/primary',
                '-l', '/tmp/primary.log', '-o', primary_options, 'start', timeout=120)
            wait_sql(db, 'SELECT 1', '1')
            output['setup']['postgres_version'] = psql(db, 'SHOW server_version')
            output['setup']['schema_migrations'] = apply_schema(db)
            run('podman', 'exec', db, 'pg_basebackup', '-h', '127.0.0.1',
                '-U', 'postgres', '-D', '/var/lib/postgresql/base', '-Fp', '-X', 'none',
                '--manifest-checksums=SHA256', '--checkpoint=fast', timeout=180)
            psql(db, "INSERT INTO core.tenants (id,installation_id) VALUES "
                 "('00000000-0000-4000-8000-000000000002','" + UID + "')")
            psql(db, 'SELECT pg_switch_wal()')
            for _ in range(80):
                count = int(run('podman', 'exec', db, 'sh', '-c',
                                'find /var/lib/postgresql/archive -maxdepth 1 -type f | wc -l').stdout.strip())
                if count >= 2:
                    break
                time.sleep(0.25)
            else:
                raise RuntimeError('owned WAL archive did not complete')
            output['setup']['archived_segments'] = count
            run('podman', 'exec', db, 'cp', '-a', '/var/lib/postgresql/base',
                '/var/lib/postgresql/recovery')
            run('podman', 'exec', db, 'touch', '/var/lib/postgresql/recovery/standby.signal')
            run('podman', 'exec', '-i', db, 'tee', '-a',
                '/var/lib/postgresql/recovery/postgresql.auto.conf',
                input="\nrestore_command = 'cp /var/lib/postgresql/archive/%f %p'\n")
            recovery_options = '-c port=5433 -c listen_addresses=127.0.0.1 -c hot_standby=on'
            run('podman', 'exec', db, 'pg_ctl', '-D', '/var/lib/postgresql/recovery',
                '-l', '/tmp/recovery.log', '-o', recovery_options, 'start', timeout=120)
            wait_sql(db, 'SELECT pg_is_in_recovery()', 't', port=5433)
            wait_sql(db, 'SELECT pg_is_wal_replay_paused()', 'f', port=5433)
            run('podman', 'exec', db, 'psql', '-X', '-U', 'postgres', '-p', '5433',
                '-c', 'SELECT pg_wal_replay_pause()')
            wait_sql(db, 'SELECT pg_is_wal_replay_paused()', 't', port=5433)
            output['setup']['recovery_lsn'] = psql(db, 'SELECT pg_last_wal_replay_lsn()', port=5433)
            output['setup']['primary_is_recovery'] = psql(db, 'SELECT pg_is_in_recovery()')
            output['setup']['recovery_is_paused'] = psql(db, 'SELECT pg_is_wal_replay_paused()', port=5433)
            run('podman', 'cp', db + ':/var/lib/postgresql/base', str(root/'base'))
            run('podman', 'cp', db + ':/var/lib/postgresql/archive', str(root/'archive'))
            env = dict(os.environ, PYTHONPATH=str(ROOT/'src'), PYTHONDONTWRITEBYTECODE='1',
                       O01_RECOVERY_CONTAINER=db, O01_BASE_DIR=str(root/'base'),
                       O01_ARCHIVE_DIR=str(root/'archive'))
            suite = subprocess.run([sys.executable, str(ROOT/'tests/test_receipts.py'),
                                    str(ROOT/'tests/backup_recovery_pg')],
                                   text=True, capture_output=True, env=env, timeout=300)
            output['suite'] = {'exit_code': suite.returncode,
                               'stdout': suite.stdout, 'stderr': suite.stderr}
            exit_code = suite.returncode
        except Exception as error:
            output['setup_error'] = str(error)
        finally:
            run('podman', 'pod', 'rm', '-f', pod, check=False)
            remaining = run('podman', 'ps', '-a', '--filter', 'label=cortex.test=' + label,
                            '--format', '{{.Names}}').stdout.strip()
            output['cleanup'] = 'pass' if not remaining and run(
                'podman', 'pod', 'exists', pod, check=False).returncode != 0 else 'failed'
            print(json.dumps(output, sort_keys=True), flush=True)
    return exit_code if output['cleanup'] == 'pass' else 1


if __name__ == '__main__':
    raise SystemExit(main())
