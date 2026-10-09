"""C04 real-PG auth/RLS contract, committed before its implementation."""
from dataclasses import asdict
import hashlib
import os
from pathlib import Path
import sys
import unittest
from uuid import UUID

import psycopg
from cortex_core.auth import AuthError, authorized

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'schema'))
import test_schema

READ_A = b'synthetic-only-read-a'
WRITE_A = b'synthetic-only-write-a'
OWNER_A = b'synthetic-only-owner-a'
CONTROL_A = b'synthetic-only-control-a'
READ_B = b'synthetic-only-read-b'
REQUEST = 'kaidera-runtime-core-request'
VERIFIER = 'kaidera-runtime-core-verifier'
API = 'kaidera-test-core-api'
uid = test_schema.uid


class AuthorizationTests(unittest.TestCase):
    reset = test_schema.SchemaTests.reset

    def setUp(self):
        test_schema.SchemaTests.setUp(self)
        self.admin = self.db
        self.admin.execute(f'''DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='{API}') THEN
                CREATE ROLE "{API}" LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
            END IF;
        END $$''')
        self.admin.execute(f'GRANT "{REQUEST}" TO "{API}"')
        self.request = psycopg.connect(os.environ['TEST_DATABASE_URL'].replace('postgres@', API+'@'), autocommit=True)
        self.addCleanup(self.request.close)
        self.request.execute(f'SET ROLE "{REQUEST}"')
        self.admin.execute('INSERT INTO core.installations(id) VALUES (%s)', (uid(20),))
        self.admin.execute('INSERT INTO core.tenants(id,installation_id) VALUES (%s,%s)', (uid(21),uid(20)))
        self.admin.execute("INSERT INTO core.projects(tenant_id,id,name) VALUES (%s,%s,'tenant-b')", (uid(21),uid(3)))
        body=b'synthetic tenant-b payload'
        self.admin.execute('INSERT INTO core.payloads(tenant_id,project_id,id,body,sha256) VALUES (%s,%s,%s,%s,%s)', (uid(21),uid(3),uid(5),body,hashlib.sha256(body).hexdigest()))
        with self.admin.transaction():
            self.admin.execute("INSERT INTO core.records VALUES (%s,%s,%s,'memory',1,false)", (uid(21),uid(3),uid(6)))
            self.admin.execute('INSERT INTO core.record_revisions(tenant_id,project_id,record_id,revision,payload_ref,tombstone) VALUES (%s,%s,%s,1,%s,false)', (uid(21),uid(3),uid(6),uid(5)))
        for tenant,principal,key,permissions in ((2,8,READ_A,['read']),(2,10,WRITE_A,['read','write']),
                                                (2,12,OWNER_A,['owner']),(2,14,CONTROL_A,['control']),
                                                (21,8,READ_B,['read'])):
            self.admin.execute("INSERT INTO auth.principals(tenant_id,id,name) VALUES (%s,%s,%s)", (uid(tenant),uid(principal),str(principal)))
            self.admin.execute('INSERT INTO auth.project_grants VALUES (%s,%s,%s,%s)', (uid(tenant),uid(3),uid(principal),permissions))
            self.admin.execute('INSERT INTO auth.credentials(tenant_id,id,principal_id,key_digest) VALUES (%s,%s,%s,%s)', (uid(tenant),uid(principal+100),uid(principal),hashlib.sha256(key).hexdigest()))

    def auth(self, key=READ_A, installation=1, project=3, action='read', connection=None):
        return authorized(connection or self.request, key, UUID(uid(installation)), UUID(uid(project)), action)

    def test_scope_is_derived_from_credential_and_registry(self):
        with self.auth() as scope:
            self.assertEqual((scope.installation_id,scope.tenant_id,scope.project_id,scope.principal_id), tuple(UUID(uid(n)) for n in (1,2,3,8)))
            self.assertGreater(scope.permission_generation,0)
            self.assertEqual(self.request.execute('SELECT tenant_id::text,project_id::text,id::text FROM core.records').fetchall(), [(uid(2),uid(3),uid(6))])
            self.assertNotIn(READ_A.decode(),repr(scope))
            self.assertNotIn(hashlib.sha256(READ_A).hexdigest(),repr(asdict(scope)))

    def test_forged_tenant_and_principal_context_cannot_expand_scope(self):
        with self.auth():
            self.request.execute("SELECT set_config('cortex.tenant_id',%s,true),set_config('cortex.principal_id',%s,true)", (uid(21),uid(12)))
            self.assertEqual(self.request.execute('SELECT tenant_id::text FROM core.records').fetchall(), [(uid(2),)])
        self.request.execute("SELECT set_config('cortex.tenant_id',%s,false),set_config('cortex.project_id',%s,false)", (uid(21),uid(3)))
        self.assertEqual(self.request.execute('SELECT count(*) FROM core.records').fetchone()[0],0)

    def test_missing_unknown_expired_revoked_disabled_credentials_refused(self):
        for key in (None,b'',b'unknown-synthetic-key'):
            with self.subTest(key_kind=type(key).__name__),self.assertRaises(AuthError):
                with self.auth(key): pass
        digest=hashlib.sha256(READ_A).hexdigest()
        for column,value in [('expires_at',"clock_timestamp()-interval '1 second'"),('revoked_at','clock_timestamp()')]:
            self.admin.execute(f'UPDATE auth.credentials SET {column}={value} WHERE key_digest=%s',(digest,))
            with self.subTest(column=column),self.assertRaises(AuthError):
                with self.auth(): pass
            self.admin.execute(f'UPDATE auth.credentials SET {column}=NULL WHERE key_digest=%s',(digest,))
        self.admin.execute('UPDATE auth.principals SET disabled=true WHERE tenant_id=%s AND id=%s',(uid(2),uid(8)))
        with self.assertRaises(AuthError):
            with self.auth(): pass

    def test_wrong_installation_project_and_unknown_action_refused(self):
        for args in ({'installation':20},{'installation':99},{'project':4},{'project':99},{'action':'root-everywhere'}):
            with self.subTest(args=args),self.assertRaises(AuthError):
                with self.auth(**args): pass

    def test_cross_tenant_reads_and_insert_update_are_denied(self):
        with self.auth(WRITE_A,action='write'):
            self.assertEqual(self.request.execute('SELECT count(*) FROM core.payloads WHERE tenant_id=%s',(uid(21),)).fetchone()[0],0)
            self.assertEqual(self.request.execute("UPDATE core.records SET kind=kind WHERE tenant_id=%s",(uid(21),)).rowcount,0)
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                with self.request.transaction():
                    self.request.execute('INSERT INTO core.payloads(tenant_id,project_id,id,body,sha256) VALUES (%s,%s,%s,%s,%s)',(uid(21),uid(3),uid(40),self.body,self.digest))

    def test_authorized_write_preserves_atomic_record_history(self):
        with self.auth(WRITE_A,action='write'):
            self.request.execute('INSERT INTO core.payloads(tenant_id,project_id,id,body,sha256) VALUES (%s,%s,%s,%s,%s)',(*self.scope,uid(40),self.body,self.digest))
            self.request.execute("INSERT INTO core.records VALUES (%s,%s,%s,'memory',1,false)",(*self.scope,uid(41)))
            self.request.execute('INSERT INTO core.record_revisions(tenant_id,project_id,record_id,revision,payload_ref,tombstone) VALUES (%s,%s,%s,1,%s,false)',(*self.scope,uid(41),uid(40)))
        self.assertEqual(self.admin.execute('SELECT count(*) FROM core.records WHERE tenant_id=%s',(uid(2),)).fetchone()[0],2)
        with self.auth():
            self.assertEqual(self.request.execute('SELECT count(*) FROM core.records').fetchone()[0],2)

    def test_read_context_cannot_write_or_control(self):
        with self.auth(WRITE_A,action='read'):
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                with self.request.transaction():
                    self.request.execute('INSERT INTO core.payloads(tenant_id,project_id,id,body,sha256) VALUES (%s,%s,%s,%s,%s)',(*self.scope,uid(40),self.body,self.digest))
        with self.assertRaises(AuthError):
            with self.auth(action='control'): pass

    def test_control_requires_scoped_owner_or_admin_without_fallback(self):
        with self.auth(OWNER_A,action='control') as scope:
            self.assertEqual(scope.action,'control')
        for key in (READ_A,CONTROL_A):
            with self.subTest(credential_kind='read' if key==READ_A else 'control-only'),self.assertRaises(AuthError):
                with self.auth(key,action='control'): pass
        with self.assertRaises(AuthError):
            with self.auth(OWNER_A,project=4,action='control'): pass

    def test_request_role_is_nonowner_nonbypass_and_cannot_elevate(self):
        row=self.request.execute('SELECT rolsuper,rolbypassrls,rolcreaterole,rolcreatedb FROM pg_roles WHERE rolname=current_user').fetchone()
        self.assertEqual(row,(False,False,False,False))
        with self.assertRaises(psycopg.errors.InsufficientPrivilege):
            self.request.execute(f'SET ROLE "{VERIFIER}"')
        with self.auth():
            for sql in ('SELECT * FROM auth.credentials','TRUNCATE core.records','CREATE TABLE core.forbidden(id int)'):
                with self.subTest(sql_kind=sql.split()[0]),self.assertRaises(psycopg.errors.InsufficientPrivilege):
                    with self.request.transaction(): self.request.execute(sql)

    def test_privileged_or_owning_runtime_connection_refused(self):
        with self.assertRaises(AuthError):
            with self.auth(connection=self.admin): pass
        self.admin.execute('CREATE TABLE core.c04_ownership_probe(id int)')
        self.addCleanup(self.admin.execute,'DROP TABLE IF EXISTS core.c04_ownership_probe')
        self.admin.execute(f'ALTER TABLE core.c04_ownership_probe OWNER TO "{REQUEST}"')
        with self.assertRaises(AuthError):
            with self.auth(): pass

    def test_all_business_tables_force_rls_private_functions_stay_private(self):
        rows=self.admin.execute("SELECT n.nspname,c.relname,c.relrowsecurity,c.relforcerowsecurity FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname IN ('core','auth','coordination','retrieval') AND c.relkind='r' AND c.relname<>'schema_migrations'").fetchall()
        self.assertEqual(len(rows),28)
        for schema,table,enabled,forced in rows:
            with self.subTest(schema=schema,table=table): self.assertEqual((enabled,forced),(True,True))
        fn=self.admin.execute("SELECT prosecdef,proconfig,pg_get_userbyid(proowner),has_function_privilege('public','auth.resolve_scope(text,uuid,uuid,text)','EXECUTE') FROM pg_proc WHERE oid='auth.resolve_scope(text,uuid,uuid,text)'::regprocedure").fetchone()
        self.assertTrue(fn[0]); self.assertIn('search_path=pg_catalog, auth, core, pg_temp',fn[1]); self.assertEqual(fn[2],VERIFIER);self.assertFalse(fn[3])

    def test_installer_ledger_and_global_feed_are_not_request_resources(self):
        with self.auth():
            for table in ('core.schema_migrations','coordination.feed_state','coordination.consumer_checkpoints','coordination.snapshot_floors'):
                with self.subTest(table=table),self.assertRaises(psycopg.errors.InsufficientPrivilege):
                    with self.request.transaction(): self.request.execute('SELECT * FROM '+table)

    def test_scope_clears_on_commit_rollback_and_pool_reuse(self):
        digest=hashlib.sha256(READ_A).hexdigest()
        self.request.execute("SELECT set_config('cortex.credential_digest',%s,false),set_config('cortex.installation_id',%s,false),set_config('cortex.project_id',%s,false),set_config('cortex.action','read',false)",(digest,uid(1),uid(3)))
        with self.auth(READ_B,installation=20):
            self.assertEqual(self.request.execute('SELECT tenant_id::text FROM core.records').fetchall(),[(uid(21),)])
        self.assertEqual(self.request.execute('SELECT count(*) FROM core.records').fetchone()[0],0)
        with self.assertRaisesRegex(RuntimeError,'synthetic rollback'):
            with self.auth(): raise RuntimeError('synthetic rollback')
        self.assertEqual(self.request.execute('SELECT count(*) FROM core.records').fetchone()[0],0)
        with self.assertRaises(AuthError):
            with self.auth(b'unknown'): pass
        self.assertEqual(self.request.execute('SELECT count(*) FROM core.records').fetchone()[0],0)

    def test_permission_generation_and_cache_partition_track_authority(self):
        with self.auth() as a: before=a.permission_generation
        with self.auth(READ_B,installation=20) as b:
            self.assertNotEqual(a.cache_partition,b.cache_partition)
        self.admin.execute("UPDATE auth.project_grants SET permissions=ARRAY['read','write'] WHERE tenant_id=%s AND principal_id=%s",(uid(2),uid(8)))
        with self.auth() as changed:
            self.assertGreater(changed.permission_generation,before)
            self.assertNotEqual(a.cache_partition,changed.cache_partition)
        before=changed.permission_generation
        self.admin.execute('UPDATE auth.credentials SET revoked_at=clock_timestamp() WHERE key_digest=%s',(hashlib.sha256(READ_A).hexdigest(),))
        self.assertGreater(self.admin.execute('SELECT generation FROM auth.permission_generations WHERE tenant_id=%s AND project_id=%s',self.scope).fetchone()[0],before)
        with self.assertRaises(AuthError):
            with self.auth(): pass

    def test_revocation_linearizes_with_accepted_request(self):
        self.admin.execute("SET lock_timeout='100ms'")
        with self.auth():
            with self.assertRaises(psycopg.errors.LockNotAvailable):
                self.admin.execute('UPDATE auth.credentials SET revoked_at=clock_timestamp() WHERE key_digest=%s',(hashlib.sha256(READ_A).hexdigest(),))
        self.admin.execute('UPDATE auth.credentials SET revoked_at=clock_timestamp() WHERE key_digest=%s',(hashlib.sha256(READ_A).hexdigest(),))
        with self.assertRaises(AuthError):
            with self.auth(): pass

    def test_expiry_before_response_refuses_and_rolls_back(self):
        self.admin.execute("UPDATE auth.credentials SET expires_at=clock_timestamp()+interval '200 milliseconds' WHERE key_digest=%s",(hashlib.sha256(WRITE_A).hexdigest(),))
        with self.assertRaises(AuthError):
            with self.auth(WRITE_A,action='write'):
                self.request.execute('INSERT INTO core.payloads(tenant_id,project_id,id,body,sha256) VALUES (%s,%s,%s,%s,%s)',(*self.scope,uid(40),self.body,self.digest))
                self.request.execute('SELECT pg_sleep(0.25)')
        self.assertEqual(self.admin.execute('SELECT count(*) FROM core.payloads WHERE id=%s',(uid(40),)).fetchone()[0],0)

    def test_closed_core_is_typed_unavailable_without_key_leak(self):
        self.request.close()
        with self.assertRaises(AuthError) as error:
            with self.auth(): pass
        self.assertEqual(error.exception.code,'core_unavailable')
        self.assertNotIn(READ_A.decode(),str(error.exception))

    def test_auth_owns_transaction_and_refuses_ambient_transaction(self):
        with self.request.transaction():
            with self.assertRaises(AuthError):
                with self.auth(): pass

    def test_other_project_in_same_tenant_is_not_visible_or_writable(self):
        self.admin.execute('INSERT INTO core.payloads(tenant_id,project_id,id,body,sha256) VALUES (%s,%s,%s,%s,%s)',(uid(2),uid(4),uid(5),self.body,self.digest))
        with self.admin.transaction():
            self.admin.execute("INSERT INTO core.records VALUES (%s,%s,%s,'memory',1,false)",(uid(2),uid(4),uid(6)))
            self.admin.execute('INSERT INTO core.record_revisions(tenant_id,project_id,record_id,revision,payload_ref,tombstone) VALUES (%s,%s,%s,1,%s,false)',(uid(2),uid(4),uid(6),uid(5)))
        with self.auth(WRITE_A,action='write'):
            self.assertEqual(self.request.execute('SELECT project_id::text FROM core.records').fetchall(),[(uid(3),)])
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                with self.request.transaction():
                    self.request.execute('INSERT INTO core.payloads(tenant_id,project_id,id,body,sha256) VALUES (%s,%s,%s,%s,%s)',(uid(2),uid(4),uid(40),self.body,self.digest))

    def test_owner_force_rls_does_not_depend_on_api_role_guard(self):
        self.admin.execute(f'ALTER TABLE core.records OWNER TO "{REQUEST}"')
        self.assertEqual(self.request.execute('SELECT count(*) FROM core.records').fetchone()[0],0)

    def test_idempotency_receipts_are_principal_scoped(self):
        for principal in (8,10):
            self.admin.execute("INSERT INTO coordination.idempotency(tenant_id,project_id,principal_id,request_key,request_sha256,outcome) VALUES (%s,%s,%s,'same',%s,'unresolved')",(*self.scope,uid(principal),self.digest))
        with self.auth():
            self.assertEqual(self.request.execute('SELECT principal_id::text FROM coordination.idempotency').fetchall(),[(uid(8),)])
        with self.auth(WRITE_A,action='write'):
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                with self.request.transaction():
                    self.request.execute("INSERT INTO coordination.idempotency(tenant_id,project_id,principal_id,request_key,request_sha256,outcome) VALUES (%s,%s,%s,'other',%s,'unresolved')",(*self.scope,uid(8),self.digest))

    def test_principal_disable_invalidates_permission_generation(self):
        with self.auth() as scope: before=scope.permission_generation
        self.admin.execute('UPDATE auth.principals SET disabled=true WHERE tenant_id=%s AND id=%s',(uid(2),uid(8)))
        self.assertGreater(self.admin.execute('SELECT generation FROM auth.permission_generations WHERE tenant_id=%s AND project_id=%s',self.scope).fetchone()[0],before)
        with self.assertRaises(AuthError):
            with self.auth(): pass
