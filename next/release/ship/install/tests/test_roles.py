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
from types import SimpleNamespace  # noqa: E402

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


PGBIN = pg_bin()
FIVE = ('bootstrap', 'migrator', 'api_request', 'probe', 'installer_enrollment')
SERVICE = ('api_request', 'probe')
FINITE = ('installer_enrollment', 'migrator', 'bootstrap')
REQUEST_GROUP = 'kaidera-runtime-core-request'
INSTALLER = 'kaidera-runtime-core-installer'
# Stand-ins that mirror the published Core carrier (runtime/bootstrap_config.py: Role(login, secret_ref), BootstrapConfig.roles/.vault_root).
# A validated config admits only the purposes of ITS mode, so the five are gathered from the seed, serve and migrate views of the ONE config file.
LOGINS = {'bootstrap': 'cx_boot', 'migrator': 'cx_migrate', 'api_request': 'cx_api', 'probe': 'cx_probe', 'installer_enrollment': INSTALLER}
MODES = {'seed': ('bootstrap', 'installer_enrollment', 'api_request'), 'serve': ('api_request', 'probe'), 'migrate': ('migrator',)}


def config(vault, logins=None, refs=None):
    logins = dict(LOGINS, **(logins or {}))
    refs = dict({n: f'secrets/cortex/{n}-secret' for n in FIVE}, **(refs or {}))
    return [SimpleNamespace(vault_root=str(vault), mode=m,
                            roles={p: SimpleNamespace(login=logins[p], secret_ref=refs[p]) for p in ps}) for m, ps in MODES.items()]


def make_vault():
    vault = Path(tempfile.mkdtemp(prefix='nix-vault-'))
    (vault / 'secrets/cortex').mkdir(parents=True)
    values = {}
    for n in FIVE:
        values[n] = f'pw-{n}-' + os.urandom(6).hex()
        (vault / f'secrets/cortex/{n}-secret').write_text(values[n] + '\n')
    return vault, values


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


FLAGS = "SELECT rolname,rolcanlogin,rolsuper,rolcreatedb,rolcreaterole,rolinherit,rolreplication,rolbypassrls FROM pg_roles WHERE rolname LIKE 'kaidera-%' OR rolname LIKE 'cx\\_%' ORDER BY 1"
MEMBERS = "SELECT m.rolname,g.rolname,a.set_option,a.inherit_option,a.admin_option FROM pg_auth_members a JOIN pg_roles m ON m.oid=a.member JOIN pg_roles g ON g.oid=a.roleid WHERE m.rolname LIKE 'kaidera-%' OR m.rolname LIKE 'cx\\_%' ORDER BY 1,2"


def flag_row(login, spec):
    f = lambda b: 't' if b else 'f'
    return (login, f(spec['login']), f(spec['superuser']), f(spec['createdb']), f(spec['createrole']),
            f(spec['inherit']), f(spec['replication']), f(spec['bypassrls']))


class Policy(unittest.TestCase):
    def test_only_the_five_approved_purposes_exist(self):
        loaded = R.load_roles()
        self.assertEqual(tuple(loaded), FIVE)
        self.assertNotIn('supervisor', loaded)
        self.assertNotIn('observer', loaded)
        self.assertEqual(loaded['api_request']['memberships'], [REQUEST_GROUP])
        legacy = contract_roles()
        for p in FIVE:
            self.assertEqual(loaded[p], legacy[p], 'the A projection reuses the pinned flags unchanged')

    def test_load_refuses_a_contract_missing_an_approved_purpose(self):
        p = Path(tempfile.mkdtemp()) / 'c.json'
        p.write_text(json.dumps({'roles': {'api_request': {}}}))
        with self.assertRaises(R.RoleRefusal) as e:
            R.load_roles(p)
        self.assertEqual(e.exception.reason, 'roles_contract_invalid')

    def test_close_requires_the_seed_acknowledgement(self):
        vault, _ = make_vault()
        with self.assertRaises(R.RoleRefusal) as e:
            R.render_close(R.bind(config(vault)), seed_ack=False)
        self.assertEqual(e.exception.reason, 'seed_not_acknowledged')

    def test_rendered_sql_never_contains_a_secret_value(self):
        vault, values = make_vault()
        b = R.bind(config(vault))
        sql = R.render_pre(b) + R.render_post(b) + R.render_close(b, True)
        for v in values.values():
            self.assertNotIn(v, sql)
        self.assertIn('cat ', sql)


class ConfigJoin(unittest.TestCase):
    def test_logins_and_secret_paths_come_from_the_validated_config(self):
        vault, _ = make_vault()
        b = R.bind(config(vault, logins={'api_request': 'zz_api'}, refs={'api_request': 'secrets/cortex/zz-ref'}))
        sql = R.render_pre(b)
        self.assertIn('"zz_api"', sql)
        self.assertIn(f"{vault}/secrets/cortex/zz-ref", sql)
        self.assertNotIn('kaidera-runtime-api-request', sql)
        self.assertNotIn('/run/cortex/secrets', sql)

    def test_one_view_per_mode_is_enough_but_all_five_are_required(self):
        vault, _ = make_vault()
        with self.assertRaises(R.RoleRefusal) as e:
            R.bind(config(vault)[:2])
        self.assertEqual(e.exception.reason, 'role_config_incomplete:migrator')

    def test_conflicting_views_of_one_purpose_are_refused(self):
        vault, _ = make_vault()
        views = config(vault)
        views[1].roles['api_request'] = SimpleNamespace(login='other_api', secret_ref='secrets/cortex/api_request-secret')
        with self.assertRaises(R.RoleRefusal) as e:
            R.bind(views)
        self.assertEqual(e.exception.reason, 'role_config_conflict:api_request')

    def test_installer_login_must_be_the_name_the_unchanged_migration_fixes(self):
        vault, _ = make_vault()
        with self.assertRaises(R.RoleRefusal) as e:
            R.bind(config(vault, logins={'installer_enrollment': 'my_installer'}))
        self.assertEqual(e.exception.reason, 'role_name_conflict:installer_enrollment')

    def test_a_login_may_not_take_a_migration_group_name(self):
        vault, _ = make_vault()
        for group in (REQUEST_GROUP, 'kaidera-runtime-core-verifier'):
            with self.assertRaises(R.RoleRefusal) as e:
                R.bind(config(vault, logins={'probe': group}))
            self.assertEqual(e.exception.reason, 'role_name_conflict:probe')

    def test_different_vault_roots_are_refused(self):
        a, _ = make_vault()
        b, _ = make_vault()
        views = config(a)
        views[2].vault_root = str(b)
        with self.assertRaises(R.RoleRefusal) as e:
            R.bind(views)
        self.assertEqual(e.exception.reason, 'role_config_conflict:vault_root')

    def test_a_reference_that_leaves_the_vault_is_refused(self):
        vault, _ = make_vault()
        with self.assertRaises(R.RoleRefusal) as e:
            R.bind(config(vault, refs={'probe': 'secrets/cortex/../../x'}))
        self.assertEqual(e.exception.reason, 'role_ref_unsafe:probe')


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
