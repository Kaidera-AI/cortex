"""C07 manifest, migration and table custody checks."""
import hashlib
import json
from pathlib import Path
from common import ConsumerFixture


class Manifest(ConsumerFixture):
    def test_ninth_migration_and_exact_three_tables_are_additive(self):
        root=Path(__file__).resolve().parents[2]
        manifest=json.loads((root/'schema/manifest.json').read_text())
        migrations=manifest['migrations']
        self.assertEqual(len(migrations),16)
        ids=[entry['id'] for entry in migrations]
        self.assertEqual(len(ids),len(set(ids)))
        expected_order=['core-0001', 'auth-0001', 'coordination-0001', 'retrieval-0000', 'auth-0002', 'coordination-0002', 'auth-0003', 'outbox-0002', 'module-consumers-0003', 'api-replay-0004', 'retrieval-0001', 'retrieval-0002', 'retrieval-0003', 'session-source-0005', 'retrieval-0004', 'session-ids-read-0007']
        self.assertEqual(ids,expected_order)
        bound_files={'module-consumers-0003': 'coordination/003-module-consumers.sql', 'api-replay-0004': 'coordination/004-api-replay.sql', 'retrieval-0001': 'retrieval/001-pg-search.sql', 'retrieval-0002': 'retrieval/002-query-cache.sql', 'retrieval-0003': 'retrieval/003-pg-graph.sql', 'session-source-0005': 'coordination/005-session-source.sql', 'retrieval-0004': 'retrieval/004-request-authority.sql', 'session-ids-read-0007': 'coordination/007-session-ids-read.sql'}
        for migration_id, filename in bound_files.items():
            self.assertIn(migration_id,ids)
            entry=migrations[ids.index(migration_id)]
            self.assertEqual(entry['file'],filename)
            self.assertEqual(entry['sha256'],hashlib.sha256((root/'schema'/filename).read_bytes()).hexdigest())
        names={f'{schema}.{table}' for schema,table in self.admin.execute("SELECT n.nspname,c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname IN ('core','auth','coordination','retrieval') AND c.relkind='r' AND c.relname<>'schema_migrations'").fetchall()}
        self.assertEqual(len(names),41)
        self.assertTrue({'coordination.consumer_project_checkpoints','coordination.consumer_aggregate_heads','coordination.consumer_event_outcomes'}<=names)
        expected_tables={'retrieval.generations', 'core.extraction_facts', 'core.record_aliases', 'auth.principals', 'auth.credentials', 'auth.project_grants', 'coordination.jobs', 'coordination.published_events', 'core.analytics_facts', 'coordination.quarantine', 'core.payloads', 'auth.permission_generations', 'retrieval.graph_edges', 'core.records', 'core.projects', 'retrieval.graph_generations', 'coordination.leases', 'retrieval.search_vectors', 'retrieval.graph_nodes', 'core.tenants', 'retrieval.query_embeddings', 'coordination.idempotency', 'retrieval.embeddings', 'coordination.consumer_project_checkpoints', 'coordination.job_attempts', 'coordination.session_sources', 'core.blob_manifests', 'core.record_revisions', 'coordination.outbox', 'retrieval.graph_state', 'coordination.snapshot_floors', 'coordination.consumer_event_outcomes', 'core.installations', 'retrieval.search_state', 'retrieval.search_sources', 'retrieval.embedding_models', 'coordination.feed_state', 'coordination.consumer_checkpoints', 'coordination.job_results', 'retrieval.graph_applied', 'coordination.consumer_aggregate_heads'}
        self.assertEqual(names,expected_tables)

    def test_new_tables_force_rls_and_request_has_no_direct_write(self):
        rows=self.admin.execute("SELECT relname,relrowsecurity,relforcerowsecurity FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='coordination' AND relname LIKE 'consumer_%' AND relkind='r'").fetchall()
        self.assertEqual({name for name,*_ in rows}, {'consumer_checkpoints','consumer_project_checkpoints','consumer_aggregate_heads','consumer_event_outcomes'})
        self.assertTrue(all(enabled and forced for _,enabled,forced in rows))
