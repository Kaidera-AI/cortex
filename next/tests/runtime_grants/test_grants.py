"""Frozen ratified9-role /20-44-73 real PostgreSQL oracle; no product SQL."""
import hashlib
import json
import time
from uuid import UUID

import psycopg
from psycopg import sql

from runtime_grants_fixture import (Fixture,MATRIX,ROLE_NAMES,APP_SCHEMAS,TABLE_PRIVS,expected,
                     uid,READ_A,WRITE_A,READ_B,REQUEST,OPERATION,EMBEDDING_KIND,SurfaceAbsent)


class Grants(Fixture):
    def test_original_C04_fixture_has_real_API_session_and_valid_scope(self):
        self.assertEqual(self.request.execute('SELECT session_user,current_user').fetchone(),
                         (ROLE_NAMES['api_request'],REQUEST))
        with self.auth() as scope:
            self.assertEqual(str(scope.installation_id),uid(1))
            self.assertEqual(self.request.execute('SELECT count(*) FROM core.records').fetchone()[0],1)

    def test_cross_installation_valid_credential_is_not_session_authority(self):
        self.candidate_if_present()
        admitted=False
        try:
            with self.auth(READ_B,installation=20):admitted=True
        except Exception as error:
            if getattr(error,'code',None) not in ('forbidden','scope_mismatch','unauthenticated'):raise
        self.assertFalse(admitted,'SEMANTIC:valid B credential bypassed API session installation A')

    def test_no_service_can_assume_private_or_finite_owner(self):
        self.candidate_if_present()
        for key in ('verifier_owner','bootstrap','migrator','installer_enrollment','supervisor'):
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                self.request.execute(sql.SQL('SET ROLE {}').format(sql.Identifier(ROLE_NAMES[key])))

    def test_probe_is_real_connect_select1_and_has_no_tenant_read(self):
        self.candidate_if_present()
        conn=self.login('probe')
        self.assertEqual(conn.execute('SELECT 1').fetchone(),(1,))
        for name in ('core.records','auth.credentials','coordination.session_sources'):
            self.refusal(conn,'SELECT count(*) FROM '+name)

    def test_request_read_lock_and_cache_write_exception_and_sequence(self):
        self.candidate_if_present()
        self.root.execute("INSERT INTO retrieval.search_state VALUES (%s,%s,'pgo','ready')",self.scope)
        before=self.root.execute('SELECT * FROM retrieval.search_state').fetchall()
        with self.auth() as bound:
            self.assertEqual(self.request.execute('SELECT identity FROM retrieval.search_state FOR SHARE').fetchone(),('pgo',))
            try:
                with self.request.transaction():
                    changed=self.request.execute('UPDATE retrieval.search_state SET state=state').rowcount
            except psycopg.errors.InsufficientPrivilege:changed=0
            self.assertEqual(changed,0,'SEMANTIC:lock-support UPDATE became actual write')
            self.request.execute('''INSERT INTO retrieval.query_embeddings
              (tenant_id,project_id,permission_generation,identity,query_digest,owner,lease_until)
              VALUES (%s,%s,%s,'pgo',%s,%s,clock_timestamp()+interval '1 second')''',
              (*self.scope,str(bound.permission_generation),'1'*64,uid(100)))
            self.assertEqual(self.request.execute("UPDATE retrieval.query_embeddings SET expires_at=clock_timestamp()+interval '1 second'").rowcount,1)
            self.assertEqual(self.request.execute('DELETE FROM retrieval.query_embeddings').rowcount,1)
            self.assertTrue(type(self.request.execute("SELECT nextval('retrieval.query_embedding_fences')").fetchone()[0]) is int)
            self.refusal(self.request,"SELECT setval('retrieval.query_embedding_fences',99)")
        self.assertEqual(self.root.execute('SELECT * FROM retrieval.search_state').fetchall(),before)

    def test_public_function_and_type_defaults_closed_for_both_creators(self):
        self.candidate_if_present()
        for creator in ('migrator','verifier_owner'):
            with self.root.transaction():
                if creator=='verifier_owner':
                    self.root.execute(sql.SQL('GRANT CREATE ON SCHEMA auth TO {}').format(sql.Identifier(ROLE_NAMES[creator])))
                self.root.execute(sql.SQL('SET LOCAL ROLE {}').format(sql.Identifier(ROLE_NAMES[creator])))
                self.root.execute('CREATE FUNCTION auth.pgo_default_canary() RETURNS integer LANGUAGE sql AS $$SELECT 1$$')
                self.root.execute('CREATE TYPE auth.pgo_default_type AS ENUM (\'x\')')
                self.root.execute('CREATE TABLE auth.pgo_default_table(id integer)')
                self.root.execute('RESET ROLE')
                # F01: every unauthorized effective recipient, including grant
                # option. Finite superusers and the actual creator are explicit
                # ownership exceptions, not evidence of a PUBLIC default leak.
                for logical in ROLE_NAMES:
                    if MATRIX['roles'][logical]['superuser'] or logical==creator:
                        continue
                    for privilege in ('EXECUTE','EXECUTE WITH GRANT OPTION'):
                        self.assertFalse(self.root.execute('SELECT has_function_privilege(%s,%s,%s)',
                            (ROLE_NAMES[logical],'auth.pgo_default_canary()',privilege)).fetchone()[0],
                            'SEMANTIC:future function leaked to '+logical+':'+privilege)
                    for privilege in ('USAGE','USAGE WITH GRANT OPTION'):
                        self.assertFalse(self.root.execute('SELECT has_type_privilege(%s,%s,%s)',
                            (ROLE_NAMES[logical],'auth.pgo_default_type',privilege)).fetchone()[0],
                            'SEMANTIC:future type leaked to '+logical+':'+privilege)
                for logical in ('request_group','api_request','probe','supervisor','observer','installer_enrollment'):
                    for priv in TABLE_PRIVS:
                        self.assertFalse(self.root.execute("SELECT has_table_privilege(%s,'auth.pgo_default_table',%s)",(ROLE_NAMES[logical],priv)).fetchone()[0],
                                         'SEMANTIC:future table leaked to '+logical)
                self.root.execute('DROP FUNCTION auth.pgo_default_canary()')
                self.root.execute('DROP TYPE auth.pgo_default_type')
                self.root.execute('DROP TABLE auth.pgo_default_table')
                if creator=='verifier_owner':
                    self.root.execute(sql.SQL('REVOKE CREATE ON SCHEMA auth FROM {}').format(sql.Identifier(ROLE_NAMES[creator])))

    def test_map_is_forced_private_and_one_oid_cannot_bind_two_installations(self):
        self.candidate_if_present();self.require('auth.runtime_login_bindings')
        columns={r[0] for r in self.root.execute("SELECT column_name FROM information_schema.columns WHERE table_schema='auth' AND table_name='runtime_login_bindings'")}
        needed={'role_oid','role_name','logical_role','installation_id','active','revision','operation_id'}
        self.assertTrue(needed<=columns,'SEMANTIC:protected map shape differs from owner binding contract')
        self.bind_current()
        row=self.root.execute('''SELECT role_oid,role_name,logical_role,active,revision,operation_id
              FROM auth.runtime_login_bindings WHERE role_name=%s''',(ROLE_NAMES['api_request'],)).fetchone()
        self.assertIsNotNone(row)
        names=[x[0] for x in self.root.execute("SELECT attname FROM pg_attribute WHERE attrelid='auth.runtime_login_bindings'::regclass AND attnum>0 AND NOT attisdropped ORDER BY attnum")]
        selected=[sql.SQL('%s::uuid') if name=='installation_id' else sql.Identifier(name) for name in names]
        with self.assertRaises(psycopg.errors.UniqueViolation):
            self.root.execute(sql.SQL('INSERT INTO auth.runtime_login_bindings ({}) SELECT {} FROM auth.runtime_login_bindings WHERE role_name=%s').format(
                sql.SQL(',').join(map(sql.Identifier,names)),sql.SQL(',').join(selected)),(uid(20),ROLE_NAMES['api_request']))
        flags=self.root.execute("SELECT relrowsecurity,relforcerowsecurity FROM pg_class WHERE oid='auth.runtime_login_bindings'::regclass").fetchone()
        self.assertEqual(flags,(True,True))
        self.refusal(self.request,'SELECT count(*) FROM auth.runtime_login_bindings')

    def test_session_map_ignores_forged_GUC_and_definer_current_user(self):
        self.private('auth.runtime_request_installation(uuid)')
        with self.auth():
            self.assertIs(self.allowed(self.request,'SELECT auth.runtime_request_installation(%s)',(uid(1),)),True)
            self.request.execute("SELECT set_config('cortex.installation_id',%s,true)",(uid(20),))
            self.refusal(self.request,'SELECT auth.runtime_request_installation(%s)',(uid(20),),false_ok=True)
            self.request.execute("SELECT set_config('cortex.installation_id',%s,true)",(uid(1),))
        sup=self.login('supervisor')
        sup.execute("SELECT set_config('cortex.installation_id',%s,false)",(uid(20),))
        self.refusal(sup,'SELECT coordination.runtime_lease_acquire(%s,%s,1000)',(uid(20),uid(100)),false_ok=True)

    def test_stale_oid_name_recreation_does_not_adopt_old_map(self):
        self.private('auth.runtime_request_installation(uuid)')
        self.request.close()
        name=ROLE_NAMES['api_request']
        old=self.root.execute('SELECT oid FROM pg_roles WHERE rolname=%s',(name,)).fetchone()[0]
        db=self.root.execute('SELECT current_database()').fetchone()[0]
        self.root.execute(sql.SQL('REVOKE ALL ON DATABASE {} FROM {}').format(sql.Identifier(db),sql.Identifier(name)))
        self.root.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(name)))
        self.root.execute(sql.SQL('CREATE ROLE {} LOGIN NOINHERIT NOSUPERUSER NOBYPASSRLS').format(sql.Identifier(name)))
        self.root.execute(sql.SQL('GRANT {} TO {} WITH INHERIT FALSE,SET TRUE,ADMIN FALSE').format(sql.Identifier(REQUEST),sql.Identifier(name)))
        self.root.execute(sql.SQL('GRANT CONNECT ON DATABASE {} TO {}').format(sql.Identifier(db),sql.Identifier(name)))
        self.assertNotEqual(old,self.root.execute('SELECT oid FROM pg_roles WHERE rolname=%s',(name,)).fetchone()[0])
        self.request=self.login('api_request',request_role=True)
        self.refusal(self.request,'SELECT auth.runtime_request_installation(%s)',(uid(1),),false_ok=True)

    def test_wrong_logical_kind_and_revoked_map_refuse(self):
        self.private('auth.runtime_request_installation(uuid)')
        for column,value in (('logical_role','supervisor'),('active',False)):
            with self.root.transaction():
                self.root.execute(sql.SQL('UPDATE auth.runtime_login_bindings SET {}=%s WHERE role_name=%s').format(sql.Identifier(column)),(value,ROLE_NAMES['api_request']))
            self.refusal(self.request,'SELECT auth.runtime_request_installation(%s)',(uid(1),),false_ok=True)
            self.root.execute("UPDATE auth.runtime_login_bindings SET logical_role='api_request',active=true WHERE role_name=%s",(ROLE_NAMES['api_request'],))

    def test_installer_first_identity_is_scoped_once_and_exact_replay(self):
        self.candidate_if_present()
        self._empty_identity(2,3)
        conn=self.login('installer_enrollment')
        args=(uid(1),uid(2),uid(3),uid(300),uid(301),hashlib.sha256(b'public-first-identity').hexdigest(),'pgo-first')
        first=self.allowed(conn,'SELECT auth.first_identity(%s,%s,%s,%s,%s,%s,%s)',args)
        replay=self.allowed(conn,'SELECT auth.first_identity(%s,%s,%s,%s,%s,%s,%s)',args)
        self.assertEqual(first['principal_id'],uid(300));self.assertIs(replay['replayed'],True)
        self.refusal(conn,'SELECT auth.first_identity(%s,%s,%s,%s,%s,%s,%s)',args[:-1]+('different',))

    def test_installer_other_installation_is_not_admitted_by_name_only(self):
        self.candidate_if_present();self._empty_identity(21,3)
        conn=self.login('installer_enrollment')
        self.refusal(conn,'SELECT auth.first_identity(%s,%s,%s,%s,%s,%s,%s)',
            (uid(20),uid(21),uid(3),uid(300),uid(301),'2'*64,'pgo-wrong-install'))

    def test_exact_object_owners_and_forced_private_table_policies(self):
        self.candidate_if_present()
        for table in MATRIX['tables']:
            self.require(table['name'])
            owner=('verifier_owner' if 'OWNER_IMPLICIT_ALL' in table['direct_privileges']['verifier_owner'] else 'migrator')
            actual=self.root.execute('SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid=%s::regclass',(table['name'],)).fetchone()[0]
            self.assertEqual(actual,ROLE_NAMES[owner],table['name'])
        for fn in MATRIX['functions']:
            self.require(fn['signature'],'function')
            actual=self.root.execute('SELECT pg_get_userbyid(proowner) FROM pg_proc WHERE oid=%s::regprocedure',(fn['signature'],)).fetchone()[0]
            self.assertEqual(actual,ROLE_NAMES[fn['owner']],fn['signature'])
        self.assertEqual(self.root.execute('SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid=%s::regclass',(MATRIX['view']['name'],)).fetchone()[0],ROLE_NAMES['verifier_owner'])
        for name in APP_SCHEMAS:
            self.assertEqual(self.root.execute('SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname=%s',(name,)).fetchone()[0],ROLE_NAMES['migrator'])

    def _empty_identity(self,tenant,project):
        for table in ('credentials','project_grants','permission_generations','principals'):
            self.root.execute('DELETE FROM auth.'+table+' WHERE tenant_id=%s',(uid(tenant),))
        self.root.execute("UPDATE core.projects SET name='default' WHERE tenant_id=%s AND id=%s",(uid(tenant),uid(project)))


def role_case(logical):
    def body(self):
        self.candidate_if_present();p=MATRIX['roles'][logical];name=ROLE_NAMES[logical]
        columns=('rolcanlogin','rolsuper','rolbypassrls','rolinherit','rolcreatedb','rolcreaterole','rolreplication')
        row=self.root.execute('SELECT '+','.join(columns)+' FROM pg_roles WHERE rolname=%s',(name,)).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row,tuple(p[k] for k in ('login','superuser','bypassrls','inherit','createdb','createrole','replication')))
        edges=self.root.execute('''SELECT granted.rolname,m.set_option,m.inherit_option,m.admin_option
           FROM pg_auth_members m JOIN pg_roles granted ON granted.oid=m.roleid
           JOIN pg_roles member ON member.oid=m.member WHERE member.rolname=%s''',(name,)).fetchall()
        self.assertEqual(edges,[(REQUEST,True,False,False)] if logical=='api_request' else [])
        if logical=='verifier_owner':
            self.assertEqual(self.root.execute('SELECT count(*) FROM pg_auth_members WHERE roleid=(SELECT oid FROM pg_roles WHERE rolname=%s)',(name,)).fetchone()[0],0)
    return body


def table_case(logical):
    def body(self):
        self.candidate_if_present()
        for table in MATRIX['tables']:
            self.require(table['name']);privileges=table['direct_privileges'][logical]
            for priv in TABLE_PRIVS:
                actual=self.root.execute('SELECT has_table_privilege(%s,%s,%s)',(ROLE_NAMES[logical],table['name'],priv)).fetchone()[0]
                self.note('table',logical,table['name'],priv,actual)
                self.assertEqual(actual,expected(privileges,priv),f'SEMANTIC:{logical}:{table["name"]}:{priv}')
                option=self.root.execute('SELECT has_table_privilege(%s,%s,%s)',(ROLE_NAMES[logical],table['name'],priv+' WITH GRANT OPTION')).fetchone()[0]
                self.assertEqual(option,expected(privileges,'GRANT_OPTION'))
            flags=self.root.execute('SELECT relrowsecurity,relforcerowsecurity FROM pg_class WHERE oid=%s::regclass',(table['name'],)).fetchone()
            self.assertEqual(flags,(True,True),table['name'])
    return body


def function_case(logical):
    def body(self):
        self.candidate_if_present()
        for fn in MATRIX['functions']:
            self.require(fn['signature'],'function');privileges=fn['direct_privileges'][logical]
            for option,priv in ((False,'EXECUTE'),(True,'EXECUTE WITH GRANT OPTION')):
                actual=self.root.execute('SELECT has_function_privilege(%s,%s,%s)',(ROLE_NAMES[logical],fn['signature'],priv)).fetchone()[0]
                self.note('function',logical,fn['signature'],priv,actual)
                self.assertEqual(actual,expected(privileges,'GRANT_OPTION' if option else 'EXECUTE'),
                                 f'SEMANTIC:{logical}:{fn["signature"]}:{priv}')
    return body


def other_case(logical):
    def body(self):
        self.candidate_if_present();name=ROLE_NAMES[logical]
        finite=MATRIX['roles'][logical]['superuser']
        schema_rights=MATRIX['schemas'].get(logical,[])
        for schema in APP_SCHEMAS+('public',):
            for priv in ('USAGE','CREATE'):
                allowed=finite or any(x.split()[:2]==[priv,schema] for x in schema_rights)
                actual=self.root.execute('SELECT has_schema_privilege(%s,%s,%s)',(name,schema,priv)).fetchone()[0]
                self.note('schema',logical,schema,priv,actual);self.assertEqual(actual,allowed,f'SEMANTIC:{logical}:{schema}:{priv}')
                option=self.root.execute('SELECT has_schema_privilege(%s,%s,%s)',(name,schema,priv+' WITH GRANT OPTION')).fetchone()[0]
                self.assertEqual(option,finite)
        for priv in ('CONNECT','TEMP','CREATE'):
            actual=self.root.execute('SELECT has_database_privilege(%s,current_database(),%s)',(name,priv)).fetchone()[0]
            self.note('database',logical,'owned',priv,actual)
            self.assertEqual(actual,expected(MATRIX['database']['direct_privileges'][logical],priv),f'SEMANTIC:{logical}:database:{priv}')
            self.assertEqual(self.root.execute('SELECT has_database_privilege(%s,current_database(),%s)',(name,priv+' WITH GRANT OPTION')).fetchone()[0],finite)
        for priv in ('SELECT','INSERT','UPDATE','DELETE'):
            actual=self.root.execute('SELECT has_table_privilege(%s,%s,%s)',(name,MATRIX['view']['name'],priv)).fetchone()[0]
            self.assertEqual(actual,expected(MATRIX['view']['direct_privileges'][logical],priv))
            self.assertEqual(self.root.execute('SELECT has_table_privilege(%s,%s,%s)',(name,MATRIX['view']['name'],priv+' WITH GRANT OPTION')).fetchone()[0],finite or logical==MATRIX['view']['owner'])
        for priv in ('USAGE','SELECT','UPDATE'):
            actual=self.root.execute('SELECT has_sequence_privilege(%s,%s,%s)',(name,MATRIX['sequence']['name'],priv)).fetchone()[0]
            self.assertEqual(actual,expected(MATRIX['sequence']['direct_privileges'][logical],priv))
            self.assertEqual(self.root.execute('SELECT has_sequence_privilege(%s,%s,%s)',(name,MATRIX['sequence']['name'],priv+' WITH GRANT OPTION')).fetchone()[0],finite or logical==MATRIX['sequence']['owner'])
        actual=self.root.execute('SELECT has_type_privilege(%s,%s,\'USAGE\')',(name,MATRIX['type']['name'])).fetchone()[0]
        self.assertEqual(actual,expected(MATRIX['type']['direct_privileges'][logical],'USAGE'))
        self.assertEqual(self.root.execute('SELECT has_type_privilege(%s,%s,\'USAGE WITH GRANT OPTION\')',(name,MATRIX['type']['name'])).fetchone()[0],finite or logical==MATRIX['type']['owner'])
        for priv in TABLE_PRIVS:
            actual=self.root.execute("SELECT has_table_privilege(%s,'core.schema_migrations',%s)",(name,priv)).fetchone()[0]
            self.assertEqual(actual,finite)
    return body


def signature_case(fn):
    def body(self):
        self.candidate_if_present();self.require(fn['signature'],'function')
        row=self.root.execute('''SELECT p.prosecdef,r.rolname,pg_get_function_result(p.oid),p.provolatile,p.proconfig
            FROM pg_proc p JOIN pg_roles r ON r.oid=p.proowner WHERE p.oid=%s::regprocedure''',(fn['signature'],)).fetchone()
        self.assertEqual(row[:3],(True,ROLE_NAMES['verifier_owner'],fn['return_type']))
        self.assertIn(row[3],('v','s'))
        settings=dict(x.split('=',1) for x in row[4] or []);path=settings.get('search_path','').replace(' ','').split(',')
        self.assertTrue(path and path[0]=='pg_catalog' and path[-1]=='pg_temp' and 'public' not in path)
        self.assertEqual(len(path),len(set(path)))
        for schema in path[1:-1]:
            self.assertIn(schema,APP_SCHEMAS,'SEMANTIC:untrusted middle search_path schema')
            self.assertEqual(self.root.execute('SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname=%s',
                (schema,)).fetchone(),(ROLE_NAMES['migrator'],),'SEMANTIC:middle schema owner')
        if fn['signature'] in ('coordination.runtime_observe(uuid)','coordination.runtime_lease_health(uuid)'):
            self.assertEqual(row[3],'v')
    return body


for role in MATRIX['roles']:
    for label,builder in (('role',role_case),('tables',table_case),('functions',function_case),('other_objects',other_case)):
        setattr(Grants,'test_'+label+'__'+role,builder(role))
for fn in MATRIX['functions']:
    if fn.get('proposed'):
        setattr(Grants,'test_signature__'+fn['signature'].split('(')[0].replace('.','__'),signature_case(fn))
