"""Owned real-PG fixtures; reuse C04 seed and U13, never emulate product grants."""
import hashlib
import importlib
import json
import os
from pathlib import Path
import sys
import threading
import time
from unittest.mock import patch
from uuid import UUID

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

HERE=Path(__file__).resolve().parent
NEXT=HERE.parents[1]
sys.path.insert(0,str(NEXT/'tests'))
import core_db_fixture
from core_db_fixture import auth_fixture,uid,READ_A,WRITE_A,READ_B,REQUEST
from cortex_core.auth import AuthError
from cortex_core.migrations import apply_migrations

MATRIX_BYTES=(HERE/'ratified_matrix.json').read_bytes()
assert hashlib.sha256(MATRIX_BYTES).hexdigest()=='0e101ad23039101e95bc2bae2026b53817a1ae84fdd91b6dc1dc25770fe58af2'
MATRIX=json.loads(MATRIX_BYTES)
ORIGINAL=json.loads((HERE/'original_manifest.json').read_bytes())
SCHEMA=NEXT/'schema'
STEP='runtime-grants-0001'
ROLE_NAMES={
 'bootstrap':'kaidera-test-grant-bootstrap',
 'migrator':'kaidera-test-grant-migrator',
 'request_group':REQUEST,
 'verifier_owner':'kaidera-runtime-core-verifier',
 'api_request':auth_fixture.API,
 'probe':'kaidera-test-grant-probe',
 'supervisor':'kaidera-test-grant-supervisor',
 'observer':'kaidera-test-grant-observer',
 'installer_enrollment':'kaidera-runtime-core-installer',
}
APP_SCHEMAS=('core','auth','coordination','retrieval')
TABLE_PRIVS=('SELECT','INSERT','UPDATE','DELETE','TRUNCATE','REFERENCES','TRIGGER','MAINTAIN')
OPERATION=UUID(uid(900))
EMBEDDING_KIND='pgo.fixture.embedding'  # public synthetic owner input, NOT production identity
OBSERVATIONS=[]


class SurfaceAbsent(AssertionError):
    """Missing source surface; never counted as a semantic boundary proof."""


def expected(privileges,privilege):
    return (privilege in privileges or any(x in privileges for x in
            ('OWNER_IMPLICIT_ALL','SUPERUSER_DURING_FINITE_PHASE',
             'OBJECT_OWNER_AFTER_DISABLE','OWNER_IMPLICIT_ALL_AFTER_DISABLE')))


class Fixture(core_db_fixture.Fixture):
    def setUp(self):
        self.dsn=os.environ['TEST_DATABASE_URL']
        self.root=psycopg.connect(self.dsn,autocommit=True)
        self.addCleanup(self.root.close)
        database=self.root.execute('SELECT current_database()').fetchone()[0]
        token=os.environ['PGO_OWNED_RUN_ID']
        if not database.startswith('kaidera-test-grant-db-'):
            raise RuntimeError('refuse non-owned database name')
        marker=self.root.execute('SELECT run_id FROM public.pgo_owned_fixture').fetchall()
        if marker!=[(token,)]:raise RuntimeError('owned database marker differs')
        self._prepare_fixture_roles()
        # The original seed always runs the immutable19 prefix. New candidate
        # application/binding occurs in TEST BODY so absence is never setup RED.
        with patch.object(auth_fixture.test_schema,'apply_migrations',self._apply_original):
            core_db_fixture.Fixture.setUp(self)
        self._candidate=False

    def _prepare_fixture_roles(self):
        for key,policy in MATRIX['roles'].items():
            name=ROLE_NAMES[key]
            if not self.root.execute('SELECT 1 FROM pg_roles WHERE rolname=%s',(name,)).fetchone():
                self.root.execute(sql.SQL('CREATE ROLE {}').format(sql.Identifier(name)))
            flags=['LOGIN' if policy['login'] else 'NOLOGIN',
                   'SUPERUSER' if policy['superuser'] else 'NOSUPERUSER',
                   'CREATEDB' if policy['createdb'] else 'NOCREATEDB',
                   'CREATEROLE' if policy['createrole'] else 'NOCREATEROLE',
                   'INHERIT' if policy['inherit'] else 'NOINHERIT',
                   'REPLICATION' if policy['replication'] else 'NOREPLICATION',
                   'BYPASSRLS' if policy['bypassrls'] else 'NOBYPASSRLS']
            self.root.execute(sql.SQL('ALTER ROLE {} '+ ' '.join(flags)).format(sql.Identifier(name)))
        # Reset only the explicitly owned disposable role graph between cases.
        edges=self.root.execute('''SELECT granted.rolname,member.rolname
            FROM pg_auth_members m JOIN pg_roles granted ON granted.oid=m.roleid
            JOIN pg_roles member ON member.oid=m.member
            WHERE granted.rolname=ANY(%s) OR member.rolname=ANY(%s)''',
            (list(ROLE_NAMES.values()),list(ROLE_NAMES.values()))).fetchall()
        for granted,member in edges:
            self.root.execute(sql.SQL('REVOKE {} FROM {}').format(sql.Identifier(granted),sql.Identifier(member)))
        self.root.execute(sql.SQL('ALTER DATABASE {} OWNER TO {}').format(
            sql.Identifier(self.root.execute('SELECT current_database()').fetchone()[0]),
            sql.Identifier(ROLE_NAMES['bootstrap'])))

    def login(self,logical,*,request_role=False):
        conn=psycopg.connect(make_conninfo(self.dsn,user=ROLE_NAMES[logical]),autocommit=True)
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute('SELECT session_user').fetchone()[0],ROLE_NAMES[logical])
        if request_role:conn.execute(sql.SQL('SET ROLE {}').format(sql.Identifier(REQUEST)))
        return conn

    def _apply_original(self,_ignored,*args,**kwargs):
        # Reuse the original migration engine, selecting its named19th target;
        # all manifests/checksums are still validated by that same engine.
        with psycopg.connect(make_conninfo(self.dsn,user=ROLE_NAMES['migrator']),autocommit=True) as conn:
            return apply_migrations(conn,through=ORIGINAL['migrations'][-1]['id'])

    def has(self,name,kind='table'):
        function='to_regclass' if kind=='table' else 'to_regprocedure'
        return self.root.execute('SELECT '+function+'(%s)',(name,)).fetchone()[0] is not None

    def require(self,name,kind='table'):
        if not self.has(name,kind):raise SurfaceAbsent(f'SURFACE_ABSENCE:{kind}:{name}')

    def require_candidate(self):
        manifest=json.loads((SCHEMA/'manifest.json').read_bytes())
        if STEP not in {row['id'] for row in manifest['migrations']}:
            raise SurfaceAbsent('SURFACE_ABSENCE:migration:'+STEP)
        self.assertEqual(len(manifest['migrations']),20)
        self.assertEqual(manifest['migrations'][:19],ORIGINAL['migrations'])
        self.assertEqual(manifest['migrations'][-1]['id'],STEP)
        self.assertEqual(manifest['migrations'][-1]['file'],'auth/007-runtime-grants.sql')
        return manifest

    def client(self):
        try:module=importlib.import_module('cortex_core.runtime.role_grants')
        except ModuleNotFoundError as error:
            if error.name not in ('cortex_core.runtime','cortex_core.runtime.role_grants'):raise
            raise SurfaceAbsent('SURFACE_ABSENCE:finite-owner:runtime.role_grants') from None
        for name in ('prepare_runtime_roles','bind_runtime_roles','close_finite_roles','restore_runtime_roles'):
            if not callable(getattr(module,name,None)):
                raise SurfaceAbsent('SURFACE_ABSENCE:finite-owner:'+name)
        return module

    def candidate_if_present(self):
        if self._candidate:return
        manifest=json.loads((SCHEMA/'manifest.json').read_bytes())
        if STEP not in {row['id'] for row in manifest['migrations']}:return
        self.require_candidate()
        self.request.close()
        with self.login('migrator') as conn:apply_migrations(conn,through=STEP)
        self._candidate=True
        self.bind_current()

    def bind_current(self,*,embedding_kind=EMBEDDING_KIND,operation=OPERATION):
        self.require('auth.runtime_login_bindings')
        client=self.client()
        self.request.close()
        with self.login('bootstrap') as owner:
            receipt=client.bind_runtime_roles(owner,installation_id=UUID(uid(1)),
                role_names=dict(ROLE_NAMES),operation_id=operation,
                embedding_job_kind=embedding_kind,activate=True)
        self.request=self.login('api_request',request_role=True)
        return receipt

    def private(self,signature):
        self.candidate_if_present()
        self.require(signature,'function')
        self.require('auth.runtime_login_bindings')

    def note(self,kind,role,name,privilege,actual):
        OBSERVATIONS.append({'node':self.id(),'class':kind,'role':role,
                             'object':name,'privilege':privilege,'actual':actual})

    def allowed(self,conn,statement,args=()):
        try:return conn.execute(statement,args).fetchone()[0]
        except psycopg.errors.InsufficientPrivilege as error:
            self.fail('SEMANTIC:valid-positive-operation-denied:'+error.sqlstate)

    def refusal(self,conn,statement,args=(),*,false_ok=False):
        try:
            with conn.transaction():value=conn.execute(statement,args).fetchone()
        except psycopg.Error as error:
            if error.sqlstate in ('42501','55000'):return error.sqlstate
            raise  # syntax/FK/setup/network errors are INCONCLUSIVE, never RED
        self.assertTrue(false_ok and value==(False,),
                        'SEMANTIC:operation admitted instead of closed refusal')
        return 'FALSE'

    def lease(self,conn=None,*,holder=100,ttl=10000):
        self.private('coordination.runtime_lease_acquire(uuid,uuid,integer)')
        conn=conn or self.login('supervisor')
        fence=self.allowed(conn,'SELECT coordination.runtime_lease_acquire(%s,%s,%s)',
                           (uid(1),uid(holder),ttl))
        self.assertTrue(type(fence) is int and fence>0)
        return conn,fence

    def background(self,logical,statement,args=(),*,readonly=False):
        conn=self.login(logical)
        if readonly:conn.execute('BEGIN READ ONLY ISOLATION LEVEL READ COMMITTED')
        pid=conn.info.backend_pid;box={}
        def work():
            try:box['value']=conn.execute(statement,args).fetchone()[0]
            except BaseException as error:box['error']=error
        thread=threading.Thread(target=work,daemon=True);thread.start()
        self.addCleanup(lambda:conn.close())
        return conn,pid,box,thread

    def wait_blocked(self,pid,blocker):
        end=time.monotonic()+2
        while time.monotonic()<end:
            if blocker in self.root.execute('SELECT pg_blocking_pids(%s)',(pid,)).fetchone()[0]:return
            time.sleep(.01)
        self.fail('SEMANTIC:expected real PostgreSQL lock barrier not reached')

    def joined(self,thread,box):
        thread.join(3);self.assertFalse(thread.is_alive(),'worker not drained')
        return box
