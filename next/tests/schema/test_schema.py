"""Real Postgres C03 invariants; committed RED before any implementation."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest

import psycopg
from cortex_core.migrations import apply_migrations, MigrationError

NEXT = Path(__file__).resolve().parents[2]


def uid(n):
    return f"00000000-0000-4000-8000-{n:012d}"


class SchemaTests(unittest.TestCase):
    def setUp(self):
        self.db = psycopg.connect(os.environ["TEST_DATABASE_URL"], autocommit=True)
        self.addCleanup(self.db.close)
        self.reset()
        apply_migrations(self.db)
        self.db.execute("INSERT INTO core.installations (id) VALUES (%s)", (uid(1),))
        self.db.execute("INSERT INTO core.tenants (id, installation_id) VALUES (%s,%s)", (uid(2), uid(1)))
        for project in (3, 4):
            self.db.execute("INSERT INTO core.projects (tenant_id,id,name) VALUES (%s,%s,%s)", (uid(2), uid(project), str(project)))
        self.scope = (uid(2), uid(3))
        self.body = b'{"text":"synthetic canonical source"}'
        self.digest = hashlib.sha256(self.body).hexdigest()
        self.db.execute("INSERT INTO core.payloads (tenant_id,project_id,id,body,sha256) VALUES (%s,%s,%s,%s,%s)", (*self.scope, uid(5), self.body, self.digest))
        with self.db.transaction():
            self.db.execute("INSERT INTO core.records (tenant_id,project_id,id,kind,current_revision,tombstone) VALUES (%s,%s,%s,'memory',1,false)", (*self.scope, uid(6)))
            self.db.execute("INSERT INTO core.record_revisions (tenant_id,project_id,record_id,revision,payload_ref,tombstone) VALUES (%s,%s,%s,1,%s,false)", (*self.scope, uid(6), uid(5)))

    def reset(self):
        for schema in ("retrieval", "coordination", "auth", "core"):
            self.db.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")

    def event(self, event_id=7, operation="upsert", tombstone=False):
        self.db.execute("""INSERT INTO coordination.outbox
            (event_id,installation_id,tenant_id,project_id,aggregate_id,aggregate_kind,
             aggregate_revision,operation,tombstone,schema_version,payload_ref,payload_sha256)
            VALUES (%s,%s,%s,%s,%s,'memory',1,%s,%s,1,%s,%s)""",
            (uid(event_id), uid(1), *self.scope, uid(6), operation, tombstone, uid(5), self.digest))

    def test_schemas_and_future_fact_slots_exist(self):
        required = {
            "core": {"installations", "tenants", "projects", "records", "record_revisions", "record_aliases", "payloads", "blob_manifests", "extraction_facts", "analytics_facts", "schema_migrations"},
            "auth": {"principals", "credentials", "project_grants", "permission_generations"},
            "coordination": {"jobs", "job_attempts", "job_results", "leases", "idempotency", "outbox", "published_events", "quarantine", "feed_state", "consumer_checkpoints", "snapshot_floors"},
            "retrieval": {"embedding_models", "embeddings", "generations"},
        }
        rows=self.db.execute("SELECT table_schema, table_name FROM information_schema.tables WHERE table_type='BASE TABLE'").fetchall()
        for schema, names in required.items():
            self.assertTrue(names <= {name for owner, name in rows if owner == schema})

    def test_stable_ids_count_and_payload_digest(self):
        row = self.db.execute("SELECT id::text,current_revision,tombstone FROM core.records").fetchone()
        self.assertEqual(row, (uid(6), 1, False))
        body, digest = self.db.execute("SELECT body,sha256 FROM core.payloads").fetchone()
        self.assertEqual(bytes(body), self.body)
        self.assertEqual(hashlib.sha256(bytes(body)).hexdigest(), digest)
        self.assertEqual(self.db.execute("SELECT count(*) FROM core.record_revisions").fetchone()[0], 1)

    def test_head_without_history_fails_at_commit(self):
        with self.assertRaises(psycopg.errors.ForeignKeyViolation):
            with self.db.transaction():
                self.db.execute("INSERT INTO core.records (tenant_id,project_id,id,kind,current_revision,tombstone) VALUES (%s,%s,%s,'memory',1,false)", (*self.scope, uid(8)))
        self.assertEqual(self.db.execute("SELECT count(*) FROM core.records").fetchone()[0], 1)

    def test_cross_project_payload_refused(self):
        with self.assertRaises(psycopg.errors.ForeignKeyViolation):
            self.db.execute("INSERT INTO core.record_revisions (tenant_id,project_id,record_id,revision,payload_ref,tombstone) VALUES (%s,%s,%s,2,%s,false)", (uid(2), uid(4), uid(6), uid(5)))

    def test_revision_tombstone_and_history_immutable(self):
        with self.assertRaises(psycopg.errors.CheckViolation):
            self.db.execute("UPDATE core.records SET current_revision=0 WHERE id=%s", (uid(6),))
        with self.assertRaises(psycopg.errors.ObjectNotInPrerequisiteState):
            self.db.execute("UPDATE core.record_revisions SET tombstone=true")
        with self.assertRaises(psycopg.errors.ForeignKeyViolation):
            with self.db.transaction():
                self.db.execute("UPDATE core.records SET tombstone=true WHERE id=%s", (uid(6),))

    def test_payload_digest_and_immutability(self):
        with self.assertRaises(psycopg.errors.CheckViolation):
            self.db.execute("INSERT INTO core.payloads (tenant_id,project_id,id,body,sha256) VALUES (%s,%s,%s,%s,%s)", (*self.scope, uid(8), self.body, '0'*64))
        with self.assertRaises(psycopg.errors.ObjectNotInPrerequisiteState):
            self.db.execute("UPDATE core.payloads SET body='changed'::bytea")

    def test_legacy_alias_uniqueness_preserves_source_identity(self):
        args=(*self.scope, "public.messages", "42", uid(6))
        self.db.execute("INSERT INTO core.record_aliases VALUES (%s,%s,%s,%s,%s)", args)
        with self.assertRaises(psycopg.errors.UniqueViolation):
            self.db.execute("INSERT INTO core.record_aliases VALUES (%s,%s,%s,%s,%s)", args)
        self.db.execute("INSERT INTO core.record_aliases VALUES (%s,%s,'cortex.messages','42',%s)", (*self.scope, uid(6)))
        self.assertEqual(self.db.execute("SELECT count(*) FROM core.record_aliases").fetchone()[0], 2)

    def test_original_and_generated_blob_ownership(self):
        for n,kind in [(8,"original"),(9,"generated")]:
            self.db.execute("INSERT INTO core.blob_manifests (tenant_id,project_id,id,record_id,kind,object_key,sha256,byte_length,mime_type) VALUES (%s,%s,%s,%s,%s,%s,%s,10,'text/plain')", (*self.scope,uid(n),uid(6),kind,'objects/'+uid(n),self.digest))
        self.assertEqual(self.db.execute("SELECT count(*) FROM core.blob_manifests").fetchone()[0], 2)
        with self.assertRaises(psycopg.errors.ForeignKeyViolation):
            self.db.execute("INSERT INTO core.blob_manifests (tenant_id,project_id,id,record_id,kind,object_key,sha256,byte_length,mime_type) VALUES (%s,%s,%s,%s,'original','objects/new',%s,10,'text/plain')", (uid(2),uid(4),uid(10),uid(6),self.digest))

    def test_fact_history_is_canonical_and_immutable(self):
        self.db.execute("INSERT INTO core.extraction_facts (tenant_id,project_id,id,record_id,source_revision,extractor_identity,payload_ref) VALUES (%s,%s,%s,%s,1,'parser@1',%s)", (*self.scope,uid(8),uid(6),uid(5)))
        self.db.execute("INSERT INTO core.analytics_facts (tenant_id,project_id,id,kind,payload_ref) VALUES (%s,%s,%s,'job.completed',%s)", (*self.scope,uid(9),uid(5)))
        for table in ("extraction_facts","analytics_facts"):
            with self.assertRaises(psycopg.errors.ObjectNotInPrerequisiteState):
                self.db.execute(f"UPDATE core.{table} SET payload_ref=payload_ref")

    def test_principal_credential_and_project_grant_scope(self):
        self.db.execute("INSERT INTO auth.principals (tenant_id,id,name) VALUES (%s,%s,'worker')", (uid(2),uid(8)))
        self.db.execute("INSERT INTO auth.credentials (tenant_id,id,principal_id,key_digest) VALUES (%s,%s,%s,%s)", (uid(2),uid(9),uid(8),self.digest))
        with self.assertRaises(psycopg.errors.CheckViolation):
            self.db.execute("INSERT INTO auth.project_grants (tenant_id,project_id,principal_id,permissions) VALUES (%s,%s,%s,ARRAY['root-everywhere'])", (*self.scope,uid(8)))
        with self.assertRaises(psycopg.errors.ForeignKeyViolation):
            self.db.execute("INSERT INTO auth.project_grants (tenant_id,project_id,principal_id,permissions) VALUES (%s,%s,%s,ARRAY['read'])", (*self.scope,uid(99)))

    def test_jobs_attempts_results_and_idempotency(self):
        self.db.execute("INSERT INTO coordination.jobs (tenant_id,project_id,id,kind,payload_ref,idempotency_key) VALUES (%s,%s,%s,'extract',%s,'same-request')", (*self.scope,uid(8),uid(5)))
        with self.assertRaises(psycopg.errors.UniqueViolation):
            self.db.execute("INSERT INTO coordination.jobs (tenant_id,project_id,id,kind,payload_ref,idempotency_key) VALUES (%s,%s,%s,'extract',%s,'same-request')", (*self.scope,uid(9),uid(5)))
        self.db.execute("INSERT INTO coordination.job_attempts (tenant_id,project_id,id,job_id,attempt_number,fence,worker_id) VALUES (%s,%s,%s,%s,1,1,'worker')", (*self.scope,uid(9),uid(8)))
        self.db.execute("INSERT INTO coordination.job_results (tenant_id,project_id,attempt_id,outcome,payload_ref) VALUES (%s,%s,%s,'unresolved',%s)", (*self.scope,uid(9),uid(5)))
        with self.assertRaises(psycopg.errors.CheckViolation):
            self.db.execute("UPDATE coordination.job_attempts SET fence=0")
        self.assertEqual(self.db.execute("SELECT outcome FROM coordination.job_results").fetchone()[0], 'unresolved')

    def test_fence_never_regresses(self):
        self.db.execute("INSERT INTO coordination.leases (tenant_id,project_id,kind,resource_id,holder,fence,expires_at) VALUES (%s,%s,'job',%s,'worker',2,now()+interval '1 minute')", (*self.scope,uid(8)))
        with self.assertRaises(psycopg.errors.ObjectNotInPrerequisiteState):
            self.db.execute("UPDATE coordination.leases SET fence=1")
        self.db.execute("UPDATE coordination.leases SET expires_at=now()+interval '2 minutes'")

    def test_outbox_shape_tombstone_digest_and_immutability(self):
        self.event()
        with self.assertRaises(psycopg.errors.CheckViolation):
            self.event(8, "delete", False)
        with self.assertRaises(psycopg.errors.ObjectNotInPrerequisiteState):
            self.db.execute("UPDATE coordination.outbox SET aggregate_revision=2")
        with self.assertRaises(psycopg.errors.ForeignKeyViolation):
            self.db.execute("INSERT INTO coordination.quarantine (tenant_id,project_id,event_id,module_id,error_code) VALUES (%s,%s,%s,'search','bad_payload')", (uid(2),uid(4),uid(7)))
        self.db.execute("INSERT INTO coordination.quarantine (tenant_id,project_id,event_id,module_id,error_code) VALUES (%s,%s,%s,'search','bad_payload')", (*self.scope,uid(7)))
        columns={r[0] for r in self.db.execute("SELECT column_name FROM information_schema.columns WHERE table_schema='coordination' AND table_name='outbox'")}
        self.assertNotIn('published_cursor',columns)

    def test_feed_retention_floor_and_publication_identity(self):
        self.event()
        self.db.execute("INSERT INTO coordination.feed_state (installation_id) VALUES (%s)", (uid(1),))
        self.assertEqual(self.db.execute("SELECT retention_seconds FROM coordination.feed_state").fetchone()[0],604800)
        with self.assertRaises(psycopg.errors.CheckViolation):
            self.db.execute("UPDATE coordination.feed_state SET retained_floor=2")
        self.db.execute("INSERT INTO coordination.published_events (installation_id,cursor,tenant_id,project_id,event_id) VALUES (%s,1,%s,%s,%s)",(uid(1),*self.scope,uid(7)))
        with self.assertRaises(psycopg.errors.UniqueViolation):
            self.db.execute("INSERT INTO coordination.published_events (installation_id,cursor,tenant_id,project_id,event_id) VALUES (%s,2,%s,%s,%s)",(uid(1),*self.scope,uid(7)))
        self.db.execute("INSERT INTO coordination.consumer_checkpoints (installation_id,module_id) VALUES (%s,'search')", (uid(1),))
        seconds=self.db.execute("SELECT extract(epoch FROM expires_at-last_seen_at) FROM coordination.consumer_checkpoints").fetchone()[0]
        self.assertEqual(seconds,604800)

    def test_embedding_model_dimensions_and_source_revision(self):
        self.db.execute("INSERT INTO retrieval.embedding_models (tenant_id,project_id,id,provider,model,model_version,dimensions,preprocessing_sha256) VALUES (%s,%s,%s,'current','synthetic','1',3,%s)", (*self.scope,uid(8),self.digest))
        args=(*self.scope,uid(6),1,uid(8),3,[0.1,0.2,0.3])
        self.db.execute("INSERT INTO retrieval.embeddings (tenant_id,project_id,record_id,source_revision,model_id,dimensions,embedding) VALUES (%s,%s,%s,%s,%s,%s,%s)",args)
        for vector in ([0.1,0.2], [float('nan'),0.2,0.3]):
            with self.assertRaises(psycopg.errors.CheckViolation):
                self.db.execute("UPDATE retrieval.embeddings SET embedding=%s",(vector,))
        with self.assertRaises(psycopg.errors.ForeignKeyViolation):
            self.db.execute("UPDATE retrieval.embeddings SET source_revision=99")

    def test_ledger_idempotence_and_applied_drift_refused(self):
        self.assertEqual(apply_migrations(self.db), [])
        self.db.execute("UPDATE core.schema_migrations SET sha256=%s WHERE migration_id='core-0001'",('0'*64,))
        with self.assertRaises(MigrationError):
            apply_migrations(self.db)

    def test_manifest_tamper_refused_before_ddl(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'schema';shutil.copytree(NEXT/'schema',path)
            self.reset()
            (path/'core/001-core.sql').write_text('CREATE TABLE evil(id int);')
            with self.assertRaises(MigrationError):
                apply_migrations(self.db,path)
            self.assertIsNone(self.db.execute("SELECT to_regnamespace('core')").fetchone()[0])

    def test_migration_failure_rolls_back_whole_install(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'schema';shutil.copytree(NEXT/'schema',path)
            manifest=json.loads((path/'manifest.json').read_text())
            last=manifest['migrations'][-1]
            bad=b'SELECT missing_c03_function();\n'
            (path/last['file']).write_bytes(bad)
            last['sha256']=hashlib.sha256(bad).hexdigest()
            (path/'manifest.json').write_text(json.dumps(manifest))
            self.reset()
            with self.assertRaises(psycopg.errors.UndefinedFunction):
                apply_migrations(self.db,path)
            self.assertIsNone(self.db.execute("SELECT to_regnamespace('core')").fetchone()[0])
