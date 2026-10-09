"""Valid input probes keep mutation failures causal instead of crashing DDL."""
import json
from pathlib import Path
import shutil
import tempfile
import unittest

import psycopg
import test_schema
from cortex_core.migrations import apply_migrations, MigrationError


class SemanticProbes(unittest.TestCase):
    setUp = test_schema.SchemaTests.setUp
    reset = test_schema.SchemaTests.reset

    def test_valid_sql_tamper_refused_without_side_effects(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'schema'
            shutil.copytree(test_schema.NEXT / 'schema', target)
            core = target / 'core/001-core.sql'
            core.write_bytes(core.read_bytes() + b'\n-- c03_checksum_probe\n')
            self.reset()
            with self.assertRaises(MigrationError):
                apply_migrations(self.db, target)
            self.assertIsNone(self.db.execute("SELECT to_regnamespace('core')").fetchone()[0])

    def test_unknown_event_version_refused(self):
        with self.assertRaises(psycopg.errors.CheckViolation):
            self.db.execute('''INSERT INTO coordination.outbox
                (event_id,installation_id,tenant_id,project_id,aggregate_id,aggregate_kind,
                 aggregate_revision,operation,tombstone,schema_version,payload_ref,payload_sha256)
                VALUES (%s,%s,%s,%s,%s,'memory',1,'upsert',false,2,%s,%s)''',
                (test_schema.uid(30),test_schema.uid(1),*self.scope,test_schema.uid(6),test_schema.uid(5),self.digest))
