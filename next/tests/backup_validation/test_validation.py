"""O01b: no mixed boundary and no corrupt set reported recoverable."""
import hashlib
import io
import inspect
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from cortex_core.backup import BackupError, basebackup_command, seal_encrypted_bundle
from cortex_core.backup_validation import (_validate_native_pg, seal_complete_bundle,
                                           verify_complete_bundle)


SEG = 1 << 20
REC = '00000000-0000-4000-8000-000000000010'
MODEL = '00000000-0000-4000-8000-000000000020'


def sha(data):
    return hashlib.sha256(data).hexdigest()


class SyntheticSnapshotConnection:
    """Unit-only byte-comparison input; real recovery is exercised separately."""

    def __init__(self, value):
        self.value = value

    def execute(self, sql):
        if 'pg_is_in_recovery()' not in sql or 'pg_is_wal_replay_paused()' not in sql:
            raise AssertionError('paused-recovery SQL guard is absent')
        return self

    def fetchone(self):
        return (json.dumps(self.value),)


class ValidationTests(unittest.TestCase):
    def test_backup_entrypoints_require_caller_supplied_executables(self):
        for function, names in (
            (basebackup_command, ('executable',)),
            (seal_encrypted_bundle, ('age_binary',)),
            (seal_complete_bundle, ('age_binary', 'pg_verifybackup_binary',
                                    'pg_waldump_binary')),
            (_validate_native_pg, ('pg_verifybackup_binary', 'pg_waldump_binary')),
            (verify_complete_bundle, ('age_binary', 'pg_verifybackup_binary',
                                      'pg_waldump_binary')),
        ):
            for name in names:
                with self.subTest(function=function.__name__, executable=name):
                    parameter = inspect.signature(function).parameters.get(name)
                    if parameter is None:
                        self.fail(f'{function.__name__} lacks required {name}')
                    self.assertIs(parameter.default, inspect.Parameter.empty)

    def test_bare_basebackup_executable_refuses(self):
        with self.assertRaises(BackupError):
            basebackup_command('/tmp/synthetic-base', host='127.0.0.1',
                               user='postgres', executable='pg_basebackup')

    def setUp(self):
        native = patch('cortex_core.backup_validation._validate_native_pg')
        native.start()
        self.addCleanup(native.stop)
        self.temporary = tempfile.TemporaryDirectory(prefix='cox-o01b-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.age_binary = shutil.which('age')
        self.pg_verifybackup_binary = shutil.which('pg_verifybackup')
        self.pg_waldump_binary = shutil.which('pg_waldump')
        self.assertTrue(all((self.age_binary, self.pg_verifybackup_binary,
                             self.pg_waldump_binary)))
        self.base = self.root/'base'
        self.archive = self.root/'archive'
        self.blobs = self.root/'blobs'
        for path in (self.base, self.archive, self.blobs/'synthetic'):
            path.mkdir(parents=True)
        (self.base/'PG_VERSION').write_text('18\n')
        (self.base/'backup_label').write_text(
            'START WAL LOCATION: 0/1000010 (file 000000010000000000000010)\nSTART TIMELINE: 1\n')
        (self.base/'backup_manifest').write_text(json.dumps({
            'PostgreSQL-Backup-Manifest-Version': 2, 'System-Identifier': 123456789,
            'Files': [], 'WAL-Ranges': [{'Timeline': 1, 'Start-LSN': '0/1000010',
                                        'End-LSN': '0/1000100'}]}))
        (self.base/'base').mkdir()
        (self.base/'base/123').mkdir()
        (self.base/'base/123/456').write_bytes(b'vector-after')
        native = json.loads((self.base/'backup_manifest').read_text())
        native['Files'] = [
            {'Path': name, 'Size': (self.base/name).stat().st_size,
             'Checksum-Algorithm': 'SHA256', 'Checksum': sha((self.base/name).read_bytes())}
            for name in ('PG_VERSION', 'backup_label', 'base/123/456')]
        prefix = json.dumps(native, separators=(',', ':'))[:-1] + ',\n'
        (self.base/'backup_manifest').write_text(
            prefix + '"Manifest-Checksum": "' + sha(prefix.encode()) + '"}\n')
        for number in (16, 17, 18):
            (self.archive/f'00000001000000000000{number:04X}').write_bytes(bytes([number])*SEG)
        (self.blobs/'synthetic/blob').write_bytes(b'synthetic blob')
        self.after = {
            'installation_id': '00000000-0000-4000-8000-000000000001',
            'replay_lsn': '0/1300000',
            'schema_ledger': [{'id': 'core-0001', 'sha256': 'a'*64}],
            'consumer_generations': [{'module': 'graph', 'project': '00000000-0000-4000-8000-000000000003', 'generation': 1, 'cursor': 8}],
            'model_identities': [{'id': MODEL, 'provider': 'fixture', 'model': 'synthetic-1', 'version': '1', 'dimensions': 3}],
            'blob_inventory': [{'object_key': 'synthetic/blob', 'sha256': sha(b'synthetic blob'), 'byte_length': 14}],
            'record_heads': [{'id': REC, 'revision': 2, 'payload_sha256': 'c'*64}],
            'vectors': [{'record_id': REC, 'revision': 2, 'model_id': MODEL, 'sha256': sha(b'vector-after')}],
        }
        self.before = json.loads(json.dumps(self.after))
        self.before['record_heads'][0]['revision'] = 1
        self.before['vectors'][0]['revision'] = 1
        self.before['vectors'][0]['sha256'] = sha(b'vector-before')
        self.before['blob_inventory'][0]['object_key'] = 'synthetic/old-blob'
        self.before['consumer_generations'][0]['cursor'] = 7
        self.identity = self.root/'synthetic-age-key'
        subprocess.run(['age-keygen', '-o', str(self.identity)], capture_output=True, check=True)
        self.recipient = next(line.split(': ', 1)[1] for line in self.identity.read_text().splitlines()
                              if line.startswith('# public key: '))
        self.target = self.root/'complete.tar.age'

    def seal(self):
        manifest = seal_complete_bundle(self.base, self.archive, '0/1300000', self.after,
                                         segment_bytes=SEG, blob_root=self.blobs,
                                         recipient=self.recipient, destination=self.target,
                                         age_binary=self.age_binary,
                                         pg_verifybackup_binary=self.pg_verifybackup_binary,
                                         pg_waldump_binary=self.pg_waldump_binary)
        digest = sha(json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode())
        return manifest, digest

    def test_path_substitution_cannot_receive_synthetic_backup_plaintext(self):
        fake_dir = self.root/'fake-path'
        fake_dir.mkdir()
        marker = self.root/'fake-age-ran'
        fake = fake_dir/'age'
        fake.write_text(f'#!{sys.executable}\nimport pathlib,sys\n'
                        f'sys.stdin.buffer.read()\npathlib.Path({str(marker)!r}).write_text("ran")\n'
                        'sys.stdout.buffer.write(b"fake-ciphertext")\n')
        fake.chmod(0o700)
        with patch.dict(os.environ, {'PATH': str(fake_dir) + os.pathsep + os.environ['PATH']}):
            try:
                seal_encrypted_bundle(self.base, self.archive, '0/1300000', self.after,
                                      segment_bytes=SEG, blob_root=self.blobs,
                                      recipient=self.recipient, destination=self.target,
                                      age_binary='age')
            except BackupError:
                pass
        self.assertFalse(marker.exists(), 'backup plaintext reached a PATH-substituted age')
        self.assertFalse(self.target.exists(), 'invalid executable published ciphertext')

    def test_invalid_executable_shapes_refuse_before_output_or_verdict(self):
        not_executable = self.root/'not-executable'
        not_executable.write_text('synthetic')
        not_executable.chmod(0o600)
        invalid = ('age', str(self.root/'missing'), str(self.root), str(not_executable))
        for binary in invalid:
            with self.subTest(binary=binary):
                with self.assertRaises(BackupError):
                    basebackup_command(self.root/'base-out', host='127.0.0.1',
                                       user='postgres', executable=binary)
                with patch('cortex_core.backup.subprocess.Popen') as spawn:
                    with self.assertRaises(BackupError):
                        seal_encrypted_bundle(self.base, self.archive, '0/1300000',
                                              self.after, segment_bytes=SEG,
                                              blob_root=self.blobs, recipient=self.recipient,
                                              destination=self.target, age_binary=binary)
                    spawn.assert_not_called()
                for name in ('age_binary', 'pg_verifybackup_binary',
                             'pg_waldump_binary'):
                    options = {'age_binary': self.age_binary,
                               'pg_verifybackup_binary': self.pg_verifybackup_binary,
                               'pg_waldump_binary': self.pg_waldump_binary}
                    options[name] = binary
                    with self.subTest(entrypoint='seal_complete_bundle', tool=name):
                        with patch('cortex_core.backup.subprocess.Popen') as spawn:
                            with self.assertRaises(BackupError):
                                seal_complete_bundle(self.base, self.archive, '0/1300000',
                                                     self.after, segment_bytes=SEG,
                                                     blob_root=self.blobs,
                                                     recipient=self.recipient,
                                                     destination=self.target, **options)
                            spawn.assert_not_called()
                    with self.subTest(entrypoint='verify_complete_bundle', tool=name):
                        with patch('cortex_core.backup_validation.subprocess.Popen') as spawn:
                            with self.assertRaises(BackupError):
                                verify_complete_bundle(
                                    self.target, self.identity, 'a'*64,
                                    SyntheticSnapshotConnection(self.after), **options)
                            spawn.assert_not_called()
                self.assertFalse(self.target.exists())

    def test_explicit_decryption_binary_ignores_path_substitution(self):
        _, digest = self.seal()
        fake_dir = self.root/'fake-decrypt-path'
        fake_dir.mkdir()
        marker = self.root/'fake-decrypt-ran'
        fake = fake_dir/'age'
        fake.write_text(f'#!{sys.executable}\nimport pathlib,sys\n'
                        f'sys.stdin.buffer.read()\npathlib.Path({str(marker)!r}).write_text("ran")\n')
        fake.chmod(0o700)
        with patch.dict(os.environ, {'PATH': str(fake_dir) + os.pathsep + os.environ['PATH']}):
            try:
                result = self.verify(self.target, digest)
            except BackupError:
                result = None
        self.assertFalse(marker.exists(), 'decryption used an ambient PATH executable')
        self.assertIsNotNone(result)
        self.assertEqual(result['recoverable'], True)

    def test_explicit_native_verifiers_ignore_path_substitution(self):
        fake_dir = self.root/'fake-native-path'
        fake_dir.mkdir()
        marker = self.root/'fake-native-ran'
        for name in ('pg_verifybackup', 'pg_waldump'):
            fake = fake_dir/name
            fake.write_text(f'#!{sys.executable}\nimport pathlib\n'
                            f'pathlib.Path({str(marker)!r}).write_text({name!r})\n')
            fake.chmod(0o700)
        trusted = self.root/'synthetic-native-verifier'
        trusted.write_text('#!/bin/sh\nexit 0\n')
        trusted.chmod(0o700)
        manifest = {'timeline': 1, 'backup_start_lsn': '0/1000010',
                    'archive_end_lsn': '0/1300000'}
        with patch.dict(os.environ, {'PATH': str(fake_dir) + os.pathsep + os.environ['PATH']}):
            _validate_native_pg(self.base, self.archive, manifest,
                                pg_verifybackup_binary=trusted, pg_waldump_binary=trusted)
        self.assertFalse(marker.exists(), 'native recovery verdict used an ambient PATH tool')

    def verify(self, path, digest, snapshot=None, identity=None):
        return verify_complete_bundle(path, identity or self.identity, digest,
                                      SyntheticSnapshotConnection(snapshot or self.after),
                                      age_binary=self.age_binary,
                                      pg_verifybackup_binary=self.pg_verifybackup_binary,
                                      pg_waldump_binary=self.pg_waldump_binary)

    def changed_bundle(self, target_name, replacement=None):
        plain = subprocess.run(['age', '-d', '-i', str(self.identity)],
                               input=self.target.read_bytes(), capture_output=True,
                               check=True).stdout
        members = []
        with tarfile.open(fileobj=io.BytesIO(plain), mode='r:') as source:
            for member in source:
                members.append((member.name, source.extractfile(member).read()))
        result = io.BytesIO()
        with tarfile.open(fileobj=result, mode='w') as target:
            for name, body in members:
                if name == target_name:
                    if replacement is None:
                        continue
                    body = replacement(body)
                info = tarfile.TarInfo(name)
                info.size = len(body)
                target.addfile(info, io.BytesIO(body))
        changed = self.root/'changed.tar.age'
        changed.write_bytes(subprocess.run(['age', '-r', self.recipient], input=result.getvalue(),
                                           capture_output=True, check=True).stdout)
        return changed

    def test_complete_bundle_binds_blob_and_full_snapshot(self):
        manifest, digest = self.seal()
        self.assertEqual(manifest['metadata'], self.after)
        plain = subprocess.run(['age', '-d', '-i', str(self.identity)],
                               input=self.target.read_bytes(), capture_output=True,
                               check=True).stdout
        with tarfile.open(fileobj=io.BytesIO(plain), mode='r:') as archive:
            self.assertIn('blobs/synthetic/blob', archive.getnames())
        self.assertTrue(self.verify(self.target, digest)['recoverable'])

    def test_handbuilt_snapshot_cannot_issue_recovery_verdict(self):
        _, digest = self.seal()
        with self.assertRaises(BackupError):
            verify_complete_bundle(
                self.target, self.identity, digest, self.after,
                age_binary=self.age_binary,
                pg_verifybackup_binary=self.pg_verifybackup_binary,
                pg_waldump_binary=self.pg_waldump_binary,
            )

    def test_mixed_record_vector_blob_checkpoint_snapshot_is_refused(self):
        _, digest = self.seal()
        mixed = json.loads(json.dumps(self.after))
        mixed['vectors'] = self.before['vectors']
        mixed['blob_inventory'] = self.before['blob_inventory']
        mixed['consumer_generations'] = self.before['consumer_generations']
        with self.assertRaises(BackupError):
            self.verify(self.target, digest, snapshot=mixed)

    def test_manifest_endpoint_must_equal_actual_replay_lsn(self):
        delayed = json.loads(json.dumps(self.after))
        delayed['replay_lsn'] = '0/1400000'
        with self.assertRaises(BackupError):
            seal_complete_bundle(self.base, self.archive, '0/1300000', delayed,
                                 segment_bytes=SEG, blob_root=self.blobs,
                                 recipient=self.recipient, destination=self.target,
                                 age_binary=self.age_binary,
                                 pg_verifybackup_binary=self.pg_verifybackup_binary,
                                 pg_waldump_binary=self.pg_waldump_binary)

    def test_missing_blob_is_refused(self):
        _, digest = self.seal()
        with self.assertRaises(BackupError):
            self.verify(self.changed_bundle('blobs/synthetic/blob'), digest)

    def test_altered_blob_is_refused(self):
        _, digest = self.seal()
        with self.assertRaises(BackupError):
            self.verify(self.changed_bundle('blobs/synthetic/blob', lambda _: b'changed blob!!'), digest)

    def test_truncated_wal_is_refused(self):
        _, digest = self.seal()
        with self.assertRaises(BackupError):
            self.verify(self.changed_bundle('wal/000000010000000000000011', lambda body: body[:100]), digest)

    def test_altered_vector_file_is_refused(self):
        _, digest = self.seal()
        with self.assertRaises(BackupError):
            self.verify(self.changed_bundle('base/base/123/456', lambda _: b'vector-before'), digest)

    def test_altered_schema_ledger_is_refused(self):
        _, digest = self.seal()
        def change(body):
            value = json.loads(body)
            value['metadata']['schema_ledger'][0]['sha256'] = 'f'*64
            return json.dumps(value).encode()
        with self.assertRaises(BackupError):
            self.verify(self.changed_bundle('manifest.json', change), digest)

    def test_missing_manifest_member_is_refused(self):
        _, digest = self.seal()
        with self.assertRaises(BackupError):
            self.verify(self.changed_bundle('manifest.json'), digest)

    def test_missing_or_wrong_age_identity_is_refused(self):
        _, digest = self.seal()
        with self.assertRaises(BackupError):
            self.verify(self.target, digest, identity=self.root/'missing-key')
        wrong = self.root/'wrong-key'
        subprocess.run(['age-keygen', '-o', str(wrong)], capture_output=True, check=True)
        with self.assertRaises(BackupError):
            self.verify(self.target, digest, identity=wrong)

    def test_native_wal_parser_failure_refuses_recoverable(self):
        _, digest = self.seal()
        with patch('cortex_core.backup_validation._validate_native_pg',
                   side_effect=BackupError('invalid WAL')):
            with self.assertRaises(BackupError):
                self.verify(self.target, digest)

    def test_native_wal_parser_failure_blocks_seal(self):
        with patch('cortex_core.backup_validation._validate_native_pg',
                   side_effect=BackupError('invalid WAL')):
            with self.assertRaises(BackupError):
                self.seal()

    def test_vector_file_changed_before_seal_is_refused_by_native_manifest(self):
        (self.base/'base/123/456').write_bytes(b'changed-vector')
        with self.assertRaises(BackupError):
            self.seal()

    def test_native_manifest_changed_before_seal_is_refused(self):
        path = self.base/'backup_manifest'
        path.write_bytes(path.read_bytes().replace(b'123456789', b'123456788'))
        with self.assertRaises(BackupError):
            self.seal()


if __name__ == '__main__':
    unittest.main()
