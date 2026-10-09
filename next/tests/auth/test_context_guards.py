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
