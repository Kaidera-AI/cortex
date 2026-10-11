"""Actual old 28-table assertion RED against the additive C07 migration."""
import json
from pathlib import Path
import sys
import unittest

NEXT = Path('/tmp/next')
ARCHIVE = Path('/tmp/c07-frozen')
sys.path[:0] = [str(NEXT/'tests'),str(NEXT/'tests/auth'),
                str(NEXT/'tests/auth_identity'),str(NEXT/'tests/outbox')]
from test_receipts import AssertionResult, MARKER

files = {
    NEXT/'tests/auth/test_authorization.py': ARCHIVE/'auth-test_authorization.py',
    NEXT/'tests/auth_identity/test_identity.py': ARCHIVE/'auth_identity-test_identity.py',
    NEXT/'tests/outbox/test_outbox.py': ARCHIVE/'outbox-test_outbox.py',
}
originals = {path:path.read_bytes() for path in files}
try:
    for path,archive in files.items():
        assert archive.is_file() and archive.read_bytes()!=originals[path]
        path.write_bytes(archive.read_bytes())
    names = [
        'test_authorization.AuthorizationTests.test_all_business_tables_force_rls_private_functions_stay_private',
        'test_identity.IdentityTests.test_additive_kind_roles_defaults_private_memberships_and_frozen_table_count',
        'test_outbox.OutboxTests.test_additive_manifest_and_frozen_business_table_boundary',
    ]
    tests = unittest.defaultTestLoader.loadTestsFromNames(names)
    assert tests.countTestCases()==3
    result = unittest.TextTestRunner(verbosity=2,resultclass=AssertionResult).run(tests)
    value = {'tests_run':result.testsRun,'failures':result.assertions,
             'errors':[{'id':test.id(),'traceback':traceback} for test,traceback in result.errors]}
finally:
    for path,body in originals.items():
        path.write_bytes(body)
    assert all(path.read_bytes()==body for path,body in originals.items()), 'guard source not restored'
print(MARKER+json.dumps(value),flush=True)
raise SystemExit(0 if result.wasSuccessful() else 1)
