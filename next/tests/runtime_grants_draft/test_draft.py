"""PG-A only: synthetic owner bindings, direct unledgered draft transaction.
These controls do not renew frozen105 or prove the finite production client.
"""
import hashlib
import json
from pathlib import Path
from uuid import UUID

import psycopg
from psycopg import sql
from support import Fixture, SurfaceAbsent, ROLE_NAMES, OPERATION, uid, READ_B, MATRIX
from test_c04 import C04
from test_grants import Grants

NEXT=Path(__file__).resolve().parents[2]
DRAFT=NEXT/'schema/auth/007-runtime-grants.sql'

class DraftFixture(Fixture):
    def candidate_if_present(self):
        if self._candidate:return
        if not DRAFT.is_file():raise SurfaceAbsent('SURFACE_ABSENCE:PG-A-draft-SQL')
        # Explicitly a disposable test transaction, not apply_migrations/suffix20.
        with self.root.transaction():self.root.execute(DRAFT.read_text())
        database_oid,owner=self.root.execute('SELECT oid,datdba FROM pg_database WHERE datname=current_database()').fetchone()
        # Only an oracle fixture controller creates these synthetic admissions.
        # Finite-owner material and client implementation remain separately held.
        for logical in ('api_request','probe','supervisor','observer','installer_enrollment'):
            name=ROLE_NAMES[logical]
            oid=self.root.execute('SELECT oid FROM pg_roles WHERE rolname=%s',(name,)).fetchone()[0]
            self.root.execute('INSERT INTO auth.runtime_login_bindings\n              (role_oid,role_name,logical_role,installation_id,active,revision,operation_id,database_oid,database_owner_oid)\n              VALUES (%s,%s,%s,%s,true,1,%s,%s,%s)',(oid,name,logical,uid(1),OPERATION,database_oid,owner))
            self.root.execute(sql.SQL('GRANT CONNECT ON DATABASE {} TO {}').format(sql.Identifier(self.root.execute('SELECT current_database()').fetchone()[0]),sql.Identifier(name)))
            for grant in MATRIX['schemas'].get(logical,[]):
                privilege,schema,*_=grant.split()
                self.root.execute(sql.SQL('GRANT '+privilege+' ON SCHEMA {} TO {}').format(sql.Identifier(schema),sql.Identifier(name)))
        self.root.execute(sql.SQL('GRANT {} TO {} WITH INHERIT FALSE,SET TRUE,ADMIN FALSE').format(sql.Identifier(ROLE_NAMES['request_group']),sql.Identifier(ROLE_NAMES['api_request'])))
        self._candidate=True

class Draft(DraftFixture):
    def test_draft_census_and_private_tables(self):
        self.candidate_if_present()
        count=self.root.execute("SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname IN ('core','auth','coordination','retrieval') AND c.relkind='r' AND c.relname<>'schema_migrations'").fetchone()[0]
        self.assertEqual(count,44)
        for name in ('auth.runtime_login_bindings','coordination.supervisor_leases'):
            self.assertEqual(self.root.execute('SELECT relrowsecurity,relforcerowsecurity FROM pg_class WHERE oid=%s::regclass',(name,)).fetchone(),(True,True))
            for role in ('api_request','probe','supervisor','observer','installer_enrollment'):
                self.refusal(self.login(role),'SELECT count(*) FROM '+name)
        self.assertEqual(self.root.execute('SELECT count(*) FROM core.schema_migrations').fetchone()[0],19)

    def test_map_rejects_duplicate_oid_and_stale_database_owner(self):
        self.candidate_if_present()
        row=self.root.execute('SELECT role_oid,role_name,logical_role,installation_id,active,revision,operation_id,database_oid,database_owner_oid FROM auth.runtime_login_bindings WHERE role_name=%s',(ROLE_NAMES['api_request'],)).fetchone()
        with self.assertRaises(psycopg.errors.UniqueViolation):self.root.execute('INSERT INTO auth.runtime_login_bindings VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)',row)
        self.root.execute('UPDATE auth.runtime_login_bindings SET database_owner_oid=0 WHERE role_name=%s',(ROLE_NAMES['api_request'],))
        self.refusal(self.request,'SELECT auth.runtime_request_installation(%s)',(uid(1),),false_ok=True)

    def test_cross_installation_valid_credential(self):
        self.candidate_if_present();Grants.test_cross_installation_valid_credential_is_not_session_authority(self)

    def test_guard_is_real_session_not_GUC_or_direct_private_helper(self):
        self.candidate_if_present()
        with self.auth():
            self.assertIs(self.allowed(self.request,'SELECT auth.runtime_request_installation(%s)',(uid(1),)),True)
            self.request.execute("SELECT set_config('cortex.installation_id',%s,true)",(uid(20),))
            self.refusal(self.request,'SELECT auth.runtime_request_installation(%s)',(uid(20),),false_ok=True)
        self.refusal(self.request,"SELECT auth.runtime_bound_installation('api_request')")

    def test_map_oid_role_graph_flags_kind_and_revocation(self):
        self.candidate_if_present()
        name=ROLE_NAMES['api_request']
        for command in ('ALTER ROLE {} BYPASSRLS','ALTER ROLE {} SUPERUSER','ALTER ROLE {} INHERIT'):
            with self.root.transaction():
                self.root.execute(sql.SQL(command).format(sql.Identifier(name)))
                self.refusal(self.request,'SELECT auth.runtime_request_installation(%s)',(uid(1),),false_ok=True)
                self.root.execute(sql.SQL('ALTER ROLE {} NOSUPERUSER NOBYPASSRLS NOINHERIT').format(sql.Identifier(name)))
        for column,value in (('role_oid',0),('logical_role','supervisor'),('active',False),('database_oid',0)):
            with self.root.transaction():
                old=self.root.execute(sql.SQL('SELECT {} FROM auth.runtime_login_bindings WHERE role_name=%s').format(sql.Identifier(column)),(name,)).fetchone()[0]
                self.root.execute(sql.SQL('UPDATE auth.runtime_login_bindings SET {}=%s WHERE role_name=%s').format(sql.Identifier(column)),(value,name))
                self.refusal(self.request,'SELECT auth.runtime_request_installation(%s)',(uid(1),),false_ok=True)
                self.root.execute(sql.SQL('UPDATE auth.runtime_login_bindings SET {}=%s WHERE role_name=%s').format(sql.Identifier(column)),(old,name))
        self.root.execute(sql.SQL('GRANT {} TO {} WITH INHERIT TRUE,SET TRUE,ADMIN FALSE').format(sql.Identifier(ROLE_NAMES['request_group']),sql.Identifier(name)))
        self.refusal(self.request,'SELECT auth.runtime_request_installation(%s)',(uid(1),),false_ok=True)

    def test_fence_trigger_positive_decrease_is_refused_and_unchanged(self):
        self.candidate_if_present()
        self.root.execute('INSERT INTO coordination.supervisor_leases VALUES (%s,%s,2,clock_timestamp())',(uid(1),uid(100)))
        with self.assertRaises(psycopg.errors.ObjectNotInPrerequisiteState):self.root.execute('UPDATE coordination.supervisor_leases SET fence=1')
        self.assertEqual(self.root.execute('SELECT fence FROM coordination.supervisor_leases').fetchone(),(2,))
        self.assertEqual(self.root.execute("SELECT tgfoid::regprocedure::text FROM pg_trigger WHERE tgrelid='coordination.supervisor_leases'::regclass AND tgname='supervisor_fence_monotonic'").fetchone(),('coordination.refuse_fence_regression()',))

    def test_frozen_request_lock_cache_and_sequence_positive(self):
        self.candidate_if_present();Grants.test_request_read_lock_and_cache_write_exception_and_sequence(self)

    def test_installer_scope_and_exact_replay(self):
        self.candidate_if_present()
        Grants.test_installer_first_identity_is_scoped_once_and_exact_replay(self)
        Grants.test_installer_other_installation_is_not_admitted_by_name_only(self)
    _empty_identity=Grants._empty_identity

    def test_defaults_closed(self):
        self.candidate_if_present();Grants.test_public_function_and_type_defaults_closed_for_both_creators(self)

    def test_size_connect_exact_database_and_no_other_capability(self):
        self.candidate_if_present()
        verifier=ROLE_NAMES['verifier_owner'];db=self.root.execute('SELECT current_database()').fetchone()[0]
        self.assertEqual(self.root.execute('SELECT rolcanlogin,rolsuper,rolbypassrls,rolcreatedb,rolcreaterole,rolinherit,rolreplication FROM pg_roles WHERE rolname=%s',(verifier,)).fetchone(),(False,False,False,False,False,False,False))
        self.assertEqual(self.root.execute('SELECT count(*) FROM pg_auth_members WHERE roleid=(SELECT oid FROM pg_roles WHERE rolname=%s) OR member=(SELECT oid FROM pg_roles WHERE rolname=%s)',(verifier,verifier)).fetchone()[0],0)
        for privilege,want in [('CONNECT',True),('TEMP',True),('CREATE',False),('CONNECT WITH GRANT OPTION',False)]:
            self.assertIs(self.root.execute('SELECT has_database_privilege(%s,current_database(),%s)',(verifier,privilege)).fetchone()[0],want)
        # Role must have no explicit privilege on any other DB; PUBLIC on foreign
        # databases is outside this migration's authority and is not revoked.
        self.assertEqual(self.root.execute("SELECT count(*) FROM pg_database d CROSS JOIN LATERAL aclexplode(d.datacl) a WHERE d.datname<>current_database() AND a.grantee=(SELECT oid FROM pg_roles WHERE rolname=%s)",(verifier,)).fetchone()[0],0)
        with self.root.transaction():
            self.root.execute(sql.SQL('SET LOCAL ROLE {}').format(sql.Identifier(verifier)))
            self.assertGreater(self.root.execute('SELECT pg_database_size(current_database())').fetchone()[0],0)
        with self.root.transaction():
            self.root.execute(sql.SQL('REVOKE CONNECT ON DATABASE {} FROM {}').format(sql.Identifier(db),sql.Identifier(verifier)))
            self.root.execute(sql.SQL('SET LOCAL ROLE {}').format(sql.Identifier(verifier)))
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                with self.root.transaction():self.root.execute('SELECT pg_database_size(current_database())')
            self.root.execute('RESET ROLE')
            self.root.execute(sql.SQL('GRANT CONNECT ON DATABASE {} TO {}').format(sql.Identifier(db),sql.Identifier(verifier)))

# Unchanged inherited C04 bodies, with only the supplemental draft application
# before their BODY. These are separate controls, not frozen105 node credit.
for name,value in vars(C04).items():
    if name.startswith('test_'):
        def body(self,original=value):
            self.candidate_if_present();original(self)
        setattr(Draft,name,body)
