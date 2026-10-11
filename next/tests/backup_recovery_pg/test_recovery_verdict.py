"""Real disposable-PostgreSQL controls for the O01 recovery verdict boundary."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest

from cortex_core.backup import BackupError
from cortex_core.backup_validation import (read_restored_snapshot,
                                           seal_complete_bundle,
                                           verify_complete_bundle)


class PodPsqlConnection:
    """Execute the product SQL on the task-owned server through podman exec."""

    def __init__(self, container, port, *, closed=False):
        self.container, self.port, self.closed = container, port, closed
        self.row = None

    def execute(self, sql):
        if self.closed:
            raise OSError('connection closed')
        result = subprocess.run(
            ['podman', 'exec', self.container, 'psql', '-X', '-A', '-t',
             '-v', 'ON_ERROR_STOP=1', '-U', 'postgres', '-p', str(self.port), '-c', sql],
            text=True, capture_output=True, timeout=30,
        )
        if result.returncode:
            raise OSError('owned PostgreSQL query failed: ' + result.stderr[-200:])
        value = result.stdout.strip()
        self.row = (value if value else None,)
        return self

    def fetchone(self):
        return self.row


class RecoveryVerdictTests(unittest.TestCase):
    def test_real_paused_recovery_controls_verdict(self):
        container = os.environ['O01_RECOVERY_CONTAINER']
        base = Path(os.environ['O01_BASE_DIR'])
        archive = Path(os.environ['O01_ARCHIVE_DIR'])
        primary = PodPsqlConnection(container, 5432)
        recovery = PodPsqlConnection(container, 5433)

        with self.assertRaises(BackupError):
            read_restored_snapshot(primary)
        print('CORTEX_RECOVERY_CONTROL=primary_refused', flush=True)

        snapshot = read_restored_snapshot(recovery)
        self.assertIsInstance(snapshot['replay_lsn'], str)
        self.assertTrue(snapshot['schema_ledger'])
        with tempfile.TemporaryDirectory(prefix='cox-o01-recovery-') as root_name:
            root = Path(root_name)
            blobs = root/'blobs'
            blobs.mkdir()
            identity = root/'age-key'
            keygen = subprocess.run(['age-keygen', '-o', str(identity)],
                                    text=True, capture_output=True, check=True)
            self.assertEqual(keygen.returncode, 0)
            recipient = next(line.split(': ', 1)[1]
                             for line in identity.read_text().splitlines()
                             if line.startswith('# public key: '))
            bundle = root/'complete.tar.age'
            tools = {'age_binary': '/opt/homebrew/bin/age',
                     'pg_verifybackup_binary': '/opt/homebrew/bin/pg_verifybackup',
                     'pg_waldump_binary': '/opt/homebrew/bin/pg_waldump'}
            manifest = seal_complete_bundle(
                base, archive, snapshot['replay_lsn'], snapshot,
                segment_bytes=1 << 20, blob_root=blobs, recipient=recipient,
                destination=bundle, **tools,
            )
            digest = hashlib.sha256(json.dumps(
                manifest, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
            self.assertTrue(bundle.is_file())

            with self.assertRaises(BackupError):
                verify_complete_bundle(bundle, identity, digest, snapshot, **tools)
            with self.assertRaises(BackupError):
                verify_complete_bundle(bundle, identity, digest, primary, **tools)
            print('CORTEX_RECOVERY_CONTROL=handbuilt_and_primary_refused', flush=True)

            recovery.execute('SELECT pg_wal_replay_resume()')
            for _ in range(40):
                if recovery.execute('SELECT pg_is_wal_replay_paused()').fetchone() == ('f',):
                    break
                time.sleep(0.1)
            else:
                self.fail('owned recovery did not resume')
            with self.assertRaises(BackupError):
                verify_complete_bundle(bundle, identity, digest, recovery, **tools)
            print('CORTEX_RECOVERY_CONTROL=unpaused_refused', flush=True)

            recovery.execute('SELECT pg_wal_replay_pause()')
            for _ in range(40):
                if recovery.execute('SELECT pg_is_wal_replay_paused()').fetchone() == ('t',):
                    break
                time.sleep(0.1)
            else:
                self.fail('owned recovery did not pause')
            self.assertEqual(read_restored_snapshot(recovery), snapshot)
            result = verify_complete_bundle(bundle, identity, digest, recovery, **tools)
            self.assertIs(result['recoverable'], True)
            self.assertEqual(result['manifest_sha256'], digest)
            print('CORTEX_RECOVERY_CONTROL=matching_paused_recoverable', flush=True)

            changed = json.loads(json.dumps(snapshot))
            changed['model_identities'].append({
                'id': '00000000-0000-4000-8000-000000000020', 'provider': 'fixture',
                'model': 'different', 'version': '1', 'dimensions': 3,
            })
            mismatched_bundle = root/'mismatched.tar.age'
            changed_manifest = seal_complete_bundle(
                base, archive, snapshot['replay_lsn'], changed,
                segment_bytes=1 << 20, blob_root=blobs, recipient=recipient,
                destination=mismatched_bundle, **tools,
            )
            changed_digest = hashlib.sha256(json.dumps(
                changed_manifest, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
            with self.assertRaises(BackupError):
                verify_complete_bundle(mismatched_bundle, identity, changed_digest,
                                       recovery, **tools)
            with self.assertRaises(BackupError):
                verify_complete_bundle(bundle, identity, digest,
                                       PodPsqlConnection(container, 5433, closed=True), **tools)
            print('CORTEX_RECOVERY_CONTROL=mismatch_and_closed_refused', flush=True)


if __name__ == '__main__':
    unittest.main()
