"""Real missing-loader failures, never a fake psycopg module or SQL action."""
from contextlib import contextmanager
import importlib
import importlib.abc
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import UUID
from cortex_core.install import lifecycle,owner
from pgb2_preimages import PGPreimageFixture

@contextmanager
def missing_driver(error_name='psycopg'):
    saved={k:v for k,v in sys.modules.items() if k=='psycopg' or k.startswith('psycopg.')}
    for k in saved:del sys.modules[k]
    class Missing(importlib.abc.MetaPathFinder):
        def find_spec(self,fullname,path=None,target=None):
            if fullname=='psycopg' or fullname.startswith('psycopg.'):
                raise ModuleNotFoundError('missing loader dependency',name=error_name)
    finder=Missing();sys.meta_path.insert(0,finder)
    try:yield
    finally:
        sys.meta_path.remove(finder)
        for k in list(sys.modules):
            if k=='psycopg' or k.startswith('psycopg.'):del sys.modules[k]
        sys.modules.update(saved)

class ImportBoundary(unittest.TestCase):
    def arguments(self):
        root=tempfile.TemporaryDirectory();self.addCleanup(root.cleanup);f=PGPreimageFixture(self,root.name)
        matrix=json.loads((Path(__file__).resolve().parents[1]/'runtime_grants/ratified_matrix.json').read_bytes())
        calls=[]
        class NoConnection:
            def __getattr__(self,name):calls.append(name);raise AssertionError('connection access before driver availability')
        conn=NoConnection()
        return dict(history=f.history,identity=f.identity,connection=conn,recovery_connection=conn,
            role_names={k:'source_'+k for k in matrix['roles']},installation_id=UUID(f.identity['installation_id']),
            embedding_job_kind='fixture',policy=matrix,materials=None,backends={}),calls
    def test_absent_driver_sql_boundary_is_typed_before_connection_access(self):
        api=importlib.import_module('cortex_core.runtime.role_grants');args,calls=self.arguments()
        with missing_driver():
            try:api._RoleAdapter(**args)
            except owner.OwnerRefusal as error:self.assertEqual(error.detail,'backend_unavailable')
            except Exception as error:self.fail('PG_B2_MISSING_DRIVER_NOT_TYPED:'+repr(error))
            else:self.fail('PG_B2_MISSING_DRIVER_SQL_CONSTRUCTOR_ADMITTED')
        self.assertEqual(calls,[])
    def test_transitive_import_failure_is_preserved_before_connection_access(self):
        api=importlib.import_module('cortex_core.runtime.role_grants');args,calls=self.arguments()
        with missing_driver('pgb2_transitive_dependency'):
            try:api._RoleAdapter(**args)
            except ModuleNotFoundError as error:self.assertEqual(error.name,'pgb2_transitive_dependency')
            except Exception as error:self.fail('PG_B2_TRANSITIVE_IMPORT_MASKED:'+repr(error))
            else:self.fail('PG_B2_TRANSITIVE_IMPORT_NOT_OBSERVED')
        self.assertEqual(calls,[])
    def test_public_owner_composition_does_not_mask_transitive_import_failure(self):
        api=importlib.import_module('cortex_core.runtime.role_grants');error=ModuleNotFoundError('broken dependency',name='pgb2_transitive_dependency')
        with patch.object(lifecycle,'_installed_lifecycle',side_effect=error):
            try:api.prepare_runtime_roles(None,role_names={},operation_id='foreign')
            except ModuleNotFoundError as caught:self.assertIs(caught,error)
            except Exception as caught:self.fail('PG_B2_PUBLIC_TRANSITIVE_IMPORT_MASKED:'+repr(caught))
            else:self.fail('transitive error lost')
