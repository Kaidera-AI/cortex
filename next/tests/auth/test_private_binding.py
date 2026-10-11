"""Private transaction state cannot be replaced, rebound or treated as a wildcard."""
import hashlib
import unittest
from uuid import UUID

import psycopg
import test_authorization as fixture
from cortex_core.auth import AuthError


class PrivateBindingTests(unittest.TestCase):
    reset=fixture.AuthorizationTests.reset
    setUp=fixture.AuthorizationTests.setUp
    auth=fixture.AuthorizationTests.auth

    def bind(self,credential=fixture.WRITE_A,action='write'):
        return self.request.execute('SELECT auth.bind_scope(%s,%s,%s,%s)',
            (hashlib.sha256(credential).hexdigest(),UUID(fixture.uid(1)),UUID(fixture.uid(3)),action)).fetchone()[0]

    def test_discard_temp_and_session_unlock_cannot_rebind(self):
        with self.auth(fixture.WRITE_A,action='read'):
            self.request.execute('DISCARD TEMP')
            self.request.execute('SELECT pg_advisory_unlock_all()')
            self.assertFalse(self.bind())
            self.assertEqual(self.request.execute('SELECT count(*) FROM core.records').fetchone()[0],0)
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                with self.request.transaction():
                    self.request.execute('INSERT INTO core.payloads(tenant_id,project_id,id,body,sha256) VALUES (%s,%s,%s,%s,%s)',
                        (*self.scope,fixture.uid(40),self.body,self.digest))
        self.assertEqual(self.admin.execute('SELECT count(*) FROM core.payloads WHERE id=%s',(fixture.uid(40),)).fetchone()[0],0)

    def test_duplicate_binding_is_refused_without_widening_original_scope(self):
        with self.auth():
            self.assertFalse(self.bind())
            self.assertEqual(self.request.execute('SELECT count(*) FROM core.records').fetchone()[0],1)

    def test_caller_owned_context_even_with_verifier_grants_is_refused(self):
        self.request.execute('CREATE TEMP TABLE c04_request_context(digest text NOT NULL,installation uuid NOT NULL,project uuid NOT NULL,action text NOT NULL)')
        self.request.execute('GRANT ALL ON pg_temp.c04_request_context TO "kaidera-runtime-core-verifier"')
        with self.assertRaises(AuthError):
            with self.auth():
                pass
        self.assertEqual(self.request.execute('SELECT count(*) FROM pg_temp.c04_request_context').fetchone()[0],0)

    def test_request_cannot_select_change_or_drop_private_context(self):
        with self.auth():
            for sql in ('SELECT * FROM pg_temp.c04_request_context',
                        "UPDATE pg_temp.c04_request_context SET action='write'",
                        'TRUNCATE pg_temp.c04_request_context',
                        'DROP TABLE pg_temp.c04_request_context',
                        'ALTER TABLE pg_temp.c04_request_context RENAME TO stolen_context'):
                with self.subTest(sql=sql),self.assertRaises(psycopg.errors.InsufficientPrivilege):
                    with self.request.transaction():
                        self.request.execute(sql)
            self.assertEqual(self.request.execute('SELECT count(*) FROM core.records').fetchone()[0],1)

    def test_null_policy_targets_and_need_are_refused(self):
        tenant,project=tuple(UUID(item) for item in self.scope)
        with self.auth(fixture.OWNER_A):
            for arguments in ((None,project,'read'),(tenant,None,'read'),(tenant,project,None)):
                with self.subTest(arguments=arguments):
                    self.assertFalse(self.request.execute('SELECT auth.has_access(%s,%s,%s)',arguments).fetchone()[0])
