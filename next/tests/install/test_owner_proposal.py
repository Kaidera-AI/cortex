"""Path A SOURCE DATA oracle. No signature/native/owner/effect acceptance.

The existing Nemo double supplies read-only original bytes at the sole RC2 port.
It never supplies a signature PASS, reservation, history or execution receipt.
"""
import ast
import copy
from dataclasses import FrozenInstanceError, replace
import hashlib
import importlib
import inspect
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from cortex_core.runtime import signed_member
from non_authoritative_signed_member_double import NonAuthoritativeSignedMemberDouble

NEXT=Path(__file__).resolve().parents[2]
FIX=Path(__file__).parent/'fixtures'
F=json.loads((FIX/'owner-proposal-subjects.json').read_bytes())


def encode(value):
    return (json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False)+'\n').encode()


class OwnerProposalOracle(unittest.TestCase):
    def api(self):
        self.assertTrue((NEXT/'src/cortex_core/install/owner.py').is_file(),
                        'B1_PATH_A_SURFACE_ABSENT: pre-consume DATA owner.py')
        return importlib.import_module('cortex_core.install.owner')

    def data(self):
        return copy.deepcopy(F)

    def edit_record(self,d,field,change):
        context=json.loads(d['context_raw'])
        reference=context[field]
        value=json.loads(d['originals'][reference['path']])
        change(value)
        raw=encode(value)
        d['originals'][reference['path']]=raw.decode()
        context[field]={**reference,'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}
        # Consent independently binds the peer original, not a caller claim.
        if field=='peer_ref':
            consent=json.loads(d['originals'][context['consent_ref']['path']])
            consent['subject_ref']=context[field]
            b=encode(consent);d['originals'][context['consent_ref']['path']]=b.decode()
            context['consent_ref']={**context['consent_ref'],'bytes':len(b),'sha256':hashlib.sha256(b).hexdigest()}
        d['context_raw']=encode(context).decode()

    def context(self,d,change):
        c=json.loads(d['context_raw']);change(c);d['context_raw']=encode(c).decode()

    def clock(self,d,change):
        c=json.loads(d['current_clock_raw']);change(c);d['current_clock_raw']=encode(c).decode()

    def scope(self,d,change):
        context=json.loads(d['context_raw']);consent=json.loads(d['originals'][context['consent_ref']['path']])
        ref=consent['scope_ref'];value=json.loads(d['originals'][ref['path']]);change(value)
        raw=encode(value);d['originals'][ref['path']]=raw.decode()
        consent['scope_ref']={**ref,'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}
        b=encode(consent);d['originals'][context['consent_ref']['path']]=b.decode()
        context['consent_ref']={**context['consent_ref'],'bytes':len(b),'sha256':hashlib.sha256(b).hexdigest()}
        d['context_raw']=encode(context).decode()

    def clock_receipt(self,d,change):
        context=json.loads(d['context_raw']);c=json.loads(d['originals'][context['clock_ref']['path']])
        ref=c['clock_receipt_ref'];v=json.loads(d['originals'][ref['path']]);change(v)
        raw=encode(v);d['originals'][ref['path']]=raw.decode()
        c['clock_receipt_ref']={**ref,'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}
        current=json.loads(d['current_clock_raw'])
        if current['clock_receipt_ref']==ref:
            current['clock_receipt_ref']=c['clock_receipt_ref']
            d['current_clock_raw']=encode(current).decode()
        b=encode(c);d['originals'][context['clock_ref']['path']]=b.decode()
        context['clock_ref']={**context['clock_ref'],'bytes':len(b),'sha256':hashlib.sha256(b).hexdigest()}
        d['context_raw']=encode(context).decode()

    def inputs(self,api,d):
        return api._Inputs(d['context_raw'].encode(),{k:v.encode() for k,v in d['originals'].items()},
                           d['current_clock_raw'].encode(),d['aggregate_id'])

    def run_gate(self,api,d=None,*,fresh=None,request=None):
        d=self.data() if d is None else d
        values=[self.inputs(api,d),self.inputs(api,d if fresh is None else fresh)]
        calls=[]
        def installed():
            calls.append('owner-observation')
            return values[min(len(calls)-1,1)]
        double=NonAuthoritativeSignedMemberDouble(F['runtime_lock_raw'].encode(),aggregate_id=d['aggregate_id'],member_name='runtime.lock.json')
        with patch.object(api,'_installed_owner_inputs',installed),double.installed(signed_member):
            result=api.propose_effect(d['request_raw'].encode() if request is None else request)
        return result,calls

    def refusal(self,api,detail,fn):
        with self.assertRaises(api.OwnerRefusal) as got:fn()
        self.assertEqual(got.exception.detail,detail)
        self.assertNotIn('SENTINEL',str(got.exception))
        return got.exception

    def test_default_gap_precedes_candidate_and_all_reads(self):
        api=self.api()
        class Trap:
            def __getattribute__(self,name):raise AssertionError('candidate touched')
        def fail(*a,**kw):self.fail('default gate reached input or effect')
        with patch.object(Path,'read_bytes',fail),patch('builtins.open',fail),patch('socket.socket',fail),patch('subprocess.Popen',fail):
            e=self.refusal(api,'required_binding_gap',lambda:api.propose_effect(Trap()))
        self.assertEqual((e.result,e.reason_code,e.unit),('BLOCKED','dependency_not_run',api.MISSING_UNIT))

    def test_public_entry_has_no_provider_context_clock_or_mint_parameters(self):
        api=self.api()
        self.assertEqual(list(inspect.signature(api.propose_effect).parameters),['original_request'])
        self.assertEqual(set(api.__all__),{'OwnerRefusal','OwnerProposal','ProposedDeadline','propose_effect'})

    def test_host_plus5_positive_is_data_with_exact_shorter_deadlines(self):
        api=self.api();p,calls=self.run_gate(api)
        self.assertEqual((p.deadline.expires_at,p.deadline.expires_monotonic_ns),(1700000005,105000000000))
        self.assertEqual(len(calls),2)
        self.assertEqual(p.stage,'PRECONSUME_DATA')
        self.assertFalse(p.effect_authority);self.assertFalse(p.native_qualified)
        self.assertFalse(p.reservation_created);self.assertFalse(p.consumed)
        self.assertEqual(p.observed_state_revision,7)

    def test_host_plus7_positive_retains_earlier_bound(self):
        api=self.api();d=self.data()
        self.context(d,lambda c:c['host_lease'].update(expires_at=1700000007))
        self.clock_receipt(d,lambda r:r['monotonic_deadlines'].update(host=107000000000))
        p,_=self.run_gate(api,d)
        self.assertEqual((p.deadline.expires_at,p.deadline.expires_monotonic_ns),(1700000007,107000000000))

    def test_host_plus5_utc_equal_refuses(self):
        api=self.api();d=self.data();self.clock(d,lambda c:c.update(now_utc=1700000005))
        self.refusal(api,'reservation_expired',lambda:self.run_gate(api,d))

    def test_host_plus5_utc_after_refuses(self):
        api=self.api();d=self.data();self.clock(d,lambda c:c.update(now_utc=1700000006))
        self.refusal(api,'reservation_expired',lambda:self.run_gate(api,d))

    def test_host_plus5_monotonic_equal_refuses(self):
        api=self.api();d=self.data();self.clock(d,lambda c:c.update(monotonic_ns=105000000000))
        self.refusal(api,'reservation_expired',lambda:self.run_gate(api,d))

    def test_host_plus5_monotonic_after_refuses(self):
        api=self.api();d=self.data();self.clock(d,lambda c:c.update(monotonic_ns=105000000001))
        self.refusal(api,'reservation_expired',lambda:self.run_gate(api,d))

    def test_default_window_is_30_when_all_independent_bounds_are_later(self):
        api=self.api();d=self.data()
        self.context(d,lambda c:c['host_lease'].update(expires_at=1700000300))
        self.clock_receipt(d,lambda r:r['monotonic_deadlines'].update(host=400000000000))
        p,_=self.run_gate(api,d)
        self.assertEqual((p.deadline.expires_at,p.deadline.expires_monotonic_ns),(1700000030,130000000000))

    def test_consent_policy_and_session_each_shorten_utc_proposal(self):
        api=self.api()
        for kind in ('consent','policy','session'):
            with self.subTest(bound=kind):
                d=self.data()
                if kind=='consent':self.edit_record(d,'consent_ref',lambda v:v.update(expires_at=1700000003))
                if kind=='policy':self.edit_record(d,'policy_ref',lambda v:v['validity'].update(expires_at=1700000003))
                if kind=='session':self.edit_record(d,'peer_ref',lambda v:v.update(expires_at=1700000003))
                p,_=self.run_gate(api,d);self.assertEqual(p.deadline.expires_at,1700000003)

    def test_each_independent_monotonic_bound_can_shorten_and_equal_refuses(self):
        api=self.api()
        for kind in ('consent','policy','session','host'):
            with self.subTest(bound=kind):
                d=self.data();self.clock_receipt(d,lambda r:r['monotonic_deadlines'].update({kind:104000000000}))
                p,_=self.run_gate(api,d);self.assertEqual(p.deadline.expires_monotonic_ns,104000000000)
                self.clock(d,lambda c:c.update(monotonic_ns=104000000000))
                self.refusal(api,'reservation_expired',lambda:self.run_gate(api,d))

    def test_private_deadlines_shorten_without_equating_host_and_private_fences(self):
        api=self.api();d=self.data();c=json.loads(d['context_raw'])
        private={'kind':'runtime','phase':'runtime','generation':11,'holder':'private-owner','fence':52,
            'expires_at':1700000003,'installation_id':c['identity']['installation_id'],
            'operation_id':c['identity']['operation_id'],'session_user_oid':16384,'session_user_name':'private-owner','map_revision':4}
        raw=encode(private);d['originals']['b1-private.json']=raw.decode()
        self.context(d,lambda v:v.update(private_binding={'kind':'private','binding_ref':{'path':'b1-private.json','bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}}))
        self.clock_receipt(d,lambda r:r['monotonic_deadlines'].update(private=103000000000))
        p,_=self.run_gate(api,d)
        self.assertEqual((p.deadline.expires_at,p.deadline.expires_monotonic_ns),(1700000003,103000000000))

    def test_monotonic_deadlines_require_the_same_epoch(self):
        api=self.api();d=self.data();self.clock_receipt(d,lambda r:r.update(owner_epoch='other'))
        self.refusal(api,'time_unavailable',lambda:self.run_gate(api,d))

    def test_unknown_clock_quality_refuses(self):
        api=self.api();d=self.data();self.clock_receipt(d,lambda r:r.update(quality='unknown'))
        self.refusal(api,'time_unavailable',lambda:self.run_gate(api,d))

    def test_current_clock_receipt_is_resolved_and_can_only_shorten_monotonic_budget(self):
        api=self.api();d=self.data();clock=json.loads(d['current_clock_raw'])
        value=json.loads(d['originals'][clock['clock_receipt_ref']['path']])
        value['monotonic_deadlines']['host']=103000000000
        def current_reference(record):
            raw=encode(record);d['originals']['b1-current-clock-provider.json']=raw.decode()
            self.clock(d,lambda c:c.update(clock_receipt_ref={'path':'b1-current-clock-provider.json','bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}))
        current_reference(value)
        p,_=self.run_gate(api,d)
        self.assertEqual(p.deadline.expires_monotonic_ns,103000000000)
        value['quality']='unknown';current_reference(value)
        self.refusal(api,'time_unavailable',lambda:self.run_gate(api,d))

    def test_full_uncertainty_upper_endpoint_equal_to_expiry_refuses(self):
        api=self.api();d=self.data();self.clock(d,lambda c:c.update(now_utc=1700000004,uncertainty_seconds=1))
        self.refusal(api,'reservation_expired',lambda:self.run_gate(api,d))

    def test_not_before_lower_endpoint_is_inclusive_and_outside_refuses(self):
        api=self.api();d=self.data()
        self.edit_record(d,'consent_ref',lambda c:c.update(not_before=1700000000))
        self.clock(d,lambda c:c.update(now_utc=1700000001,uncertainty_seconds=1))
        self.run_gate(api,d)
        self.clock(d,lambda c:c.update(uncertainty_seconds=2))
        self.refusal(api,'time_unavailable',lambda:self.run_gate(api,d))

    def test_clock_boolean_float_negative_and_overflow_refuse(self):
        api=self.api()
        for value in (True,1.5,-1,2**63):
            with self.subTest(value=value):
                d=self.data();self.clock(d,lambda c:c.update(monotonic_ns=value))
                self.refusal(api,'time_unavailable',lambda:self.run_gate(api,d))

    def test_clock_rollback_refuses_without_extending_window(self):
        api=self.api()
        for key,value in (('now_utc',1699999999),('monotonic_ns',99999999999)):
            with self.subTest(clock=key):
                d=self.data();self.clock(d,lambda c:c.update({key:value}))
                self.refusal(api,'time_unavailable',lambda:self.run_gate(api,d))

    def test_every_independent_not_before_bound_requires_an_exact_utc_integer(self):
        api=self.api()
        for kind in ('consent','policy','session'):
            for invalid in (True,-1,253402300800):
                with self.subTest(bound=kind,value=invalid):
                    d=self.data()
                    if kind=='consent':self.edit_record(d,'consent_ref',lambda r:r.update(not_before=invalid))
                    if kind=='policy':self.edit_record(d,'policy_ref',lambda r:r['validity'].update(not_before=invalid))
                    if kind=='session':self.edit_record(d,'peer_ref',lambda r:r.update(not_before=invalid))
                    self.refusal(api,'time_unavailable',lambda:self.run_gate(api,d))

    def test_peer_denial_precedes_malformed_request(self):
        api=self.api();d=self.data();self.edit_record(d,'peer_ref',lambda p:p.update(owner_epoch='wrong'))
        self.refusal(api,'peer_denied',lambda:self.run_gate(api,d,request=b'not-json'))

    def test_peer_uid_and_host_are_independently_bound(self):
        api=self.api()
        for field,value in (('owner_uid',502),('host_identity_ref',{'path':'other','bytes':1,'sha256':'0'*64})):
            with self.subTest(field=field):
                d=self.data();self.edit_record(d,'peer_ref',lambda p:p.update({field:value}))
                self.refusal(api,'peer_denied',lambda:self.run_gate(api,d))

    def test_malformed_request_is_typed(self):
        api=self.api();self.refusal(api,'invalid_request',lambda:self.run_gate(api,request=b'not-json'))

    def test_scope_identity_operation_target_release_and_effect_are_exact(self):
        api=self.api()
        changes=[lambda s:s['identity'].update(request_id='other'),lambda s:s.update(operation='uninstall'),
            lambda s:s['target'].update(host_arch='other'),lambda s:s['release_binding_ref'].update(sha256='0'*64),
            lambda s:s['effect'].update(effect_id='other')]
        for number,change in enumerate(changes):
            with self.subTest(change=number):
                d=self.data();self.scope(d,change)
                self.refusal(api,'scope_denied',lambda:self.run_gate(api,d))

    def test_revoked_consent_refuses(self):
        api=self.api();d=self.data();self.edit_record(d,'consent_ref',lambda c:c.update(revoked=True))
        self.refusal(api,'consent_revoked',lambda:self.run_gate(api,d))

    def test_original_owner_effect_must_satisfy_the_existing_closed_p1_shape(self):
        api=self.api()
        for change in (lambda e:e.update(extra=True),lambda e:e.update(effect_id=True),
                       lambda e:e['expected_before_ref'].update(sha256='not-a-digest')):
            d=self.data()
            self.context(d,lambda c:change(c['effect']))
            self.scope(d,lambda s:change(s['effect']))
            self.refusal(api,'scope_denied',lambda:self.run_gate(api,d))

    def test_consent_expiry_equality_is_strict(self):
        api=self.api();d=self.data();self.edit_record(d,'consent_ref',lambda c:c.update(expires_at=1700000001))
        self.refusal(api,'consent_expired',lambda:self.run_gate(api,d))

    def test_consent_subject_must_match_independent_peer(self):
        api=self.api();d=self.data();self.edit_record(d,'consent_ref',lambda c:c['subject_ref'].update(sha256='0'*64))
        self.refusal(api,'consent_denied',lambda:self.run_gate(api,d))

    def test_request_hash_substitution_is_not_authority(self):
        api=self.api();d=self.data();q=json.loads(d['request_raw']);q['authority_ref']={'path':'forged','bytes':1,'sha256':'0'*64}
        self.refusal(api,'scope_denied',lambda:self.run_gate(api,d,request=encode(q)))

    def test_original_owner_record_custody_is_checked(self):
        api=self.api();d=self.data();c=json.loads(d['context_raw']);d['originals'][c['policy_ref']['path']]+=' '
        self.refusal(api,'owner_unavailable',lambda:self.run_gate(api,d))

    def test_closed_context_and_exact_generation_types(self):
        api=self.api()
        for change in (lambda c:c.update(extra=True),lambda c:c.update(policy_generation=True)):
            d=self.data();self.context(d,change)
            self.refusal(api,'owner_unavailable',lambda:self.run_gate(api,d))

    def test_current_generation_and_host_fence_drift_refuse(self):
        api=self.api()
        for field,detail in (('generation','generation_changed'),('host_fence','fence_changed')):
            with self.subTest(field=field):
                d=self.data();self.edit_record(d,'current_state_ref',lambda s:s.update({field:s[field]+1}))
                self.refusal(api,detail,lambda:self.run_gate(api,d))

    def test_reobservation_after_full_readonly_release_audit_refuses_expiry(self):
        api=self.api();fresh=self.data();self.clock(fresh,lambda c:c.update(now_utc=1700000006))
        self.refusal(api,'reservation_expired',lambda:self.run_gate(api,fresh=fresh))

    def test_reobservation_after_audit_refuses_changed_context(self):
        api=self.api();fresh=self.data()
        self.context(fresh,lambda c:c.update(nonce='other-nonce'))
        self.refusal(api,'nonce_replay',lambda:self.run_gate(api,fresh=fresh))

    def test_sole_r01_unbound_refusal_stays_a_dependency_gap(self):
        api=self.api();d=self.data()
        with patch.object(api,'_installed_owner_inputs',lambda:self.inputs(api,d)):
            e=self.refusal(api,'r01_required_binding_gap',lambda:api.propose_effect(d['request_raw'].encode()))
        self.assertEqual((e.result,e.reason_code),('BLOCKED','dependency_not_run'))
        self.assertEqual(e.r01_reason,'required_binding_gap')

    def test_exact_r01_refusal_and_opaque_dependency_text_are_sanitized(self):
        api=self.api();d=self.data()
        with patch.object(api,'_installed_owner_inputs',lambda:self.inputs(api,d)),patch.object(signed_member,'request_member',side_effect=signed_member.Refusal('member_digest_mismatch')):
            e=self.refusal(api,'r01_other_refusal',lambda:api.propose_effect(d['request_raw'].encode()))
            self.assertEqual(e.r01_reason,'member_digest_mismatch')
        with patch.object(api,'_installed_owner_inputs',side_effect=RuntimeError('SENTINEL')):
            self.refusal(api,'owner_unavailable',lambda:api.propose_effect(object()))

    def test_data_is_immutable_cannot_opt_into_authority_and_has_no_reservation_id(self):
        api=self.api();p,_=self.run_gate(api)
        with self.assertRaises(FrozenInstanceError):p.observed_state_revision=8
        with self.assertRaises(TypeError):p.context['nonce']='forged'
        with self.assertRaises(TypeError):replace(p,effect_authority=True)
        self.assertFalse(hasattr(p,'reservation_id'))

    def test_refusal_leaves_all_original_data_unchanged(self):
        api=self.api();d=self.data();self.clock(d,lambda c:c.update(now_utc=1700000005));before=copy.deepcopy(d)
        self.refusal(api,'reservation_expired',lambda:self.run_gate(api,d))
        self.assertEqual(d,before)

    def test_private_binding_kind_must_match_its_independent_phase(self):
        api=self.api();d=self.data();c=json.loads(d['context_raw'])
        private={'kind':'forged-kind','phase':'runtime','generation':11,'holder':'private-owner','fence':52,
            'expires_at':1700000003,'installation_id':c['identity']['installation_id'],
            'operation_id':c['identity']['operation_id'],'session_user_oid':16384,'session_user_name':'private-owner','map_revision':4}
        raw=encode(private);d['originals']['b1-private.json']=raw.decode()
        self.context(d,lambda v:v.update(private_binding={'kind':'private','binding_ref':{'path':'b1-private.json','bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}}))
        self.clock_receipt(d,lambda r:r['monotonic_deadlines'].update(private=103000000000))
        self.refusal(api,'backend_unavailable',lambda:self.run_gate(api,d))

    def test_fixture_index_and_held_f2_preserve_separate_acceptance(self):
        self.api();index=json.loads((FIX/'subjects.json').read_bytes());raw=(FIX/'owner-proposal-subjects.json').read_bytes()
        self.assertEqual(index['B1_proposal'],{'path':'owner-proposal-subjects.json','bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()})
        self.assertNotIn('GateB_accepted_execution',index)
        self.assertEqual(index['B1_held_F2']['reason'],'waits for P2a durable reserve/consume/result (C0 §3.4/3.6/3.7)')
        self.assertEqual(len(index['B1_held_F2']['tests']),30)
        self.assertIn('test_owner_session.OwnerSessionSurface.test_F2_host_P',index['B1_held_F2']['tests'])

    def test_product_has_no_test_factory_public_binder_or_effect_mechanism(self):
        api=self.api();tree=ast.parse(Path(api.__file__).read_bytes())
        imports=[n.module or '' for n in ast.walk(tree) if isinstance(n,ast.ImportFrom)]
        self.assertFalse(any('test' in v or 'support' in v for v in imports))
        calls={n.func.attr for n in ast.walk(tree) if isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute)}
        self.assertFalse(calls & {'fsync','flock','save','connect','Popen','write_bytes','write_text'})
        self.assertFalse(any(name.startswith(('bind','mint','reserve','consume','dispatch')) for name in api.__dict__ if not name.startswith('_')))
