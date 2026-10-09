"""Fresh-verifier regression cases, committed and executed before their fixes."""
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest

import psycopg
import test_schema
from cortex_core.migrations import apply_migrations, MigrationError


class IntegrityTests(unittest.TestCase):
    setUp = test_schema.SchemaTests.setUp
    reset = test_schema.SchemaTests.reset
    event = test_schema.SchemaTests.event

    def test_outbox_payload_matches_that_exact_revision(self):
        other = b'{"text":"different canonical revision bytes"}'
        digest = hashlib.sha256(other).hexdigest()
        self.db.execute("INSERT INTO core.payloads (tenant_id,project_id,id,body,sha256) VALUES (%s,%s,%s,%s,%s)", (*self.scope,test_schema.uid(9),other,digest))
        with self.assertRaises(psycopg.errors.ForeignKeyViolation):
            self.db.execute("""INSERT INTO coordination.outbox
                (event_id,installation_id,tenant_id,project_id,aggregate_id,aggregate_kind,
                 aggregate_revision,operation,tombstone,schema_version,payload_ref,payload_sha256)
                VALUES (%s,%s,%s,%s,%s,'memory',1,'upsert',false,1,%s,%s)""",
                (test_schema.uid(7),test_schema.uid(1),*self.scope,test_schema.uid(6),test_schema.uid(9),digest))

    def test_publication_installation_matches_event_installation(self):
        self.event()
        self.db.execute("INSERT INTO core.installations (id) VALUES (%s)",(test_schema.uid(20),))
        with self.assertRaises(psycopg.errors.ForeignKeyViolation):
            self.db.execute("INSERT INTO coordination.published_events (installation_id,cursor,tenant_id,project_id,event_id) VALUES (%s,1,%s,%s,%s)",(test_schema.uid(20),*self.scope,test_schema.uid(7)))

    def test_noninteger_manifest_version_refused(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'schema';shutil.copytree(test_schema.NEXT/'schema',path)
            manifest=json.loads((path/'manifest.json').read_text())
            for version in (True, 1.0):
                manifest['version']=version
                (path/'manifest.json').write_text(json.dumps(manifest))
                with self.subTest(version=version), self.assertRaises(MigrationError):
                    apply_migrations(self.db,path)

    def test_all_applied_hashes_checked_before_any_pending_sql(self):
        self.db.execute("CREATE SEQUENCE public.c03_preflight_probe")
        self.addCleanup(self.db.execute,"DROP SEQUENCE IF EXISTS public.c03_preflight_probe")
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'schema';shutil.copytree(test_schema.NEXT/'schema',path)
            sql=b"SELECT nextval('public.c03_preflight_probe');\n"
            (path/'probe.sql').write_bytes(sql)
            manifest=json.loads((path/'manifest.json').read_text())
            manifest['migrations'].insert(0,{'id':'probe-0001','file':'probe.sql','sha256':hashlib.sha256(sql).hexdigest()})
            (path/'manifest.json').write_text(json.dumps(manifest))
            self.db.execute("UPDATE core.schema_migrations SET sha256=%s WHERE migration_id='core-0001'",('0'*64,))
            with self.assertRaises(MigrationError):
                apply_migrations(self.db,path)
            self.assertEqual(self.db.execute("SELECT is_called FROM public.c03_preflight_probe").fetchone()[0],False)
