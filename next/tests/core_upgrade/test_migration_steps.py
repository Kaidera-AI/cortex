"""The explicit migration port must stop at a named manifest prefix."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest

import psycopg
from psycopg.pq import TransactionStatus
from cortex_core.migrations import apply_migrations, MigrationError


class ObservedConnection(psycopg.Connection):
    """Genuine driver connection; fail the witness on caller-BEGIN DDL attempts.

    Catalog poststate alone could miss DDL rolled back inside a nested context.
    This assertion observes the attempt before libpq executes the managed SQL.
    It raises AssertionError, never the MigrationError expected by the witness.
    """
    caller_begin = False

    def execute(self, query, *args, **kwargs):
        if query == 'BEGIN': self.caller_begin = True
        if query == 'ROLLBACK': self.caller_begin = False
        if self.caller_begin and isinstance(query, str) and query.lstrip().upper().startswith(
                ('CREATE ', 'ALTER ', 'DROP ', 'INSERT INTO CORE.SCHEMA_MIGRATIONS')):
            raise AssertionError('managed SQL attempted inside caller BEGIN: '+query)
        return super().execute(query, *args, **kwargs)


class MigrationSteps(unittest.TestCase):
    def setUp(self):
        self.db = ObservedConnection.connect(os.environ['TEST_DATABASE_URL'], autocommit=True)
        self.addCleanup(self.db.close)
        for schema in ('retrieval', 'coordination', 'auth', 'core'):
            self.db.execute(f'DROP SCHEMA IF EXISTS {schema} CASCADE')
        self.temp = tempfile.TemporaryDirectory(dir=os.environ['CORTEX_U13_CASE_ROOT'])
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

    def test_caller_begin_refuses_before_any_managed_ddl(self):
        # Observe the caller's own transaction: a second session cannot see its
        # uncommitted DDL and would be an invalid refusal-before-DDL witness.
        catalog = ("SELECT n.nspname,c.relname,c.relkind FROM pg_class c "
                   "JOIN pg_namespace n ON n.oid=c.relnamespace "
                   "WHERE n.nspname IN ('core','auth','coordination','retrieval') "
                   "ORDER BY n.nspname,c.relname,c.relkind")
        self.db.execute('BEGIN')
        try:
            self.assertIs(self.db.info.transaction_status, TransactionStatus.INTRANS)
            before = self.db.execute(catalog).fetchall()
            self.assertIsNone(self.db.execute("SELECT to_regclass('core.schema_migrations')").fetchone()[0])
            with self.assertRaises(MigrationError):
                apply_migrations(self.db, self.root, through='expand-1')
            self.assertIs(self.db.info.transaction_status, TransactionStatus.INTRANS)
            self.assertEqual(self.db.execute(catalog).fetchall(), before)
            self.assertIsNone(self.db.execute("SELECT to_regclass('core.schema_migrations')").fetchone()[0])
            self.assertIsNone(self.db.execute("SELECT to_regclass('core.upgrade_probe_1')").fetchone()[0])
        finally:
            self.db.execute('ROLLBACK')  # The caller alone owns this transaction.
        self.assertIs(self.db.info.transaction_status, TransactionStatus.IDLE)

    def test_idle_top_level_commit_visible_before_return(self):
        self.assertIs(self.db.info.transaction_status, TransactionStatus.IDLE)
        self.assertEqual(apply_migrations(self.db, self.root, through='expand-1'), ['expand-1'])
        self.assertIs(self.db.info.transaction_status, TransactionStatus.IDLE)
        digest = hashlib.sha256((self.root/'expand-1.sql').read_bytes()).hexdigest()
        with psycopg.connect(os.environ['TEST_DATABASE_URL'], autocommit=True) as observer:
            self.assertEqual(observer.execute("SELECT to_regclass('core.upgrade_probe_1')::text").fetchone()[0],
                             'core.upgrade_probe_1')
            self.assertEqual(observer.execute('SELECT migration_id,sha256 FROM core.schema_migrations').fetchall(),
                             [('expand-1', digest)])
        self.assertEqual(apply_migrations(self.db, self.root, through='expand-1'), [])
        self.assertIs(self.db.info.transaction_status, TransactionStatus.IDLE)


if __name__ == '__main__':
    unittest.main()
