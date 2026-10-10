"""O01b: no mixed boundary and no corrupt set reported recoverable."""
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest

from cortex_core.backup import BackupError
from cortex_core.backup_validation import seal_complete_bundle, verify_complete_bundle


SEG = 1 << 20
REC = '00000000-0000-4000-8000-000000000010'
MODEL = '00000000-0000-4000-8000-000000000020'


def sha(data):
    return hashlib.sha256(data).hexdigest()


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='cox-o01b-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
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
        for number in (16, 17, 18):
            (self.archive/f'00000001000000000000{number:04X}').write_bytes(bytes([number])*SEG)
        (self.blobs/'synthetic/blob').write_bytes(b'synthetic blob')
        self.after = {
            'installation_id': '00000000-0000-4000-8000-000000000001',
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
                                         recipient=self.recipient, destination=self.target)
        digest = sha(json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode())
        return manifest, digest

    def verify(self, path, digest, snapshot=None, identity=None):
        return verify_complete_bundle(path, identity or self.identity, digest,
                                      snapshot or self.after)

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

    def test_mixed_record_vector_blob_checkpoint_snapshot_is_refused(self):
        _, digest = self.seal()
        mixed = json.loads(json.dumps(self.after))
        mixed['vectors'] = self.before['vectors']
        mixed['blob_inventory'] = self.before['blob_inventory']
        mixed['consumer_generations'] = self.before['consumer_generations']
        with self.assertRaises(BackupError):
            self.verify(self.target, digest, snapshot=mixed)

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


if __name__ == '__main__':
    unittest.main()
