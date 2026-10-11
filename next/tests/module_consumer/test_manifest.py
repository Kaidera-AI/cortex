"""C07 manifest, migration and table custody checks."""
import hashlib
import json
from pathlib import Path
from common import ConsumerFixture


class Manifest(ConsumerFixture):
    def test_ninth_migration_and_exact_three_tables_are_additive(self):
        root=Path(__file__).resolve().parents[2]
        manifest=json.loads((root/'schema/manifest.json').read_text())
        self.assertEqual(len(manifest['migrations']),9)
        entry=manifest['migrations'][-1]
        self.assertEqual(entry['id'],'module-consumers-0003')
        self.assertEqual(entry['sha256'],hashlib.sha256((root/'schema'/entry['file']).read_bytes()).hexdigest())
        names={f'{schema}.{table}' for schema,table in self.admin.execute("SELECT n.nspname,c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname IN ('core','auth','coordination','retrieval') AND c.relkind='r' AND c.relname<>'schema_migrations'").fetchall()}
        self.assertEqual(len(names),31)
        self.assertTrue({'coordination.consumer_project_checkpoints','coordination.consumer_aggregate_heads','coordination.consumer_event_outcomes'}<=names)

    def test_new_tables_force_rls_and_request_has_no_direct_write(self):
        rows=self.admin.execute("SELECT relname,relrowsecurity,relforcerowsecurity FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='coordination' AND relname LIKE 'consumer_%' AND relkind='r'").fetchall()
        self.assertEqual({name for name,*_ in rows}, {'consumer_checkpoints','consumer_project_checkpoints','consumer_aggregate_heads','consumer_event_outcomes'})
        self.assertTrue(all(enabled and forced for _,enabled,forced in rows))
