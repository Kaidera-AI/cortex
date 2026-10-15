"""HOLD-1 private lifecycle interface, real test-owned P1/fsync/flock only.

Finite markers/checkpoint payloads are source DATA, not PostgreSQL outcome,
native lineage/durability, installed authority or producer qualification.
Baseline calls omit unsupported kwargs to reach old behavior; assertions never
credit a missing signature, TypeError, import or fixture failure.
"""
from contextlib import contextmanager
import copy
from dataclasses import replace
import fcntl
import inspect
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from cortex_core.install import lifecycle as p1, owner
from cortex_core.release.codec import canonical_bytes
from cortex_core.release.contracts import freeze, thaw
from cortex_core.runtime import signed_member
from non_authoritative_signed_member_double import NonAuthoritativeSignedMemberDouble
import test_lifecycle_receipt as storage


def call(fn, *args, **kwargs):
    parameters = inspect.signature(fn).parameters
    return fn(*args, **{k:v for k,v in kwargs.items() if k in parameters})


class Subject:
    def __init__(self, root):
        self.root = Path(root)
        self.fixture = storage.LifecycleReceiptOracle()
        self.helper, self.data, self.proposal = self.fixture.setup_data()
        self.deadline = self.proposal.deadline
        self.history = p1._History(self.root)
        self.before = self.fixture.preimages()
        self.initial_revision = self.proposal.observed_state_revision
        self.trace = []; self.barrier = False; self.effects = 0
        self.expired = False; self.revoked = False; self.on_inputs = None
        self.owner_reads = 0; self.consumed = None; self.reserved = None
        phase_before = self.history.retain(canonical_bytes({
            'originals':{k:self.history.retain(v) for k,v in self.before.items()}}))
        binding = self.history.retain(canonical_bytes({'source_data_only':'unqualified-native-binding'}))
        c = thaw(self.proposal.context)
        self.mandate = canonical_bytes({'protocol_version':2,'identity':c['identity'],
            'effect':c['effect'],'request_ref':c['request_ref'],'phase':'prepare',
            'phase_before_ref':phase_before,'native_binding_ref':binding})
        # Existing lock files are prepared before byte-exact no-write assertions.
        with self.history.operation(): pass
        fd=os.open(self.root/'.journal.lock',os.O_RDWR|os.O_CREAT,0o600);os.close(fd)

    def inputs(self):
        self.owner_reads += 1
        if self.on_inputs is not None:self.on_inputs(self)
        d=copy.deepcopy(self.data)
        revision=self.history.current_revision() or self.initial_revision
        self.helper.context(d,lambda c:c.update(state_revision=revision))
        self.helper.edit_record(d,'current_state_ref',lambda s:s.update(revision=revision))
        if self.expired:self.helper.clock(d,lambda c:c.update(now_utc=1700000005))
        if self.revoked:self.helper.edit_record(d,'consent_ref',lambda c:c.update(revoked=True))
        return self.helper.inputs(owner,d)

    @contextmanager
    def installed(self, primitive=None):
        double=NonAuthoritativeSignedMemberDouble(self.data['runtime_lock_raw'].encode(),
            aggregate_id=self.data['aggregate_id'],member_name='runtime.lock.json')
        kind=thaw(self.proposal.context)['effect']['resource_kind']
        with patch.object(owner,'_installed_owner_inputs',self.inputs),double.installed(signed_member), \
                patch.object(p1,'_installed_lifecycle',return_value=p1._Bindings(
                    self.history,{} if primitive is None else {kind:primitive})):
            yield

    def current(self):
        return owner.propose_effect(self.data['request_raw'].encode())

    def reserve(self, *, mandate=True, deadline=True):
        inputs=self.inputs();proposal=self.current()
        self.reserved=call(p1._reserve,self.history,self.data['request_raw'].encode(),proposal,
            inputs.originals,self.before,clock_raw=inputs.current_clock_raw,inputs=inputs,
            sql_mandate_raw=self.mandate if mandate else None,
            deadline=self.deadline if deadline else None)
        return self.reserved

    def consume(self, *, deadline=True):
        inputs=self.inputs();current=self.current()
        self.consumed=call(p1._consume,self.history,self.reserved,current,
            inputs.current_clock_raw,originals=inputs.originals,inputs=inputs,
            deadline=self.deadline if deadline else None)
        return self.consumed

    def start(self):self.reserve();self.consume();return self.consumed

    def entry(self, ref=None):
        ref=self.history.head()[0] if ref is None else ref
        event,obs=p1._load_entry(self.history,thaw(ref))
        return event,obs,{k:p1._decode(self.history.read(obs[k+'_ref']))
                          for k in ('context','reservation','attempt')}

    def checkpoint(self, stage, *, edit=None, forged=False):
        head=self.history.head()[0];event,obs,parts=self.entry()
        a=parts['attempt'];a.setdefault('sql_mandate_ref',self.history.retain(self.mandate))
        a.setdefault('sql_xact_ref',None);a.setdefault('sql_postimage_ref',None)
        fact={'stage':stage,'consumed_ref':self.consumed,
              'sql_mandate_ref':a['sql_mandate_ref'],
              'sql_xact_ref':a['sql_xact_ref'] if stage=='postimage' else None,
              'observed':{'source_data_only':stage}}
        a['sql_xact_ref' if stage=='xid' else 'sql_postimage_ref']=self.history.retain(canonical_bytes(fact))
        if edit:edit(event,obs,parts,fact)
        if forged:
            bundle={k+'_ref':self.history.retain(canonical_bytes(v)) for k,v in parts.items()}
            bundle['preimages_ref']=obs['preimages_ref']
            candidate={**event,'sequence':event['sequence']+1,'previous_event_ref':head,
                       'observations_ref':self.history.retain(canonical_bytes(bundle))}
            return call(self.history.append,candidate,expected_ref=head,deadline=self.deadline)
        return call(p1._publish_entry,self.history,context=parts['context'],reservation=parts['reservation'],
            attempt=a,preimages_ref=obs['preimages_ref'],request_ref=event['request_ref'],
            release_ref=event['release_binding_ref'],expected_ref=head,phase=event['phase'],
            recorded_at=1700000001,deadline=self.deadline)

    def result(self, state='applied_verified', *, head=None):
        event,_,_=self.entry(self.consumed)
        return canonical_bytes({'identity':event['identity'],'effect':event['effect'],
            'request_ref':event['request_ref'],'state':state,
            'observed':{'source_data_only':True,'sql_checkpoint_head':self.history.head()[0] if head is None else head}})

    def terminal(self, raw=None, *, head=None, revision=None, deadline=True):
        raw=self.result() if raw is None else raw
        return call(p1._record_result,self.history,self.consumed,raw,
            publication_head=self.history.head()[0] if head is None else head,
            publication_revision=self.history.current_revision() if revision is None else revision,
            deadline=self.deadline if deadline else None)

    def tree(self):
        return {p.relative_to(self.root).as_posix():p.read_bytes()
                for p in self.root.rglob('*') if p.is_file()}

    def recovery(self):
        q=json.loads(self.data['request_raw']);q.update(operation='recover',journal_ref=self.consumed)
        old=q['identity'];q['identity']={**old,'request_id':'hold-recover-request',
            'attempt_id':'hold-recover-attempt','parent_attempt_id':old['attempt_id']}
        raw=canonical_bytes(q);c=json.loads(self.data['context_raw']);path=c['request_ref']['path']
        self.data['request_raw']=raw.decode();self.data['originals'][path]=raw.decode()
        self.helper.context(self.data,lambda c:c.update(identity=q['identity'],request_ref=p1._ref(path,raw),
            owner_epoch='hold-recovery-epoch',session_id='hold-recovery-session',nonce='hold-recovery-nonce'))
        self.helper.scope(self.data,lambda s:s.update(identity=q['identity'],operation='recover'))
        self.helper.edit_record(self.data,'peer_ref',lambda p:p.update(
            owner_epoch='hold-recovery-epoch',session_id='hold-recovery-session'))
        self.helper.clock_receipt(self.data,lambda r:r.update(owner_epoch='hold-recovery-epoch'))
        self.helper.edit_record(self.data,'clock_ref',lambda c:c.update(owner_epoch='hold-recovery-epoch'))
        self.helper.clock(self.data,lambda c:c.update(owner_epoch='hold-recovery-epoch'))
        self.deadline=self.current().deadline

    def primitive(self, *, recovering=False):
        s=self
        class Finite:
            sql_mandate_raw=s.mandate
            def snapshot(self):return dict(s.before)
            def dispatch(self, effect):
                s.consumed=thaw(effect['journal_ref']);s.original_effect=effect
                s.received_deadline=effect.get('deadline')
                s.checkpoint('xid');s.effects+=1
                (s.root/'effect-marker').write_bytes(b'one finite source effect\n')
                s.checkpoint('postimage')
            @contextmanager
            def measure(self, effect):
                s.barrier=True;s.trace.append('measure-enter')
                try:
                    raw=s.result()
                    yield ('APPLIED',raw) if recovering else raw
                finally:s.trace.append('measure-exit');s.barrier=False
        return Finite()


class Hold1InterfaceOracle(unittest.TestCase):
    def success(self, fn):
        try:return fn()
        except owner.OwnerRefusal as error:
            self.fail('required lifecycle behavior refused in BODY: '+error.detail)

    def refused(self, fn, *, detail=None):
        with self.assertRaises(owner.OwnerRefusal) as caught:fn()
        if detail:self.assertEqual(caught.exception.detail,detail)

    def test_H1_mandate_is_bound_before_consumption_and_never_terminal_truth(self):
        with tempfile.TemporaryDirectory() as root:
            s=Subject(root)
            with s.installed():
                s.reserve();_,_,reserved=s.entry();s.consume();_,_,consumed=s.entry()
                a=consumed['attempt']
                self.assertIsNotNone(a.get('sql_mandate_ref'),'mandate was not durably linked before consume')
                self.assertEqual(s.history.read(a['sql_mandate_ref']),s.mandate)
                self.assertEqual(a['sql_mandate_ref'],reserved['attempt']['sql_mandate_ref'])
                self.assertIsNone(a['sql_xact_ref']);self.assertIsNone(a['sql_postimage_ref'])
                self.assertIsNone(a['result_ref']);self.assertIsNone(a['reconciliation_ref'])

    def test_H1_mandate_subject_and_canonical_bytes_are_checked_before_intent(self):
        for field in ('identity','effect','request_ref','phase_before_ref','native_binding_ref','canonical'):
            with self.subTest(field=field),tempfile.TemporaryDirectory() as root:
                s=Subject(root);m=json.loads(s.mandate)
                if field=='identity':m[field]['attempt_id']='foreign'
                elif field=='effect':m[field]['effect_id']='foreign'
                elif field=='request_ref':m[field]['sha256']='0'*64
                elif field!='canonical':m[field]['sha256']='0'*64
                s.mandate=canonical_bytes(m)+(b' ' if field=='canonical' else b'')
                with s.installed():
                    before=s.tree();self.refused(s.reserve);self.assertEqual(s.tree(),before)

    def test_H1_missing_deadline_cannot_mandate_or_consume_v2(self):
        with tempfile.TemporaryDirectory() as root:
            s=Subject(root)
            with s.installed():
                self.refused(lambda:s.reserve(deadline=False));self.assertIsNone(s.history.head()[0])
                s.reserve();before=s.tree();self.refused(lambda:s.consume(deadline=False))
                self.assertEqual(s.tree(),before)

    def test_H1_failed_or_unknown_mandatory_publication_never_dispatches(self):
        for point in ('before-intent','after-intent','after-consume'):
            with self.subTest(point=point),tempfile.TemporaryDirectory() as root:
                s=Subject(root);slot=s.primitive();publish=s.history._publish
                def interrupted(folder,name,raw):
                    if folder=='events':
                        event=json.loads(raw);parts=p1._decode(s.history.read(event['observations_ref']))
                        attempt=p1._decode(s.history.read(parts['attempt_ref']))
                        mandated=attempt.get('sql_mandate_ref') is not None
                        hit=mandated and ((point.endswith('intent') and attempt['dispatch_state']=='not_consumed')
                            or (point=='after-consume' and attempt['dispatch_state']=='consumed_result_unknown'))
                        if hit and point=='before-intent':raise owner.OwnerRefusal('atomicity_unavailable')
                        publish(folder,name,raw)
                        if hit:raise owner.OwnerRefusal('atomicity_unavailable')
                    else:publish(folder,name,raw)
                with s.installed(slot),patch.object(s.history,'_publish',interrupted):
                    self.refused(lambda:p1.execute_effect(s.data['request_raw'].encode()))
                self.assertEqual(s.effects,0,'finite effect crossed unacknowledged mandatory publication')

    def test_H2_ordered_same_consumption_prefix_accepts_only_complete_applied(self):
        with tempfile.TemporaryDirectory() as root:
            s=Subject(root)
            with s.installed():
                s.start();original=s.tree();s.checkpoint('xid');s.checkpoint('postimage')
                raw=s.result();ref=self.success(lambda:s.terminal(raw));event,_,parts=s.entry(ref)
                self.assertEqual(s.history.read(event['result_ref']),raw)
                for name,b in original.items():self.assertEqual((s.root/name).read_bytes(),b)
                self.assertEqual(parts['attempt']['consumption_sequence'],s.entry(s.consumed)[0]['sequence'])

    def test_H2_not_applied_can_publish_at_each_valid_prefix(self):
        for stages in ((),('xid',),('xid','postimage')):
            with self.subTest(stages=stages),tempfile.TemporaryDirectory() as root:
                s=Subject(root)
                with s.installed():
                    s.start()
                    for stage in stages:s.checkpoint(stage)
                    raw=s.result('not_applied_verified');ref=self.success(lambda:s.terminal(raw))
                    self.assertEqual(s.history.read(s.entry(ref)[0]['result_ref']),raw)

    def test_H2_applied_without_postimage_and_tentative_truth_are_refused(self):
        for kind in ('no-xid','xid-only','tentative-result','tentative-reconciliation'):
            with self.subTest(kind=kind),tempfile.TemporaryDirectory() as root:
                s=Subject(root)
                with s.installed():
                    s.start()
                    if kind!='no-xid':s.checkpoint('xid')
                    if kind.startswith('tentative'):
                        def edit(event,obs,parts,fact):
                            a=parts['attempt'];a['result_ref' if kind.endswith('result') else 'reconciliation_ref']=a['sql_postimage_ref']
                        s.checkpoint('postimage',edit=edit,forged=True)
                    before=s.tree();self.refused(s.terminal);self.assertEqual(s.tree(),before)

    def test_H2_foreign_fork_skipped_duplicate_and_changed_originals_refuse(self):
        for kind in ('foreign','fork','skipped','duplicate','mandate','first-originals','consume-sequence','authority','raw-binding'):
            with self.subTest(kind=kind),tempfile.TemporaryDirectory() as root:
                s=Subject(root)
                with s.installed():
                    s.start()
                    if kind!='skipped':s.checkpoint('xid')
                    def edit(event,obs,parts,fact):
                        a=parts['attempt']
                        if kind=='foreign':event['identity']={**event['identity'],'attempt_id':'foreign'}
                        elif kind=='fork':
                            v=dict(fact);v['consumed_ref']=s.reserved
                            a['sql_postimage_ref']=s.history.retain(canonical_bytes(v))
                        elif kind=='mandate':a['sql_mandate_ref']=s.history.retain(canonical_bytes({'protocol_version':99}))
                        elif kind=='first-originals':obs['preimages_ref']=s.history.retain(canonical_bytes({'identity':event['identity'],'originals':{}}))
                        elif kind=='consume-sequence':a['consumption_sequence']+=1
                        elif kind=='authority':parts['context']['nonce']='replacement'
                    s.checkpoint('xid' if kind=='duplicate' else 'postimage',edit=edit,forged=True)
                    raw=s.result(head=s.consumed) if kind=='raw-binding' else s.result()
                    before=s.tree();self.refused(lambda:s.terminal(raw));self.assertEqual(s.tree(),before)

    def test_H2_caller_head_and_revision_cannot_prove_current_prefix(self):
        with tempfile.TemporaryDirectory() as root:
            s=Subject(root)
            with s.installed():
                s.start();xid=s.checkpoint('xid');s.checkpoint('postimage');before=s.tree()
                self.refused(lambda:s.terminal(head=xid))
                self.refused(lambda:s.terminal(revision=s.history.current_revision()+1))
                self.assertEqual(s.tree(),before)

    def test_H3_publication_advances_fresh_revision_without_renewing_consumption(self):
        with tempfile.TemporaryDirectory() as root:
            s=Subject(root)
            with s.installed():
                s.start();_,_,old=s.entry();s.checkpoint('xid');s.checkpoint('postimage')
                before=s.history.current_revision();ref=self.success(s.terminal);_,_,new=s.entry(ref)
                self.assertEqual(s.history.current_revision(),before+1)
                for key in ('reservation_id','state','expires_at','expires_monotonic_ns'):
                    self.assertEqual(new['reservation'][key],old['reservation'][key])
                for key in ('consumption_sequence','consume_clock_ref','origin_request_ref'):
                    self.assertEqual(new['attempt'][key],old['attempt'][key])
                self.assertEqual(new['context']['nonce'],old['context']['nonce'])

    def test_H3_lost_terminal_ack_reopens_exact_bytes_without_duplicate_append(self):
        with tempfile.TemporaryDirectory() as root:
            s=Subject(root)
            with s.installed():
                s.start();s.checkpoint('xid');s.checkpoint('postimage');raw=s.result();publish=s.history._publish
                def lost(folder,name,b):
                    publish(folder,name,b)
                    if folder=='events' and json.loads(b)['result_ref'] is not None:
                        raise owner.OwnerRefusal('atomicity_unavailable')
                with patch.object(s.history,'_publish',lost):self.refused(lambda:s.terminal(raw))
                survivor=s.history.head()[0];s.history=p1._History(s.root);before=s.tree()
                replay=self.success(lambda:s.terminal(raw));self.assertEqual(replay,survivor)
                self.assertEqual(s.tree(),before);self.assertEqual(s.history.read(s.entry(replay)[0]['result_ref']),raw)
                different=json.loads(raw);different['observed']['replacement']=True
                self.refused(lambda:s.terminal(canonical_bytes(different)))

    def test_H3_original_consumption_cannot_resume_after_terminal_publication(self):
        with tempfile.TemporaryDirectory() as root:
            s=Subject(root)
            with s.installed():
                s.start();s.checkpoint('xid');s.checkpoint('postimage');self.success(s.terminal)
                before=s.tree();self.refused(s.consume);self.assertEqual(s.tree(),before)

    def held_lock(self, name):
        with tempfile.TemporaryDirectory() as root:
            s=Subject(root);out=[];entered=[]
            with s.installed():
                s.start();head=s.history.head()[0];event,_,_=s.entry()
                candidate={**event,'sequence':event['sequence']+1,'previous_event_ref':head}
                fd=os.open(s.root/name,os.O_RDWR);fcntl.flock(fd,fcntl.LOCK_EX)
                reads=s.owner_reads
                def advance(subject):
                    if subject.owner_reads>=reads+3:subject.expired=True
                s.on_inputs=advance
                def waiter():
                    try:
                        if name=='.operation.lock':
                            with call(s.history.operation,deadline=s.deadline):entered.append(True)
                        else:call(s.history.append,candidate,expected_ref=head,deadline=s.deadline);entered.append(True)
                    except owner.OwnerRefusal as error:out.append(error.detail)
                worker=threading.Thread(target=waiter);worker.start();worker.join(.3);bounded=not worker.is_alive()
                fcntl.flock(fd,fcntl.LOCK_UN);os.close(fd);worker.join(2)
                self.assertFalse(worker.is_alive(),'source lock worker leaked')
                self.assertTrue(bounded,'admitted deadline did not bound a held '+name)
                self.assertEqual(out,['reservation_expired']);self.assertEqual(entered,[])
                self.assertEqual(s.history.head()[0],head)

    def test_H4_operation_lock_wait_is_bounded_by_admitted_deadline(self):self.held_lock('.operation.lock')
    def test_H4_journal_lock_wait_is_bounded_by_admitted_deadline(self):self.held_lock('.journal.lock')

    def test_H4_post_acquisition_owner_change_refuses_before_entry(self):
        with tempfile.TemporaryDirectory() as root:
            s=Subject(root);attempts=[];flock=fcntl.flock
            def acquired(fd,flags):
                value=flock(fd,flags)
                if flags & fcntl.LOCK_EX:s.revoked=True
                return value
            with s.installed(),patch.object(p1.fcntl,'flock',acquired):
                with self.assertRaises(owner.OwnerRefusal):
                    with call(s.history.operation,deadline=s.deadline):attempts.append('entered')
                self.assertEqual(attempts,[])

    def test_H4_original_v2_deadline_cannot_be_extended(self):
        with tempfile.TemporaryDirectory() as root:
            s=Subject(root)
            with s.installed():
                s.reserve();s.deadline=replace(s.deadline,expires_at=s.deadline.expires_at+10,
                    expires_monotonic_ns=s.deadline.expires_monotonic_ns+10000000000)
                before=s.tree();self.refused(s.consume);self.assertEqual(s.tree(),before)

    def observe_terminal(self,s):
        publish=s.history._publish
        def observed(folder,name,raw):
            if folder=='events' and json.loads(raw)['result_ref'] is not None:
                self.assertTrue(s.barrier,'measurement/SQL barrier released before terminal publication')
                fd=os.open(s.root/'.operation.lock',os.O_RDWR)
                try:
                    with self.assertRaises(BlockingIOError):fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                finally:os.close(fd)
                s.trace.append('terminal-under-both-exclusions')
            return publish(folder,name,raw)
        return observed

    def test_H5_execute_threads_mandate_deadline_and_holds_measure_through_terminal_ack(self):
        with tempfile.TemporaryDirectory() as root:
            s=Subject(root);slot=s.primitive()
            with s.installed(slot),patch.object(s.history,'_publish',self.observe_terminal(s)):
                result=self.success(lambda:p1.execute_effect(s.data['request_raw'].encode()))
            self.assertIsNotNone(result);self.assertEqual(s.received_deadline,s.deadline)
            self.assertEqual(s.trace,['measure-enter','terminal-under-both-exclusions','measure-exit'])
            self.assertEqual(s.effects,1);self.assertFalse(s.barrier)
            self.assertEqual(s.history.read(s.entry(result)[2]['attempt']['sql_mandate_ref']),s.mandate)

    def test_H5_fresh_recovery_keeps_barriers_through_terminal_without_dispatch(self):
        with tempfile.TemporaryDirectory() as root:
            s=Subject(root)
            with s.installed():s.start();s.checkpoint('xid');s.checkpoint('postimage');s.recovery()
            slot=s.primitive(recovering=True)
            with s.installed(slot),patch.object(s.history,'_publish',self.observe_terminal(s)):
                result=self.success(lambda:p1.execute_effect(s.data['request_raw'].encode()))
            self.assertIsNotNone(result);self.assertEqual(s.effects,0)
            self.assertEqual(s.trace,['measure-enter','terminal-under-both-exclusions','measure-exit'])
            self.assertFalse(s.barrier)

    def test_H5_failed_terminal_publication_exits_held_without_original_replay(self):
        with tempfile.TemporaryDirectory() as root:
            s=Subject(root);slot=s.primitive();publish=s.history._publish
            def fail(folder,name,raw):
                if folder=='events' and json.loads(raw)['result_ref'] is not None:
                    self.assertTrue(s.barrier);raise owner.OwnerRefusal('atomicity_unavailable')
                return publish(folder,name,raw)
            with s.installed(slot),patch.object(s.history,'_publish',fail):
                self.refused(lambda:p1.execute_effect(s.data['request_raw'].encode()))
            self.assertEqual(s.effects,1);self.assertFalse(s.barrier)
            with s.installed(slot):self.refused(lambda:p1.execute_effect(s.data['request_raw'].encode()))
            self.assertEqual(s.effects,1)

    def test_legacy_none_path_keeps_original_bytes_and_exact_head_rule(self):
        with tempfile.TemporaryDirectory() as root:
            s=Subject(root)
            with s.installed():
                s.reserve(mandate=False,deadline=False);s.consume(deadline=False)
                raw=s.result();ref=p1._record_result(s.history,s.consumed,raw)
                self.assertEqual(s.history.read(s.entry(ref)[0]['result_ref']),raw)
                self.refused(lambda:p1._record_result(s.history,s.consumed,raw))
