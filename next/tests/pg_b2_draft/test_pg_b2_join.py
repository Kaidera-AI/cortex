"""PG-B2 candidate join controls; no frozen PG-O/native qualification credit."""
import copy
import importlib
import json
from pathlib import Path
import tempfile
import unittest
from cortex_core.install import owner
from cortex_core.release.contracts import thaw
from pgb2_preimages import PGPreimageFixture, FIELDS

class CandidateJoin(unittest.TestCase):
    def api(self):
        path=Path(__file__).resolve().parents[2]/'src/cortex_core/runtime/role_grants.py'
        self.assertTrue(path.is_file(),'PG_B2_SURFACE_ABSENT: finite owner/client join')
        return importlib.import_module('cortex_core.runtime.role_grants')

    def test_existing_finite_callable_shape(self):
        api=self.api()
        for name in ('prepare_runtime_roles','bind_runtime_roles','close_finite_roles','restore_runtime_roles'):
            self.assertTrue(callable(getattr(api,name,None)),name)

    def test_default_binding_refuses_before_connection_access(self):
        api=self.api()
        calls=[]
        class Unavailable:
            def __getattr__(self,key):calls.append(key);raise AssertionError('connection accessed before fixed owner')
        with self.assertRaises(owner.OwnerRefusal):
            api.prepare_runtime_roles(Unavailable(),role_names={},operation_id='foreign')
        self.assertEqual(calls,[])

    def test_candidate_loads_exact_originals_after_live_bytes_change_and_reload(self):
        api=self.api()
        with tempfile.TemporaryDirectory() as root:
            f=PGPreimageFixture(self,root);context=f.consume();f.reload()
            actual=api._originals(f.history,context,expected_identity=f.identity)
            f.assert_restored(actual)
            self.assertEqual(set(actual),set(FIELDS))

    def test_foreign_operation_refuses_before_original_access(self):
        api=self.api()
        with tempfile.TemporaryDirectory() as root:
            f=PGPreimageFixture(self,root);context=f.consume()
            foreign={**f.identity,'operation_id':'00000000-0000-4000-8000-000000000099'}
            with self.assertRaises(owner.OwnerRefusal):api._originals(f.history,context,expected_identity=foreign)

    def test_completed_dispatch_context_is_not_reusable(self):
        api=self.api()
        with tempfile.TemporaryDirectory() as root:
            f=PGPreimageFixture(self,root);context=f.consume()
            f.api._record_result(f.history,f.consumed,f.helper.measurement(f.data,f.current))
            with self.assertRaises(owner.OwnerRefusal):api._originals(f.history,context,expected_identity=f.identity)

    def damage(self,field,tamper):
        api=self.api()
        with tempfile.TemporaryDirectory() as root:
            f=PGPreimageFixture(self,root);context=f.consume()
            bundle=json.loads(f.history.read(f.preimages_ref));ref=f.preimages_ref if field=='bundle' else bundle['originals'][field]
            path=f.history.root/'facts'/(ref['sha256']+'.json');original=path.read_bytes()
            if tamper:
                changed=bytearray(original);changed[0]^=1;path.write_bytes(changed)
            else:path.unlink()
            with self.assertRaises(owner.OwnerRefusal) as caught:api._originals(f.history,context,expected_identity=f.identity)
            self.assertEqual(caught.exception.detail,'journal_invalid')
            path.write_bytes(original);path.chmod(0o600);f.reload()
            f.assert_restored(api._originals(f.history,context,expected_identity=f.identity))

for field in ('bundle',*FIELDS):
    for tamper in (False,True):
        def method(self,field=field,tamper=tamper):self.damage(field,tamper)
        setattr(CandidateJoin,'test_'+('tampered_' if tamper else 'missing_')+field,method)
