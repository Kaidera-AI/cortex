"""Current Day-2 source homes; no native or installed acceptance."""
import hashlib
import json
from pathlib import Path
import unittest
ROOT=Path(__file__).resolve().parents[4]
V2='next/modules/cli-v2/tests/'
SCHEMA='next/src/cortex_core/schema/tests/'
RUNTIME='next/src/cortex_core/runtime/tests/'
class CurrentHome(unittest.TestCase):
    def manifest(self):return json.loads((ROOT/'.github/next-offline-suites.json').read_bytes())
    def test_versioned_transport_and_actual_health_are_loopback_only(self):
        value=self.manifest()
        for name in ('admission','composition','processes','projection','results'):
            self.assertIn(V2+'test_transport_'+name+'.py',value['loopback'])
        for name in ('test_namespace.py','test_health_join.py'):
            self.assertIn(V2+name,value['loopback'])
        self.assertFalse(any(p.startswith('packages/cli/tests/test_transport') for p in value['loopback']))
        self.assertFalse(set(value['loopback']) & set(value['run']))
    def test_latest_health_contract_has_explicit_loopback_home(self):
        self.assertIn(V2+'test_health_contract.py',self.manifest()['loopback'])
    def test_seed_caller_has_explicit_loopback_home(self):
        self.assertIn(SCHEMA+'test_seed_operator.py',self.manifest()['loopback'])
        self.assertIn(SCHEMA+'test_seed_owner_contract.py',self.manifest()['run'])
    def test_current_runtime_bindings_and_lifecycle_are_admitted(self):
        for name in ('test_catalog_binding.py','test_lifecycle.py','test_services.py','test_status_binding.py'):
            self.assertIn(RUNTIME+name,self.manifest()['run'])
    def test_current_provisioner_and_this_control_are_admitted(self):
        for name in ('next/release/ship/install/tests/test_provision.py',
                     'next/tests/ci_gates/tests/test_current_home.py'):
            self.assertIn(name,self.manifest()['run'])
    def test_legacy_cli_and_regression_are_exact_unchanged_baseline(self):
        for name,digest in {
            'packages/cli/_cortex_api.sh':'d78ad0c87ffdf286c69b2485d3224186decc81b37608a4ae92fe40806d84f636',
            'packages/api/tests/test_mcp_live.py':'687377a57488802a50fa383176aa4bae1dd51ebc095b15d49e16236df21fb8f3',
        }.items():
            self.assertEqual(hashlib.sha256((ROOT/name).read_bytes()).hexdigest(),digest)
if __name__=='__main__':unittest.main()
