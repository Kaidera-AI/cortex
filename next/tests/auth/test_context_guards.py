"""Exercise independent exit and session cleanup guards without other-policy masking."""
import hashlib
import unittest

import test_authorization as fixture
from cortex_core.auth import AuthError


class ContextGuardTests(unittest.TestCase):
    reset=fixture.AuthorizationTests.reset
    setUp=fixture.AuthorizationTests.setUp
    auth=fixture.AuthorizationTests.auth

    def test_persistent_action_change_without_write_refuses_acceptance(self):
        with self.assertRaises(AuthError):
            with self.auth(fixture.WRITE_A,action='read'):
                self.request.execute("SELECT set_config('cortex.action','write',true)")

    def test_session_credential_context_is_cleared_after_commit(self):
        with self.auth():
            self.request.execute("SELECT set_config('cortex.credential_digest',%s,false)",
                                 (hashlib.sha256(fixture.READ_A).hexdigest(),))
        self.assertEqual(self.request.execute("SELECT current_setting('cortex.credential_digest',true)").fetchone()[0],'')

    def test_all_session_scope_fields_clear_on_commit_and_rollback(self):
        names=('credential_digest','installation_id','project_id','action','tenant_id','principal_id','permission_generation')
        for rollback in (False,True):
            with self.subTest(rollback=rollback):
                def request():
                    with self.auth() as scope:
                        values=(hashlib.sha256(fixture.READ_A).hexdigest(),str(scope.installation_id),
                                str(scope.project_id),scope.action,str(scope.tenant_id),
                                str(scope.principal_id),str(scope.permission_generation))
                        for name,value in zip(names,values):
                            self.request.execute('SELECT set_config(%s,%s,false)',('cortex.'+name,value))
                        if rollback:
                            raise RuntimeError('synthetic rollback')
                if rollback:
                    with self.assertRaisesRegex(RuntimeError,'synthetic rollback'):
                        request()
                else:
                    request()
                actual=tuple(self.request.execute('SELECT current_setting(%s,true)',('cortex.'+name,)).fetchone()[0] for name in names)
                self.assertEqual(actual,('',)*7)
