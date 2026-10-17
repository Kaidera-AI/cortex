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

SNAPSHOT = ("SELECT 'ns', nspname, coalesce(nspacl::text,'') FROM pg_namespace WHERE nspname !~ '^pg_|information_schema' "
            "UNION ALL SELECT 'rel', n.nspname||'.'||c.relname, coalesce(c.relacl::text,'') FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname !~ '^pg_|information_schema' AND c.relkind IN ('r','v','m','p','S') "
            "UNION ALL SELECT 'fn', n.nspname||'.'||p.proname||'/'||p.oid, coalesce(p.proacl::text,'') FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname !~ '^pg_|information_schema' ORDER BY 1,2")


def items(acl):
    return set(x for x in acl.strip('{}').split(',') if x)


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
        cls.before = cls.c.rows(SNAPSHOT)
        cls.c.psql(R.render_ledger_grant(cls.b))
        cls.after = cls.c.rows(SNAPSHOT)
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

    def test_6_the_ledger_step_changes_exactly_two_acls_and_adds_only_the_api_login(self):
        before = {(k, n): a for k, n, a in self.before}
        after = {(k, n): a for k, n, a in self.after}
        self.assertEqual(set(before), set(after))
        changed = {key for key in after if after[key] != before[key]}
        self.assertEqual(changed, {('ns', 'core'), ('rel', 'core.schema_migrations')})
        for key, priv in ((('ns', 'core'), 'U'), (('rel', 'core.schema_migrations'), 'r')):
            self.assertEqual(items(before[key]) - items(after[key]), set(), 'nothing is revoked')
            added = items(after[key]) - items(before[key])
            self.assertEqual(len(added), 1, added)
            grantee, _, rest = next(iter(added)).partition('=')
            self.assertEqual((grantee.strip('"'), rest.split('/')[0]), (self.api, priv))

    def test_7_probe_has_no_core_usage_the_request_group_has_no_ledger_select_and_baseline_usage_remains(self):
        yes = lambda q: self.c.rows(q)[0][0] == 't'
        self.assertFalse(yes(f"SELECT has_schema_privilege('{self.probe}','core','USAGE')"))
        self.assertFalse(yes(f"SELECT has_table_privilege('{R.REQUEST_GROUP}','core.schema_migrations','SELECT')"))
        self.assertFalse(yes(f"SELECT has_table_privilege('{R.VERIFIER_GROUP}','core.schema_migrations','SELECT')"))
        for schema in ('core', 'auth', 'coordination', 'retrieval'):
            if self.c.rows(f"SELECT 1 FROM pg_namespace WHERE nspname='{schema}'"):
                self.assertTrue(yes(f"SELECT has_schema_privilege('{R.REQUEST_GROUP}','{schema}','USAGE')"), schema)
        for schema in ('core', 'auth'):
            self.assertTrue(yes(f"SELECT has_schema_privilege('{R.VERIFIER_GROUP}','{schema}','USAGE')"), schema)
        pub = lambda rows: sorted((k, n, x) for k, n, a in rows for x in items(a) if x.startswith('='))
        self.assertEqual(pub(self.after), pub(self.before), 'no PUBLIC entry added anywhere (stock defaults unchanged)')
        self.assertEqual([r for r in pub(self.after) if r[:2] in (('ns', 'core'), ('rel', 'core.schema_migrations'))], [])



class ProvisionStepsPg(unittest.TestCase):
    """The same provisioning through roles.provision_steps (secrets as BOUND parameters, psql \\bind on PostgreSQL 16+) gives the same roles, flags and ACL as the psql scripts."""

    @classmethod
    def setUpClass(cls):
        cls.c = P.Cluster()
        cls.vault, cls.values = make_vault()
        cls.b = R.bind(config(cls.vault))
        cls.c.psql(LEDGER)
        cls.c.psql('CREATE ROLE cx_boot LOGIN SUPERUSER')
        cls.api = cls.b.logins['api_request']

    @classmethod
    def tearDownClass(cls):
        cls.c.stop()

    def run_steps(self, phase, values=None, ack=False):
        """one transaction like Core's .transaction('bootstrap'): each step in order; the secret travels only as the bound parameter"""
        self.assertTrue(hasattr(R, 'provision_steps'), 'roles.provision_steps')
        lines = ['\\set ON_ERROR_STOP on', 'BEGIN;']
        for sql, purpose in R.provision_steps(self.b, phase, seed_ack=ack):
            if purpose is None:
                lines.append(sql.rstrip(';') + ';')
            else:
                lines.append(sql.replace('%s', '$1') + f" \\bind '{(values or self.values)[purpose]}' \\g")
        lines.append('COMMIT;')
        return self.c.psql('\n'.join(lines) + '\n', check=False)

    def test_1_pre_creates_the_four_logins_with_policy_flags_and_scram(self):
        r = self.run_steps('pre')
        self.assertEqual(r.returncode, 0, r.stderr)
        got = {x[0]: x for x in self.c.rows(P.FLAGS)}
        for p in ('migrator', 'api_request', 'probe', 'installer_enrollment'):
            self.assertEqual(got[LOGINS[p]], P.flag_row(LOGINS[p], R.load_roles()[p]), p)
        hashes = dict(self.c.rows("SELECT rolname,left(rolpassword,14) FROM pg_authid WHERE rolname LIKE 'cx\\_%' OR rolname LIKE 'kaidera-%'"))
        for p in ('migrator', 'api_request', 'probe', 'installer_enrollment'):
            self.assertEqual(hashes[LOGINS[p]], 'SCRAM-SHA-256$', p)

    def test_2_the_bound_secret_never_appears_in_statement_text(self):
        self.assertTrue(hasattr(R, 'provision_steps'))
        text = '\n'.join(s[0] for s in R.provision_steps(self.b, 'pre'))
        for v in self.values.values():
            self.assertNotIn(v, text)

    def test_3_a_short_secret_creates_nothing_and_leaves_no_password_setting(self):
        self.assertTrue(hasattr(R, 'provision_steps'), 'roles.provision_steps')
        other = P.Cluster()
        try:
            short = {k: 'short' for k in self.values}
            lines = ['\\set ON_ERROR_STOP on', 'BEGIN;']
            for sql, purpose in R.provision_steps(self.b, 'pre'):
                lines.append(sql.rstrip(';') + ';' if purpose is None else sql.replace('%s', '$1') + f" \\bind '{short[purpose]}' \\g")
            lines.append('COMMIT;')
            r = other.psql('\n'.join(lines) + '\n', check=False)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn('secret_missing_or_short:', r.stderr)
            self.assertEqual(other.rows("SELECT count(*) FROM pg_roles WHERE rolname LIKE 'cx\\_%' OR rolname LIKE 'kaidera-%'"), [('0',)])
        finally:
            other.stop()

    def test_4_post_ledger_and_close_steps_apply_and_give_the_same_acl_as_the_scripts(self):
        self.c.migrate()
        for phase, ack in (('post', False), ('ledger', False)):
            r = self.run_steps(phase, ack=ack)
            self.assertEqual(r.returncode, 0, (phase, r.stderr))
        self.c.psql("INSERT INTO core.schema_migrations VALUES ('core-0001', repeat('a',64))")
        self.assertEqual(self.run_as_api(SELECT).stdout.strip(), '1')
        r = self.run_steps('close', ack=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        got = {x[0]: x for x in self.c.rows(P.FLAGS)}
        for p in ('migrator', 'installer_enrollment'):
            self.assertEqual(got[LOGINS[p]][1], 'f', p)
        self.assertEqual(self.run_as_api(SELECT).stdout.strip(), '1')

    def run_as_api(self, sql):
        return self.c.psql(sql, user=self.api, check=False)


if __name__ == '__main__':
    unittest.main()
