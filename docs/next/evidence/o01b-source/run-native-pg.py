"""O01b synthetic concurrent-mutation and corruption proof on one owned PG."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

HERE = Path(__file__).resolve().parent
WT = HERE.parents[3]
sys.path[:0] = [str(WT/'next/src'), str(WT/'docs/next/evidence/c06-source')]
from cortex_core.backup import BackupError, basebackup_command, build_manifest
from cortex_core.backup_validation import (SNAPSHOT_SQL, seal_complete_bundle,
                                           verify_complete_bundle)
from replay_lifecycle import Lifecycle

NAME = 'kaidera-test-o01b-pg-1'
IMAGE = 'sha256:db676a0ed906c00f55020fb8999e4fb30c598bf5c3b5c188630aef2812d3f11d'
OUT = HERE/'native-pg-final-003.json'
assert not OUT.exists()
assert Path(__file__).read_bytes() == subprocess.check_output([
    'git', '-C', str(WT), 'show', 'HEAD:docs/next/evidence/o01b-source/run-native-pg.py'])
tree = subprocess.check_output(['git', '-C', str(WT), 'rev-parse', 'HEAD'], text=True).strip()
inputs = [WT/'next/src/cortex_core/backup.py',
          WT/'next/src/cortex_core/backup_validation.py',
          WT/'next/tests/backup_producer/test_manifest.py',
          WT/'next/tests/backup_validation/test_validation.py',
          WT/'next/schema/manifest.json',
          HERE/'synthetic-before.sql', HERE/'synthetic-mutate.sql', Path(__file__)]
source = {str(path.relative_to(WT)): hashlib.sha256(path.read_bytes()).hexdigest()
          for path in inputs}
results = []
native = {}
errors = []
passed = False


def run(args, timeout=180):
    try:
        completed = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        results.append({'command': args, 'timed_out': True})
        raise
    results.append({'command': args, 'exit_code': completed.returncode,
                    'stdout': completed.stdout, 'stderr': completed.stderr})
    if completed.returncode:
        raise RuntimeError(f"command failed: {args[:6]}: {completed.stderr[-600:]}")
    return completed.stdout.strip()


def sql(query, port=5432):
    return run(['podman', 'exec', NAME, 'psql', '-U', 'postgres', '-h',
                '/tmp' if port == 5433 else '/var/run/postgresql', '-p', str(port),
                '-At', '-v', 'ON_ERROR_STOP=1', '-c', query])


def state(port=5432):
    return tuple(map(int, sql("""SELECT
      (SELECT current_revision FROM core.records),
      (SELECT max(source_revision) FROM retrieval.embeddings),
      (SELECT count(*) FROM core.blob_manifests),
      (SELECT applied_cursor FROM coordination.consumer_project_checkpoints)
    """, port).split('|')))


def refused(action):
    try:
        action()
    except BackupError:
        return True
    raise AssertionError('corrupt or mixed backup was reported recoverable')


life = Lifecycle(Path('/Users/amadmalik/DevVault/helix'),
                 lambda args: subprocess.run(args, text=True, capture_output=True))
try:
    for directory, count in [('backup_producer', 6), ('backup_validation', 12)]:
        env = os.environ.copy()
        env['PYTHONPATH'] = str(WT/'next/src')
        result = subprocess.run(['python3.12', str(WT/'next/tests/test_receipts.py'),
                                 str(WT/'next/tests'/directory)],
                                env=env, text=True, capture_output=True)
        results.append({'command': ['test_receipts', directory], 'exit_code': result.returncode,
                        'stdout': result.stdout, 'stderr': result.stderr})
        report = json.loads(next(line.split('=', 1)[1] for line in result.stdout.splitlines()
                                 if line.startswith('CORTEX_TEST_RESULT=')))
        assert result.returncode == 0 and report['tests_run'] == count
        assert not report['failures'] and not report['errors']
    life.acquire()
    assert subprocess.run(['podman', 'container', 'exists', NAME]).returncode == 1
    life.create('container', NAME, ['podman', 'run', '-d', '--name', NAME,
                '--network', 'none', '--cpus', '2', '--memory', '1g', '--read-only',
                '--user', '70:70', '--cap-drop=ALL', '--security-opt=no-new-privileges',
                '--image-volume=ignore',
                '--tmpfs', '/var/lib/postgresql:rw,size=805306368,mode=1777',
                '--tmpfs', '/var/run/postgresql:rw,size=16777216,mode=1777',
                '--tmpfs', '/tmp:rw,size=536870912,mode=1777',
                '--tmpfs', '/archive:rw,size=268435456,mode=1777',
                '--env', 'PGDATA=/var/lib/postgresql/18/data',
                '--env', 'POSTGRES_HOST_AUTH_METHOD=trust'] + life.labels() +
                ['--label', 'slice=O01b', IMAGE, '-c', 'archive_mode=on',
                 '-c', 'archive_command=cp %p /archive/%f',
                 '-c', 'max_wal_size=128MB'])
    for _ in range(40):
        if subprocess.run(['podman', 'exec', NAME, 'pg_isready', '-U', 'postgres'],
                          capture_output=True).returncode == 0:
            break
        time.sleep(1)
    else:
        raise RuntimeError('synthetic PG did not start')
    assert sql('SHOW server_version;') == '18.4'
    assert sql('SHOW archive_mode;') == 'on'
    run(['podman', 'cp', str(WT/'next/schema'), NAME+':/tmp/schema'])
    for source_file, target in [('synthetic-before.sql', 'before.sql'),
                                ('synthetic-mutate.sql', 'mutate.sql')]:
        run(['podman', 'cp', str(HERE/source_file), NAME+':/tmp/'+target])
    migrations = json.loads((WT/'next/schema/manifest.json').read_text())['migrations']
    for entry in migrations:
        run(['podman', 'exec', NAME, 'psql', '-U', 'postgres', '-v', 'ON_ERROR_STOP=1',
             '-f', '/tmp/schema/'+entry['file']])
    sql('CREATE TABLE core.schema_migrations (migration_id text PRIMARY KEY,sha256 text NOT NULL);')
    for entry in migrations:
        sql("INSERT INTO core.schema_migrations VALUES ('%s','%s');" %
            (entry['id'], entry['sha256']))
    run(['podman', 'exec', NAME, 'psql', '-U', 'postgres', '-v', 'ON_ERROR_STOP=1',
         '-f', '/tmp/before.sql'])
    run(['podman', 'exec', NAME, 'sh', '-c',
         "mkdir -p /tmp/blobs/synthetic; printf 'before blob' > /tmp/blobs/synthetic/before; "
         "printf 'after blob' > /tmp/blobs/synthetic/after"])
    before = state()
    assert before == (1, 1, 1, 0)
    sql("CREATE TABLE synthetic_ballast AS SELECT g,repeat('x',2048) AS body "
        "FROM generate_series(1,30000) AS g;")
    sql('SELECT pg_switch_wal();')
    command = ['podman', 'exec', NAME] + basebackup_command(
        '/tmp/base', host='127.0.0.1', user='postgres') + ['--max-rate=32M']
    started = time.monotonic()
    process = subprocess.Popen(command, text=True, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE)
    time.sleep(0.5)
    assert process.poll() is None, 'base backup ended before concurrent mutation'
    mutation_started = time.monotonic()
    run(['podman', 'exec', NAME, 'psql', '-U', 'postgres', '-v', 'ON_ERROR_STOP=1',
         '-f', '/tmp/mutate.sql'])
    mutation_finished = time.monotonic()
    assert process.poll() is None, 'mutation did not overlap the base backup'
    output, error = process.communicate(timeout=180)
    finished = time.monotonic()
    results.append({'command': command, 'exit_code': process.returncode,
                    'stdout': output, 'stderr': error})
    assert process.returncode == 0
    after_source = state()
    assert after_source == (2, 2, 2, 1)
    run(['podman', 'exec', NAME, 'pg_verifybackup', '-n', '/tmp/base'])
    sql('CREATE TABLE synthetic_tail(id integer PRIMARY KEY);')
    for number in (1, 2, 3):
        sql(f'INSERT INTO synthetic_tail VALUES ({number});')
        archive_end = sql('SELECT pg_current_wal_lsn();')
        sql('SELECT pg_switch_wal();')
    segment_output = run(['podman', 'exec', NAME, 'pg_controldata',
                          '/var/lib/postgresql/18/data'])
    segment_bytes = int(next(line.split(':', 1)[1].strip()
                             for line in segment_output.splitlines()
                             if line.startswith('Bytes per WAL segment:')))

    with tempfile.TemporaryDirectory(prefix='cox-o01b-native-', dir=WT) as folder:
        root = Path(folder)
        config = root/'recovery.conf'
        config.write_text("restore_command = 'cp /archive/%f %p'\n" +
                          f"recovery_target_lsn = '{archive_end}'\n" +
                          "recovery_target_action = 'pause'\n")
        run(['podman', 'cp', str(config), NAME+':/tmp/recovery.conf'])
        run(['podman', 'exec', NAME, 'sh', '-c',
             'cp -a /tmp/base /tmp/restored && cat /tmp/recovery.conf >> '
             '/tmp/restored/postgresql.auto.conf && touch /tmp/restored/recovery.signal'])
        run(['podman', 'exec', NAME, 'pg_ctl', '-D', '/tmp/restored', '-o',
             '-p 5433 -k /tmp -c listen_addresses=', '-w', 'start'], 90)
        for _ in range(30):
            status = sql('SELECT pg_is_in_recovery(),pg_is_wal_replay_paused();', 5433)
            if status == 't|t':
                break
            time.sleep(1)
        else:
            raise RuntimeError('isolated recovery did not pause at target')
        replay_lsn = sql('SELECT pg_last_wal_replay_lsn();', 5433)
        requested_end_lsn = archive_end
        archive_end = replay_lsn
        assert state(5433) == (2, 2, 2, 1)
        snapshot = json.loads(sql(SNAPSHOT_SQL, 5433))
        assert len(snapshot['schema_ledger']) == 9
        assert len(snapshot['model_identities']) == 1
        assert len(snapshot['blob_inventory']) == 2
        assert snapshot['record_heads'][0]['revision'] == 2
        assert snapshot['vectors'][0]['revision'] == 2
        assert snapshot['consumer_generations'][0]['cursor'] == 1

        base, archive, blobs = root/'base', root/'archive', root/'blobs'
        run(['podman', 'cp', NAME+':/tmp/base', str(base)])
        run(['podman', 'cp', NAME+':/tmp/blobs', str(blobs)])
        for _ in range(30):
            if archive.exists():
                import shutil
                shutil.rmtree(archive)
            run(['podman', 'cp', NAME+':/archive', str(archive)])
            try:
                preliminary = build_manifest(base, archive, archive_end, snapshot,
                                             segment_bytes=segment_bytes)
                break
            except BackupError:
                time.sleep(1)
        else:
            raise RuntimeError('continuous WAL archive did not complete')
        assert len(preliminary['wal_segments']) >= 3
        identity = root/'synthetic-age-key'
        run(['age-keygen', '-o', str(identity)])
        recipient = next(line.split(': ', 1)[1] for line in identity.read_text().splitlines()
                         if line.startswith('# public key: '))
        target = root/'complete.tar.age'
        manifest = seal_complete_bundle(base, archive, archive_end, snapshot,
                                        segment_bytes=segment_bytes, blob_root=blobs,
                                        recipient=recipient, destination=target)
        digest = hashlib.sha256(json.dumps(manifest, sort_keys=True,
                                         separators=(',', ':')).encode()).hexdigest()
        verified = verify_complete_bundle(target, identity, digest, snapshot)
        assert verified['recoverable']

        mixed = json.loads(json.dumps(snapshot))
        mixed['vectors'][0]['revision'] = 1
        mixed['consumer_generations'][0]['cursor'] = 0
        native['mixed_snapshot_refused'] = refused(
            lambda: verify_complete_bundle(target, identity, digest, mixed))
        missing = blobs/'synthetic/after'
        missing_hold = blobs/'synthetic/after.held'
        missing.rename(missing_hold)
        try:
            native['missing_blob_refused'] = refused(lambda: seal_complete_bundle(
                base, archive, archive_end, snapshot, segment_bytes=segment_bytes,
                blob_root=blobs, recipient=recipient, destination=root/'missing.age'))
        finally:
            missing_hold.rename(missing)
        vector_path = sql("SELECT pg_relation_filepath('retrieval.embeddings');")
        vector = base/vector_path
        old_vector = vector.read_bytes()
        vector.write_bytes(bytes([old_vector[0] ^ 1]) + old_vector[1:])
        try:
            native['altered_vector_refused'] = refused(lambda: seal_complete_bundle(
                base, archive, archive_end, snapshot, segment_bytes=segment_bytes,
                blob_root=blobs, recipient=recipient, destination=root/'vector.age'))
        finally:
            vector.write_bytes(old_vector)
        schema_path = sql("SELECT pg_relation_filepath('core.schema_migrations');")
        schema_file = base/schema_path
        old_schema = schema_file.read_bytes()
        schema_file.write_bytes(bytes([old_schema[0] ^ 1]) + old_schema[1:])
        try:
            native['altered_schema_refused'] = refused(lambda: seal_complete_bundle(
                base, archive, archive_end, snapshot, segment_bytes=segment_bytes,
                blob_root=blobs, recipient=recipient, destination=root/'schema.age'))
        finally:
            schema_file.write_bytes(old_schema)
        wal_file = archive/preliminary['wal_segments'][-1]['name']
        old_wal = wal_file.read_bytes()
        wal_file.write_bytes(bytes([old_wal[0] ^ 1]) + old_wal[1:])
        try:
            native['altered_wal_refused'] = refused(lambda: seal_complete_bundle(
                base, archive, archive_end, snapshot, segment_bytes=segment_bytes,
                blob_root=blobs, recipient=recipient, destination=root/'wal.age'))
        finally:
            wal_file.write_bytes(old_wal)
        native['missing_identity_refused'] = refused(lambda: verify_complete_bundle(
            target, root/'missing-age-key', digest, snapshot))
        native['bad_manifest_digest_refused'] = refused(lambda: verify_complete_bundle(
            target, identity, '0'*64, snapshot))
        native.update({'pg_version': '18.4', 'segment_bytes': segment_bytes,
                       'before': before, 'after_source': after_source,
                       'after_recovery': state(5433), 'replay_lsn': replay_lsn,
                       'requested_end_lsn': requested_end_lsn,
                       'archive_end_lsn': archive_end,
                       'mutation_overlapped_backup': started < mutation_started < mutation_finished < finished,
                       'backup_duration_s': round(finished-started, 3),
                       'wal_segments': [row['name'] for row in manifest['wal_segments']],
                       'base_files': len(manifest['base_files']),
                       'blob_members': len(snapshot['blob_inventory']),
                       'encrypted_bundle_sha256': hashlib.sha256(target.read_bytes()).hexdigest(),
                       'manifest_sha256': digest, 'verified_member_count': verified['member_count']})
    passed = True
except Exception as error:
    errors.append(type(error).__name__+': '+str(error))
finally:
    cleanup_error = life.finish()
    if cleanup_error:
        errors.append('cleanup: '+str(cleanup_error))
    receipt = {'tree': tree, 'source_sha256': source, 'passed': passed and not errors,
               'native': native, 'results': results, 'cleanup': life.receipt(),
               'errors': errors, 'synthetic_only': True,
               'published_ports': [], 'bind_mounts': [],
               'limits': {'cpus': 2, 'memory': '1g'}}
    OUT.write_text(json.dumps(receipt, indent=2)+'\n')
    print(json.dumps({'passed': receipt['passed'], 'native': native,
                      'cleanup_verified': receipt['cleanup']['cleanup_verified'],
                      'errors': errors}), flush=True)
if not receipt['passed']:
    raise SystemExit(1)
