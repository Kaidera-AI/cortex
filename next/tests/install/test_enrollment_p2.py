"""Actual P2/X12 source join; real P1 IO, synthetic B1/SQL/roundtrip originals."""
from contextlib import contextmanager
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cortex_core.install import lifecycle as p1, owner
from cortex_core.release.codec import canonical_bytes
from cortex_core.release.contracts import freeze, thaw
from cortex_core.runtime import enrollment
from test_hold1_interface import Subject
import test_x12_enrollment as x12
import test_owner_deadline_retention as old


class EnrollmentSubject:
    def __init__(self, root, *, history=None):
        self.s = s = Subject(root)
        if history is not None: s.history = history
        self.request = canonical_bytes(x12.request_doc())
        self.before = canonical_bytes({'installation_id':x12.INST, 'first_identity':None,
                                       'credential':None, 'enrollment_receipt':None})
        c=json.loads(s.data['context_raw']);q=json.loads(s.data['request_raw'])
        q['identity'].update(installation_id=x12.INST,request_id='enrollment-request',attempt_id='enrollment-attempt')
        raw=canonical_bytes(q);path=c['request_ref']['path']
        s.data['request_raw']=raw.decode();s.data['originals'][path]=raw.decode()
        effect=copy.deepcopy(c['effect']);effect.update(effect_id=enrollment.EFFECT_ID,resource_kind='interface',
            expected_before_ref=p1._ref('enrollment/before.json',self.before),
            expected_after_contract_ref=p1._ref('enrollment/request.json',self.request))
        s.data['originals']['enrollment/before.json']=self.before.decode()
        s.data['originals']['enrollment/request.json']=self.request.decode()
        s.helper.context(s.data,lambda c:c.update(identity=q['identity'],effect=effect,
            request_ref=p1._ref(path,raw),nonce='enrollment-nonce'))
        s.helper.scope(s.data,lambda c:c.update(identity=q['identity'],effect=effect))
        # Existing role history in the paired test is the same installation.
        with s.installed():s.proposal=s.current();s.deadline=s.proposal.deadline
        self.core=x12.FakeCore();self.raw=None;self.context=None;self.calls=0;self.edit=None;self.after=None

    def snapshot(self):return {'interface':self.before}

    def dispatch(self, effect):
        self.calls+=1;self.context=effect
        candidate=thaw(effect)
        if self.edit:self.edit(candidate)
        self.raw=enrollment.enroll(freeze(candidate),self.request,
            first_identity=self.core.first_identity,roundtrip=x12.good_roundtrip)
        if self.after:self.after(effect)

    def measure(self,effect):
        c=thaw(effect['context'])
        return canonical_bytes({k:c[k] for k in ('identity','effect','request_ref')} | {
            'state':'applied_verified','observed':{'enrollment_receipt_ref':self.s.history.retain(self.raw)}})

    def run(self):
        with self.s.installed(self):return p1.execute_effect(self.s.data['request_raw'].encode())


class EnrollmentP2(unittest.TestCase):
    def run_ok(self,c):
        try:return c.run()
        except owner.OwnerRefusal as error:self.fail('P2_ENROLLMENT_DISPATCH_MISSING:'+error.detail)

    def test_enrollment_uses_one_real_reserve_consume_result_and_exact_preimage(self):
        with tempfile.TemporaryDirectory() as root:
            c=EnrollmentSubject(root);ref=self.run_ok(c);s=c.s
            event,obs,parts=s.entry(ref)
            bundle=p1._decode(s.history.read(obs['preimages_ref']))
            self.assertEqual(set(bundle['originals']),{'interface'})
            self.assertEqual(s.history.read(bundle['originals']['interface']),c.before)
            self.assertEqual(parts['reservation']['state'],'consumed')
            self.assertEqual(parts['attempt']['dispatch_state'],'applied_verified')
            result=p1._decode(s.history.read(event['result_ref']))
            self.assertEqual(s.history.read(result['observed']['enrollment_receipt_ref']),c.raw)
            self.assertEqual(enrollment.parse_receipt(c.raw)['installation_id'],x12.INST)
            self.assertEqual(c.calls,1);self.assertEqual(c.core.identities,1);self.assertEqual(len(s.history.events()),3)
            self.assertEqual(p1._History(s.root).head()[0],thaw(ref))
            with self.assertRaises(owner.OwnerRefusal):c.run()
            self.assertEqual(c.calls,1);self.assertEqual(c.core.identities,1)

    def test_enrollment_family_follows_database_family_without_replacing_it(self):
        with tempfile.TemporaryDirectory() as root:
            s=Subject(root)
            # Match installation while retaining distinct actual effect/attempt.
            q=json.loads(s.data['request_raw']);q['identity']['installation_id']=x12.INST
            raw=canonical_bytes(q);path=json.loads(s.data['context_raw'])['request_ref']['path']
            s.data['request_raw']=raw.decode();s.data['originals'][path]=raw.decode()
            s.helper.context(s.data,lambda c:c.update(identity=q['identity'],request_ref=p1._ref(path,raw)))
            s.helper.scope(s.data,lambda c:c.update(identity=q['identity']))
            with s.installed():s.proposal=s.current()
            with s.installed(old.X1NonSQLDeadline().primitive(s)):
                p1.execute_effect(s.data['request_raw'].encode())
            original=p1._retained_preimages(s.history,thaw(s.proposal.context)['identity'])
            before=s.history.read(original)
            c=EnrollmentSubject(root,history=s.history);self.run_ok(c)
            self.assertEqual(s.history.read(original),before)
            self.assertEqual(p1._retained_preimages(s.history,thaw(s.proposal.context)['identity']),original)
            self.assertEqual(c.calls,1);self.assertEqual(len(s.history.events()),6)

    def test_default_seam_still_refuses_caller_DATA_before_parse(self):
        with patch.object(enrollment,'parse_request',side_effect=AssertionError('parsed unconsumed DATA')):
            with self.assertRaises(enrollment.Refusal) as got:
                enrollment.enroll(x12.effect_context(b'x'),b'x',first_identity=lambda _:self.fail(),roundtrip=lambda _:self.fail())
        self.assertEqual(got.exception.reason,'required_binding_gap')

    def test_changed_effect_context_and_second_use_refuse_before_identity(self):
        for mode in ('journal','preimages','effect','twice'):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as root:
                c=EnrollmentSubject(root)
                if mode=='journal':c.edit=lambda e:e.update(journal_ref=e['preimages_ref'])
                elif mode=='preimages':c.edit=lambda e:e.update(preimages_ref=e['journal_ref'])
                elif mode=='effect':c.edit=lambda e:e['context']['effect'].update(effect_id='another')
                else:
                    def twice(e):
                        with self.assertRaises(enrollment.Refusal):
                            enrollment.enroll(e,c.request,first_identity=c.core.first_identity,roundtrip=x12.good_roundtrip)
                    c.after=twice
                if mode=='twice':self.run_ok(c);self.assertEqual(c.core.identities,1)
                else:
                    with self.assertRaises(owner.OwnerRefusal):c.run()
                    self.assertEqual(c.core.identities,0)

    def test_retained_context_does_not_grant_later_or_other_thread_dispatch(self):
        import threading
        with tempfile.TemporaryDirectory() as root:
            c=EnrollmentSubject(root);self.run_ok(c);seen=[]
            def attempt():
                try:enrollment.enroll(c.context,c.request,first_identity=c.core.first_identity,roundtrip=x12.good_roundtrip)
                except enrollment.Refusal as error:seen.append(error.reason)
            attempt();t=threading.Thread(target=attempt);t.start();t.join()
            self.assertEqual(seen,['required_binding_gap']*2);self.assertEqual(c.core.identities,1)

    def test_lost_identity_reply_is_original_unknown_without_automatic_replay(self):
        with tempfile.TemporaryDirectory() as root:
            c=EnrollmentSubject(root);c.core.lost_reply_once=True
            with self.assertRaises(owner.OwnerRefusal):c.run()
            self.assertEqual(c.calls,1);self.assertEqual(c.core.identities,1)
            self.assertEqual(c.s.entry()[2]['attempt']['dispatch_state'],'consumed_result_unknown')
            with self.assertRaises(owner.OwnerRefusal):c.run()
            self.assertEqual(c.calls,1);self.assertEqual(c.core.identities,1)

    def test_enrollment_refuses_wrong_kind_preimage_and_revocation_before_dispatch(self):
        for mode in ('kind','preimage','revoked'):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as root:
                c=EnrollmentSubject(root);s=c.s
                if mode=='kind':
                    s.helper.context(s.data,lambda v:v['effect'].update(resource_kind='file'))
                    s.helper.scope(s.data,lambda v:v['effect'].update(resource_kind='file'))
                    with s.installed():s.proposal=s.current()
                elif mode=='preimage':c.snapshot=lambda:{'interface':b'changed'}
                else:s.revoked=True
                with self.assertRaises(owner.OwnerRefusal):c.run()
                self.assertEqual(c.core.identities,0);self.assertEqual(c.calls,0)
