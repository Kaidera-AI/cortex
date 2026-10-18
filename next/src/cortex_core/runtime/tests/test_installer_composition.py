"""Real S3 composition SOURCE controls, not PostgreSQL/effects acceptance.

The runner stages byte-identical ship modules into their existing package layout.
Only NONSECRET unlock/memory-record/noncrypto-cipher/clock/entropy and CA/driver IO
are doubled. Owner, phase guard, connectors, resources, migration/seed bodies,
catalog observer/policy and durable ACK run unchanged. SQL responses below are a
strict named fixture, not an SQL interpreter or proof of PostgreSQL semantics.
"""
from contextlib import contextmanager
import contextvars
import copy
from enum import IntEnum
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import UUID

from cortex_core import migrations
from cortex_core.release.codec import canonical_bytes
from cortex_core.runtime import custody, custody_fs, resources
from cortex_core.runtime.bootstrap_config import decode_config
from cortex_core.runtime.signed_member import Refusal
from cortex_core.runtime.tests.test_bootstrap_config import sample
from cortex_core.runtime.tests.test_installer_custody import SourceAdapters, PURPOSES
from cortex_core.schema import seed
from cortex_ship.install import provision, roles


class SyntheticTransactionStatus(IntEnum):
    IDLE = 0
    INTRANS = 2


def normalized(sql):
    return ' '.join(sql.split())


class NonsecretSQLFixture:
    """Below-driver fixture for this exact finite trace; unknown SQL is an error.

    It models rows/transaction state only, never role privileges, SQL execution,
    TLS, locks or database durability. Migration SQL bytes must exactly match the
    immutable manifest. Provision SQL must exactly match the unchanged renderer.
    The source tests independently assert order, private authority and cleanup.
    """
    def __init__(self, test):
        self.test = test
        self.config = test.config
        self.events = []
        self.connections = []
        self.hook = None
        self.close_failure = None
        self.bad_flags = False
        self.bad_ledger = False
        self.receipt = []
        self.anchors = [[], [], []]
        self.bindings = []
        self.ledger = []
        self.prepared = migrations.prepare_migrations()
        self.applied_sql = []
        self.policy = roles.load_roles()
        self.oids = {p: 100+i for i,p in enumerate(PURPOSES)}
        self.group_oids = {g: 200+i for i,g in enumerate(roles.GROUPS)}
        self.created = {'bootstrap'}
        self.edges = []
        binding = roles.bind_logins(dict(self.config.role_logins), self.config.vault_root)
        self.steps = {s: roles.provision_steps(binding, s.lower(), seed_ack=(s=='CLOSE'))
                      for s in ('PRE','POST','LEDGER','CLOSE')}
        self.step_index = {s: 0 for s in self.steps}

    @property
    def owner(self):
        return custody._INSTALLER.get()[0]

    def connect(self, **kwargs):
        purpose = next(p for p,r in self.config.roles.items() if r.login==kwargs['user'])
        self.test.assertEqual(kwargs['sslmode'], 'verify-full')
        self.test.assertEqual(kwargs['password'], self.test.password(purpose).decode())
        self.test.assertEqual(kwargs['connect_timeout'], 10)
        self.test.assertTrue(kwargs['autocommit'])
        connection = NonsecretSQLConnection(self, purpose)
        self.connections.append(connection)
        self.events.append(('connect', self.owner._stage, purpose))
        return connection

    def rows(self, connection, sql, params):
        stage = self.owner._stage
        q = normalized(sql)
        self.events.append(('sql', stage, connection.purpose, q, params))
        if self.hook:
            self.hook(stage, connection, q)
        if q.startswith("SELECT pg_catalog.set_config('statement_timeout'"):
            self.test.assertEqual(params, ('100000','100000'))
            return []
        if q.startswith('SET TRANSACTION ISOLATION LEVEL '):
            self.test.assertIn(q, ('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY',
                                  'SET TRANSACTION ISOLATION LEVEL READ COMMITTED READ ONLY'))
            return []
        if stage in self.steps:
            index = self.step_index[stage]
            expected_sql, purpose = self.steps[stage][index]
            self.test.assertEqual(sql, expected_sql)
            self.test.assertEqual(params, None if purpose is None else (self.test.password(purpose).decode(),))
            self.step_index[stage] += 1
            if stage=='PRE' and q.startswith('DO $$'):
                self.created.add(provision.FOUR[(index-3)//3])
            if stage=='POST' and q.startswith('GRANT '):
                self.edges = [(self.oids['api_request'], self.group_oids[roles.REQUEST_GROUP], True, False, False)]
            if stage=='CLOSE':
                self.test.assertIsNotNone(self.owner._ack)
            return []
        if stage=='MIGRATE':
            if q in ('SELECT pg_advisory_xact_lock(6203001)', 'CREATE SCHEMA IF NOT EXISTS core'):
                return []
            if q == normalized('''CREATE TABLE IF NOT EXISTS core.schema_migrations (
                migration_id text PRIMARY KEY, sha256 text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
                applied_at timestamptz NOT NULL DEFAULT now())'''):
                return []
            if q=='INSERT INTO core.schema_migrations (migration_id,sha256) VALUES (%s,%s)':
                self.test.assertEqual(tuple(params), self.prepared[len(self.ledger)][:2])
                self.ledger.append(tuple(params)); return []
            if any(sql==body for _,_,body in self.prepared):
                self.test.assertEqual(sql, self.prepared[len(self.applied_sql)][2])
                self.applied_sql.append(sql); return []
        if q=='SELECT migration_id,sha256 FROM core.schema_migrations':
            return self.ledger[:-1] if self.bad_ledger else list(self.ledger)
        if stage=='SEED':
            if q=='SELECT datname,oid::bigint,datdba::bigint FROM pg_catalog.pg_database WHERE datname=current_database()':
                return [(self.config.database['name'], 300, self.oids['bootstrap'])]
            if q=='SELECT session_user,usesysid::bigint,datid::bigint FROM pg_catalog.pg_stat_activity WHERE pid=pg_backend_pid()':
                return [(self.config.role_logins['bootstrap'],self.oids['bootstrap'],300)]
            if q.startswith('SELECT rolname,oid::bigint,rolcanlogin,rolsuper,'):
                self.test.assertEqual(self.created, set(PURPOSES))
                rows=[]
                for p in PURPOSES:
                    flags=[self.policy[p][k] for k in roles._FLAG_KEYS]
                    if p=='api_request' and self.bad_flags: flags[1]=True
                    rows.append((self.config.role_logins[p], self.oids[p], *flags))
                return rows+[(g,self.group_oids[g],*([False]*7)) for g in roles.GROUPS]
            if q.startswith('SELECT member::bigint,roleid::bigint, set_option,inherit_option,admin_option '):
                return self.edges
            if q.startswith('SELECT role_oid::bigint,role_name::text, logical_role,installation_id::text,active,revision,operation_id::text,'):
                return list(self.bindings)
            if q.startswith("SELECT 'schema',nspname,nspname,nspowner::bigint "):
                return [('schema','core','core',self.oids['bootstrap'])]
            if q in ('SELECT pg_catalog.pg_advisory_xact_lock(6203001)',
                     'LOCK TABLE auth.runtime_login_bindings,core.installations, core.tenants,core.projects IN SHARE ROW EXCLUSIVE MODE'):
                return []
            if q=='SELECT id::text FROM core.installations WHERE id=%s::uuid FOR UPDATE': return list(self.anchors[0])
            if q=='SELECT id::text,installation_id::text FROM core.tenants WHERE id=%s::uuid OR installation_id=%s::uuid FOR UPDATE': return list(self.anchors[1])
            if q=='SELECT tenant_id::text,id::text,name FROM core.projects WHERE tenant_id=%s::uuid FOR UPDATE': return list(self.anchors[2])
            if q.startswith('SELECT installation_id::text,tenant_id::text, project_id::text,principal_id::text,credential_id::text,key_digest,request_key '): return list(self.receipt)
            if q in ('SELECT 1 FROM auth.principals WHERE tenant_id=%s::uuid',
                     "SELECT 1 FROM core.record_aliases WHERE tenant_id=%s::uuid AND source_namespace='cortex.core.identity'",
                     "SELECT 1 FROM core.records WHERE tenant_id=%s::uuid AND kind='core.identity'"): return []
            if q=='INSERT INTO core.installations(id) VALUES(%s::uuid)': self.anchors[0]=[tuple(params)]; return []
            if q=='INSERT INTO core.tenants(id,installation_id) VALUES(%s::uuid,%s::uuid)': self.anchors[1]=[tuple(params)]; return []
            if q=="INSERT INTO core.projects(tenant_id,id,name) VALUES(%s::uuid,%s::uuid,'default')": self.anchors[2]=[(*params,'default')]; return []
            if q.startswith('INSERT INTO auth.runtime_login_bindings('): self.bindings.append(tuple(params)); self.bindings.sort(); return []
            if q=='SELECT auth.first_identity( %s::uuid,%s::uuid,%s::uuid,%s::uuid,%s::uuid,%s,%s)':
                self.receipt=[tuple(params)]
                return [({'principal_id':params[3],'credential_id':params[4],'project_id':params[2],'replayed':False},)]
            if q=='SET LOCAL ROLE "kaidera-runtime-core-request"': return []
            if q=='SELECT auth.enrollment_readiness(%s::uuid)::text': return [('ready',)]
        raise AssertionError('unmodelled below-driver SQL: '+stage+': '+q)


class NonsecretSQLConnection:
    def __init__(self, fixture, purpose):
        self.fixture, self.purpose = fixture, purpose
        self.closed = False
        self.autocommit = True
        self.info = SimpleNamespace(transaction_status=SyntheticTransactionStatus.IDLE)

    @contextmanager
    def transaction(self):
        f=self.fixture
        f.test.assertIs(self.info.transaction_status,SyntheticTransactionStatus.IDLE)
        self.info.transaction_status=SyntheticTransactionStatus.INTRANS
        f.events.append(('begin',f.owner._stage,self.purpose))
        try:
            yield SimpleNamespace(savepoint_name='')
        except BaseException:
            f.events.append(('rollback',f.owner._stage,self.purpose)); raise
        else:
            f.events.append(('commit',f.owner._stage,self.purpose))
        finally:
            self.info.transaction_status=SyntheticTransactionStatus.IDLE

    def execute(self, sql, params=None, **kwargs):
        rows=self.fixture.rows(self,sql,params)
        return SimpleNamespace(fetchall=lambda:copy.deepcopy(rows))

    def close(self):
        f=self.fixture
        f.events.append(('close',f.owner._stage,self.purpose))
        self.closed=True
        if f.close_failure==(f.owner._stage,self.purpose): raise OSError('NONSECRET-close-fault')


class InstallerCompositionTests(unittest.TestCase):
    def setUp(self):
        from contextlib import ExitStack
        self.stack=ExitStack();self.addCleanup(self.stack.close)
        self.adapters=SourceAdapters()
        self.stack.enter_context(self.adapters.installed())
        value=sample('/source-only/NONSECRET-vault')
        value['roles']['installer_enrollment']['login']=roles.INSTALLER_LOGIN
        self.raw=json.dumps(value).encode()
        self.config=decode_config(self.raw,mode='install')
        self.fixture=NonsecretSQLFixture(self)
        self.stack.enter_context(patch.object(resources,'time',SimpleNamespace(monotonic=lambda:self.adapters.clock)))
        self.stack.enter_context(patch.object(resources,'_certificate',return_value=(Path('/NONSECRET/ca'),b'NONSECRET-ca-digest')))
        self.stack.enter_context(patch.object(resources,'_driver_connect',lambda **kwargs:self.fixture.connect(**kwargs)))
        pq=ModuleType('psycopg.pq');pq.TransactionStatus=SyntheticTransactionStatus
        self.stack.enter_context(patch.dict(sys.modules,{'psycopg.pq':pq}))
        self.stack.enter_context(patch.object(seed.secrets,'token_hex',return_value='c'*64))
        self.stack.enter_context(patch.object(seed,'uuid4',side_effect=[UUID('10000000-0000-0000-0000-000000000004'),UUID('10000000-0000-0000-0000-000000000005')]))
        self.calls=[]
        self.reads=[]
        actual_read=self.adapters.read
        def read(directory,name):
            owner=custody._INSTALLER.get()
            stage=None if owner is None else owner[0]._stage
            if name=='client':
                self.reads.append((stage,self.adapters.events.count('write-double'),[c.closed for c in self.fixture.connections]))
            return actual_read(directory,name)
        self.stack.enter_context(patch.object(custody_fs,'read_record',read))
        previous=sys.getprofile()
        def profile(frame,event,arg):
            if event=='call' and frame.f_code.co_name in ('_installer_password','seed_runtime_bindings','_reload_seed_receipt','apply_migrations','validate_catalog'):
                self.calls.append((frame.f_code.co_name,frame.f_code.co_filename,dict(frame.f_locals)))
        sys.setprofile(profile);self.addCleanup(lambda:sys.setprofile(previous))

    def password(self,purpose): return ('NONSECRET-bound-'+purpose+'-fixture-long').encode()

    @contextmanager
    def opened(self):
        # Subtest invocations get independent fixture state, not success retained
        # from a previous run. This is below-IO state, never installer authority.
        self.fixture=NonsecretSQLFixture(self)
        self.adapters.records.clear();self.adapters.payloads.clear()
        with custody.open_installer_custody(self.config,unlock_fd=99,key_id='NONSECRET-id',deadline=200.0) as owner:
            for purpose in PURPOSES: owner.import_credential(purpose,self.password(purpose))
            yield owner

    def complete(self,owner):
        try: return owner.run('NONSECRET-operation')
        except Refusal as error: self.fail('actual fixed sequence must complete, refused '+error.reason)

    def test_fixed_owner_sequence_completes_without_callback_authority(self):
        with self.opened() as owner:
            result=self.complete(owner)
            self.assertEqual(owner._stage,'COMPLETE')
            self.assertEqual(owner._next,7)
            self.assertEqual(result['operation_key'],'NONSECRET-operation')
            self.assertEqual(canonical_bytes(result),owner._ack)
            self.assertTrue(all(c.closed for c in self.fixture.connections))
            self.assertIsNone(resources._SEED.get())
        self.assertEqual(self.fixture.applied_sql,[sql for _,_,sql in self.fixture.prepared])
        self.assertEqual(self.fixture.ledger,[(i,d) for i,d,_ in self.fixture.prepared])

    def test_real_call_order_password_sites_seed_catalog_ack_and_cleanup(self):
        with self.opened() as owner:
            self.complete(owner)
        connects=[(e[1],e[2]) for e in self.fixture.events if e[0]=='connect']
        self.assertEqual(connects,[('PRE','bootstrap'),('MIGRATE','migrator'),('POST','bootstrap'),('LEDGER','bootstrap'),('SEED','bootstrap'),('SEED','installer_enrollment'),('SEED','api_request'),('CLOSE','bootstrap')])
        for phase in self.fixture.steps:
            self.assertEqual(self.fixture.step_index[phase],len(self.fixture.steps[phase]))
        calls=[x[0] for x in self.calls]
        self.assertEqual(calls.count('apply_migrations'),1)
        self.assertEqual(calls.count('seed_runtime_bindings'),1)
        self.assertEqual(calls.count('_reload_seed_receipt'),1)
        self.assertEqual(calls.count('validate_catalog'),2)
        private=[c[2]['purpose'] for c in self.calls if c[0]=='_installer_password']
        self.assertEqual(private,list(provision.FOUR))
        catalog_calls=[c[2] for c in self.calls if c[0]=='validate_catalog']
        self.assertTrue(all(type(c['live']) is resources.SeedResources and c['live']._config is self.config for c in catalog_calls))
        ack_reads=[r for r in self.reads if r[0]=='ACK']
        self.assertTrue(ack_reads)
        self.assertTrue(all(all(closed) for _,_,closed in ack_reads))
        self.assertEqual({writes for _,writes,_ in ack_reads},{7})
        self.assertEqual(self.adapters.events.count('write-double'),7)
        stages=[]
        for event in self.fixture.events:
            if not stages or stages[-1]!=event[1]: stages.append(event[1])
        self.assertEqual(stages,['PRE','MIGRATE','POST','LEDGER','SEED','CLOSE'])
        for before,after in zip(('PRE','MIGRATE','POST','LEDGER','SEED'),('MIGRATE','POST','LEDGER','SEED','CLOSE')):
            last_close=max(i for i,e in enumerate(self.fixture.events) if e[0]=='close' and e[1]==before)
            first_connect=min(i for i,e in enumerate(self.fixture.events) if e[0]=='connect' and e[1]==after)
            self.assertLess(last_close,first_connect)
        migrate_begins=[e for e in self.fixture.events if e[:2]==('begin','MIGRATE')]
        self.assertEqual(len(migrate_begins),1)
        self.assertEqual([e[2] for e in self.fixture.events if e[:2]==('close','SEED')],['api_request','installer_enrollment','bootstrap'])

    def test_public_resolver_is_never_called_by_the_real_chain(self):
        with self.opened() as owner:
            # No replacement resolver, even a refusing one: profile actual calls.
            seen=[];previous=sys.getprofile()
            def trace(frame,event,arg):
                if event=='call' and frame.f_code is custody.CustodyVault.resolve.__code__: seen.append(frame)
                if previous: previous(frame,event,arg)
            sys.setprofile(trace)
            try: self.complete(owner)
            finally: sys.setprofile(previous)
            self.assertEqual(seen,[])

    def test_direct_phase_and_sequence_calls_refuse_without_connector(self):
        for phase in ('PRE','MIGRATE','POST','LEDGER','CLOSE'):
            with self.subTest(phase=phase),self.opened() as owner:
                before=len(self.fixture.events)
                with self.assertRaises(Refusal):
                    provision.run_phase(self.config,custody=owner._vault,deadline=200.0,phase=phase)
                self.assertEqual(len(self.fixture.events),before)
                self.assertTrue(owner._tainted)

        with self.opened() as owner:
            before=len(self.fixture.events)
            with self.assertRaises(Refusal):
                provision.provision_sequence(self.config,custody=owner._vault,deadline=200.0,operation_key='NONSECRET-operation')
            self.assertEqual(len(self.fixture.events),before)
            self.assertTrue(owner._tainted)

    def test_foreign_identity_deadline_and_context_refuse_without_io(self):
        actions=[lambda o:provision.run_phase(decode_config(self.raw,mode='install'),custody=o._vault,deadline=200.0,phase='PRE'),
                 lambda o:provision.run_phase(self.config,custody={},deadline=200.0,phase='PRE'),
                 lambda o:provision.run_phase(self.config,custody=o._vault,deadline=201.0,phase='PRE'),
                 lambda o:contextvars.copy_context().run(provision.run_phase,self.config,custody=o._vault,deadline=200.0,phase='PRE')]
        for action in actions:
            with self.subTest(action=actions.index(action)),self.opened() as owner:
                before=len(self.fixture.events)
                with self.assertRaises(Refusal): action(owner)
                self.assertEqual(len(self.fixture.events),before)

    def test_reentrant_run_and_repeated_phase_deny_inside_real_pre(self):
        for kind in ('reentrant','repeat'):
            with self.subTest(kind=kind),self.opened() as owner:
                fired=[]
                def hook(stage,connection,q):
                    if fired:return
                    fired.append(stage)
                    with self.assertRaises(Refusal):
                        if kind=='reentrant': owner.run('NONSECRET-operation')
                        else: provision.run_phase(self.config,custody=owner._vault,deadline=200.0,phase='PRE')
                self.fixture.hook=hook
                with self.assertRaises(Refusal): owner.run('NONSECRET-operation')
                self.assertEqual(fired,['PRE'])
                self.assertTrue(owner._tainted)
                self.assertFalse(any(e[:2]==('connect','MIGRATE') for e in self.fixture.events))
                self.fixture.hook=None

    def test_original_deadline_expiry_blocks_later_stage(self):
        with self.opened() as owner:
            def hook(stage,connection,q):
                if stage=='PRE':self.adapters.clock=200.0
            self.fixture.hook=hook
            with self.assertRaises(Refusal): owner.run('NONSECRET-operation')
            self.assertTrue(owner._tainted)
            self.assertFalse(any(e[:2]==('connect','MIGRATE') for e in self.fixture.events))
        self.assertTrue(all(c.closed for c in self.fixture.connections))

    def test_cleanup_failure_blocks_next_phase_and_closes_all_seed_sessions(self):
        for phase,purpose in [('PRE','bootstrap'),('SEED','api_request')]:
            with self.subTest(phase=phase),self.opened() as owner:
                self.fixture.close_failure=(phase,purpose)
                with self.assertRaisesRegex(Refusal,'resources_cleanup'): owner.run('NONSECRET-operation')
                self.assertTrue(all(c.closed for c in self.fixture.connections))
                self.assertTrue(owner._tainted)
                self.assertIsNone(resources._SEED.get())
                self.assertFalse(any(e[:2]==('connect','CLOSE') for e in self.fixture.events))
                self.assertIsNone(owner._ack)
                self.fixture.close_failure=None

    def test_invalid_durable_receipt_refuses_ack_and_never_closes_logins(self):
        with self.opened() as owner:
            actual_read=custody_fs.read_record
            def corrupt_read(directory,name):
                value=actual_read(directory,name)
                if owner._stage=='ACK' and name=='client' and value is not None:
                    envelope=json.loads(value[0]); token=custody._unbase(envelope['ciphertext'],maximum=48000)
                    nonce,plain,aad=self.adapters.payloads[token]
                    payload=json.loads(plain);payload['receipt']['credential_sha256']='0'*64
                    self.adapters.payloads[token]=(nonce,json.dumps(payload).encode(),aad)
                return value
            with patch.object(custody_fs,'read_record',corrupt_read):
                with self.assertRaises(Refusal): owner.run('NONSECRET-operation')
            self.assertTrue(owner._tainted)
            self.assertIsNone(owner._ack)
            self.assertFalse(any(e[:2]==('connect','CLOSE') for e in self.fixture.events))

    def test_catalog_predicates_and_complete_schema_are_not_bypassed(self):
        for fault in ('bad_flags','bad_ledger'):
            with self.subTest(fault=fault),self.opened() as owner:
                setattr(self.fixture,fault,True)
                with self.assertRaises(Refusal): owner.run('NONSECRET-operation')
                self.assertIsNone(owner._ack)
                self.assertFalse(any(e[:2]==('connect','CLOSE') for e in self.fixture.events))
                setattr(self.fixture,fault,False)

    def test_seed_accessor_and_catalog_reject_stale_real_resource(self):
        retained=[]
        with self.opened() as owner:
            def hook(stage,connection,q):
                if stage=='SEED' and not retained:
                    retained.append(resources.current_seed_resources(self.config,200.0))
                    with self.assertRaises(Refusal):resources.current_seed_resources(self.config,201.0)
                    with self.assertRaises(Refusal):resources.current_seed_resources(decode_config(self.raw,mode='install'),200.0)
                if stage=='CLOSE':
                    with self.assertRaises(Refusal):retained[0].check_deadline()
                    with self.assertRaises(roles.RoleRefusal):roles.validate_catalog(self.config,{},phase='seed',live=retained[0])
            self.fixture.hook=hook
            self.complete(owner)
            self.assertTrue(retained[0]._closed)
            with self.assertRaises(Refusal):resources.current_seed_resources(self.config,200.0)
            with self.assertRaises(Refusal):owner.run('NONSECRET-operation')
            self.assertEqual(len([e for e in self.fixture.events if e[:2]==('connect','PRE')]),1)


if __name__=='__main__':unittest.main()
