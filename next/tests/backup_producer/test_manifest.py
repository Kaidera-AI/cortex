"""Frozen O01a group-1 controls: a usable backup needs bound continuous WAL."""
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest

from cortex_core.backup import BackupError, build_manifest, seal_encrypted_bundle


SEGMENT_BYTES = 1 << 20
START = '0/1000010'
BACKUP_END = '0/1000100'
ARCHIVE_END = '0/1300000'


def metadata():
    return {
        'installation_id': '00000000-0000-4000-8000-000000000001',
        'schema_ledger': [{'id': 'core-0001', 'sha256': 'a' * 64}],
        'consumer_generations': [{'module': 'graph', 'project': '00000000-0000-4000-8000-000000000003', 'generation': 1, 'cursor': 7}],
        'model_identities': [{'id': '00000000-0000-4000-8000-000000000004', 'provider': 'fixture', 'model': 'synthetic-1', 'version': '1', 'dimensions': 3}],
        'blob_inventory': [{'object_key': 'synthetic/blob', 'sha256': 'b' * 64, 'byte_length': 13}],
    }


class BackupManifestTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='cox-o01a-manifest-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.base = self.root / 'base'
        self.archive = self.root / 'archive'
        self.base.mkdir()
        self.archive.mkdir()
        (self.base / 'PG_VERSION').write_text('18\n')
        (self.base / 'backup_label').write_text('START WAL LOCATION: ' + START + ' (file 000000010000000000000010)\nSTART TIMELINE: 1\n')
        (self.base / 'backup_manifest').write_text(json.dumps({
            'PostgreSQL-Backup-Manifest-Version': 2,
            'System-Identifier': 123456789,
            'Files': [],
            'WAL-Ranges': [{'Timeline': 1, 'Start-LSN': START, 'End-LSN': BACKUP_END}],
        }))

    def put_wal(self, number):
        name = f'00000001000000000000{number:04X}'
        (self.archive / name).write_bytes(bytes([number]) * SEGMENT_BYTES)
        return name

    def build(self, end=ARCHIVE_END):
        return build_manifest(self.base, self.archive, end, metadata(), segment_bytes=SEGMENT_BYTES)

    def test_base_backup_without_archived_wal_is_refused(self):
        with self.assertRaises(BackupError):
            self.build()

    def test_gap_inside_archive_interval_is_refused(self):
        self.put_wal(16)
        self.put_wal(18)
        with self.assertRaises(BackupError):
            self.build()

    def test_interval_not_beyond_backup_end_is_refused(self):
        self.put_wal(16)
        with self.assertRaises(BackupError):
            self.build(BACKUP_END)

    def test_complete_set_binds_metadata_and_exact_wal_digests(self):
        names = [self.put_wal(n) for n in (16, 17, 18)]
        result = self.build()
        self.assertTrue(result.get('usable'))
        self.assertEqual(result['postgres_system_identifier'], 123456789)
        self.assertEqual(result['backup_start_lsn'], START)
        self.assertEqual(result['backup_end_lsn'], BACKUP_END)
        self.assertEqual(result['archive_end_lsn'], ARCHIVE_END)
        self.assertEqual(result['metadata'], metadata())
        self.assertEqual([entry['name'] for entry in result['wal_segments']], names)
        self.assertEqual(result['wal_segments'][1]['sha256'], hashlib.sha256((self.archive / names[1]).read_bytes()).hexdigest())
        self.assertEqual(result['base_files']['backup_label']['sha256'], hashlib.sha256((self.base / 'backup_label').read_bytes()).hexdigest())

    def test_complete_set_is_sealed_with_synthetic_age_key(self):
        for number in (16, 17, 18):
            self.put_wal(number)
        identity = self.root / 'synthetic-age-key'
        subprocess.run(['age-keygen', '-o', str(identity)], capture_output=True, check=True)
        recipient = next(line.split(': ', 1)[1] for line in identity.read_text().splitlines()
                         if line.startswith('# public key: '))
        target = self.root / 'backup.tar.age'
        manifest = seal_encrypted_bundle(self.base, self.archive, ARCHIVE_END, metadata(),
                                         segment_bytes=SEGMENT_BYTES, recipient=recipient,
                                         destination=target)
        self.assertTrue(target.is_file())
        self.assertEqual(manifest['metadata'], metadata())
        plaintext = subprocess.run(['age', '-d', '-i', str(identity)],
                                   input=target.read_bytes(), capture_output=True,
                                   check=True).stdout
        with tarfile.open(fileobj=io.BytesIO(plaintext), mode='r:') as archive:
            names = archive.getnames()
            self.assertIn('manifest.json', names)
            self.assertIn('base/backup_manifest', names)
            self.assertIn('wal/000000010000000000000011', names)
            inner = json.load(archive.extractfile('manifest.json'))
            self.assertEqual(inner, manifest)


if __name__ == '__main__':
    unittest.main()
