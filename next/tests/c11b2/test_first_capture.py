"""CX-GAP-2a-i private bootstrap capture, with a disposable-only SQL probe."""

import hashlib
import json
import os
from uuid import UUID

import psycopg

from core_db_fixture import API, Fixture, OWNER_A, REQUEST, uid
from cortex_core.outbox import Outbox


INSTALLER = 'kaidera-runtime-core-installer'
HELPER = 'auth.first_identity_bootstrap_scope()'
PROBE = 'auth.cxgap2ai_test_probe(uuid,uuid,text,boolean,boolean,text,uuid)'
SECRET = b'synthetic-cxgap2ai-only'
DIGEST = hashlib.sha256(SECRET).hexdigest()
PROBE_SQL = """
CREATE FUNCTION auth.cxgap2ai_test_probe(p_principal uuid,p_credential uuid,p_digest text,
    p_receipt boolean,p_fact boolean,p_kind text,p_fact_target uuid) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,auth,core,coordination,pg_temp AS $$
DECLARE audit_id uuid:=gen_random_uuid(); body bytea; event uuid;
BEGIN
    INSERT INTO auth.principals(tenant_id,id,name,kind,identity_adopted)
      VALUES('00000000-0000-4000-8000-000000000002',p_principal,'fixture-first','human',true);
    INSERT INTO auth.project_grants(tenant_id,project_id,principal_id,permissions,roles)
      VALUES('00000000-0000-4000-8000-000000000002',
             '00000000-0000-4000-8000-000000000003',p_principal,
             ARRAY['read','write'],ARRAY[]::text[]);
    INSERT INTO auth.credentials(tenant_id,id,principal_id,key_digest)
      VALUES('00000000-0000-4000-8000-000000000002',p_credential,p_principal,p_digest);
    IF p_receipt THEN
      INSERT INTO auth.first_identity_receipts(
        installation_id,tenant_id,project_id,principal_id,credential_id,key_digest,request_key)
        VALUES('00000000-0000-4000-8000-000000000001',
               '00000000-0000-4000-8000-000000000002',
               '00000000-0000-4000-8000-000000000003',p_principal,p_credential,
               p_digest,'fixture-first');
    END IF;
    body:=convert_to(jsonb_build_object('actor_kind','installer',
      'actor','kaidera-runtime-core-installer','operation','first_identity',
      'principal_id',COALESCE(p_fact_target,p_principal))::text,'UTF8');
    INSERT INTO core.payloads(tenant_id,project_id,id,body,sha256)
      VALUES('00000000-0000-4000-8000-000000000002',
             '00000000-0000-4000-8000-000000000003',audit_id,body,
             encode(sha256(body),'hex'));
    IF p_fact THEN
      event:=coordination.c06_fact(p_kind,COALESCE(p_fact_target,p_principal),audit_id);
    END IF;
    RETURN event;
END;
$$;
ALTER FUNCTION auth.cxgap2ai_test_probe(uuid,uuid,text,boolean,boolean,text,uuid)
    OWNER TO "kaidera-runtime-core-verifier";
REVOKE ALL ON FUNCTION auth.cxgap2ai_test_probe(uuid,uuid,text,boolean,boolean,text,uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION auth.cxgap2ai_test_probe(uuid,uuid,text,boolean,boolean,text,uuid)
    TO "kaidera-runtime-core-installer","kaidera-test-core-api";
CREATE FUNCTION auth.cxgap2ai_test_scope() RETURNS jsonb
LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,auth,core,pg_temp AS $$
    SELECT to_jsonb(s) FROM auth.first_identity_bootstrap_scope() s;
$$;
ALTER FUNCTION auth.cxgap2ai_test_scope() OWNER TO "kaidera-runtime-core-verifier";
REVOKE ALL ON FUNCTION auth.cxgap2ai_test_scope() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION auth.cxgap2ai_test_scope() TO "kaidera-runtime-core-installer";
"""


class FirstCapture(Fixture):
    def setUp(self):
        super().setUp()
        self.admin.execute("UPDATE core.projects SET name='default' "
                           "WHERE tenant_id=%s AND id=%s", (uid(2), uid(3)))

    def gate(self):
        self.assertIsNotNone(self.admin.execute('SELECT to_regprocedure(%s)',
                                                (HELPER,)).fetchone()[0])

    def install_probe(self):
        self.admin.execute(PROBE_SQL, prepare=False)
        self.admin.execute(f'GRANT USAGE ON SCHEMA auth TO "{API}"')

    def connection(self, role=INSTALLER):
        return psycopg.connect(os.environ['TEST_DATABASE_URL'].replace('postgres@',
                               role + '@'), autocommit=True)

    def probe(self, *, role=INSTALLER, principal=70, credential=170,
              receipt=True, fact=True, kind='identity', fact_target=None):
        with self.connection(role) as db:
            try:
                result = db.execute('SELECT auth.cxgap2ai_test_probe(%s,%s,%s,%s,%s,%s,%s)',
                                    (uid(principal), uid(credential), DIGEST,
                                     receipt, fact, kind,
                                     uid(fact_target) if fact_target is not None else None)).fetchone()[0]
                return ('ok', result)
            except psycopg.Error as error:
                return ('error', (error.sqlstate, error.diag.message_primary,
                                  error.diag.context))

    def test_catalog_role_helper_acl_and_no_shipped_probe(self):
        self.gate()
        role = self.admin.execute('SELECT rolcanlogin,rolinherit,rolsuper,rolcreatedb,'
            'rolcreaterole,rolreplication,rolbypassrls FROM pg_roles WHERE rolname=%s',
            (INSTALLER,)).fetchone()
        self.assertEqual(role, (True, False, False, False, False, False, False))
        members = self.admin.execute('SELECT count(*) FROM pg_auth_members '
            'WHERE member=(SELECT oid FROM pg_roles WHERE rolname=%s) '
            'OR roleid=(SELECT oid FROM pg_roles WHERE rolname=%s)',
            (INSTALLER, INSTALLER)).fetchone()[0]
        self.assertEqual(members, 0)
        for target in (REQUEST, 'kaidera-runtime-core-verifier'):
            self.assertFalse(self.admin.execute('SELECT pg_has_role(%s,%s,%s)',
                (INSTALLER, target, 'MEMBER')).fetchone()[0])
        for role in (INSTALLER, REQUEST, API):
            self.assertFalse(self.admin.execute('SELECT has_function_privilege(%s,%s,%s)',
                (role, HELPER, 'EXECUTE')).fetchone()[0])
            self.assertFalse(self.admin.execute('SELECT has_table_privilege(%s,%s,%s)',
                (role, 'auth.first_identity_receipts', 'INSERT')).fetchone()[0])
        self.assertTrue(self.admin.execute('SELECT has_function_privilege(%s,%s,%s)',
            ('kaidera-runtime-core-verifier', HELPER, 'EXECUTE')).fetchone()[0])
        self.assertEqual(self.admin.execute('SELECT relrowsecurity,relforcerowsecurity '
            'FROM pg_class WHERE oid=%s::regclass',
            ('auth.first_identity_receipts',)).fetchone(), (True, True))
        for name in ('c06_fact(text,uuid,uuid)', 'c06_capture_revision()',
                     'c06_identity_guard()'):
            signature = 'coordination.' + name
            self.assertEqual(self.admin.execute('SELECT pg_get_userbyid(proowner) '
                'FROM pg_proc WHERE oid=%s::regprocedure', (signature,)).fetchone()[0],
                'kaidera-runtime-core-verifier')
            self.assertFalse(self.admin.execute('SELECT has_function_privilege(%s,%s,%s)',
                (INSTALLER, signature, 'EXECUTE')).fetchone()[0])
        self.assertFalse(self.admin.execute("SELECT EXISTS(SELECT 1 FROM aclexplode(proacl) a "
            "WHERE a.grantee=0 AND a.privilege_type='EXECUTE') FROM pg_proc "
            'WHERE oid=%s::regprocedure', (HELPER,)).fetchone()[0])
        self.assertIsNone(self.admin.execute('SELECT to_regprocedure(%s)',
                                             (PROBE,)).fetchone()[0])
        from pathlib import Path
        root = Path(__file__).resolve().parents[2] / 'schema'
        manifest = json.loads((root / 'manifest.json').read_text())
        self.assertFalse(any('cxgap2ai_test_probe' in (root / entry['file']).read_text()
                             for entry in manifest['migrations']))

    def test_fact_before_current_transaction_receipt_has_no_scope(self):
        self.gate()
        self.install_probe()
        self.assertEqual(self.probe(receipt=False, fact=True)[0], 'error')
        self.assertEqual(self.admin.execute('SELECT count(*) FROM coordination.outbox').fetchone()[0], 0)
        self.assertEqual(self.admin.execute('SELECT count(*) FROM auth.first_identity_receipts').fetchone()[0], 0)

    def test_exact_receipt_captures_and_publishes_one_identity_event(self):
        self.gate()
        self.install_probe()
        status, event = self.probe()
        self.assertEqual(status, 'ok', event)
        self.assertIsNotNone(event)
        row = self.admin.execute('SELECT aggregate_kind,aggregate_revision,payload_ref '
            'FROM coordination.outbox WHERE event_id=%s', (event,)).fetchone()
        self.assertEqual(row[:2], ('core.identity', 1))
        payload = self.admin.execute('SELECT body FROM core.payloads WHERE id=%s',
                                     (row[2],)).fetchone()[0]
        audit = json.loads(bytes(payload))
        self.assertEqual((audit['actor_kind'], audit['actor'], audit['operation']),
                         ('installer', INSTALLER, 'first_identity'))
        self.assertNotIn(DIGEST, bytes(payload).decode())
        self.assertEqual(self.admin.execute('SELECT count(*) FROM coordination.outbox').fetchone()[0], 1)
        published = Outbox(self.request, OWNER_A, UUID(uid(1)), UUID(uid(3))).publish()
        self.assertEqual(len(published), 1)
        self.assertEqual(self.admin.execute('SELECT count(*) FROM coordination.published_events').fetchone()[0], 1)
        self.assertEqual(self.probe()[0], 'error')
        self.assertEqual(self.admin.execute('SELECT count(*) FROM coordination.published_events').fetchone()[0], 1)

    def test_missing_fact_wrong_login_and_prior_receipt_refuse(self):
        self.gate()
        self.install_probe()
        self.assertEqual(self.probe(fact=False)[0], 'error')
        self.assertEqual(self.probe(role=API)[0], 'error')
        self.assertEqual(self.admin.execute('SELECT count(*) FROM auth.first_identity_receipts').fetchone()[0], 0)
        self.assertEqual(self.probe()[0], 'ok')
        self.assertEqual(self.probe(principal=71, credential=171)[0], 'error')
        with self.connection() as db:
            self.assertIsNone(db.execute('SELECT auth.cxgap2ai_test_scope()').fetchone()[0])

    def test_wrong_fact_kind_refuses_inside_fact_before_capture(self):
        self.gate()
        self.install_probe()
        status, failure = self.probe(kind='job')
        self.assertEqual(status, 'error')
        self.assertIn('c06_fact', failure[2] or '')
        self.assertNotIn('c06_capture_revision', failure[2] or '')
        self.assertEqual(self.admin.execute('SELECT count(*) FROM coordination.outbox').fetchone()[0], 0)

    def test_prior_identity_alias_refuses_new_first_capture(self):
        self.gate()
        self.install_probe()
        self.admin.execute('INSERT INTO core.record_aliases '
            '(tenant_id,project_id,source_namespace,external_id,record_id) '
            'VALUES (%s,%s,%s,%s,%s)',
            (uid(2), uid(3), 'cortex.core.identity', uid(90), uid(6)))
        self.assertEqual(self.probe()[0], 'error')
        self.assertEqual(self.admin.execute('SELECT count(*) FROM coordination.outbox').fetchone()[0], 0)

    def test_prior_transaction_receipt_alone_has_no_scope(self):
        self.gate()
        self.install_probe()
        self.admin.execute("INSERT INTO auth.principals(tenant_id,id,name,kind,identity_adopted) "
            "VALUES (%s,%s,'fixture-prior','human',true)", (uid(2), uid(70)))
        self.admin.execute('INSERT INTO auth.credentials(tenant_id,id,principal_id,key_digest) '
            'VALUES (%s,%s,%s,%s)', (uid(2), uid(170), uid(70), DIGEST))
        self.admin.execute('INSERT INTO auth.first_identity_receipts '
            '(installation_id,tenant_id,project_id,principal_id,credential_id,key_digest,request_key) '
            'VALUES (%s,%s,%s,%s,%s,%s,%s)',
            (uid(1), uid(2), uid(3), uid(70), uid(170), DIGEST, 'fixture-prior'))
        with self.connection() as db:
            self.assertIsNone(db.execute('SELECT auth.cxgap2ai_test_scope()').fetchone()[0])

