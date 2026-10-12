"""O01a synthetic native PG18.4 backup/WAL/encryption receipt, no host binds."""
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import time

HERE = Path(__file__).resolve().parent
WT = HERE.parents[3]
sys.path.insert(0, str(WT / 'next/src'))
sys.path.insert(0, str(WT / 'docs/next/evidence/c06-source'))
from cortex_core.backup import BackupError, basebackup_command, build_manifest, seal_encrypted_bundle
from replay_lifecycle import Lifecycle

NAME = 'kaidera-test-o01a-pg-1'
IMAGE = 'sha256:db676a0ed906c00f55020fb8999e4fb30c598bf5c3b5c188630aef2812d3f11d'
OUT = HERE / 'native-pg-final-003.json'
assert not OUT.exists()
assert Path(__file__).read_bytes() == subprocess.check_output([
    'git', '-C', str(WT), 'show', 'HEAD:docs/next/evidence/o01a-source/run-native-pg.py'])
source = {str(path.relative_to(WT)): hashlib.sha256(path.read_bytes()).hexdigest()
          for path in [WT/'next/src/cortex_core/backup.py',
                       WT/'next/tests/backup_producer/test_manifest.py',
                       WT/'docs/next/evidence/o01a-source/run-native-pg.py']}
tree = subprocess.check_output(['git', '-C', str(WT), 'rev-parse', 'HEAD'], text=True).strip()
results = []
errors = []
passed = False


def run(args, timeout=120, env=None):
    try:
        result = subprocess.run(args, text=True, capture_output=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired as error:
        results.append({'command': args, 'timeout': True})
        raise error
    results.append({'command': args, 'exit_code': result.returncode,
                    'stdout': result.stdout, 'stderr': result.stderr})
    if result.returncode:
        raise RuntimeError(f"command failed: {args[:5]}: {result.stderr[-500:]}")
    return result


def psql(sql):
    return run(['podman', 'exec', NAME, 'psql', '-U', 'postgres', '-At',
                '-v', 'ON_ERROR_STOP=1', '-c', sql]).stdout.strip()


life = Lifecycle(Path('/Users/amadmalik/DevVault/helix'),
                 lambda args: subprocess.run(args, text=True, capture_output=True))
native = {}
try:
    local_env = os.environ.copy()
    local_env['PYTHONPATH'] = str(WT/'next/src')
    local = run(['python3.12', str(WT/'next/tests/test_receipts.py'),
                 str(WT/'next/tests/backup_producer')], env=local_env)
    report = json.loads(next(line.split('=', 1)[1] for line in local.stdout.splitlines()
                             if line.startswith('CORTEX_TEST_RESULT=')))
    assert report['tests_run'] == 6 and not report['failures'] and not report['errors']
    life.acquire()
    assert subprocess.run(['podman', 'container', 'exists', NAME]).returncode == 1
    life.create('container', NAME, ['podman', 'run', '-d', '--name', NAME,
                '--network', 'none', '--cpus', '2', '--memory', '1g', '--read-only',
                '--user', '70:70', '--cap-drop=ALL', '--security-opt=no-new-privileges',
                '--image-volume=ignore',
                '--tmpfs', '/var/lib/postgresql:rw,size=536870912,mode=1777',
                '--tmpfs', '/var/run/postgresql:rw,size=16777216,mode=1777',
                '--tmpfs', '/tmp:rw,size=268435456,mode=1777',
                '--tmpfs', '/archive:rw,size=134217728,mode=1777',
                '--env', 'PGDATA=/var/lib/postgresql/18/data',
                '--env', 'POSTGRES_HOST_AUTH_METHOD=trust']
                + life.labels() + ['--label', 'slice=O01a', IMAGE,
                '-c', 'archive_mode=on', '-c', 'archive_command=cp %p /archive/%f',
                '-c', 'max_wal_size=64MB', '-c', 'min_wal_size=32MB'])
    for _ in range(40):
        if subprocess.run(['podman', 'exec', NAME, 'pg_isready', '-U', 'postgres'],
                          capture_output=True).returncode == 0:
            break
        time.sleep(1)
    else:
        raise RuntimeError('synthetic PG did not start')
    assert psql('SHOW server_version;') == '18.4'
    assert psql('SHOW archive_mode;') == 'on'
    psql('CREATE TABLE synthetic_backup (id integer PRIMARY KEY, body text NOT NULL);')
    psql("INSERT INTO synthetic_backup VALUES (1,'synthetic before backup');")
    psql('SELECT pg_switch_wal();')
    run(['podman', 'exec', NAME] + basebackup_command(
        '/tmp/base', host='127.0.0.1', user='postgres'), 180)
    run(['podman', 'exec', NAME, 'pg_verifybackup', '-n', '/tmp/base'], 120)
    for number in (2, 3, 4):
        psql(f"INSERT INTO synthetic_backup VALUES ({number},'synthetic after backup {number}');")
        archive_end = psql('SELECT pg_current_wal_lsn();')
        psql('SELECT pg_switch_wal();')
    segment_output = run(['podman', 'exec', NAME, 'pg_controldata',
                          '/var/lib/postgresql/18/data']).stdout
    segment_bytes = int(next(line.split(':', 1)[1].strip() for line in segment_output.splitlines()
                             if line.startswith('Bytes per WAL segment:')))
    schema = json.loads((WT/'next/schema/manifest.json').read_text())
    metadata = {
        'installation_id': '00000000-0000-4000-8000-000000000001',
        'schema_ledger': [{'id': x['id'], 'sha256': x['sha256']} for x in schema['migrations']],
        'consumer_generations': [{'module': 'graph', 'project': '00000000-0000-4000-8000-000000000003', 'generation': 1, 'cursor': 0}],
        'model_identities': [{'id': '00000000-0000-4000-8000-000000000004', 'provider': 'fixture', 'model': 'synthetic-1', 'version': '1', 'dimensions': 3}],
        'blob_inventory': [{'object_key': 'synthetic/blob', 'sha256': hashlib.sha256(b'synthetic blob').hexdigest(), 'byte_length': 14}],
    }
    with tempfile.TemporaryDirectory(prefix='cox-o01a-native-', dir=WT) as folder:
        root = Path(folder)
        base, archive = root/'base', root/'archive'
        run(['podman', 'cp', NAME+':/tmp/base', str(base)], 120)
        for _ in range(30):
            if archive.exists():
                import shutil
                shutil.rmtree(archive)
            run(['podman', 'cp', NAME+':/archive', str(archive)], 120)
            try:
                manifest = build_manifest(base, archive, archive_end, metadata,
                                          segment_bytes=segment_bytes)
                break
            except BackupError:
                time.sleep(1)
        else:
            raise RuntimeError('continuous archive did not complete')
        assert len(manifest['wal_segments']) >= 3
        empty = root/'empty'
        empty.mkdir()
        try:
            build_manifest(base, empty, archive_end, metadata, segment_bytes=segment_bytes)
        except BackupError:
            native['base_only_refused'] = True
        else:
            raise AssertionError('base backup alone was accepted')
        middle = archive/manifest['wal_segments'][1]['name']
        held = middle.with_suffix('.held')
        middle.rename(held)
        try:
            build_manifest(base, archive, archive_end, metadata, segment_bytes=segment_bytes)
        except BackupError:
            native['gap_refused'] = True
        else:
            raise AssertionError('WAL gap was accepted')
        finally:
            held.rename(middle)
        try:
            build_manifest(base, archive, manifest['backup_end_lsn'], metadata,
                           segment_bytes=segment_bytes)
        except BackupError:
            native['unbound_interval_refused'] = True
        else:
            raise AssertionError('unbound interval was accepted')
        identity = root/'synthetic-age-key'
        run(['age-keygen', '-o', str(identity)])
        recipient = next(line.split(': ', 1)[1] for line in identity.read_text().splitlines()
                         if line.startswith('# public key: '))
        target = root/'backup.tar.age'
        sealed = seal_encrypted_bundle(base, archive, archive_end, metadata,
                                       segment_bytes=segment_bytes, recipient=recipient,
                                       destination=target)
        assert sealed == manifest
        decrypted = subprocess.run(['age', '-d', '-i', str(identity)],
                                   input=target.read_bytes(), capture_output=True, check=True).stdout
        with tarfile.open(fileobj=io.BytesIO(decrypted), mode='r:') as bundle:
            assert json.load(bundle.extractfile('manifest.json')) == manifest
            assert 'base/backup_manifest' in bundle.getnames()
            assert 'wal/'+manifest['wal_segments'][1]['name'] in bundle.getnames()
        native.update({'pg_version': '18.4', 'segment_bytes': segment_bytes,
                       'postgres_system_identifier': manifest['postgres_system_identifier'],
                       'backup_start_lsn': manifest['backup_start_lsn'],
                       'backup_end_lsn': manifest['backup_end_lsn'],
                       'archive_end_lsn': archive_end,
                       'wal_segments': [x['name'] for x in manifest['wal_segments']],
                       'base_files': len(manifest['base_files']),
                       'encrypted_bundle_sha256': hashlib.sha256(target.read_bytes()).hexdigest(),
                       'synthetic_age_roundtrip': True,
                       'pg_verifybackup_passed': True})
    passed = True
except Exception as error:
    errors.append(type(error).__name__+': '+str(error))
finally:
    cleanup_error = life.finish()
    if cleanup_error:
        errors.append('cleanup: '+str(cleanup_error))
    receipt = {'tree': tree, 'source_sha256': source, 'passed': passed and not errors,
               'native': native, 'local_tests': 6, 'results': results,
               'cleanup': life.receipt(), 'errors': errors,
               'published_ports': [], 'bind_mounts': [], 'synthetic_only': True,
               'limits': {'cpus': 2, 'memory': '1g'}}
    OUT.write_text(json.dumps(receipt, indent=2)+'\n')
    print(json.dumps({'passed': receipt['passed'], 'native': native,
                      'cleanup_verified': receipt['cleanup']['cleanup_verified'],
                      'errors': errors}), flush=True)
if not receipt['passed']:
    raise SystemExit(1)
