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
PG = 'docker.io/pgvector/pgvector@sha256:42e7f6b4e1eceb02ff14e3e6bc6108bbe259abbe83879dc1845d0da1ddeb555d'
PY = 'sha256:ce9a404c2c0138e747a43e6ea022d2f7e670ed868df35d627a663ba7fb940ea9'
MUTANTS = {
    'writer_guard': ('src/cortex_core/api/c11b.py',
                     'if row is None or row[0] is not True:', 'if False:'),
    'legacy_action': ('src/cortex_core/api/c11b.py',
                      'created = receipt.revision == 1', 'created = False'),
    'replay_lookup': ('src/cortex_core/api/c11b.py',
                      'saved = records.lookup_request(request_key)', 'saved = None'),
    'principal_scope': ('schema/coordination/004-api-replay.sql',
                        'AND i.principal_id=scope.principal_id AND i.request_key=p_key',
                        'AND i.request_key=p_key'),
    'writer_sql': ('schema/coordination/004-api-replay.sql',
                   'AND p.name=p_name AND p.disabled=false',
                   'AND p.disabled=false'),
    'lookup_wrong_action': ('src/cortex_core/records.py',
                            "with self._authorized('write'):\n            row = _private(self.connection,",
                            "with self._authorized('read'):\n            row = _private(self.connection,"),
    'malformed_key': ('src/cortex_core/api/c11a.py',
                      "except UnicodeError:\n                return await _respond(send, 400, _packet('invalid_input', retryable=False))",
                      "except UnicodeError:\n                request_key = None"),
    'd1_403_leak': ('src/cortex_core/api/c11a.py',
                    "return 404, _packet('not_found', retryable=False)\n        return _error_status(error)",
                    "return 403, _packet('forbidden', retryable=False)\n        return _error_status(error)"),
    'd1_revoked_401_split': ('src/cortex_core/api/c11a.py',
                             "return 404, _packet('not_found', retryable=False)\n        return _error_status(error)",
                             "return 401, _packet('credential_required', retryable=False)\n"
                             "        return _error_status(error)"),
    'd1_tombstone_skipped': ('src/cortex_core/api/c11b.py',
                             "if value.tombstone:\n            raise RecordError('gone')",
                             "if False:\n            raise RecordError('gone')"),
    'd1_digest_dropped': ('src/cortex_core/api/c11b.py',
                          "'payload_sha256': value.payload_sha256}",
                          "'payload_sha256': None}"),
    'd1_memory_filter_readded': ('src/cortex_core/api/c11b.py',
                                 "if value.kind == 'memory':\n            body = json.loads(value.body)",
                                 "if value.kind != 'memory':\n            return None\n"
                                 "        if value.kind == 'memory':\n            body = json.loads(value.body)"),
}


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
    mutation = os.environ.get('C11B_MUTATION')
    if mutation and mutation not in MUTANTS:
        raise RuntimeError('unknown C11b mutation')
    output['mutation'] = mutation
    suite = os.environ.get('C11B_SUITE', 'c11b')
    if suite not in {'c11b', 'records', 'auth', 'auth_identity', 'outbox',
                     'module_consumer', 'schema'} or mutation and suite != 'c11b':
        raise RuntimeError('unknown C11b suite selection')
    output['suite'] = suite
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
        if mutation:
            path, before, after = MUTANTS[mutation]
            mutate = (
                'import hashlib,json,sys;from pathlib import Path;'
                'p=Path("/tmp/next")/sys.argv[1];b=p.read_text();'
                'assert b.count(sys.argv[2])==1;'
                'p.write_text(b.replace(sys.argv[2],sys.argv[3]));'
                'm=Path("/tmp/next/schema/manifest.json");'
                'v=json.loads(m.read_text());'
                '[(x.__setitem__("sha256",hashlib.sha256(p.read_bytes()).hexdigest())) '
                'for x in v["migrations"] if x["file"]=="coordination/004-api-replay.sql"] '
                'if sys.argv[1].endswith("004-api-replay.sql") else None;'
                'm.write_text(json.dumps(v)) '
                'if sys.argv[1].endswith("004-api-replay.sql") else None'
            )
            run('podman', 'exec', driver, 'python', '-c', mutate, path, before, after)
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
                   driver, 'python', '/tmp/next/tests/test_receipts.py', '/tmp/next/tests/' + suite]
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
