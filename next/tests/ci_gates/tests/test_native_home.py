"""Synthetic phase receipts have zero PostgreSQL or installed-candidate credit."""
import ast
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
import yaml

ROOT = Path(__file__).resolve().parents[4]
SUITE = 'next/release/ship/install/tests/test_roles_pg.py'
METHODS = (
    'RolesInDatabase::test_1_pre_creates_exactly_the_approved_logins_with_exact_flags_and_scram',
    'RolesInDatabase::test_2_pre_leaves_the_two_groups_to_the_unchanged_migrations',
    'RolesInDatabase::test_3_post_grants_exactly_one_set_only_membership',
    'RolesInDatabase::test_4_post_refuses_an_unexpected_membership',
    'RolesInDatabase::test_5_phases_rerun_to_the_same_state',
    'RolesInDatabase::test_6_close_closes_only_the_finite_logins',
    'RolesInDatabase::test_7_a_closed_login_cannot_connect_and_a_service_login_still_can',
    'RolesInDatabase::test_8_close_reruns_cleanly',
    'SecretGuard::test_a_missing_or_short_secret_creates_nothing',
)
NODES = [SUITE + '::' + name for name in METHODS]
PROFILE = ROOT / '.github/workflows/ci.yml'


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


worker = load('day2_pg_worker', '.github/scripts/pytest_pg_worker.py')
offline = load('day2_offline', '.github/scripts/pytest_next_offline.py')


def evidence(root=ROOT):
    return dict(kind='native_postgres_fixture', source=SUITE,
                sha256=hashlib.sha256((root / SUITE).read_bytes()).hexdigest(),
                symbol='Cluster.__init__')


def packet():
    return dict(suite=SUITE, source_sha256=evidence()['sha256'], expected=NODES[:],
                selected=NODES[:], collected=NODES[:], deselected=[], pytest_exit=0,
                gate_exit=0, declared_unavailable=[], retired=[], excluded_methods=[],
                reports=[dict(nodeid=node, when=phase, outcome='passed', wasxfail=False)
                         for node in NODES for phase in ('setup', 'call', 'teardown')])


class NativeHome(unittest.TestCase):
    def assert_dual_python_exact_head(self, workflow):
        jobs = workflow['jobs']
        names = []
        for name, receipts in (
                ('pytest-pg', ['u13-pg-receipts', 'pytest-pg-receipts']),
                ('pytest-offline', ['pytest-offline-receipts'])):
            job = jobs[name]
            self.assertEqual(job.get('strategy'), dict(**{'fail-fast': False},
                matrix=dict(python=['3.13.7', '3.14.0'])), 'exact Python matrix')
            setup = [step for step in job['steps']
                     if step.get('uses', '').startswith('actions/setup-python@')]
            self.assertEqual(len(setup), 1)
            self.assertEqual(setup[0]['with']['python-version'], '${{ matrix.python }}',
                             'matrix Python binding')
            uploads = [step['with']['name'] for step in job['steps']
                       if step.get('uses', '').startswith('actions/upload-artifact@')]
            self.assertEqual(uploads, [name+'-${{ matrix.python }}' for name in receipts],
                             'distinct Python receipt names')
            names.extend(value.replace('${{ matrix.python }}', version)
                         for value in uploads for version in ('3.13.7', '3.14.0'))
        self.assertEqual(len(names), len(set(names)), 'colliding Python receipts')
        self.assertEqual(jobs['launcher-contract']['strategy'],
                         dict(matrix=dict(runtime=['node', 'bun'])), 'single launcher matrix')
        for name in ('pytest-pg', 'pytest-offline', 'launcher-contract'):
            checkout = [step for step in jobs[name]['steps']
                        if step.get('uses', '').startswith('actions/checkout@')]
            self.assertEqual(len(checkout), 1)
            self.assertEqual(checkout[0].get('with', {}).get('ref'),
                '${{ github.event.pull_request.head.sha || github.sha }}', 'exact PR head')
            self.assertIs(checkout[0]['with'].get('persist-credentials'), False,
                          'checkout credentials disabled')

    def test_dual_python_jobs_use_exact_head_and_distinct_receipts(self):
        self.assert_dual_python_exact_head(yaml.safe_load(PROFILE.read_text()))

    def test_dual_python_contract_refuses_missing_versions_refs_and_collisions(self):
        original = yaml.safe_load(PROFILE.read_text())
        for name in ('pytest-pg', 'pytest-offline'):
            for missing in ('3.13.7', '3.14.0'):
                with self.subTest(job=name, missing=missing):
                    bad = copy.deepcopy(original)
                    bad['jobs'][name]['strategy']['matrix']['python'].remove(missing)
                    with self.assertRaisesRegex(AssertionError, 'exact Python matrix'):
                        self.assert_dual_python_exact_head(bad)
            with self.subTest(job=name, fail_fast=True):
                bad = copy.deepcopy(original)
                bad['jobs'][name]['strategy']['fail-fast'] = True
                with self.assertRaisesRegex(AssertionError, 'exact Python matrix'):
                    self.assert_dual_python_exact_head(bad)
        for name in ('pytest-pg', 'pytest-offline', 'launcher-contract'):
            for field, value, reason in (
                    ('ref', '${{ github.sha }}', 'exact PR head'),
                    ('persist-credentials', True, 'checkout credentials disabled')):
                with self.subTest(job=name, field=field):
                    bad = copy.deepcopy(original)
                    checkout = next(step for step in bad['jobs'][name]['steps']
                                    if step.get('uses', '').startswith('actions/checkout@'))
                    checkout['with'][field] = value
                    with self.assertRaisesRegex(AssertionError, reason):
                        self.assert_dual_python_exact_head(bad)
        for name in ('pytest-pg', 'pytest-offline'):
            for index, step in enumerate(original['jobs'][name]['steps']):
                if not step.get('uses', '').startswith('actions/upload-artifact@'):
                    continue
                with self.subTest(job=name, artifact=index):
                    bad = copy.deepcopy(original)
                    bad['jobs'][name]['steps'][index]['with']['name'] = 'shared-receipts'
                    with self.assertRaisesRegex(AssertionError, 'distinct Python receipt names'):
                        self.assert_dual_python_exact_head(bad)

    def test_deleted_data_directory_cannot_hide_an_owned_postmaster(self):
        scan = getattr(worker, 'owned_native_postmasters', None)
        self.assertTrue(callable(scan), 'removed data directories can hide daemon leakage')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); scratch = root / 'owned'; scratch.mkdir()
            pg = root / 'bin'; pg.mkdir(); proc = root / 'proc'; proc.mkdir()
            for pid, executable, data in ((101, pg / 'postgres', scratch / 'removed/data'),
                                          (102, pg / 'postgres', root / 'foreign'),
                                          (103, root / 'other/postgres', scratch / 'data')):
                item = proc / str(pid); item.mkdir()
                (item / 'cmdline').write_bytes(b'\0'.join(str(x).encode() for x in
                    (executable, '-D', data)) + b'\0')
            self.assertEqual(scan(scratch, pg, proc), [101])

    def test_exact_nine_phase_receipt(self):
        check = getattr(worker, 'native_roles_result', None)
        self.assertTrue(callable(check), 'native roles phase admission is absent')
        self.assertEqual(worker.NATIVE_ROLE_NODES, NODES)
        self.assertTrue(check(packet(), 0, evidence()['sha256'], dict(ok=True, errors=[], residual=[])))
        for fault in ('skip', 'xfail', 'setup', 'teardown', 'missing', 'extra', 'duplicate',
                      'deselected', 'collected', 'source', 'process', 'cleanup', 'residual'):
            with self.subTest(fault=fault):
                value = copy.deepcopy(packet()); cleanup = dict(ok=True, errors=[], residual=[]); code = 0
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
                self.assertFalse(check(value, code, evidence()['sha256'], cleanup))

    def test_native_fixture_witness_is_pinned_and_semantic(self):
        self.assertTrue(offline.fixture_evidence(ROOT, SUITE, evidence()))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for relative in (SUITE, '.github/scripts/pytest_pg_worker.py'):
                path = root / relative; path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes((ROOT / relative).read_bytes())
            path = root / SUITE; original = path.read_text()
            path.write_text(original + '\n# source drift\n')
            self.assertFalse(offline.fixture_evidence(root, SUITE, evidence()))
            self.assertEqual(original.count("self.bin / 'initdb'"), 1)
            path.write_text(original.replace("self.bin / 'initdb'", "self.bin / 'echo'"))
            self.assertFalse(offline.fixture_evidence(root, SUITE, evidence(root)))
            path.write_text(original.replace('def test_8_close_reruns_cleanly(', 'def removed_test_8('))
            self.assertFalse(offline.fixture_evidence(root, SUITE, evidence(root)))

    def test_named_native_home_is_outside_offline_run(self):
        profile = json.loads((ROOT / '.github/next-offline-suites.json').read_text())
        self.assertNotIn(SUITE, profile['run'])
        rows = [row for row in profile['excluded'] if row['path'] == SUITE]
        self.assertEqual(len(rows), 1, 'native fixture has no explicit execution home')
        self.assertEqual(rows[0]['evidence'], evidence())
        self.assertIn('installer-roles', rows[0]['reason'])

    def test_dead_cluster_command_cannot_justify_exclusion_after_repin(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for relative in (SUITE, '.github/scripts/pytest_pg_worker.py'):
                path = root / relative; path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes((ROOT / relative).read_bytes())
            path = root / SUITE; original = path.read_text()
            anchor = "        subprocess.run([self.bin / 'initdb'"
            self.assertEqual(original.count(anchor), 1)
            path.write_text(original.replace(anchor, "        if False:\n    " + anchor))
            ast.parse(path.read_text())
            self.assertFalse(offline.fixture_evidence(root, SUITE, evidence(root)))

    def test_hosted_native_step_is_required_and_bounded(self):
        source = PROFILE.read_text()
        self.assertIn('installer roles (nine exact methods; native PostgreSQL)', source)
        step = source.split('installer roles (nine exact methods; native PostgreSQL)', 1)[1].split('      - ', 1)[0]
        for required in ('--mode installer-roles', '--pg-bin', '--site', 'pg_config --bindir',
                         'MemoryMax=1G', 'MemorySwapMax=0', 'CPUQuota=200%'):
            self.assertIn(required, step)
        self.assertNotIn('continue-on-error', step)
        self.assertNotIn('if:', step)
        self.assertIn('cortex-installer-roles-pg/**/*.json', source)


if __name__ == '__main__':
    unittest.main()
