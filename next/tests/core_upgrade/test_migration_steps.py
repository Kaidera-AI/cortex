"""The explicit migration port must stop at a named manifest prefix."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest

import psycopg
from cortex_core.migrations import apply_migrations, MigrationError


class MigrationSteps(unittest.TestCase):
    def setUp(self):
        self.db = psycopg.connect(os.environ['TEST_DATABASE_URL'], autocommit=True)
        self.addCleanup(self.db.close)
        for schema in ('retrieval', 'coordination', 'auth', 'core'):
            self.db.execute(f'DROP SCHEMA IF EXISTS {schema} CASCADE')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        entries = []
        for n in (1, 2):
            name = f'expand-{n}.sql'
            data = f'CREATE TABLE IF NOT EXISTS core.upgrade_probe_{n}(id integer PRIMARY KEY);\n'.encode()
            (self.root/name).write_bytes(data)
            entries.append({'id': f'expand-{n}', 'file': name,
                            'sha256': hashlib.sha256(data).hexdigest()})
        (self.root/'manifest.json').write_text(json.dumps({'version': 1, 'migrations': entries}))

    def test_named_prefix_and_reentry(self):
        self.assertEqual(apply_migrations(self.db, self.root, through='expand-1'), ['expand-1'])
        self.assertEqual(self.db.execute('SELECT migration_id FROM core.schema_migrations').fetchall(), [('expand-1',)])
        self.assertEqual(apply_migrations(self.db, self.root, through='expand-1'), [])
        self.assertEqual(apply_migrations(self.db, self.root, through='expand-2'), ['expand-2'])
        self.assertEqual(apply_migrations(self.db, self.root, through='expand-2'), [])

    def test_unknown_target_and_nonprefix_ledger_refuse_before_sql(self):
        with self.assertRaises(MigrationError):
            apply_migrations(self.db, self.root, through='wrong')
        self.assertIsNone(self.db.execute("SELECT to_regclass('core.upgrade_probe_1')").fetchone()[0])
        apply_migrations(self.db, self.root, through='expand-2')
        self.db.execute("DELETE FROM core.schema_migrations WHERE migration_id='expand-1'")
        with self.assertRaises(MigrationError):
            apply_migrations(self.db, self.root, through='expand-2')


if __name__ == '__main__':
    unittest.main()
