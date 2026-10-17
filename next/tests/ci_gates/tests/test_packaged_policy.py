"""Source closure uses the actual ship packager; no policy fixture or install."""
import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[4]
spec=importlib.util.spec_from_file_location('packaged_policy_offline',ROOT/'.github/scripts/pytest_next_offline.py')
offline=importlib.util.module_from_spec(spec);sys.modules[spec.name]=offline;spec.loader.exec_module(offline)
class PackagedPolicy(unittest.TestCase):
    def stage(self,destination):
        function=getattr(offline,'stage_ship_closure',None)
        self.assertTrue(callable(function),'actual ship package closure must be staged by the gate')
        return function(ROOT,destination,{k:os.environ[k] for k in ('PATH','LANG','LC_ALL') if k in os.environ})
    def test_actual_package_closure_contains_only_source_identical_ship_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            destination=Path(tmp)/'closure';packet=self.stage(destination)
            self.assertEqual(packet['qualification'],'SOURCE_COPIED_POLICY_ONLY')
            for source,target in (
                ('next/release/ship/install/roles.py','cortex_ship/install/roles.py'),
                ('next/release/ship/install/provision.py','cortex_ship/install/provision.py'),
                ('next/contracts/runtime/runtime-operations-v1.json','cortex_ship/install/runtime-operations-v1.json')):
                self.assertEqual((destination/target).read_bytes(),(ROOT/source).read_bytes())
                self.assertEqual(packet['source_sha256'][source],hashlib.sha256((ROOT/source).read_bytes()).hexdigest())
    def test_copied_policy_drift_refuses_even_with_a_rewritten_packager_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            destination=Path(tmp)/'closure';original=offline.subprocess.run
            def drift(*args,**kwargs):
                result=original(*args,**kwargs)
                path=destination/'cortex_ship/install/roles.py'
                path.write_bytes(path.read_bytes()+b'\n# unreviewed copied policy drift\n')
                receipt=destination/'cortex_ship/receipt.json';value=json.loads(receipt.read_bytes())
                for row in value['files']:
                    if row['path']=='cortex_ship/install/roles.py':row['sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
                receipt.write_text(json.dumps(value))
                return result
            function=getattr(offline,'stage_ship_closure',None)
            self.assertTrue(callable(function),'actual ship package closure must be staged by the gate')
            with patch.object(offline.subprocess,'run',side_effect=drift):
                with self.assertRaises(ValueError):self.stage(destination)
if __name__=='__main__':unittest.main()
