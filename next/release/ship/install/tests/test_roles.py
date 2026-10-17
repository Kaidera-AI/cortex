"""provision_roles (J02): roles-pre -> unchanged migrations -> roles-post -> (seed ack) -> roles-close.
Every database assertion runs psql against a throwaway PostgreSQL cluster on a private unix socket; nothing else is touched."""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
NEXT = HERE.parents[3]
import roles as R  # noqa: E402

PGBIN = Path(os.environ.get('CORTEX_PG_BIN', '/opt/homebrew/bin'))
SERVICE = ('api_request', 'probe', 'supervisor', 'observer')
FINITE = ('installer_enrollment', 'migrator', 'bootstrap')
REQUEST_GROUP = 'kaidera-runtime-core-request'


def contract_roles():
    return json.loads((NEXT / 'contracts/runtime/runtime-operations-v1.json').read_text())['roles']


class Cluster:
    """A disposable cluster; the harness superuser stands in for core-init's temporary core."""

    def __init__(self):
        self.dir = Path(tempfile.mkdtemp(prefix='nix-roles-'))
        self.sock = self.dir / 's'
        self.sock.mkdir()
        env = dict(os.environ, LC_ALL='C')
        subprocess.run([PGBIN / 'initdb', '-D', self.dir / 'data', '-U', 'harness', '-A', 'trust', '--locale=C', '-E', 'UTF8'],
                       check=True, capture_output=True, env=env)
        subprocess.run([PGBIN / 'pg_ctl', '-D', self.dir / 'data', '-w', '-l', self.dir / 'log', '-o',
                        f"-c listen_addresses='' -c unix_socket_directories={self.sock} -c fsync=off", 'start'],
                       check=True, capture_output=True, env=env)

    def psql(self, sql, user='harness', check=True):
        r = subprocess.run([PGBIN / 'psql', '-h', str(self.sock), '-U', user, '-d', 'postgres', '-X', '-q', '-v', 'ON_ERROR_STOP=1',
                            '-At', '-F', '|', '-f', '-'], input=sql, capture_output=True, text=True)
        if check and r.returncode:
            raise AssertionError(r.stderr)
        return r

    def rows(self, sql):
        return [tuple(line.split('|')) for line in self.psql(sql).stdout.splitlines() if line]

    def stop(self):
        subprocess.run([PGBIN / 'pg_ctl', '-D', self.dir / 'data', '-m', 'immediate', 'stop'], capture_output=True)
        shutil.rmtree(self.dir, ignore_errors=True)

    def migrate(self):
        """Stand-in for the unchanged migrations: run the REAL DO block of 002-isolation.sql that creates the two NOLOGIN groups."""
        text = (NEXT / 'schema/auth/002-isolation.sql').read_text()
        block = re.search(r'DO \$\$.*?END;\n\$\$;', text, re.S).group(0)
        self.psql(block)


FLAGS = "SELECT rolname,rolcanlogin,rolsuper,rolcreatedb,rolcreaterole,rolinherit,rolreplication,rolbypassrls FROM pg_roles WHERE rolname LIKE 'kaidera-%' OR rolname='harness' ORDER BY 1"
MEMBERS = "SELECT m.rolname,g.rolname,a.set_option,a.inherit_option,a.admin_option FROM pg_auth_members a JOIN pg_roles m ON m.oid=a.member JOIN pg_roles g ON g.oid=a.roleid WHERE m.rolname LIKE 'kaidera-%' ORDER BY 1,2"


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c = Cluster()
        cls.secrets = Path(tempfile.mkdtemp(prefix='nix-secrets-'))
        cls.roles = R.load_roles()
        cls.values = {}
        for logical in cls.roles:
            v = f'pw-{logical}-' + os.urandom(6).hex()
            cls.values[logical] = v
            (cls.secrets / R.physical_name(logical)).write_text(v + '\n')

    @classmethod
    def tearDownClass(cls):
        cls.c.stop()
        shutil.rmtree(cls.secrets, ignore_errors=True)

    def run_phase(self, sql):
        return self.c.psql(sql)


def flag_row(logical, spec):
    f = lambda b: 't' if b else 'f'
    return (R.physical_name(logical), f(spec['login']), f(spec['superuser']), f(spec['createdb']), f(spec['createrole']),
            f(spec['inherit']), f(spec['replication']), f(spec['bypassrls']))


class RolesContract(unittest.TestCase):
    def test_roles_come_from_the_one_contract(self):
        loaded = R.load_roles()
        self.assertEqual(set(loaded), set(contract_roles()))
        self.assertEqual(loaded['api_request']['memberships'], [REQUEST_GROUP])

    def test_load_refuses_a_contract_without_roles(self):
        p = Path(tempfile.mkdtemp()) / 'c.json'
        p.write_text(json.dumps({'roles': {'api_request': {}}}))
        with self.assertRaises(R.RoleRefusal) as e:
            R.load_roles(p)
        self.assertEqual(e.exception.reason, 'roles_contract_invalid')

    def test_close_requires_the_seed_acknowledgement(self):
        with self.assertRaises(R.RoleRefusal) as e:
            R.render_close(R.load_roles(), seed_ack=False)
        self.assertEqual(e.exception.reason, 'seed_not_acknowledged')

    def test_rendered_sql_never_contains_a_secret_value(self):
        d = Path(tempfile.mkdtemp())
        for logical in R.load_roles():
            (d / R.physical_name(logical)).write_text('TOPSECRET-' + logical)
        sql = R.render_pre(R.load_roles(), d) + R.render_post(R.load_roles()) + R.render_close(R.load_roles(), True)
        self.assertNotIn('TOPSECRET', sql)
        self.assertIn('cat ', sql)


class RolesInDatabase(Base):
    """One ordered story on one cluster: pre -> migrate -> post -> close, with re-runs."""

    def test_1_pre_creates_exact_flags_and_scram_passwords(self):
        self.run_phase(R.render_pre(self.roles, self.secrets))
        got = {r[0]: r for r in self.c.rows(FLAGS)}
        for logical, spec in self.roles.items():
            if spec['lifetime'] == 'persistent-nologin' or logical == 'bootstrap':
                continue
            self.assertEqual(got[R.physical_name(logical)], flag_row(logical, spec), logical)
        hashes = dict(self.c.rows("SELECT rolname,left(rolpassword,14) FROM pg_authid WHERE rolname LIKE 'kaidera-%'"))
        for logical in ('api_request', 'probe', 'supervisor', 'observer', 'migrator', 'installer_enrollment'):
            self.assertEqual(hashes[R.physical_name(logical)], 'SCRAM-SHA-256$', logical)
        self.assertEqual(self.c.rows("SELECT count(*) FROM pg_auth_members m JOIN pg_roles r ON r.oid=m.member WHERE r.rolname LIKE 'kaidera-%'"), [('0',)])

    def test_2_pre_leaves_the_two_groups_to_the_unchanged_migrations(self):
        names = {r[0] for r in self.c.rows(FLAGS)}
        self.assertNotIn(REQUEST_GROUP, names)
        self.assertNotIn('kaidera-runtime-core-verifier', names)

    def test_3_post_grants_exactly_one_set_only_membership(self):
        self.c.migrate()
        self.run_phase(R.render_post(self.roles))
        self.assertEqual(self.c.rows(MEMBERS), [(R.physical_name('api_request'), REQUEST_GROUP, 't', 'f', 'f')])

    def test_4_post_refuses_an_unexpected_membership(self):
        self.c.psql(f'GRANT "{REQUEST_GROUP}" TO "{R.physical_name("probe")}" WITH INHERIT FALSE, SET TRUE')
        r = self.c.psql(R.render_post(self.roles), check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('unexpected role membership', r.stderr)
        self.c.psql(f'REVOKE "{REQUEST_GROUP}" FROM "{R.physical_name("probe")}"')

    def test_5_phases_rerun_to_the_same_state(self):
        before = (self.c.rows(FLAGS), self.c.rows(MEMBERS))
        self.run_phase(R.render_pre(self.roles, self.secrets))
        self.run_phase(R.render_post(self.roles))
        self.assertEqual((self.c.rows(FLAGS), self.c.rows(MEMBERS)), before)

    def test_6_close_closes_only_the_finite_logins(self):
        self.run_phase(R.render_close(self.roles, True))
        got = {r[0]: r for r in self.c.rows(FLAGS)}
        for logical in FINITE:
            if logical == 'bootstrap':
                continue  # core-init made it; the stand-in superuser here is 'harness'
            self.assertEqual(got[R.physical_name(logical)][1], 'f', logical)
        for logical in SERVICE:
            self.assertEqual(got[R.physical_name(logical)][1], 't', logical)

    def test_7_a_closed_login_cannot_connect_and_a_service_login_still_can(self):
        for logical in ('migrator', 'installer_enrollment'):
            r = self.c.psql('SELECT 1', user=R.physical_name(logical), check=False)
            self.assertNotEqual(r.returncode, 0, logical)
            self.assertIn('not permitted to log in', r.stderr)
        self.assertEqual(self.c.rows('SELECT 1'), [('1',)])
        r = self.c.psql('SELECT current_user', user=R.physical_name('probe'))
        self.assertEqual(r.stdout.strip(), R.physical_name('probe'))

    def test_8_close_reruns_cleanly(self):
        self.run_phase(R.render_close(self.roles, True))


if __name__ == '__main__':
    unittest.main()
