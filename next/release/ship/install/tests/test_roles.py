"""provision_roles (J02) pure tests: policy, the config join (stand-ins and the real Core carrier) and the CI home of the database methods.
The nine real-PostgreSQL methods live in test_roles_pg.py (PG-gate); nothing here starts a native process."""
import ast
import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
NEXT = HERE.parents[3]
sys.path.insert(0, str(NEXT / 'src'))
import roles as R  # noqa: E402
from cortex_core.runtime.bootstrap_config import read_config_file  # noqa: E402  (the real Core carrier, Cox 8e1babb2)
from types import SimpleNamespace  # noqa: E402
sys.path.insert(0, str(HERE))
from _fixtures import FIVE, INSTALLER, LOGINS, REQUEST_GROUP, config, contract_roles, make_vault  # noqa: E402

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


class RealCarrierJoin(unittest.TestCase):
    """HALE-A1-NIX-JOIN-001: the three mode views must come from ONE decoded snapshot and the combined identities must stay unique.
    Uses the real Core carrier (private 0600 file in a 0700 vault); both mixed snapshots are valid for Core on their own."""

    def setUp(self):
        self.vault = Path(os.path.realpath(tempfile.mkdtemp(prefix='nix-core-')))
        self.vault.chmod(0o700)
        (self.vault / 'config').mkdir(mode=0o700)
        self.path = self.vault / 'config/cortex.json'
        ids = [str(__import__('uuid').uuid4()) for _ in range(4)]
        self.cfg = {'version': 1, 'installation_id': ids[0], 'tenant_id': ids[1], 'project_id': ids[2], 'project_names': {'main': ids[2]},
                    'ai_mode': 'off', 'vault_root': str(self.vault), 'bind': {'host': '0.0.0.0', 'port': 8500}, 'publish': {'host': '127.0.0.1', 'port': 8500},
                    'client': {'credential_ref': 'secrets/cortex/client'},
                    'database': {'host': '10.0.0.5', 'port': 5432, 'name': 'cortex', 'tls': {'mode': 'verify-full', 'root_cert_ref': 'secrets/cortex/root'}},
                    'roles': {p: {'login': LOGINS[p], 'secret_ref': f'secrets/cortex/{p}'} for p in FIVE},
                    'limits': {'request_seconds': 30, 'shutdown_seconds': 30}}

    def tearDown(self):
        shutil.rmtree(self.vault, ignore_errors=True)

    def views(self, cfg):
        self.path.write_text(json.dumps(cfg))
        self.path.chmod(0o600)
        return [read_config_file(self.path, mode=m) for m in ('seed', 'serve', 'migrate')]

    def mixed(self, change):
        """seed + serve from snapshot A, migrate from the rewritten snapshot B (each valid for Core on its own)."""
        base = self.views(self.cfg)
        other = copy.deepcopy(self.cfg)
        change(other)
        updated = self.views(other)
        return [base[0], base[1], updated[2]]

    def test_one_immutable_file_gives_five_distinct_bindings(self):
        b = R.bind(self.views(self.cfg))
        self.assertEqual(len(set(b.logins.values())), 5)
        self.assertEqual(len(set(b.secrets.values())), 5)
        self.assertIn('ALTER ROLE "cx_migrate"', R.render_pre(b))

    def test_mixed_valid_snapshots_that_share_a_login_are_refused_before_any_sql(self):
        def change(c):
            c['roles']['migrator']['login'] = 'cx_api'
            c['roles']['api_request']['login'] = 'other_api'
        with self.assertRaises(R.RoleRefusal) as e:
            R.bind(self.mixed(change))
        self.assertEqual(e.exception.reason, 'role_config_collision:login')

    def test_mixed_valid_snapshots_that_share_a_secret_reference_are_refused_before_any_sql(self):
        def change(c):
            c['roles']['migrator']['secret_ref'] = 'secrets/cortex/api_request'
            c['roles']['api_request']['secret_ref'] = 'secrets/cortex/other_api'
        with self.assertRaises(R.RoleRefusal) as e:
            R.bind(self.mixed(change))
        self.assertEqual(e.exception.reason, 'role_config_collision:secret')

    def test_views_of_different_snapshots_are_refused_even_when_every_role_is_unique(self):
        with self.assertRaises(R.RoleRefusal) as e:
            R.bind(self.mixed(lambda c: c['database'].update(name='other_db')))
        self.assertEqual(e.exception.reason, 'role_config_conflict:snapshot')

    def test_restoring_the_healthy_snapshot_is_accepted_again(self):
        self.mixed(lambda c: c['roles']['migrator'].update(login='cx_api') or c['roles']['api_request'].update(login='other_api'))
        self.assertEqual(len(set(R.bind(self.views(self.cfg)).logins.values())), 5)


class CiHome(unittest.TestCase):
    """HALE-A1-NIX-CI-001: this module is pure; the nine real-PostgreSQL methods live in tests/test_roles_pg.py (the PG-gate home)."""
    PG = HERE / 'test_roles_pg.py'

    def test_pure_module_import_needs_no_postgres(self):
        code = ("import pathlib, sys\nreal = pathlib.Path.is_file\n"
                "pathlib.Path.is_file = lambda self: False if self.name in ('initdb', 'pg_ctl', 'psql') else real(self)\n"
                f"sys.path.insert(0, {str(HERE)!r}); import test_roles\n")
        r = subprocess.run([sys.executable, '-I', '-c', code], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr[-400:])

    def test_pure_module_names_no_native_database_machinery(self):
        tree = ast.parse((HERE / 'test_roles.py').read_text())
        tree.body = [n for n in tree.body if not (isinstance(n, ast.ClassDef) and n.name == 'CiHome')]
        found = sorted({n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)
                        and any(w in n.value for w in ('initdb', 'pg_ctl', 'psql'))}
                       | {n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and n.id in ('Cluster', 'pg_bin', 'PGBIN')})
        self.assertEqual(found, [])

    def test_the_nine_real_pg_methods_have_a_named_home_with_lazy_discovery(self):
        self.assertTrue(self.PG.is_file(), 'tests/test_roles_pg.py is the PG-gate home of the nine database methods')
        tree = ast.parse(self.PG.read_text())
        tests = {c.name: sorted(f.name for f in c.body if isinstance(f, ast.FunctionDef) and f.name.startswith('test_'))
                 for c in tree.body if isinstance(c, ast.ClassDef) and c.name in ('RolesInDatabase', 'SecretGuard')}
        self.assertEqual({k: len(v) for k, v in tests.items()}, {'RolesInDatabase': 8, 'SecretGuard': 1})
        eager = [n for n in tree.body if isinstance(n, (ast.Assign, ast.Expr)) and 'pg_bin' in ast.dump(n)]
        self.assertEqual(eager, [], 'PostgreSQL discovery must run in a fixture, never at import')


if __name__ == '__main__':
    unittest.main()
