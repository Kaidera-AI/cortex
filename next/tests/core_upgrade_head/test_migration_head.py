"""RED-first current-manifest staging and PostgreSQL re-entry contract."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest

import psycopg

from cortex_core.migrations import MigrationError, apply_migrations


SCHEMA = Path(__file__).resolve().parents[2] / 'schema'
STEPS = ('auth-0004', 'auth-0005', 'auth-0006', 'ingest-head-0006')


def uid(n):
    return f'00000000-0000-4000-8000-{n:012d}'


class CurrentManifestStaging(unittest.TestCase):
    def setUp(self):
        self.db = psycopg.connect(os.environ['TEST_DATABASE_URL'], autocommit=True)
        self.addCleanup(lambda: self.db.close())
        for schema in ('retrieval', 'coordination', 'auth', 'core'):
            self.db.execute(f'DROP SCHEMA IF EXISTS {schema} CASCADE')
        self.temp = tempfile.TemporaryDirectory(prefix='cox-migration-head-')
        self.addCleanup(self.temp.cleanup)
        prefix_dir = Path(self.temp.name) / 'prefix'
        shutil.copytree(SCHEMA, prefix_dir)
        self.manifest = json.loads((SCHEMA / 'manifest.json').read_text())
        self.entries = self.manifest['migrations']
        (prefix_dir / 'manifest.json').write_text(json.dumps({
            'version': 1, 'migrations': self.entries[:15],
        }))
        apply_migrations(self.db, prefix_dir)

    def staged(self, target):
        try:
            return apply_migrations(self.db, through=target)
        except TypeError as error:
            self.fail(f'current migration port has no through target: {error}')

    def ledger(self):
        return self.db.execute(
            'SELECT migration_id,sha256 FROM core.schema_migrations ORDER BY ctid'
        ).fetchall()

    def expected_ledger(self, count):
        return [(row['id'], row['sha256']) for row in self.entries[:count]]

    def seed(self):
        scope = (uid(2), uid(3))
        body = b'{"text":"synthetic current-schema source"}'
        digest = hashlib.sha256(body).hexdigest()
        self.db.execute('INSERT INTO core.installations (id) VALUES (%s)', (uid(1),))
        self.db.execute('INSERT INTO core.tenants (id,installation_id) VALUES (%s,%s)',
                        (uid(2), uid(1)))
        self.db.execute('INSERT INTO core.projects (tenant_id,id,name) VALUES (%s,%s,%s)',
                        (uid(2), uid(3), 'default'))
        self.db.execute('INSERT INTO core.payloads '
                        '(tenant_id,project_id,id,body,sha256) VALUES (%s,%s,%s,%s,%s)',
                        (*scope, uid(5), body, digest))
        with self.db.transaction():
            self.db.execute('INSERT INTO core.records '
                            '(tenant_id,project_id,id,kind,current_revision,tombstone) '
                            "VALUES (%s,%s,%s,'memory',1,false)", (*scope, uid(6)))
            self.db.execute('INSERT INTO core.record_revisions '
                            '(tenant_id,project_id,record_id,revision,payload_ref,tombstone) '
                            'VALUES (%s,%s,%s,1,%s,false)', (*scope, uid(6), uid(5)))
        self.db.execute('INSERT INTO core.blob_manifests '
                        '(tenant_id,project_id,id,record_id,kind,object_key,sha256,'
                        "byte_length,mime_type) VALUES (%s,%s,%s,%s,'original',%s,%s,%s,'application/json')",
                        (*scope, uid(20), uid(6), 'objects/one', digest, len(body)))
        self.db.execute('INSERT INTO auth.principals (tenant_id,id,name) '
                        "VALUES (%s,%s,'reader')", (uid(2), uid(21)))
        self.db.execute('INSERT INTO auth.project_grants '
                        '(tenant_id,project_id,principal_id,permissions) '
                        "VALUES (%s,%s,%s,ARRAY['read'])", (*scope, uid(21)))
        self.db.execute('UPDATE auth.permission_generations SET generation=7 '
                        'WHERE tenant_id=%s AND project_id=%s', scope)
        self.db.execute('INSERT INTO coordination.idempotency '
                        '(tenant_id,project_id,principal_id,request_key,request_sha256,'
                        'outcome,receipt) VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb)',
                        (*scope, uid(21), 'migration-replay', digest, 'committed',
                         json.dumps({'event_id': uid(7), 'revision': 1})))
        self.db.execute('INSERT INTO coordination.outbox '
                        '(event_id,installation_id,tenant_id,project_id,aggregate_id,'
                        'aggregate_kind,aggregate_revision,operation,tombstone,'
                        'schema_version,payload_ref,payload_sha256) '
                        "VALUES (%s,%s,%s,%s,%s,'memory',1,'upsert',false,1,%s,%s)",
                        (uid(7), uid(1), *scope, uid(6), uid(5), digest))
        self.db.execute('INSERT INTO coordination.feed_state '
                        '(installation_id,last_published_cursor) VALUES (%s,9)', (uid(1),))
        self.db.execute('INSERT INTO coordination.published_events '
                        '(installation_id,cursor,tenant_id,project_id,event_id) '
                        'VALUES (%s,9,%s,%s,%s)', (uid(1), *scope, uid(7)))
        self.db.execute('INSERT INTO coordination.consumer_checkpoints '
                        '(installation_id,module_id,applied_cursor) '
                        "VALUES (%s,'search',9)", (uid(1),))
        self.db.execute('INSERT INTO retrieval.embedding_models '
                        '(tenant_id,project_id,id,provider,model,model_version,'
                        'dimensions,preprocessing_sha256) '
                        "VALUES (%s,%s,%s,'fake','embed','1',3,%s)",
                        (*scope, uid(22), digest))

    def snapshot(self):
        queries = {
            'payload': 'SELECT body,sha256 FROM core.payloads',
            'record': 'SELECT id,current_revision,tombstone FROM core.records',
            'revision': 'SELECT record_id,revision,payload_ref FROM core.record_revisions',
            'blob': 'SELECT id,object_key,sha256,byte_length FROM core.blob_manifests',
            'grant': 'SELECT principal_id,permissions FROM auth.project_grants',
            'generation': 'SELECT generation FROM auth.permission_generations',
            'receipt': 'SELECT principal_id,request_key,request_sha256,outcome,receipt '
                       'FROM coordination.idempotency',
            'outbox': 'SELECT event_id,payload_sha256,aggregate_revision FROM coordination.outbox',
            'published': 'SELECT cursor,event_id FROM coordination.published_events',
            'feed': 'SELECT last_published_cursor,retained_floor FROM coordination.feed_state',
            'consumer': 'SELECT module_id,applied_cursor,generation '
                        'FROM coordination.consumer_checkpoints',
            'model': 'SELECT provider,model,model_version,dimensions,preprocessing_sha256 '
                     'FROM retrieval.embedding_models',
        }
        return {name: self.db.execute(sql).fetchall() for name, sql in queries.items()}

    def test_current_four_steps_reconnect_reenter_and_preserve_seed(self):
        self.assertEqual(len(self.entries), 19)
        self.assertEqual(tuple(row['id'] for row in self.entries[15:]), STEPS)
        self.assertEqual(self.ledger(), self.expected_ledger(15))
        self.seed()
        before = self.snapshot()
        for offset, target in enumerate(STEPS, start=16):
            self.assertEqual(self.staged(target), [target])
            self.db.close()  # Interrupted after the committed boundary.
            self.db = psycopg.connect(os.environ['TEST_DATABASE_URL'], autocommit=True)
            try:
                replay = self.staged(target)
            except Exception as error:
                self.fail(f'committed re-entry raised {type(error).__name__}: {error}')
            self.assertEqual(replay, [], 'one ledger row and no duplicate execution')
            if offset > 16:
                self.assertEqual(self.staged(STEPS[0]), [],
                                 'a target behind the applied prefix is an idempotent no-op')
            self.assertEqual(self.ledger(), self.expected_ledger(offset))
            self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.staged(STEPS[-1]), [])
        tables = self.db.execute("SELECT n.nspname,c.relname FROM pg_class c "
                                 "JOIN pg_namespace n ON n.oid=c.relnamespace "
                                 "WHERE n.nspname IN ('core','auth','coordination','retrieval') "
                                 "AND c.relkind='r' AND c.relname<>'schema_migrations'").fetchall()
        self.assertEqual(len(tables), 42)
        self.assertIn(('auth', 'first_identity_receipts'), tables)

    def test_unknown_target_refuses_before_any_later_sql(self):
        self.assertEqual(self.ledger(), self.expected_ledger(15))
        with self.assertRaises(MigrationError):
            self.staged('unlisted-future-0001')
        self.assertEqual(self.ledger(), self.expected_ledger(15))
        self.assertIsNone(self.db.execute(
            "SELECT to_regclass('auth.first_identity_receipts')").fetchone()[0])

    def test_nonprefix_drift_and_future_ledger_refuse_before_sql(self):
        for mutation in ('missing_prefix', 'checksum_drift', 'unexpected_future'):
            with self.subTest(mutation=mutation):
                if mutation == 'missing_prefix':
                    self.db.execute("DELETE FROM core.schema_migrations "
                                    "WHERE migration_id='core-0001'")
                elif mutation == 'checksum_drift':
                    self.db.execute("UPDATE core.schema_migrations SET sha256=%s "
                                    "WHERE migration_id='core-0001'", ('0' * 64,))
                else:
                    self.db.execute('INSERT INTO core.schema_migrations '
                                    '(migration_id,sha256) VALUES (%s,%s)',
                                    ('unlisted-future-0001', '0' * 64))
                try:
                    try:
                        self.staged(STEPS[0])
                    except MigrationError:
                        pass
                    except Exception as error:
                        self.fail(f'invalid ledger reached SQL: '
                                  f'{type(error).__name__}: {error}')
                    else:
                        self.fail('invalid ledger was accepted')
                    self.assertIsNone(self.db.execute(
                        "SELECT to_regclass('auth.first_identity_receipts')").fetchone()[0])
                finally:
                    self.db.close()
                    self.db = psycopg.connect(os.environ['TEST_DATABASE_URL'], autocommit=True)
                    for schema in ('retrieval', 'coordination', 'auth', 'core'):
                        self.db.execute(f'DROP SCHEMA IF EXISTS {schema} CASCADE')
                    prefix_dir = Path(self.temp.name) / 'prefix'
                    apply_migrations(self.db, prefix_dir)


if __name__ == '__main__':
    unittest.main()
