"""PG-gate home of the nine real-PostgreSQL methods of provision_roles (HALE-A1-NIX-CI-001): RolesInDatabase (8) + SecretGuard (1).
Every database assertion runs psql against a throwaway cluster on a private unix socket; nothing else is touched. Pure tests: test_roles.py.
Run only where a PostgreSQL toolset (initdb, pg_ctl, psql) exists: see README.md "PG-gate fixture". A missing toolset FAILS here, never skips."""
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
sys.path.insert(0, str(HERE))
import roles as R  # noqa: E402
from _fixtures import FINITE, FIVE, INSTALLER, LOGINS, NEXT, REQUEST_GROUP, SERVICE, config, make_vault  # noqa: E402


def pg_bin():
    """PostgreSQL binaries for a throwaway cluster: CORTEX_PG_BIN, PATH, Debian/Ubuntu and Homebrew locations. Missing = FAIL, never skip."""
    names = ('initdb', 'pg_ctl', 'psql')
    found = [Path(os.environ['CORTEX_PG_BIN'])] if os.environ.get('CORTEX_PG_BIN') else []
    found += [Path(p) for p in os.environ.get('PATH', '').split(os.pathsep) if p]
    found += sorted(Path('/usr/lib/postgresql').glob('*/bin'), reverse=True) + [Path('/opt/homebrew/bin')]
    for d in found:
        if all((d / n).is_file() for n in names):
            return d
    raise AssertionError('no PostgreSQL initdb/pg_ctl/psql found (set CORTEX_PG_BIN)')


class Cluster:
    """A disposable cluster; the harness superuser stands in for core-init's temporary core."""

    def __init__(self):
        self.bin = pg_bin()  # discovered in the fixture, never at import: a missing PostgreSQL FAILS the test, it is never skipped
        self.dir = Path(tempfile.mkdtemp(prefix='nix-roles-'))
        self.sock = self.dir / 's'
        self.sock.mkdir()
        env = dict(os.environ, LC_ALL='C')
        subprocess.run([self.bin / 'initdb', '-D', self.dir / 'data', '-U', 'harness', '-A', 'trust', '--locale=C', '-E', 'UTF8'],
                       check=True, capture_output=True, env=env)
        subprocess.run([self.bin / 'pg_ctl', '-D', self.dir / 'data', '-w', '-l', self.dir / 'log', '-o',
                        f"-c listen_addresses='' -c unix_socket_directories={self.sock} -c fsync=off", 'start'],
                       check=True, capture_output=True, env=env)

    def psql(self, sql, user='harness', check=True):
        r = subprocess.run([self.bin / 'psql', '-h', str(self.sock), '-U', user, '-d', 'postgres', '-X', '-q', '-v', 'ON_ERROR_STOP=1',
                            '-At', '-F', '|', '-f', '-'], input=sql, capture_output=True, text=True)
        if check and r.returncode:
            raise AssertionError(r.stderr)
        return r

    def rows(self, sql):
        return [tuple(line.split('|')) for line in self.psql(sql).stdout.splitlines() if line]

    def stop(self):
        subprocess.run([self.bin / 'pg_ctl', '-D', self.dir / 'data', '-m', 'immediate', 'stop'], capture_output=True)
        shutil.rmtree(self.dir, ignore_errors=True)

    def migrate(self):
        """Stand-in for the unchanged migrations: run the REAL DO block of 002-isolation.sql that creates the two NOLOGIN groups."""
        text = (NEXT / 'schema/auth/002-isolation.sql').read_text()
        block = re.search(r'DO \$\$.*?END;\n\$\$;', text, re.S).group(0)
        self.psql(block)


FLAGS = "SELECT rolname,rolcanlogin,rolsuper,rolcreatedb,rolcreaterole,rolinherit,rolreplication,rolbypassrls FROM pg_roles WHERE rolname LIKE 'kaidera-%' OR rolname LIKE 'cx\\_%' ORDER BY 1"
MEMBERS = "SELECT m.rolname,g.rolname,a.set_option,a.inherit_option,a.admin_option FROM pg_auth_members a JOIN pg_roles m ON m.oid=a.member JOIN pg_roles g ON g.oid=a.roleid WHERE m.rolname LIKE 'kaidera-%' OR m.rolname LIKE 'cx\\_%' ORDER BY 1,2"


def flag_row(login, spec):
    f = lambda b: 't' if b else 'f'
    return (login, f(spec['login']), f(spec['superuser']), f(spec['createdb']), f(spec['createrole']),
            f(spec['inherit']), f(spec['replication']), f(spec['bypassrls']))


class RolesInDatabase(unittest.TestCase):
    """One ordered story on one cluster: pre -> migrate -> post -> close, with re-runs."""

    @classmethod
    def setUpClass(cls):
        cls.c = Cluster()
        cls.vault, cls.values = make_vault()
        cls.roles = R.load_roles()
        cls.b = R.bind(config(cls.vault))
        # core-init made the bootstrap superuser before any phase runs
        cls.c.psql('CREATE ROLE cx_boot LOGIN SUPERUSER')

    @classmethod
    def tearDownClass(cls):
        cls.c.stop()
        shutil.rmtree(cls.vault, ignore_errors=True)

    def test_1_pre_creates_exactly_the_approved_logins_with_exact_flags_and_scram(self):
        self.c.psql(R.render_pre(self.b))
        got = {r[0]: r for r in self.c.rows(FLAGS)}
        for p in ('migrator', 'api_request', 'probe', 'installer_enrollment'):
            self.assertEqual(got[LOGINS[p]], flag_row(LOGINS[p], self.roles[p]), p)
        self.assertEqual(sorted(got), sorted(['cx_boot', 'cx_migrate', 'cx_api', 'cx_probe', INSTALLER]))
        names = {r[0] for r in self.c.rows('SELECT rolname FROM pg_roles')}
        self.assertFalse({n for n in names if 'supervisor' in n or 'observer' in n}, 'no extra runtime credential')
        hashes = dict(self.c.rows("SELECT rolname,left(rolpassword,14) FROM pg_authid WHERE rolname LIKE 'cx\\_%' OR rolname LIKE 'kaidera-%'"))
        for p in ('migrator', 'api_request', 'probe', 'installer_enrollment'):
            self.assertEqual(hashes[LOGINS[p]], 'SCRAM-SHA-256$', p)
        self.assertEqual(self.c.rows("SELECT count(*) FROM pg_auth_members m JOIN pg_roles r ON r.oid=m.member WHERE r.rolname LIKE 'cx\\_%'"), [('0',)])

    def test_2_pre_leaves_the_two_groups_to_the_unchanged_migrations(self):
        names = {r[0] for r in self.c.rows(FLAGS)}
        self.assertNotIn(REQUEST_GROUP, names)
        self.assertNotIn('kaidera-runtime-core-verifier', names)

    def test_3_post_grants_exactly_one_set_only_membership(self):
        self.c.migrate()
        self.c.psql(R.render_post(self.b))
        self.assertEqual(self.c.rows(MEMBERS), [('cx_api', REQUEST_GROUP, 't', 'f', 'f')])

    def test_4_post_refuses_an_unexpected_membership(self):
        self.c.psql(f'GRANT "{REQUEST_GROUP}" TO cx_probe WITH INHERIT FALSE, SET TRUE')
        r = self.c.psql(R.render_post(self.b), check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('unexpected role membership', r.stderr)
        self.c.psql(f'REVOKE "{REQUEST_GROUP}" FROM cx_probe')

    def test_5_phases_rerun_to_the_same_state(self):
        before = (self.c.rows(FLAGS), self.c.rows(MEMBERS))
        self.c.psql(R.render_pre(self.b))
        self.c.psql(R.render_post(self.b))
        self.assertEqual((self.c.rows(FLAGS), self.c.rows(MEMBERS)), before)

    def test_6_close_closes_only_the_finite_logins(self):
        self.c.psql(R.render_close(self.b, True))
        got = {r[0]: r for r in self.c.rows(FLAGS)}
        for p in FINITE:
            self.assertEqual(got[LOGINS[p]][1], 'f', p)
        for p in SERVICE:
            self.assertEqual(got[LOGINS[p]][1], 't', p)

    def test_7_a_closed_login_cannot_connect_and_a_service_login_still_can(self):
        for p in ('migrator', 'installer_enrollment', 'bootstrap'):
            r = self.c.psql('SELECT 1', user=LOGINS[p], check=False)
            self.assertNotEqual(r.returncode, 0, p)
            self.assertIn('not permitted to log in', r.stderr)
        self.assertEqual(self.c.psql('SELECT current_user', user='cx_probe').stdout.strip(), 'cx_probe')

    def test_8_close_reruns_cleanly(self):
        self.c.psql(R.render_close(self.b, True))


class SecretGuard(unittest.TestCase):
    def test_a_missing_or_short_secret_creates_nothing(self):
        c = Cluster()
        try:
            vault, _ = make_vault()
            (vault / 'secrets/cortex/probe-secret').write_text('short\n')
            r = c.psql(R.render_pre(R.bind(config(vault))), check=False)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn('secret_missing_or_short:cx_probe', r.stderr)
            self.assertEqual(c.rows("SELECT count(*) FROM pg_roles WHERE rolname LIKE 'cx\\_%' OR rolname LIKE 'kaidera-%'"), [('0',)])
        finally:
            c.stop()


if __name__ == '__main__':
    unittest.main()
