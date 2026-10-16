"""X11-d/X1-d source composition: real P1 IO, nonauthoritative native doubles.

Existing owner/R01 doubles provide admission DATA; they are not native proof.
Measurements are held independently of publication. No installed supplier is
bound here, no engine process runs, and no caller DATA gains execution power.
"""
from contextlib import contextmanager
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cortex_core.install import lifecycle as p1, owner, files, podman
from cortex_core.release.codec import canonical_bytes
from cortex_core.release.contracts import freeze, thaw
from test_hold1_interface import Subject
import test_install_files as file_fixtures
import test_install_podman as podman_fixtures


class LostResult:
    """Legacy finite source fixture + real consumed P1; no fabricated SQL mandate."""
    def __init__(self, root, state='APPLIED'):
        self.subject = s = Subject(Path(root)/'history')
        s.helper.context(s.data, lambda c:c['effect'].update(resource_kind='file'))
        s.helper.scope(s.data, lambda c:c['effect'].update(resource_kind='file'))
        with s.installed():
            s.proposal=s.current()
            s.reserve(mandate=False); s.consume()
        self.original = s.entry(s.consumed)
        self.subject_key = {k:self.original[0][k] for k in ('identity','effect','request_ref')}
        self.marker = Path(root)/'resource'
        self.marker.write_bytes(b'after' if state=='APPLIED' else b'before')
        self.state = state; self.held = False; self.reads = 0
        self.dispatched = 1; self.after_measure = None; self.raw_edit = None
        self.raw = None
        with s.installed():s.recovery()
        # Independent fresh owner revision is always obtained from real history.
        with s.installed(): s.deadline=s.current().deadline

    def snapshot(self): raise AssertionError('recovery resnapshotted')
    def dispatch(self, effect): raise AssertionError('recovery redispatched')

    @contextmanager
    def measure(self, effect):
        self.reads += 1; self.held = True
        try:
            c=thaw(effect['context'])
            assert {k:c[k] for k in self.subject_key} == self.subject_key
            assert effect['journal_ref'] == freeze(self.subject.consumed)
            observed = self.marker.read_bytes()
            state = self.state if observed == (b'after' if self.state=='APPLIED' else b'before') else 'UNKNOWN'
            if state=='UNKNOWN': answer=('UNKNOWN',None)
            else:
                value={**self.subject_key,'state':'applied_verified' if state=='APPLIED' else 'not_applied_verified',
                       'observed':{'source_fixture':True,'resource_sha256':p1.hashlib.sha256(observed).hexdigest(),
                                   'cleanup':{'remaining':[],'reaped':True,'pipes_closed':True}}}
                if self.raw_edit:self.raw_edit(value)
                self.raw=canonical_bytes(value);answer=(state,self.raw)
            if self.after_measure:self.after_measure()
            yield answer
            assert self.marker.read_bytes()==observed, 'resource changed during P1 ACK'
        finally:self.held=False

    def run(self):
        with self.subject.installed(self):
            return p1.execute_effect(self.subject.data['request_raw'].encode())


class RecoveryTests(unittest.TestCase):
    def ok(self, case):
        try:return case.run()
        except owner.OwnerRefusal as error:self.fail('X1D first original result missing: '+error.detail)

    def once(self, state):
        with tempfile.TemporaryDirectory() as root:
            c=LostResult(root,state);s=c.subject
            before=s.history.head()[0];revision=s.history.current_revision()
            old=c.original; raw_pre=s.history.read(old[1]['preimages_ref'])
            append=s.history.append; held=[]
            def durable(*args,**kwargs):
                held.append(c.held);return append(*args,**kwargs)
            with patch.object(s.history,'append',durable), \
                    patch.object(p1,'_consume',side_effect=AssertionError('reconsume')):
                first=self.ok(c)
                first_raw=c.raw
                # New history object reads actual durable files; same original
                # native fixture remains held, never reconstructed as authority.
                s.history=p1._History(s.root)
                second=self.ok(c)
            self.assertNotEqual(thaw(first),before)
            self.assertEqual(first,second);self.assertEqual(held,[True])
            self.assertEqual(s.history.current_revision(),revision+1)
            self.assertEqual(len(s.history.events()),3)
            e,o,a=s.entry(first)
            self.assertEqual(s.history.read(e['result_ref']),first_raw)
            self.assertEqual(c.raw,first_raw)
            self.assertEqual(e['identity'],old[0]['identity'])
            self.assertEqual(e['request_ref'],old[0]['request_ref'])
            self.assertEqual(e['effect'],old[0]['effect'])
            self.assertEqual(o['preimages_ref'],old[1]['preimages_ref'])
            self.assertEqual(s.history.read(o['preimages_ref']),raw_pre)
            self.assertEqual(a['attempt']['consumption_sequence'],old[2]['attempt']['consumption_sequence'])
            self.assertEqual(a['attempt']['consume_clock_ref'],old[2]['attempt']['consume_clock_ref'])
            self.assertEqual(a['attempt']['result_ref'],e['result_ref'])
            if state=='NOT_APPLIED':self.assertEqual(a['attempt']['reconciliation_ref'],e['result_ref'])
            self.assertEqual(c.dispatched,1);self.assertEqual(c.reads,2)
            self.assertFalse(c.held)
            self.assertEqual(old[2]['reservation']['expires_at'],a['reservation']['expires_at'])
            self.assertEqual(old[2]['reservation']['expires_monotonic_ns'],a['reservation']['expires_monotonic_ns'])

    def test_X1D_RECOVER_APPLIED(self):self.once('APPLIED')
    def test_X1D_RECOVER_NOT_APPLIED(self):self.once('NOT_APPLIED')

    def test_UNKNOWN_never_publishes_or_replays(self):
        with tempfile.TemporaryDirectory() as root:
            c=LostResult(root,'UNKNOWN');s=c.subject;before=s.tree()
            with patch.object(s.history,'append',side_effect=AssertionError('UNKNOWN published')), \
                    patch.object(p1,'_consume',side_effect=AssertionError('UNKNOWN reconsumed')):
                with self.assertRaises(owner.OwnerRefusal):c.run()
            self.assertEqual(before,s.tree());self.assertEqual(c.dispatched,1);self.assertFalse(c.held)


class X11JoinTests(unittest.TestCase):
    def join(self, primitive, history):
        cls=getattr(p1,'_X11',None)
        self.assertIsNotNone(cls,'X11D held same-subject P1 projection absent')
        return cls(primitive,history)

    @contextmanager
    def opened(self, root, kind='file', deadline_extra=0):
        s=Subject(Path(root)/'history')
        s.helper.context(s.data,lambda c:c['effect'].update(resource_kind=kind))
        s.helper.scope(s.data,lambda c:c['effect'].update(resource_kind=kind))
        with s.installed():
            s.proposal=s.current();c=thaw(s.proposal.context)
        subject={k:c[k] for k in ('identity','effect','request_ref')}
        if kind=='file':
            inputs=file_fixtures.Inputs(Path(root))
            inputs.plan.update(subject=subject,deadline_ns=103000000000+deadline_extra)
            inputs.now=101000000000
            with patch.object(files,'_installed_files',return_value=inputs),files._open() as primitive:
                yield s,self.join(primitive,s.history),inputs
        else:
            engine=podman_fixtures.Engine(kind)
            engine.plan.update(subject=subject,deadline_ns=103000000000+deadline_extra)
            engine.now=101000000000
            with patch.object(podman,'_installed_engine',return_value=engine),podman._open() as primitive:
                yield s,self.join(primitive,s.history),engine

    def recover(self,s):
        with s.installed():s.recovery()
        with s.installed():s.deadline=s.current().deadline

    def test_real_files_normal_publication_uses_original_snapshot_and_held_measure(self):
        with tempfile.TemporaryDirectory() as root, self.opened(root) as (s,j,inputs):
            with s.installed(j):result=p1.execute_effect(s.data['request_raw'].encode())
            e,o,a=s.entry(result)
            self.assertEqual((inputs.root/'published').read_bytes(),b'original member\n')
            pre=json.loads(s.history.read(o['preimages_ref']))
            self.assertEqual(set(pre['originals']),{'file'})
            self.assertEqual(s.history.read(pre['originals']['file']),canonical_bytes(None))
            self.assertEqual(a['attempt']['dispatch_state'],'applied_verified')
            self.assertTrue(inputs.held)
            self.assertEqual(a['attempt'].get('sql_mandate_ref'),None)

    def lost(self,root,kind,state):
        with self.opened(root,kind) as (s,j,inputs):
            with s.installed(j):
                s.before=j.snapshot();s.reserve(mandate=False);s.consume()
                e,o,_=s.entry(s.consumed)
                effect=freeze({'context':json.loads(s.history.read(o['context_ref'])),
                    'journal_ref':s.consumed,'preimages_ref':o['preimages_ref'],'deadline':s.deadline})
                if state=='NOT_APPLIED':
                    # Failure before any created file; actual complete preimage
                    # and no writers/stage are measured, not a reply inference.
                    if kind=='file':
                        with patch.object(j.primitive,'_build',side_effect=OSError('before first write')):j.dispatch(effect)
                    else:
                        inputs.on_run=lambda engine:setattr(engine,'state',copy.deepcopy(engine.plan['before']))
                        j.dispatch(effect)
                else:j.dispatch(effect)
            self.recover(s)
            with s.installed(j):
                try:first=p1.execute_effect(s.data['request_raw'].encode());again=p1.execute_effect(s.data['request_raw'].encode())
                except owner.OwnerRefusal as error:self.fail('X11D recovery not joined: '+error.detail)
            self.assertEqual(first,again);self.assertEqual(len(s.history.events()),3)
            e,_,a=s.entry(first);raw=json.loads(s.history.read(e['result_ref']))
            self.assertEqual(raw['state'],'applied_verified' if state=='APPLIED' else 'not_applied_verified')
            self.assertEqual(raw['identity'],e['identity'])
            self.assertEqual(a['attempt']['consumption_sequence'],1)
            if kind!='file':self.assertEqual(len(inputs.calls),1)
            return raw

    def test_files_lost_APPLIED_and_NOT_APPLIED_recover_once(self):
        for state in ('APPLIED','NOT_APPLIED'):
            with self.subTest(state=state),tempfile.TemporaryDirectory() as root:self.lost(root,'file',state)

    def test_podman_lost_APPLIED_and_NOT_APPLIED_recover_once(self):
        for state in ('APPLIED','NOT_APPLIED'):
            with self.subTest(state=state),tempfile.TemporaryDirectory() as root:self.lost(root,'volume',state)


class FaultTests(unittest.TestCase):
    def held(self,c,*,unchanged=True):
        s=c.subject;before=s.tree();head=s.history.head()[0]
        with patch.object(p1,'_consume',side_effect=AssertionError('reconsumed')):
            with self.assertRaises(owner.OwnerRefusal):c.run()
        if unchanged:self.assertEqual(before,s.tree())
        self.assertEqual(c.dispatched,1);self.assertFalse(c.held)
        return head

    def test_wrong_original_identity_effect_request_and_state_refuse(self):
        for key in ('identity','effect','request_ref','state'):
            with self.subTest(key=key),tempfile.TemporaryDirectory() as root:
                c=LostResult(root)
                def changed(v):
                    if key=='state':v[key]='not_applied_verified'
                    else:
                        field=next(iter(v[key]));v[key][field]='wrong-original'
                c.raw_edit=changed
                self.held(c)

    def test_revoked_and_expired_recovery_after_measurement_refuse(self):
        for flag in ('expired','revoked'):
            with self.subTest(flag=flag),tempfile.TemporaryDirectory() as root:
                c=LostResult(root);c.after_measure=lambda:setattr(c.subject,flag,True)
                self.held(c)

    def test_unheld_first_measurement_cannot_publish(self):
        with tempfile.TemporaryDirectory() as root:
            c=LostResult(root)
            raw=canonical_bytes({**c.subject_key,'state':'applied_verified','observed':{}})
            with patch.object(c,'measure',return_value=('APPLIED',raw)):
                self.held(c)

    def test_conflicting_retained_result_cannot_be_replaced(self):
        with tempfile.TemporaryDirectory() as root:
            c=LostResult(root);first=c.run();head=c.subject.history.head()[0]
            c.raw_edit=lambda v:v['observed'].update(conflicting=True)
            self.held(c)
            self.assertEqual(thaw(first),head);self.assertEqual(len(c.subject.history.events()),3)

    def test_lost_ACK_retains_one_result_and_fresh_recovery_reuses_it(self):
        for state in ('APPLIED','NOT_APPLIED'):
            with self.subTest(state=state),tempfile.TemporaryDirectory() as root:
                c=LostResult(root,state);s=c.subject;current=p1._current_owner
                def changed(*args,**kwargs):
                    if len(s.history.events())==3:s.revoked=True
                    return current(*args,**kwargs)
                with patch.object(p1,'_current_owner',changed):self.held(c,unchanged=False)
                head=s.history.head()[0];raw=s.history.read(s.history.head()[1]['result_ref'])
                s.revoked=False
                again=c.run()
                self.assertEqual(thaw(again),head);self.assertEqual(len(s.history.events()),3)
                self.assertEqual(s.history.read(s.history.head()[1]['result_ref']),raw)

    def test_competing_first_publisher_wins_CAS_without_second_result(self):
        with tempfile.TemporaryDirectory() as root:
            c=LostResult(root);s=c.subject;winner=[]
            def compete():
                _,fresh=p1._current_owner(s.data['request_raw'].encode(),s.deadline)
                winner.append(p1._record_result(s.history,s.consumed,c.raw,
                    publication_head=s.history.head()[0],publication_revision=fresh.observed_state_revision,
                    deadline=fresh.deadline,publication_context=thaw(fresh.context)))
            c.after_measure=compete
            self.held(c,unchanged=False)
            self.assertEqual(len(winner),1);self.assertEqual(len(s.history.events()),3)
            c.after_measure=None
            self.assertEqual(thaw(c.run()),winner[0]);self.assertEqual(len(s.history.events()),3)

    def test_stale_authority_during_file_and_directory_fsync_never_ACKs(self):
        for directory in (False,True):
            with self.subTest(directory=directory),tempfile.TemporaryDirectory() as root:
                c=LostResult(root);s=c.subject;fsync=p1.os.fsync;fired=[]
                def waited(fd):
                    result=fsync(fd)
                    import stat
                    is_directory=stat.S_ISDIR(os.fstat(fd).st_mode)
                    if c.held and is_directory==directory and not fired:
                        # Only intercept event publication, not ordinary retained
                        # fact writes, so the event's file/directory frontier is named.
                        names={p.name for p in (s.root/'events').iterdir()}
                        if any(n.startswith('.pending-') for n in names) or len(s.history.events())==3:
                            fired.append(True);s.revoked=True
                    return result
                with patch.object(p1.os,'fsync',waited):self.held(c,unchanged=False)
                self.assertEqual(fired,[True])
                self.assertEqual(len(s.history.events()),3 if directory else 2)
                s.revoked=False
                self.assertEqual(thaw(c.run()),s.history.head()[0]);self.assertEqual(len(s.history.events()),3)

    def test_unconsumed_anchor_does_not_reach_measurement(self):
        with tempfile.TemporaryDirectory() as root:
            c=LostResult(root);s=c.subject
            # An admitted recovery of a reservation is not original consumption.
            q=json.loads(s.data['request_raw']);q['journal_ref']=s.reserved
            raw=canonical_bytes(q);path=json.loads(s.data['context_raw'])['request_ref']['path']
            s.data['request_raw']=raw.decode();s.data['originals'][path]=raw.decode()
            s.helper.context(s.data,lambda x:x.update(request_ref=p1._ref(path,raw)))
            self.held(c)
            self.assertEqual(c.reads,0,'reserved anchor reached original-effect measurement')

    def test_final_ACK_tail_keeps_original_operation_cutoff(self):
        with tempfile.TemporaryDirectory() as root:
            c=LostResult(root);s=c.subject;clock=[0];current=p1._current_owner;fired=[]
            def waited(*args,**kwargs):
                result=current(*args,**kwargs)
                if len(s.history.events())==3 and not fired:
                    fired.append(True);clock[0]=s.history._publication.current[2]
                return result
            with patch.object(p1.time,'monotonic_ns',lambda:clock[0]),patch.object(p1,'_current_owner',waited):
                self.held(c,unchanged=False)
            self.assertEqual(fired,[True]);self.assertEqual(len(s.history.events()),3)
            self.assertEqual(thaw(c.run()),s.history.head()[0]);self.assertEqual(len(s.history.events()),3)

    def test_X11_partial_file_stage_and_unreaped_child_remain_UNKNOWN(self):
        fixture=X11JoinTests()
        for kind in ('file','volume'):
            with self.subTest(kind=kind),tempfile.TemporaryDirectory() as root,fixture.opened(root,kind) as (s,j,inputs):
                with s.installed(j):
                    s.before=j.snapshot();s.reserve(mandate=False);s.consume()
                    _,o,_=s.entry(s.consumed)
                    effect=freeze({'context':json.loads(s.history.read(o['context_ref'])),
                        'journal_ref':s.consumed,'preimages_ref':o['preimages_ref'],'deadline':s.deadline})
                    j.dispatch(effect)
                if kind=='file':(inputs.root/inputs.plan['stage']).write_bytes(b'uncertain retained stage')
                else:inputs.reply['remaining']=[{'unknown':'child'}]
                fixture.recover(s);before=s.tree()
                with s.installed(j),patch.object(j,'dispatch',side_effect=AssertionError('replayed')):
                    with self.assertRaises(owner.OwnerRefusal):p1.execute_effect(s.data['request_raw'].encode())
                self.assertEqual(s.tree(),before);self.assertEqual(len(s.history.events()),2)

    def test_X11_substituted_preimages_refuses_before_effect(self):
        fixture=X11JoinTests()
        with tempfile.TemporaryDirectory() as root,fixture.opened(root) as (s,j,inputs):
            with s.installed(j):
                s.before=j.snapshot();s.reserve(mandate=False);s.consume()
                _,o,_=s.entry(s.consumed)
                different=s.history.retain(canonical_bytes({'wrong':'preimages'}))
                effect=freeze({'context':json.loads(s.history.read(o['context_ref'])),
                    'journal_ref':s.consumed,'preimages_ref':different,'deadline':s.deadline})
                with self.assertRaises(owner.OwnerRefusal):j.dispatch(effect)
                self.assertFalse(j.primitive.attempted);self.assertFalse((inputs.root/'published').exists())

    def test_X11_original_plan_cannot_extend_consumed_deadline(self):
        fixture=X11JoinTests()
        with tempfile.TemporaryDirectory() as root,fixture.opened(root,deadline_extra=1) as (s,j,inputs):
            with s.installed(j):
                s.before=j.snapshot();s.reserve(mandate=False);s.consume()
                _,o,_=s.entry(s.consumed)
                effect=freeze({'context':json.loads(s.history.read(o['context_ref'])),
                    'journal_ref':s.consumed,'preimages_ref':o['preimages_ref'],'deadline':s.deadline})
                with self.assertRaises(owner.OwnerRefusal):j.dispatch(effect)
                self.assertFalse(j.primitive.attempted)

    def test_X11_matching_result_does_not_permit_automatic_retry(self):
        with tempfile.TemporaryDirectory() as root:
            c=LostResult(root,'NOT_APPLIED');c.run();s=c.subject
            original=s.history.read(c.original[0]['request_ref']);head=s.history.head()[0]
            with s.installed(c):
                with self.assertRaises(owner.OwnerRefusal):p1.execute_effect(original)
            self.assertEqual(head,s.history.head()[0]);self.assertEqual(c.dispatched,1)
