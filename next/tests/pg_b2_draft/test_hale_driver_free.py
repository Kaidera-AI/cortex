"""Missing-driver import requirement, with the real loader blocked externally."""
import importlib
import unittest
from cortex_core.install import owner

class DriverFree(unittest.TestCase):
    def test_source_import_and_default_owner_refusal_do_not_require_psycopg(self):
        try:
            api=importlib.import_module('cortex_core.runtime.role_grants')
        except ModuleNotFoundError as error:
            self.fail('PG_B2_DRIVER_FREE_IMPORT_REQUIRES_PSYCOPG:'+repr(error))
        calls=[]
        class NoConnection:
            def __getattr__(self,name):calls.append(name);raise AssertionError('connection access before owner refusal')
        with self.assertRaises(owner.OwnerRefusal):api.prepare_runtime_roles(NoConnection(),role_names={},operation_id='foreign')
        self.assertEqual(calls,[])
