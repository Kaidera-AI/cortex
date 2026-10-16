"""Publication owner-scope and redundant-ref regressions, SOURCE DATA only."""
from contextlib import contextmanager
import copy
import json
import tempfile
import unittest
from unittest.mock import patch
from test_hold1_interface import Subject, call
from cortex_core.install import lifecycle as p1, owner
from cortex_core.release.codec import canonical_bytes
from cortex_core.release.contracts import thaw


class PublicationScopeOracle(unittest.TestCase):
    def foreign(self, s):
        q=json.loads(s.data['request_raw']);q.update(operation='install',journal_ref=None)
        q['identity'].update(request_id='foreign-publication-request',attempt_id='foreign-publication-attempt',
            parent_attempt_id=None,operation_id='00000000-0000-4000-8000-000000000099')
        raw=canonical_bytes(q);c=json.loads(s.data['context_raw']);path=c['request_ref']['path']
        s.data['request_raw']=raw.decode();s.data['originals'][path]=raw.decode()
        s.helper.context(s.data,lambda c:c.update(identity=q['identity'],request_ref=p1._ref(path,raw),
            nonce='foreign-publication-nonce'))
        s.helper.context(s.data,lambda c:c['effect'].update(effect_id='foreign-publication-effect'))
        effect=json.loads(s.data['context_raw'])['effect']
        s.helper.scope(s.data,lambda scope:scope.update(identity=q['identity'],operation='install',
            effect=effect,resource_ids=['foreign-owned-role']))
        fresh=owner.propose_effect(raw)
        self.assertEqual(fresh.deadline,s.deadline,'foreign replacement is not independently admitted')
        self.assertEqual(p1._admitted_deadline(s.deadline)[1].context['identity']['request_id'],
            'foreign-publication-request')
        return fresh

    def test_recovery_late_same_epoch_scope_replacement_cannot_append(self):
        with tempfile.TemporaryDirectory() as root:
            s=Subject(root)
            with s.installed():s.start();s.checkpoint('xid');s.checkpoint('postimage');s.recovery()
            primitive=s.primitive(recovering=True);original=s.history._lock;switched=[]
            @contextmanager
            def lock(name,**kwargs):
                if name=='.journal.lock' and not switched:
                    self.assertTrue(s.barrier);switched.append(self.foreign(s))
                with original(name,**kwargs) as result:yield result
            with s.installed(primitive):
                before=s.history.head()[0];request=s.data['request_raw'].encode()
                with patch.object(s.history,'_lock',side_effect=lock):
                    with self.assertRaises(owner.OwnerRefusal):p1.execute_effect(request)
                self.assertEqual(len(switched),1)
                self.assertEqual(s.history.head()[0],before,'BODY: foreign scope appended recovery terminal')
            self.assertEqual(s.effects,0);self.assertFalse(s.barrier)

    def test_normal_late_same_epoch_scope_replacement_cannot_append(self):
        with tempfile.TemporaryDirectory() as root:
            s=Subject(root);primitive=s.primitive();original=s.history._lock;switched=[];before=[]
            @contextmanager
            def lock(name,**kwargs):
                if name=='.journal.lock' and s.barrier and not switched:
                    before.append(s.history.head()[0]);switched.append(self.foreign(s))
                with original(name,**kwargs) as result:yield result
            with s.installed(primitive),patch.object(s.history,'_lock',side_effect=lock):
                with self.assertRaises(owner.OwnerRefusal):p1.execute_effect(s.data['request_raw'].encode())
                self.assertEqual(len(switched),1)
                self.assertEqual(s.history.head()[0],before[0],'BODY: foreign scope appended normal terminal')
            self.assertEqual(s.effects,1)

    def test_journal_rechecks_checked_publication_revision_before_visibility(self):
        with tempfile.TemporaryDirectory() as root:
            s=Subject(root)
            with s.installed():s.start();s.checkpoint('xid');s.checkpoint('postimage');s.recovery()
            primitive=s.primitive(recovering=True);original=s.history._lock;switched=[]
            @contextmanager
            def lock(name,**kwargs):
                if name=='.journal.lock' and not switched:
                    switched.append(True)
                    current=s.inputs();data=copy.deepcopy(s.data)
                    s.helper.context(data,lambda c:c.update(state_revision=77))
                    s.helper.edit_record(data,'current_state_ref',lambda state:state.update(revision=77))
                    replacement=s.helper.inputs(owner,data)
                    self.assertEqual(owner._evaluate(s.data['request_raw'].encode(),replacement,
                        previous_deadline=s.deadline).observed_state_revision,77)
                    with patch.object(owner,'_installed_owner_inputs',return_value=replacement):
                        with original(name,**kwargs) as result:yield result
                else:
                    with original(name,**kwargs) as result:yield result
            with s.installed(primitive):
                before=s.history.head()[0]
                with patch.object(s.history,'_lock',side_effect=lock):
                    with self.assertRaises(owner.OwnerRefusal):p1.execute_effect(s.data['request_raw'].encode())
                self.assertEqual(s.history.head()[0],before,'BODY: changed cursor appended terminal')

    def test_stable_fresh_recovery_publishes_once_under_both_barriers(self):
        with tempfile.TemporaryDirectory() as root:
            s=Subject(root)
            with s.installed():s.start();s.checkpoint('xid');s.checkpoint('postimage');s.recovery()
            primitive=s.primitive(recovering=True);publish=s.history._publish;seen=[]
            def observe(folder,name,raw):
                if folder=='events' and json.loads(raw)['result_ref'] is not None:
                    self.assertTrue(s.barrier)
                    import fcntl,os
                    fd=os.open(s.root/'.operation.lock',os.O_RDWR)
                    try:
                        with self.assertRaises(BlockingIOError):fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    finally:os.close(fd)
                    seen.append(True)
                return publish(folder,name,raw)
            with s.installed(primitive),patch.object(s.history,'_publish',observe):
                before=len(s.history.events());result=p1.execute_effect(s.data['request_raw'].encode())
                self.assertEqual(len(s.history.events()),before+1);self.assertEqual(len(seen),1)
                self.assertEqual(thaw(result),s.history.head()[0]);self.assertEqual(s.effects,0)

    def test_lost_ack_post_publication_owner_change_allows_fresh_exact_replay(self):
        with tempfile.TemporaryDirectory() as root:
            s=Subject(root)
            with s.installed():s.start();s.checkpoint('xid');s.checkpoint('postimage');raw=s.result()
            primitive=s.primitive();publish=s.history._publish;changed=[]
            def after(folder,name,value):
                publish(folder,name,value)
                if folder=='events' and json.loads(value)['result_ref'] is not None and not changed:
                    changed.append(self.foreign(s))
                    raise owner.OwnerRefusal('atomicity_unavailable')
            with s.installed(primitive),patch.object(s.history,'_publish',after):
                with self.assertRaises(owner.OwnerRefusal):s.terminal(raw)
                terminal=s.history.head()[0]
            # Replace the unrelated caller with a fresh legitimate recovery for original A.
            s.data=copy.deepcopy(s.fixture.setup_data()[1]);s.deadline=s.proposal.deadline
            with s.installed():s.recovery()
            class Replay:
                @contextmanager
                def measure(self,effect):
                    s.barrier=True
                    try:yield ('APPLIED',raw)
                    finally:s.barrier=False
                def dispatch(self,effect):raise AssertionError('recovery redispatched original effect')
            with s.installed(Replay()):
                before=s.tree();replay=p1.execute_effect(s.data['request_raw'].encode())
                self.assertEqual(thaw(replay),terminal);self.assertEqual(s.tree(),before)
                self.assertEqual(s.history.read(s.entry(replay)[0]['result_ref']),raw)

    def test_unrelated_explicit_publication_context_is_not_subject_authority(self):
        with tempfile.TemporaryDirectory() as root:
            s=Subject(root)
            with s.installed():
                s.start();s.checkpoint('xid');s.checkpoint('postimage');raw=s.result()
                foreign=self.foreign(s);before=s.history.head()[0]
                with self.assertRaises(owner.OwnerRefusal):
                    call(p1._record_result,s.history,s.consumed,raw,
                        publication_head=before,publication_revision=s.history.current_revision(),
                        deadline=s.deadline,publication_context=thaw(foreign.context))
                self.assertEqual(s.history.head()[0],before)

    def test_operation_acquisition_keeps_original_expected_scope(self):
        with tempfile.TemporaryDirectory() as root:
            s=Subject(root);primitive=s.primitive();original=s.history._lock;switched=[]
            @contextmanager
            def lock(name,**kwargs):
                if name=='.operation.lock' and not switched:switched.append(self.foreign(s))
                with original(name,**kwargs) as result:yield result
            with s.installed(primitive),patch.object(s.history,'_lock',side_effect=lock):
                with self.assertRaises(owner.OwnerRefusal):p1.execute_effect(s.data['request_raw'].encode())
                self.assertIsNone(s.history.head()[0]);self.assertEqual(s.effects,0)


class PublicationReferenceOracle(unittest.TestCase):
    def test_terminal_attempt_reservation_ref_must_resolve_and_match(self):
        """A valid checkpoint prefix cannot qualify an unbound terminal ref."""
        with tempfile.TemporaryDirectory() as root:
            s=Subject(root)
            with s.installed():
                s.start();s.checkpoint('xid');s.checkpoint('postimage')
                head=s.history.head()[0];event,obs,parts=s.entry()
                raw=s.result();result_ref=s.history.retain(raw)
                reservation=parts['reservation'];reservation['expected_state_revision']+=1
                context=parts['context'];context['state_revision']=reservation['expected_state_revision']
                attempt=parts['attempt']
                absent=p1._ref('absent-reservation',b'never-retained-reservation')
                attempt.update(dispatch_state='applied_verified',result_ref=result_ref,reservation_ref=absent)
                # Prove that the malformed edge is genuinely unreadable, rather
                # than relying on a label or a preloaded expected failure.
                with self.assertRaises(owner.OwnerRefusal):s.history.read(absent)
                try:
                    terminal=p1._publish_entry(s.history,context=context,reservation=reservation,
                        attempt=attempt,preimages_ref=obs['preimages_ref'],
                        request_ref=event['request_ref'],release_ref=event['release_binding_ref'],
                        expected_ref=head,phase='created',recorded_at=1700000001,
                        result_ref=result_ref,deadline=s.deadline)
                except owner.OwnerRefusal:
                    return
                # Reopen after a simulated lost terminal reply. The unchanged
                # result bytes and fresh owner are controls, not native truth.
                s.history=p1._History(s.root)
                try:
                    replay=s.terminal(raw)
                except owner.OwnerRefusal:
                    return
                self.assertEqual(replay,terminal)
                self.assertEqual(s.history.read(s.entry(replay)[0]['result_ref']),raw)
                self.fail('BODY: terminal publication and exact-byte replay accepted an unreadable attempt.reservation_ref')

    def test_correct_terminal_reservation_ref_remains_replayable(self):
        with tempfile.TemporaryDirectory() as root:
            s=Subject(root)
            with s.installed():
                s.start();s.checkpoint('xid');s.checkpoint('postimage')
                raw=s.result();terminal=s.terminal(raw)
                _,obs,parts=s.entry(terminal)
                self.assertEqual(parts['attempt']['reservation_ref'],obs['reservation_ref'])
                self.assertEqual(s.history.read(parts['attempt']['reservation_ref']),
                                 s.history.read(obs['reservation_ref']))
                s.history=p1._History(s.root);before=s.tree()
                self.assertEqual(s.terminal(raw),terminal)
                self.assertEqual(before,s.tree())

    def test_readable_duplicate_terminal_reservation_must_match_observation(self):
        with tempfile.TemporaryDirectory() as root:
            s=Subject(root)
            with s.installed():
                s.start();s.checkpoint('xid');s.checkpoint('postimage')
                head=s.history.head()[0];event,obs,parts=s.entry();raw=s.result()
                result_ref=s.history.retain(raw);r=parts['reservation'];r['expected_state_revision']+=1
                parts['context']['state_revision']=r['expected_state_revision']
                wrong={**r,'reservation_id':'different-reservation'}
                wrong_ref=s.history.retain(canonical_bytes(wrong))
                self.assertEqual(s.history.read(wrong_ref),canonical_bytes(wrong))
                parts['attempt'].update(dispatch_state='applied_verified',result_ref=result_ref,reservation_ref=wrong_ref)
                with self.assertRaises(owner.OwnerRefusal):
                    p1._publish_entry(s.history,context=parts['context'],reservation=r,attempt=parts['attempt'],
                        preimages_ref=obs['preimages_ref'],request_ref=event['request_ref'],
                        release_ref=event['release_binding_ref'],expected_ref=head,phase='created',
                        recorded_at=1700000001,result_ref=result_ref,deadline=s.deadline)
                self.assertEqual(s.history.head()[0],head)
