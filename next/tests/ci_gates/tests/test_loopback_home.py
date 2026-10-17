"""Required source loopback home; synthetic controls grant no installed credit."""
import copy
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[4]
SUITES = ['next/src/cortex_core/schema/tests/test_seed.py', 'next/modules/cli-v2/tests/test_transport_admission.py', 'next/modules/cli-v2/tests/test_transport_composition.py', 'next/modules/cli-v2/tests/test_transport_processes.py', 'next/modules/cli-v2/tests/test_transport_projection.py', 'next/modules/cli-v2/tests/test_transport_results.py', 'next/modules/cli-v2/tests/test_namespace.py', 'next/modules/cli-v2/tests/test_health_join.py', 'next/src/cortex_core/schema/tests/test_seed_operator.py', 'next/modules/cli-v2/tests/test_health_contract.py']

spec = importlib.util.spec_from_file_location('loopback_home_inventory', ROOT/'.github/scripts/pytest_next_offline.py')
offline = importlib.util.module_from_spec(spec)
spec.loader.exec_module(offline)


class LoopbackHome(unittest.TestCase):
    def test_actual_seed_and_all_published_transports_have_required_home(self):
        manifest = json.loads((ROOT/'.github/next-offline-suites.json').read_text())
        self.assertEqual(manifest.get('loopback'), SUITES)
        self.assertFalse(set(SUITES) & set(manifest['run']))
        self.assertFalse(set(SUITES) & {row['path'] for row in manifest['excluded']})
        checked = offline.inventory(ROOT, manifest)
        self.assertTrue(checked['ok'], checked)
        self.assertEqual(checked.get('loopback'), SUITES)
        self.assertTrue(set(SUITES) <= set(checked['found']))

    def fixture(self, root):
        for relative in ('next/tests/test_plain.py', 'packages/cli/tests/test_transport_probe.py'):
            path=root/relative;path.parent.mkdir(parents=True,exist_ok=True)
            path.write_text('def test_expected(): assert True\n')
        manifest=dict(run=['next/tests/test_plain.py'],excluded=[],loopback=['packages/cli/tests/test_transport_probe.py'])
        return manifest,dict(run=manifest['run'][:],excluded=[])

    def test_new_transport_file_cannot_disappear_from_inventory(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);manifest,base=self.fixture(root)
            (root/'packages/cli/tests/test_transport_forgotten.py').write_text('def test_new(): assert True\n')
            result=offline.inventory(root,manifest,base_manifest=base)
            self.assertFalse(result['ok'])
            self.assertEqual(result['missing'],['packages/cli/tests/test_transport_forgotten.py'])

    def test_loopback_duplicates_stale_and_unrelated_escape_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);manifest,base=self.fixture(root)
            for defect in ('duplicate','stale','escape','ordinary_move'):
                value=copy.deepcopy(manifest)
                if defect=='duplicate':value['loopback']*=2
                elif defect=='stale':value['loopback']=['packages/cli/tests/test_transport_absent.py']
                elif defect=='escape':value['loopback']=['packages/cli/tests/../test_transport_probe.py']
                else:value['loopback']+=value['run'];value['run']=[]
                with self.subTest(defect=defect):self.assertFalse(offline.inventory(root,value,base_manifest=base)['ok'])

    def test_loopback_symlink_cannot_hide_tests(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);manifest,base=self.fixture(root)
            (root/'packages/cli/tests/hidden').symlink_to(root/'next/tests',target_is_directory=True)
            self.assertFalse(offline.inventory(root,manifest,base_manifest=base)['ok'])

    def test_same_phase_oracle_rejects_seed_import_error_and_skips(self):
        node=SUITES[0]+'::SeedFoundationTests::test_installed_binding_requires_private_custody'
        packet=dict(suite=SUITES[0],source_sha256='a'*64,expected=[node],selected=[node],collected=[node],deselected=[],
                    pytest_exit=0,gate_exit=0,declared_unavailable=[],retired=[],excluded_methods=[],
                    reports=[dict(nodeid=node,when=phase,outcome='passed',wasxfail=False) for phase in ('setup','call','teardown')])
        self.assertTrue(offline.receipt_valid(packet,0))
        for phase,outcome in (('call','failed'),('setup','failed'),('teardown','failed'),('call','skipped')):
            value=copy.deepcopy(packet)
            for row in value['reports']:
                if row['when']==phase:row['outcome']=outcome;row['exception_type']='builtins.ModuleNotFoundError'
            self.assertFalse(offline.receipt_valid(value,0))

    def test_required_hosted_home_uses_existing_worker_and_private_loopback(self):
        source=(ROOT/'.github/workflows/next-offline.yml').read_text()
        self.assertIn('required helper and seed source loopback home',source)
        step=source.split('required helper and seed source loopback home',1)[1].split('      - ',1)[0]
        for required in ('--loopback','pytest_next_offline.py','PrivateNetwork=yes','RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6',
                         'NoNewPrivileges=yes','MemoryMax=1G','MemorySwapMax=0','CPUQuota=200%',"!cancelled()"):
            self.assertIn(required,step)
        self.assertNotIn('continue-on-error',step)
        self.assertIn('next-loopback-receipts',source)

    def test_mode_reuses_runner_and_keeps_source_qualification(self):
        source=(ROOT/'.github/scripts/pytest_next_offline.py').read_text()
        self.assertIn("parser.add_argument('--loopback', action='store_true'",source)
        self.assertIn('HOSTED_LOOPBACK_SOURCE_CONTRACTS_ONLY',source)
        self.assertIn("checked['loopback'] if args.loopback else checked['run']",source)
        self.assertIn('NEXT_LOOPBACK_RED',source)


if __name__=='__main__':unittest.main()
