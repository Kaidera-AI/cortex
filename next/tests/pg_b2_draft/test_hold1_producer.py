"""HOLD-1 finite SOURCE schedules, real P1 and explicit SQL recorders only.

No PostgreSQL parser/engine, native attestation, installed authority or crash
qualification. The fixed-owner contract observations below are NONAUTHORITATIVE
test data. SQL outcomes/live rows are scheduled explicitly, not inferred by a
SQL emulator. Production lineage/durability/fencing remain native gate inputs.
"""
from contextlib import contextmanager
import copy
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch
from uuid import UUID

from psycopg.pq import TransactionStatus
from cortex_core.install import lifecycle as p1, owner
from cortex_core.release.codec import canonical_bytes
from cortex_core.release.contracts import freeze, thaw
from cortex_core.runtime import role_grants as api
import test_hold1_interface as interface
import test_hale_review as hale
from test_pg_b2_rework import Connection


class Interrupted(BaseException):
    """An explicit test schedule boundary; never a product success."""


class SQLSubject(interface.Subject):
    def __init__(self, root):
        super().__init__(root)
        self.helper.context(self.data, lambda c:c['effect'].update(resource_kind='database_role'))
        self.helper.scope(self.data, lambda s:s['effect'].update(resource_kind='database_role'))
        self.roles = dict(hale.ROLES)
        self.before = {
            'role':canonical_bytes({'roles':[
                [oid,name,False,False,False,False,False,False,False,-1,None,None]
                for name,oid in hale.OIDS.items() if name != self.roles['probe']], 'memberships':[]}),
            'acl':canonical_bytes({'objects':[], 'defaults':[]}),
            'database':canonical_bytes({'database':[55,'hale_source_db',hale.OIDS[self.roles['bootstrap']],True,-1],'bindings':None}),
            'hba':b'# unchanged test material\r\n', 'carrier':b'{"source":"inert"}\n'}
        self.live = dict(self.before); self.writer_view = None
        self.status = 'aborted'; self.xid = '4294967396'; self.effect_count = 0
        self.schedule = None; self.trace = []; self.lock_available = True
        self.publication_observer = None; self.on_effect = None; self.on_commit = None
        self.systemid = '1234567890123'; self.timeline = 1
        self.original_durability = ('on','on','on')
        self.binding = {'kind':'pg_b2_hold1', 'protocol_version':2,
            'database':[55,'hale_source_db'], 'system_identifier':self.systemid,
            'timeline_id':1, 'pg_lineage':'SOURCE-only-pg-generation',
            'p1_lineage':'SOURCE-only-p1-generation'}
        self.binding_ref = self.history.retain(canonical_bytes(self.binding))
        raw = canonical_bytes(self.binding)
        self.data['originals'][self.binding_ref['path']] = raw.decode()
        self.helper.scope(self.data, lambda s:s.update(backend_binding_ref=self.binding_ref))
        c=json.loads(self.data['context_raw'])
        self.private={'kind':'bootstrap','phase':'bootstrap','generation':c['policy_generation'],
            'holder':'source-private-owner','fence':52,'expires_at':1700000005,
            'installation_id':c['identity']['installation_id'],'operation_id':c['identity']['operation_id'],
            'session_user_oid':hale.OIDS[self.roles['bootstrap']], 'session_user_name':self.roles['bootstrap'],
            'sql_native':{'native_binding_ref':self.binding_ref,
                'pg_lineage':self.binding['pg_lineage'],'p1_lineage':self.binding['p1_lineage'],
                'continuous_pg':True,'continuous_p1':True,'durability_qualified':True,
                'no_two_phase':True,'no_post_seal_work':True,'writers_ended':False,
                'writer_attempt_id':c['identity']['attempt_id']}}
        self.set_private()
        self.helper.clock_receipt(self.data,lambda r:r['monotonic_deadlines'].update(private=105000000000))
        self.proposal = self.helper.run_gate(owner,self.data)[0]; self.deadline=self.proposal.deadline
        c=thaw(self.proposal.context)
        before_ref=self.history.retain(canonical_bytes({'originals':{k:self.history.retain(v) for k,v in self.before.items()}}))
        self.mandate=canonical_bytes({'protocol_version':2,'identity':c['identity'],'effect':c['effect'],
            'request_ref':c['request_ref'],'phase':'prepare','phase_before_ref':before_ref,
            'native_binding_ref':self.binding_ref})
        s=self
        class Recorder(Connection):
            def __init__(self,writer):super().__init__();self.writer=writer
            def execute(self, statement, parameters=None):
                text=statement if isinstance(statement,str) else statement.as_string()
                s.trace.append(('writer' if self.writer else 'reader',text))
                if text.startswith('SELECT pg_catalog.pg_current_xact_id()'):
                    self.calls.append((text,parameters));return hale.Rows([(s.xid,)])
                if text.startswith('SELECT c.system_identifier'):
                    self.calls.append((text,parameters));return hale.Rows([(s.systemid,s.timeline,False)])
                if text.startswith('SELECT current_setting'):
                    self.calls.append((text,parameters));return hale.Rows([s.original_durability])
                if text.startswith('SELECT pg_catalog.pg_try_advisory_xact_lock'):
                    self.calls.append((text,parameters));return hale.Rows([(s.lock_available,)])
                if text.startswith('SELECT pg_catalog.pg_xact_status'):
                    self.calls.append((text,parameters));return hale.Rows([(s.status,)])
                if text.startswith('SELECT oid FROM pg_catalog.pg_roles'):
                    self.calls.append((text,parameters));return hale.Rows([(900,)])
                if text.startswith('CREATE ROLE '):
                    s.effect_count+=1
                    if s.on_effect:s.on_effect(s)
                    view=s.view(self)
                    roles=json.loads(view['role']);roles['roles'].append(
                        [900,s.roles['probe'],False,False,False,False,False,False,False,-1,None,None])
                    view['role']=canonical_bytes(roles)
                    if s.schedule==3:raise Interrupted('after effect before POSTIMAGE')
                return super().execute(statement,parameters)
            @contextmanager
            def transaction(self):
                previous=self.info.transaction_status
                if previous != TransactionStatus.IDLE:raise AssertionError('unexpected nested source transaction')
                self.info.transaction_status=TransactionStatus.INTRANS
                s.trace.append(('writer' if self.writer else 'reader','BEGIN'))
                if self.writer:s.writer_view=dict(s.live)
                try:
                    yield SimpleNamespace(savepoint_name=None)
                except BaseException:
                    if self.writer:s.status='aborted';s.writer_view=None
                    s.trace.append(('writer' if self.writer else 'reader','ROLLBACK'))
                    raise
                else:
                    if self.writer:
                        if s.on_commit:s.on_commit(s)
                        s.live=dict(s.writer_view);s.writer_view=None;s.status='committed'
                    s.trace.append(('writer' if self.writer else 'reader','COMMIT'))
                    if self.writer and s.schedule==5:raise Interrupted('COMMIT succeeded, reply lost')
                finally:self.info.transaction_status=previous
        self.writer=Recorder(True);self.reader=Recorder(False)
        self.adapter=api._RoleAdapter(history=self.history,identity=c['identity'],connection=self.writer,
            recovery_connection=self.reader,role_names=self.roles,installation_id=UUID(c['identity']['installation_id']),
            embedding_job_kind='source-only',policy=hale.MATRIX,
            materials=SimpleNamespace(snapshot=lambda:{k:self.live[k] for k in ('hba','carrier')}),backends={})
        self.adapter._roles=lambda conn:json.loads(self.view(conn)['role'])
        self.adapter._acl=lambda conn:json.loads(self.view(conn)['acl'])
        self.primitive=api._Primitive(self.adapter,'prepare',self.writer,{})

    def view(self,conn):
        return self.writer_view if conn is self.writer and self.writer_view is not None else self.live

    def set_private(self):
        raw=canonical_bytes(self.private);path='hold1-source-private.json'
        self.data['originals'][path]=raw.decode()
        self.helper.context(self.data,lambda c:c.update(private_binding={'kind':'private','binding_ref':p1._ref(path,raw)}))

    def effect_context(self):
        _,obs=p1._load_entry(self.history,self.consumed)
        return freeze({'context':thaw(self.current().context),'journal_ref':self.consumed,
                       'preimages_ref':obs['preimages_ref'],'deadline':self.deadline})

    def publish(self, original):
        def wrapped(*args,**kwargs):
            attempt=kwargs['attempt'];stage=None
            if attempt.get('sql_postimage_ref') is not None:stage='postimage'
            elif attempt.get('sql_xact_ref') is not None:stage='xid'
            if stage=='xid' and self.schedule==1:raise Interrupted('before xid link')
            result=original(*args,**kwargs)
            if self.publication_observer:self.publication_observer(self,stage)
            if stage=='xid' and self.schedule==2:raise Interrupted('after xid link before effect')
            if stage=='postimage' and self.schedule==4:raise Interrupted('after POSTIMAGE link before COMMIT')
            return result
        return wrapped

    def dispatch(self, *, schedule=None):
        self.schedule=schedule;self.start();effect=self.effect_context()
        with patch.object(p1,'_publish_entry',side_effect=self.publish(p1._publish_entry)):
            try:self.primitive.dispatch(effect)
            except Interrupted:pass
        self.schedule=None;return effect

    def recover(self, effect):
        super().recovery()
        self.helper.context(self.data,lambda c:c['host_lease'].update(fence=c['host_lease']['fence']+1))
        self.helper.edit_record(self.data,'current_state_ref',lambda c:c.update(host_fence=c['host_fence']+1))
        self.private['fence']+=1;self.private['sql_native']['writers_ended']=True;self.set_private()
        self.deadline=self.current().deadline
        return freeze({**thaw(effect),'deadline':self.deadline})

    @contextmanager
    def answer(self,effect):
        value=self.primitive.measure(effect)
        if hasattr(value,'__enter__'):
            with value as answer:yield answer
        else:yield value


class Hold1Producer(unittest.TestCase):
    def test_ready_mandate_uses_bound_actual_phase_before(self):
        with tempfile.TemporaryDirectory() as root:
            s=SQLSubject(root)
            with s.installed():
                prime=getattr(s.primitive,'_prime_sql',None)
                if prime:prime()
                raw=getattr(s.primitive,'sql_mandate_raw',None)
                self.assertIsInstance(raw,bytes,'HOLD1_READY_MANDATE_MISSING')
                m=json.loads(raw);before=json.loads(s.history.read(m['phase_before_ref']))
                self.assertEqual({k:s.history.read(v) for k,v in before['originals'].items()},s.before)
                self.assertEqual(m['native_binding_ref'],s.binding_ref)

    def test_xid_ACK_precedes_first_effect(self):
        with tempfile.TemporaryDirectory() as root:
            s=SQLSubject(root)
            def before_effect(s):
                _,_,parts=s.entry()
                self.assertIsNotNone(parts['attempt'].get('sql_xact_ref'),'HOLD1_EFFECT_BEFORE_XID_ACK')
            s.on_effect=before_effect
            with s.installed():s.dispatch()
            self.assertEqual(s.effect_count,1)

    def test_created_oid_POSTIMAGE_ACK_precedes_COMMIT(self):
        with tempfile.TemporaryDirectory() as root:
            s=SQLSubject(root)
            def before_commit(s):
                _,_,parts=s.entry();ref=parts['attempt'].get('sql_postimage_ref')
                self.assertIsNotNone(ref,'HOLD1_COMMIT_BEFORE_POSTIMAGE_ACK')
                fact=json.loads(s.history.read(ref));refs=fact['observed']['originals']
                roles=json.loads(s.history.read(refs['role']))['roles']
                self.assertIn([900,s.roles['probe']],[r[:2] for r in roles],'HOLD1_ACTUAL_CREATED_OID_MISSING')
            s.on_commit=before_commit
            with s.installed():s.dispatch()

    def crash(self,point,expected):
        with tempfile.TemporaryDirectory() as root:
            s=SQLSubject(root)
            with s.installed():
                effect=s.dispatch(schedule=point)
                effect=s.recover(effect);s.adapter.created.clear()
                with s.answer(effect) as answer:
                    self.assertEqual(answer[0],expected,'HOLD1_CRASH_'+str(point))
                    if expected!='UNKNOWN':
                        result=json.loads(answer[1]);self.assertEqual(result['identity'],thaw(effect['context'])['identity'])
                        self.assertEqual(result['observed']['sql_checkpoint_head'],s.history.head()[0])
            self.assertEqual(s.effect_count,0 if point<3 else 1)

    def test_crash1_before_linked_xid_ended_writer_exact_before(self):self.crash(1,'NOT_APPLIED')
    def test_crash2_after_xid_before_effect_aborted(self):self.crash(2,'NOT_APPLIED')
    def test_crash3_after_effect_before_POSTIMAGE_aborted(self):self.crash(3,'NOT_APPLIED')
    def test_crash4_after_POSTIMAGE_before_COMMIT_aborted(self):self.crash(4,'NOT_APPLIED')
    def test_crash5_COMMIT_reply_loss_created900_no_result_or_cache(self):self.crash(5,'APPLIED')

    def test_same_ID_TLI_committed_poststate_without_lineage_is_UNKNOWN(self):
        with tempfile.TemporaryDirectory() as root:
            s=SQLSubject(root)
            with s.installed():
                effect=s.dispatch(schedule=5);effect=s.recover(effect)
                s.private['sql_native']['continuous_pg']=False;s.set_private()
                with s.answer(effect) as answer:self.assertEqual(answer,('UNKNOWN',None),'HOLD1_NATIVE_LINEAGE_OMITTED')

    def test_absent_xid_live_or_paused_writer_is_not_NOT_APPLIED(self):
        with tempfile.TemporaryDirectory() as root:
            s=SQLSubject(root)
            with s.installed():
                effect=s.dispatch(schedule=1);effect=s.recover(effect)
                s.private['sql_native']['writers_ended']=False;s.set_private()
                with s.answer(effect) as answer:self.assertEqual(answer,('UNKNOWN',None),'HOLD1_ABSENCE_IS_NOT_ABORT')
