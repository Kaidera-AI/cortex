"""Frozen 2a-ii installer-direct first identity contract, real PostgreSQL RED."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
from uuid import UUID

import psycopg

from core_db_fixture import API, Fixture, REQUEST, uid
from cortex_core.auth import AuthError, authorized
from cortex_core.outbox import Outbox


INSTALLER = 'kaidera-runtime-core-installer'
FUNCTION = 'auth.first_identity(uuid,uuid,uuid,uuid,uuid,text,text)'
SECRET = b'synthetic-first-console-only'
DIGEST = hashlib.sha256(SECRET).hexdigest()
PUBLISHER = b'synthetic-fixture-publisher-only'
PRE_RECEIPT_PROBE = """
CREATE FUNCTION auth.cxgap2aii_before_receipt() RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,auth,core,coordination,pg_temp AS $$
DECLARE payload uuid:=gen_random_uuid(); body bytea;
BEGIN
    body:=convert_to(jsonb_build_object('actor_kind','installer',
      'actor','kaidera-runtime-core-installer','operation','first_identity',
      'principal_id','00000000-0000-4000-8000-000000000033')::text,'UTF8');
    INSERT INTO core.payloads(tenant_id,project_id,id,body,sha256)
      VALUES('00000000-0000-4000-8000-000000000031',
             '00000000-0000-4000-8000-000000000032',payload,body,
             encode(sha256(body),'hex'));
    RETURN coordination.c06_fact('identity',
      '00000000-0000-4000-8000-000000000033',payload);
END;
$$;
ALTER FUNCTION auth.cxgap2aii_before_receipt()
    OWNER TO "kaidera-runtime-core-verifier";
REVOKE ALL ON FUNCTION auth.cxgap2aii_before_receipt() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION auth.cxgap2aii_before_receipt()
    TO "kaidera-runtime-core-installer";
"""


class FirstIdentityPort(Fixture):
    def setUp(self):
        super().setUp()
        self.admin.execute('INSERT INTO core.installations(id) VALUES (%s)', (uid(30),))
        self.admin.execute('INSERT INTO core.tenants(id,installation_id) VALUES (%s,%s)',
                           (uid(31), uid(30)))
        self.admin.execute("INSERT INTO core.projects(tenant_id,id,name) VALUES (%s,%s,'default')",
                           (uid(31), uid(32)))
        self.admin.execute("INSERT INTO core.projects(tenant_id,id,name) VALUES (%s,%s,'side')",
                           (uid(31), uid(37)))

    def function_present(self):
        self.assertIsNotNone(self.admin.execute('SELECT to_regprocedure(%s)',
                                                (FUNCTION,)).fetchone()[0],
                             '2a-ii product function is deliberately absent before 005')

    def installer(self):
        return psycopg.connect(os.environ['TEST_DATABASE_URL'].replace('postgres@',
                               INSTALLER + '@'), autocommit=True)

    def attempt(self, *, connection=None, installation=30, tenant=31, project=32,
                principal=33, credential=34, digest=DIGEST, key='console-first'):
        owned = connection is None
        db = self.installer() if owned else connection
        try:
            try:
                value = db.execute('SELECT auth.first_identity(%s,%s,%s,%s,%s,%s,%s)',
                    tuple(uid(n) if n is not None else None for n in (installation, tenant, project,
                                           principal, credential)) + (digest, key)).fetchone()[0]
                return ('ok', value)
            except psycopg.Error as error:
                return ('error', (error.sqlstate, error.diag.message_primary))
        finally:
            if owned:
                db.close()

    def counts(self):
        return {table: self.admin.execute('SELECT count(*) FROM ' + table +
                ' WHERE tenant_id=%s', (uid(31),)).fetchone()[0]
                for table in ('auth.principals', 'auth.project_grants',
                              'auth.credentials', 'coordination.outbox')}

    def receipt_count(self):
        return self.admin.execute('SELECT count(*) FROM auth.first_identity_receipts '
                                  'WHERE installation_id=%s', (uid(30),)).fetchone()[0]

    def test_role_flags_memberships_and_no_table_write_are_frozen(self):
        role = self.admin.execute('SELECT rolcanlogin,rolinherit,rolsuper,rolcreatedb,'
            'rolcreaterole,rolreplication,rolbypassrls FROM pg_roles WHERE rolname=%s',
            (INSTALLER,)).fetchone()
        self.assertEqual(role, (True, False, False, False, False, False, False))
        self.assertEqual(self.admin.execute('SELECT count(*) FROM pg_auth_members '
            'WHERE member=(SELECT oid FROM pg_roles WHERE rolname=%s) '
            'OR roleid=(SELECT oid FROM pg_roles WHERE rolname=%s)',
            (INSTALLER, INSTALLER)).fetchone()[0], 0)
        for target in (REQUEST, 'kaidera-runtime-core-verifier'):
            self.assertFalse(self.admin.execute('SELECT pg_has_role(%s,%s,%s)',
                (INSTALLER, target, 'MEMBER')).fetchone()[0])
        for table in ('auth.principals', 'auth.project_grants', 'auth.credentials',
                      'auth.first_identity_receipts'):
            self.assertFalse(self.admin.execute('SELECT has_table_privilege(%s,%s,%s)',
                (INSTALLER, table, 'INSERT')).fetchone()[0])

    def test_private_function_owner_acl_and_fixed_search_path(self):
        self.function_present()
        owner, security_definer, config, exposed = self.admin.execute(
            'SELECT pg_get_userbyid(proowner),prosecdef,proconfig,'
            "EXISTS(SELECT 1 FROM aclexplode(proacl) a WHERE a.grantee=0 "
            "AND a.privilege_type='EXECUTE') FROM pg_proc WHERE oid=%s::regprocedure",
            (FUNCTION,)).fetchone()
        self.assertEqual(owner, 'kaidera-runtime-core-verifier')
        self.assertTrue(security_definer)
        self.assertIn('search_path=pg_catalog, auth, core, coordination, pg_temp', config)
        self.assertFalse(exposed)
        for role, expected in ((INSTALLER, True), (REQUEST, False), (API, False),
                               ('kaidera-runtime-core-verifier', True)):
            with self.subTest(role=role):
                self.assertIs(self.admin.execute('SELECT has_function_privilege(%s,%s,%s)',
                    (role, FUNCTION, 'EXECUTE')).fetchone()[0], expected)

    def test_fact_before_receipt_has_no_scope_or_event(self):
        probe = 'auth.cxgap2aii_before_receipt()'
        self.assertIsNone(self.admin.execute('SELECT to_regprocedure(%s)',
                                             (probe,)).fetchone()[0])
        root = Path(__file__).resolve().parents[2] / 'schema'
        manifest = json.loads((root / 'manifest.json').read_text())
        self.assertFalse(any('cxgap2aii_before_receipt' in (root / row['file']).read_text()
                             for row in manifest['migrations']))
        self.admin.execute(PRE_RECEIPT_PROBE, prepare=False)
        with self.installer() as db:
            with self.assertRaises(psycopg.Error) as caught:
                db.execute('SELECT auth.cxgap2aii_before_receipt()')
        self.assertEqual(caught.exception.diag.message_primary, 'outbox_forbidden')
        self.assertEqual(self.receipt_count(), 0)
        self.assertEqual(self.counts()['coordination.outbox'], 0)

    def test_fresh_install_scopes_digest_audit_and_publishes_once(self):
        self.function_present()
        status, receipt = self.attempt()
        self.assertEqual(status, 'ok', receipt)
        self.assertEqual((receipt['principal_id'], receipt['credential_id'],
                          receipt['project_id'], receipt['replayed']),
                         (uid(33), uid(34), uid(32), False))
        self.assertNotIn(DIGEST, json.dumps(receipt))
        self.assertNotIn(SECRET.decode(), json.dumps(receipt))
        self.assertEqual(self.counts(), {'auth.principals': 1, 'auth.project_grants': 1,
                                          'auth.credentials': 1, 'coordination.outbox': 1})
        self.assertEqual(self.receipt_count(), 1)
        row = self.admin.execute('SELECT p.kind,p.identity_adopted,g.permissions,g.roles,'
            'c.key_digest FROM auth.principals p JOIN auth.project_grants g '
            'ON (p.tenant_id,p.id)=(g.tenant_id,g.principal_id) JOIN auth.credentials c '
            'ON (p.tenant_id,p.id)=(c.tenant_id,c.principal_id) '
            'WHERE p.tenant_id=%s AND p.id=%s', (uid(31), uid(33))).fetchone()
        self.assertEqual(row, ('human', True, ['read', 'write'], [], DIGEST))
        event = self.admin.execute('SELECT o.event_id,o.aggregate_kind,o.aggregate_revision,'
            'o.payload_ref FROM coordination.outbox o WHERE o.installation_id=%s',
            (uid(30),)).fetchone()
        self.assertEqual(event[1:3], ('core.identity', 1))
        audit = json.loads(bytes(self.admin.execute('SELECT body FROM core.payloads '
            'WHERE tenant_id=%s AND project_id=%s AND id=%s',
            (uid(31), uid(32), event[3])).fetchone()[0]))
        self.assertEqual(audit, {'actor_kind': 'installer', 'actor': INSTALLER,
                                 'operation': 'first_identity', 'principal_id': uid(33)})
        for action in ('read', 'write'):
            with authorized(self.request, SECRET, UUID(uid(30)), UUID(uid(32)), action):
                pass
        for installation, project, action in ((30, 32, 'control'), (30, 37, 'read'),
                                               (1, 32, 'read')):
            with self.assertRaises(AuthError):
                with authorized(self.request, SECRET, UUID(uid(installation)),
                                UUID(uid(project)), action):
                    pass
        self.admin.execute("INSERT INTO auth.principals(tenant_id,id,name) "
                           "VALUES (%s,%s,'fixture-publisher')", (uid(31), uid(40)))
        self.admin.execute('INSERT INTO auth.project_grants '
            '(tenant_id,project_id,principal_id,permissions) VALUES (%s,%s,%s,%s)',
            (uid(31), uid(32), uid(40), ['owner']))
        self.admin.execute('INSERT INTO auth.credentials(tenant_id,id,principal_id,key_digest) '
            'VALUES (%s,%s,%s,%s)',
            (uid(31), uid(41), uid(40), hashlib.sha256(PUBLISHER).hexdigest()))
        delivered = Outbox(self.request, PUBLISHER, UUID(uid(30)), UUID(uid(32))).publish()
        self.assertEqual(len(delivered), 1)
        self.assertEqual(delivered[0].envelope['event_id'], str(event[0]))
        self.assertEqual(self.admin.execute('SELECT count(*) FROM coordination.published_events '
            'WHERE installation_id=%s', (uid(30),)).fetchone()[0], 1)
        again_status, again = self.attempt()
        self.assertEqual(again_status, 'ok', again)
        self.assertTrue(again['replayed'])
        self.assertEqual((again['principal_id'], again['credential_id']),
                         (receipt['principal_id'], receipt['credential_id']))
        self.assertEqual(self.counts()['coordination.outbox'], 1)
        self.assertEqual(len(Outbox(self.request, PUBLISHER, UUID(uid(30)), UUID(uid(32))).publish()), 0)

    def test_changed_replay_and_second_enrollment_refuse_without_new_event(self):
        self.function_present()
        self.assertEqual(self.attempt()[0], 'ok')
        for kwargs in ({'key': 'changed'}, {'digest': '0' * 64},
                       {'principal': 35, 'credential': 36, 'key': 'second'}):
            with self.subTest(kwargs=kwargs):
                self.assertEqual(self.attempt(**kwargs)[0], 'error')
        self.assertEqual(self.counts()['coordination.outbox'], 1)
        self.assertEqual(self.receipt_count(), 1)

    def test_wrong_linkage_nondefault_project_and_malformed_digest_refuse(self):
        self.function_present()
        for kwargs in ({'installation': 1}, {'tenant': 2}, {'project': 37},
                       {'project': 3}, {'digest': 'not-a-digest'},
                       {'digest': 'A' * 64}, {'digest': None}, {'key': None},
                       {'installation': None}, {'tenant': None}, {'project': None},
                       {'principal': None}, {'credential': None}):
            with self.subTest(kwargs=kwargs):
                self.assertEqual(self.attempt(**kwargs),
                                 ('error', ('42501', 'first_identity_forbidden')))
                self.assertEqual(self.receipt_count(), 0)
                self.assertEqual(self.counts()['coordination.outbox'], 0)

    def test_preexisting_identity_or_alias_refuses(self):
        self.function_present()
        self.admin.execute("INSERT INTO auth.principals(tenant_id,id,name) "
                           "VALUES (%s,%s,'preexisting')", (uid(31), uid(42)))
        self.admin.execute('INSERT INTO auth.credentials(tenant_id,id,principal_id,key_digest) '
            'VALUES (%s,%s,%s,%s)', (uid(31), uid(43), uid(42), 'f' * 64))
        self.assertEqual(self.attempt()[0], 'error')
        self.assertEqual(self.receipt_count(), 0)
        self.admin.execute('DELETE FROM auth.credentials WHERE tenant_id=%s', (uid(31),))
        self.admin.execute('DELETE FROM auth.principals WHERE tenant_id=%s', (uid(31),))
        body = b'synthetic preexisting identity alias fixture'
        self.admin.execute('INSERT INTO core.payloads(tenant_id,project_id,id,body,sha256) '
                           'VALUES (%s,%s,%s,%s,%s)',
                           (uid(31), uid(32), uid(52), body, hashlib.sha256(body).hexdigest()))
        with self.admin.transaction():
            self.admin.execute("INSERT INTO core.records(tenant_id,project_id,id,kind,current_revision,tombstone) "
                               "VALUES (%s,%s,%s,'memory',1,false)",
                               (uid(31), uid(32), uid(50)))
            self.admin.execute('INSERT INTO core.record_revisions '
                '(tenant_id,project_id,record_id,revision,payload_ref,tombstone) '
                'VALUES (%s,%s,%s,1,%s,false)',
                (uid(31), uid(32), uid(50), uid(52)))
        self.admin.execute('INSERT INTO core.record_aliases '
            '(tenant_id,project_id,source_namespace,external_id,record_id) '
            'VALUES (%s,%s,%s,%s,%s)',
            (uid(31), uid(32), 'cortex.core.identity', uid(51), uid(50)))
        self.assertEqual(self.attempt()[0], 'error')
        self.assertEqual(self.receipt_count(), 0)
        self.assertEqual(self.counts()['coordination.outbox'], 0)

    def test_interrupted_precommit_attempt_leaves_no_identity_receipt_or_event(self):
        self.function_present()
        with self.installer() as db:
            with self.assertRaisesRegex(RuntimeError, 'synthetic interruption'):
                with db.transaction():
                    self.assertEqual(self.attempt(connection=db)[0], 'ok')
                    raise RuntimeError('synthetic interruption')
        self.assertEqual(self.counts(), {'auth.principals': 0, 'auth.project_grants': 0,
                                          'auth.credentials': 0, 'coordination.outbox': 0})
        self.assertEqual(self.receipt_count(), 0)
        self.assertEqual(self.admin.execute('SELECT count(*) FROM core.payloads '
            'WHERE tenant_id=%s AND project_id=%s', (uid(31), uid(32))).fetchone()[0], 0)
        self.assertEqual(self.attempt()[0], 'ok')

    def test_concurrent_distinct_attempts_have_one_winner_and_one_event(self):
        self.function_present()
        with ThreadPoolExecutor(max_workers=2) as pool:
            left = pool.submit(self.attempt, principal=33, credential=34, key='left')
            right = pool.submit(self.attempt, principal=35, credential=36, key='right')
            results = (left.result(), right.result())
        self.assertEqual([status for status, _ in results].count('ok'), 1)
        self.assertEqual(self.counts(), {'auth.principals': 1, 'auth.project_grants': 1,
                                          'auth.credentials': 1, 'coordination.outbox': 1})
        self.assertEqual(self.receipt_count(), 1)
        winner = 0 if results[0][0] == 'ok' else 1
        principal, credential, key = ((33, 34, 'left'), (35, 36, 'right'))[winner]
        status, replay = self.attempt(principal=principal, credential=credential, key=key)
        self.assertEqual(status, 'ok', replay)
        self.assertTrue(replay['replayed'])
        self.assertEqual(self.counts()['coordination.outbox'], 1)

    def test_wrong_login_revocation_and_reconnect_cannot_expand_authority(self):
        self.function_present()
        self.assertEqual(self.attempt(connection=self.request)[0], 'error')
        self.assertEqual(self.attempt(connection=self.admin),
                         ('error', ('42501', 'first_identity_forbidden')))
        self.assertEqual(self.receipt_count(), 0)
        self.assertEqual(self.attempt()[0], 'ok')
        self.admin.execute('UPDATE auth.credentials SET revoked_at=clock_timestamp() '
                           'WHERE tenant_id=%s AND id=%s', (uid(31), uid(34)))
        with self.assertRaises(AuthError):
            with authorized(self.request, SECRET, UUID(uid(30)), UUID(uid(32))):
                pass
        status, replay = self.attempt()
        self.assertEqual(status, 'ok', replay)
        self.assertTrue(replay['replayed'])
        self.assertEqual(self.attempt(principal=35, credential=36, key='after-restart')[0],
                         'error')
        self.assertEqual(self.counts()['coordination.outbox'], 1)
