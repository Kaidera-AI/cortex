"""Ledger admission controls; synthetic receipts confer no PostgreSQL credit."""
import ast
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[4]
PROFILE = ROOT / '.github/next-offline-suites.json'
SUITE = 'next/release/ship/install/tests/test_ledger_acl_pg.py'
FIXTURE = 'next/release/ship/install/tests/test_roles_pg.py'
METHODS = (
    'LedgerAcl::test_1_the_exact_api_login_reads_the_ledger',
    'LedgerAcl::test_2_the_api_login_cannot_change_the_ledger_or_read_any_other_core_table',
    'LedgerAcl::test_3_probe_the_request_group_and_a_stranger_are_denied',
    'LedgerAcl::test_4_nothing_is_granted_to_public_and_the_exact_privilege_set_is_known',
    'LedgerAcl::test_5_rerunning_post_and_closing_the_finite_logins_keeps_exactly_that_access',
    'LedgerAcl::test_6_the_ledger_step_changes_exactly_two_acls_and_adds_only_the_api_login',
    'LedgerAcl::test_7_probe_has_no_core_usage_the_request_group_has_no_ledger_select_and_baseline_usage_remains',
    'ProvisionStepsPg::test_1_pre_creates_the_four_logins_with_policy_flags_and_scram',
    'ProvisionStepsPg::test_2_the_bound_secret_never_appears_in_statement_text',
    'ProvisionStepsPg::test_3_a_short_secret_creates_nothing_and_leaves_no_password_setting',
    'ProvisionStepsPg::test_4_post_ledger_and_close_steps_apply_and_give_the_same_acl_as_the_scripts',
)
NODES = [SUITE + '::' + name for name in METHODS]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


worker = load('ledger_pg_worker', '.github/scripts/pytest_pg_worker.py')
offline = load('ledger_offline', '.github/scripts/pytest_next_offline.py')


def sha(root, path):
    return hashlib.sha256((root / path).read_bytes()).hexdigest()


def evidence(root=ROOT):
    return dict(kind='native_postgres_fixture', source=SUITE,
                sha256=sha(root, SUITE), symbol='LedgerAcl.setUpClass',
                fixture=dict(source=FIXTURE, sha256=sha(root, FIXTURE), symbol='Cluster.__init__'))


def packet():
    return dict(suite=SUITE, source_sha256=sha(ROOT, SUITE), expected=NODES[:],
                selected=NODES[:], collected=NODES[:], deselected=[], pytest_exit=0,
                gate_exit=0, declared_unavailable=[], retired=[], excluded_methods=[],
                reports=[dict(nodeid=node, when=phase, outcome='passed', wasxfail=False)
                         for node in NODES for phase in ('setup', 'call', 'teardown')])


class LedgerHome(unittest.TestCase):
    def test_eleven_exact_ledger_nodes_and_all_phases_are_required(self):
        self.assertEqual(getattr(worker, 'NATIVE_LEDGER_NODES', None), NODES)
        check = worker.native_roles_result
        clean = dict(ok=True, errors=[], residual=[])
        self.assertTrue(check(packet(), 0, sha(ROOT, SUITE), clean, suite=SUITE))
        for fault in ('skip', 'xfail', 'setup', 'teardown', 'missing', 'extra', 'duplicate',
                      'deselected', 'collected', 'source', 'process', 'cleanup', 'residual'):
            with self.subTest(fault=fault):
                value = copy.deepcopy(packet()); cleanup = copy.deepcopy(clean); code = 0
                if fault == 'skip': value['reports'][1]['outcome'] = 'skipped'
                elif fault == 'xfail': value['reports'][1]['wasxfail'] = True
                elif fault == 'setup': value['reports'][0]['outcome'] = 'failed'
                elif fault == 'teardown': value['reports'][2]['outcome'] = 'failed'
                elif fault == 'missing': value['reports'].pop()
                elif fault == 'extra': value['reports'].append(dict(value['reports'][0], nodeid='foreign'))
                elif fault == 'duplicate': value['reports'].append(value['reports'][0])
                elif fault == 'deselected': value['deselected'] = NODES[:1]
                elif fault == 'collected': value['collected'].pop()
                elif fault == 'source': value['source_sha256'] = '0' * 64
                elif fault == 'process': code = 124
                elif fault == 'cleanup': cleanup['ok'] = False
                else: cleanup['residual'] = ['owned/postmaster.pid']
                self.assertFalse(check(value, code, sha(ROOT, SUITE), cleanup, suite=SUITE))

    def test_both_suite_and_imported_cluster_are_pinned(self):
        self.assertTrue(offline.fixture_evidence(ROOT, SUITE, evidence()))
        for which in ('sha256', 'fixture'):
            value = evidence()
            if which == 'fixture': value[which]['sha256'] = '0' * 64
            else: value[which] = '0' * 64
            self.assertFalse(offline.fixture_evidence(ROOT, SUITE, value))
        self.assertFalse(offline.fixture_evidence(ROOT, FIXTURE, evidence()))

    def test_repin_cannot_turn_dead_or_missing_cluster_use_into_native_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for relative in (SUITE, FIXTURE, '.github/scripts/pytest_pg_worker.py'):
                path = root / relative; path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes((ROOT / relative).read_bytes())
            path = root / SUITE; original = path.read_text()
            self.assertEqual(original.count('        cls.c = P.Cluster()'), 2)
            path.write_text(original.replace('        cls.c = P.Cluster()',
                '        if False:\n            cls.c = P.Cluster()', 1))
            ast.parse(path.read_text())
            self.assertFalse(offline.fixture_evidence(root, SUITE, evidence(root)))
            path.write_text(original.replace('import test_roles_pg as P', 'import unrelated_fixture as P'))
            self.assertFalse(offline.fixture_evidence(root, SUITE, evidence(root)))
            path.write_text(original)
            fixture = root / FIXTURE; text = fixture.read_text()
            fixture.write_text(text.replace("self.bin / 'initdb'", "self.bin / 'echo'"))
            self.assertFalse(offline.fixture_evidence(root, SUITE, evidence(root)))

    def test_new_or_removed_ledger_node_refuses_even_after_repin(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for relative in (SUITE, FIXTURE, '.github/scripts/pytest_pg_worker.py'):
                path = root / relative; path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes((ROOT / relative).read_bytes())
            path = root / SUITE
            path.write_text(path.read_text().replace('def test_5_rerunning_post_', 'def removed_5_rerunning_post_'))
            self.assertFalse(offline.fixture_evidence(root, SUITE, evidence(root)))

    def test_three_pure_suites_run_and_ledger_has_a_named_native_home(self):
        profile = json.loads(PROFILE.read_text())
        for name in ('test_catalog.py', 'test_package.py', 'test_provision_steps.py'):
            self.assertIn('next/release/ship/install/tests/' + name, profile['run'])
        self.assertIn('next/tests/ci_gates/tests/test_ledger_home.py', profile['run'])
        self.assertNotIn(SUITE, profile['run'])
        rows = [row for row in profile['excluded'] if row['path'] == SUITE]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['evidence'], evidence())
        self.assertIn('installer-ledger', rows[0]['reason'])

    def test_hosted_ledger_step_is_required_bounded_and_receipted(self):
        source = (ROOT / '.github/workflows/ci.yml').read_text()
        self.assertIn('installer ledger (eleven exact methods; native PostgreSQL)', source)
        step = source.split('installer ledger (eleven exact methods; native PostgreSQL)', 1)[1].split('      - ', 1)[0]
        for required in ('--mode installer-ledger', '--pg-bin', '--site', 'pg_config --bindir',
                         'MemoryMax=1G', 'MemorySwapMax=0', 'CPUQuota=200%', '!cancelled()'):
            self.assertIn(required, step)
        self.assertNotIn('continue-on-error', step)
        self.assertIn('cortex-installer-ledger-pg/**/*.json', source)
        self.assertIn('cortex-installer-ledger-pg/**/*.txt', source)

    def test_ledger_environment_has_explicit_bounded_migrations_and_no_inherited_credentials(self):
        environment = getattr(worker, 'native_environment', None)
        self.assertTrue(callable(environment), 'native fixture environment is not inspectable')
        with patch.dict(os.environ, {'CORTEX_ACL_THROUGH': 'invented', 'PGPASSWORD': 'foreign',
                                     'CORTEX_PG_DSN': 'foreign', 'PYTEST_ADDOPTS': '-x'}):
            env = environment(Path('/pg'), Path('/site'), Path('/home'), Path('/tmp'), SUITE)
        self.assertEqual(env['CORTEX_ACL_THROUGH'], 'api-replay-0004')
        self.assertEqual(env['CORTEX_PG_BIN'], '/pg')
        for key in ('PGPASSWORD', 'CORTEX_PG_DSN', 'PYTEST_ADDOPTS'):
            self.assertNotIn(key, env)
        ordinary = environment(Path('/pg'), Path('/site'), Path('/home'), Path('/tmp'), FIXTURE)
        self.assertNotIn('CORTEX_ACL_THROUGH', ordinary)


if __name__ == '__main__':
    unittest.main()
