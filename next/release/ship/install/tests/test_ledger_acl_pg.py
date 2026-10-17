"""REAL-PostgreSQL ACL proof for the bounded startup-ledger access (J06-startup-ledger-access-v1, Kai Option A): after the installer's roles-post and roles-ledger (render_ledger_grant), the exact configured API
login can SELECT core.schema_migrations and nothing else of core; probe, the request group and any other role are denied; nothing goes to PUBLIC.
The schema is built by the REAL migration SQL in manifest order THROUGH api-replay-0004 (the first ten): later migrations need the pgvector extension, which the
Homebrew PostgreSQL does not have; the full 21-migration run belongs to the PG-gate image (CORTEX_ACL_THROUGH=<id> selects the last migration to apply).
Needs initdb/pg_ctl/psql (CORTEX_PG_BIN); a missing toolset FAILS, never skips. Home: PG-gate (Mike admits); not part of the pure suites."""
import json
import os
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import roles as R  # noqa: E402
import test_roles_pg as P  # noqa: E402
from _fixtures import LOGINS, NEXT, config, make_vault  # noqa: E402

THROUGH = os.environ.get('CORTEX_ACL_THROUGH', 'api-replay-0004')
LEDGER = ("CREATE SCHEMA IF NOT EXISTS core; CREATE TABLE IF NOT EXISTS core.schema_migrations ("
          "migration_id text PRIMARY KEY, sha256 text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'), applied_at timestamptz NOT NULL DEFAULT now())")  # the engine's own statement (migrations.py)
SELECT = 'SELECT count(*) FROM core.schema_migrations'


class LedgerAcl(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c = P.Cluster()
        cls.vault, _ = make_vault()
        cls.b = R.bind(config(cls.vault))
        cls.api, cls.probe = cls.b.logins['api_request'], cls.b.logins['probe']
        cls.c.psql(LEDGER)
        cls.c.psql("INSERT INTO core.schema_migrations VALUES ('core-0001', repeat('a',64))")
        cls.c.psql('CREATE ROLE cx_boot LOGIN SUPERUSER')
        cls.c.psql(R.render_pre(cls.b))
        applied = []
        for m in json.loads((NEXT / 'schema/manifest.json').read_text())['migrations']:
            cls.c.psql((NEXT / 'schema' / m['file']).read_text())
            applied.append(m['id'])
            if m['id'] == THROUGH:
                break
        assert applied[-1] == THROUGH, applied
        cls.c.psql(R.render_post(cls.b))
        cls.c.psql(R.render_ledger_grant(cls.b))
        cls.c.psql('CREATE ROLE stranger LOGIN')

    @classmethod
    def tearDownClass(cls):
        cls.c.stop()

    def run_as(self, user, sql):
        return self.c.psql(sql, user=user, check=False)

    def test_1_the_exact_api_login_reads_the_ledger(self):
        r = self.run_as(self.api, SELECT)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, '1'), r.stderr)

    def test_2_the_api_login_cannot_change_the_ledger_or_read_any_other_core_table(self):
        for sql in ("INSERT INTO core.schema_migrations VALUES ('x', repeat('b',64))", "UPDATE core.schema_migrations SET sha256=repeat('c',64)",
                    'DELETE FROM core.schema_migrations', 'SELECT count(*) FROM core.tenants', 'CREATE TABLE core.t (i int)'):
            r = self.run_as(self.api, sql)
            self.assertNotEqual(r.returncode, 0, sql)
            self.assertIn('permission denied', r.stderr, sql)

    def test_3_probe_the_request_group_and_a_stranger_are_denied(self):
        self.assertIn('permission denied', self.run_as(self.probe, SELECT).stderr)
        self.assertIn('permission denied', self.run_as(self.api, 'SET ROLE "kaidera-runtime-core-request"; ' + SELECT).stderr)
        self.assertIn('permission denied', self.run_as('stranger', SELECT).stderr)

    def test_4_nothing_is_granted_to_public_and_the_exact_privilege_set_is_known(self):
        rows = self.c.rows("SELECT grantee, privilege_type FROM information_schema.role_table_grants WHERE table_schema='core' AND table_name='schema_migrations' ORDER BY 1,2")
        self.assertEqual([r for r in rows if r[0] not in (self.api,)], [r for r in rows if r[0] != self.api])  # owner rows only
        self.assertNotIn('PUBLIC', {r[0] for r in rows})
        self.assertEqual([r for r in rows if r[0] == self.api], [(self.api, 'SELECT')])
        usage = self.c.rows("SELECT nspname FROM pg_namespace WHERE has_schema_privilege(%s,oid,'USAGE') AND nspname !~ '^pg_|information_schema' ORDER BY 1" % repr(self.api).replace('"', "'"))
        self.assertEqual(usage, [('core',), ('public',)])
        rel = self.c.rows("SELECT n.nspname||'.'||c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname IN ('core','auth','coordination','retrieval') "
                          "AND c.relkind IN ('r','v','m','p') AND has_table_privilege('%s',c.oid,'SELECT,INSERT,UPDATE,DELETE') ORDER BY 1" % self.api)
        self.assertEqual(rel, [('core.schema_migrations',)])

    def test_5_rerunning_post_and_closing_the_finite_logins_keeps_exactly_that_access(self):
        self.c.psql(R.render_post(self.b))
        self.c.psql(R.render_ledger_grant(self.b))
        self.c.psql(R.render_close(self.b, True))
        r = self.run_as(self.api, SELECT)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, '1'), r.stderr)
        self.assertIn('permission denied', self.run_as(self.probe, SELECT).stderr)


if __name__ == '__main__':
    unittest.main()
