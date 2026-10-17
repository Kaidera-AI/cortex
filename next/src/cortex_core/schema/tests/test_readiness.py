"""Schema adapter units; no fixture is claimed as installed PostgreSQL proof."""
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import Mock,patch

from cortex_core import migrations
try:
    from cortex_core.schema.migrate import apply_forward_migrations
    from cortex_core.schema.readiness import check_schema_ready
except ModuleNotFoundError:
    apply_forward_migrations=check_schema_ready=None


class ReadinessTests(unittest.TestCase):
    def ledger(self):
        data=json.loads((migrations.SCHEMA/'manifest.json').read_text())
        return [(e['id'],e['sha256']) for e in data['migrations']]

    def test_full_prefix_ready(self):
        self.assertIsNotNone(check_schema_ready)
        db=Mock();db.execute.return_value.fetchall.return_value=self.ledger()
        self.assertTrue(check_schema_ready(db))
        sql=db.execute.call_args.args[0]
        self.assertTrue(sql.lstrip().startswith('SELECT'))

    def test_wrong_applied_order_identity_and_digest_refuse(self):
        self.assertIsNotNone(check_schema_ready)
        original=self.ledger()
        for rows in [original[:-1],original+[('unknown','0'*64)],
                     [('wrong',original[0][1])]+original[1:],
                     [(original[0][0],'0'*64)]+original[1:],original+[original[0]]]:
            db=Mock();db.execute.return_value.fetchall.return_value=rows
            with self.subTest(rows=rows),self.assertRaises(migrations.MigrationError):check_schema_ready(db)

    def test_prefix_byte_change_before_sql(self):
        self.assertIsNotNone(check_schema_ready)
        with tempfile.TemporaryDirectory() as tmp:
            directory=Path(tmp)/'schema';shutil.copytree(migrations.SCHEMA,directory)
            manifest=json.loads((directory/'manifest.json').read_text())
            file=directory/manifest['migrations'][0]['file'];file.write_bytes(file.read_bytes()+b'-- changed\n')
            db=Mock()
            with self.assertRaises(migrations.MigrationError):check_schema_ready(db,directory)
            db.execute.assert_not_called()

    def test_manifest_bad_version_before_sql(self):
        self.assertIsNotNone(check_schema_ready)
        with tempfile.TemporaryDirectory() as tmp:
            directory=Path(tmp)/'schema';shutil.copytree(migrations.SCHEMA,directory)
            manifest=json.loads((directory/'manifest.json').read_text());manifest['version']=True
            (directory/'manifest.json').write_text(json.dumps(manifest));db=Mock()
            with self.assertRaises(migrations.MigrationError):check_schema_ready(db,directory)
            db.execute.assert_not_called()

    def test_database_unavailable_does_not_claim_ready(self):
        self.assertIsNotNone(check_schema_ready)
        db=Mock();db.execute.side_effect=RuntimeError('private-SENTINEL')
        with self.assertRaises(migrations.MigrationError) as caught:check_schema_ready(db)
        self.assertNotIn('SENTINEL',str(caught.exception))

    def test_existing_runner_is_only_migration_engine(self):
        self.assertIsNotNone(apply_forward_migrations)
        db=object()
        with patch.object(migrations,'apply_migrations',return_value=['core-0001']) as run:
            self.assertEqual(apply_forward_migrations(db),['core-0001']);run.assert_called_once_with(db,migrations.SCHEMA)

    def test_all_manifest_members_match_their_bytes(self):
        self.assertIsNotNone(check_schema_ready)
        self.assertEqual(len(self.ledger()),21)
        for entry in json.loads((migrations.SCHEMA/'manifest.json').read_text())['migrations']:
            self.assertEqual(hashlib.sha256((migrations.SCHEMA/entry['file']).read_bytes()).hexdigest(),entry['sha256'])


if __name__=='__main__':unittest.main()
