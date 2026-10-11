"""Accepted request scope must hold during operations, including transient tampering."""
import hashlib
import unittest
from uuid import UUID

import test_authorization as fixture
from cortex_core.auth import AuthError


class TransactionBindingTests(unittest.TestCase):
    reset = fixture.AuthorizationTests.reset
    setUp = fixture.AuthorizationTests.setUp
    auth = fixture.AuthorizationTests.auth

    def test_temporary_action_elevation_restored_before_exit_cannot_commit(self):
        with self.assertRaises(AuthError):
            with self.auth(fixture.WRITE_A, action='read'):
                self.request.execute("SELECT set_config('cortex.action','write',true)")
                self.request.execute('INSERT INTO core.payloads(tenant_id,project_id,id,body,sha256) VALUES (%s,%s,%s,%s,%s)',
                                     (*self.scope,fixture.uid(40),self.body,self.digest))
                self.request.execute("SELECT set_config('cortex.action','read',true)")
        self.assertEqual(self.admin.execute('SELECT count(*) FROM core.payloads WHERE id=%s',
                                          (fixture.uid(40),)).fetchone()[0],0)

    def test_temporary_granted_project_restored_before_exit_cannot_read(self):
        project=fixture.uid(9)
        self.admin.execute("INSERT INTO core.projects(tenant_id,id,name) VALUES (%s,%s,'same-tenant-second')",
                           (fixture.uid(2),project))
        self.admin.execute('INSERT INTO auth.project_grants VALUES (%s,%s,%s,%s)',
                           (fixture.uid(2),project,fixture.uid(10),['read','write']))
        self.admin.execute('INSERT INTO core.payloads(tenant_id,project_id,id,body,sha256) VALUES (%s,%s,%s,%s,%s)',
                           (fixture.uid(2),project,fixture.uid(5),self.body,self.digest))
        with self.admin.transaction():
            self.admin.execute("INSERT INTO core.records VALUES (%s,%s,%s,'memory',1,false)",
                               (fixture.uid(2),project,fixture.uid(41)))
            self.admin.execute('INSERT INTO core.record_revisions(tenant_id,project_id,record_id,revision,payload_ref,tombstone) VALUES (%s,%s,%s,1,%s,false)',
                               (fixture.uid(2),project,fixture.uid(41),fixture.uid(5)))
        with self.auth(fixture.WRITE_A):
            self.request.execute("SELECT set_config('cortex.project_id',%s,true)",(project,))
            seen=self.request.execute('SELECT count(*) FROM core.records WHERE project_id=%s',(project,)).fetchone()[0]
            self.request.execute("SELECT set_config('cortex.project_id',%s,true)",(fixture.uid(3),))
        self.assertEqual(seen,0)

    def test_temporary_other_credential_restored_before_exit_cannot_read(self):
        with self.auth():
            self.request.execute("SELECT set_config('cortex.credential_digest',%s,true)",
                                 (hashlib.sha256(fixture.READ_B).hexdigest(),))
            self.request.execute("SELECT set_config('cortex.installation_id',%s,true)",(fixture.uid(20),))
            seen=self.request.execute('SELECT count(*) FROM core.records WHERE tenant_id=%s',
                                      (fixture.uid(21),)).fetchone()[0]
            self.request.execute("SELECT set_config('cortex.credential_digest',%s,true)",
                                 (hashlib.sha256(fixture.READ_A).hexdigest(),))
            self.request.execute("SELECT set_config('cortex.installation_id',%s,true)",(fixture.uid(1),))
        self.assertEqual(seen,0)

    def test_null_action_resolver_refuses_owner_scope(self):
        row=self.request.execute('SELECT * FROM auth.resolve_scope(%s,%s,%s,%s)',
                                 (hashlib.sha256(fixture.OWNER_A).hexdigest(),
                                  UUID(fixture.uid(1)),UUID(fixture.uid(3)),None)).fetchone()
        self.assertIsNone(row)

    def test_missing_action_direct_rls_refuses_owner_scope(self):
        for name,value in [('credential_digest',hashlib.sha256(fixture.OWNER_A).hexdigest()),
                           ('installation_id',fixture.uid(1)),('project_id',fixture.uid(3))]:
            self.request.execute('SELECT set_config(%s,%s,false)',('cortex.'+name,value))
        self.assertIsNone(self.request.execute("SELECT current_setting('cortex.action',true)").fetchone()[0])
        self.assertEqual(self.request.execute('SELECT count(*) FROM core.records').fetchone()[0],0)
