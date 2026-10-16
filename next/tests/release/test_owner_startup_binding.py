"""SOURCE simulation: fixed B1 originals -> sole R01/RC2, never native credit.

The protected observer is replaced only by this test. R01 uses its real signed
aggregate, byte custody and cryptography primitive with an unrelated RAM root.
Inert startup observations never establish pre-execution bootstrap protection.
"""
import copy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from aggregate_fixture import encode, sha256
from r01u3_support.startup import StartupFixture
from cortex_core.install import owner
from cortex_core.runtime import signed_member as port

NEXT = Path(__file__).resolve().parents[2]
OWNER_FIXTURE = NEXT/'tests/install/fixtures/owner-proposal-subjects-pgc20-v1.json'


class OwnerStartupBinding(StartupFixture, unittest.TestCase):
    def prepare(self):
        api = self.init_boundary()
        data = json.loads(OWNER_FIXTURE.read_bytes())
        self.owner_data = data
        # Translate the inherited literal owner times to the independently signed
        # aggregate fixture's clock. Keep all deltas, refs and original semantics.
        def times(value):
            if type(value) is dict:
                return {k: times(v) for k,v in value.items()}
            if type(value) is list:
                return [times(v) for v in value]
            return value-1699998500 if type(value) is int and 1699990000 < value < 1700010000 else value
        self.records = {k:times(json.loads(v)) for k,v in data['originals'].items()}
        self.context = times(json.loads(data['context_raw']))
        self.clock = times(json.loads(data['current_clock_raw']))
        self.request = self.records[self.context['request_ref']['path']]
        binding = self.policy['owner_binding']
        binding.update(installation_id=self.context['identity']['installation_id'],
                       operation_id=self.context['identity']['operation_id'],
                       policy_generation=self.context['policy_generation'])
        self.authority.generation = self.context['policy_generation']
        self.authority.fence = self.context['host_lease']['fence']
        self.authority.placement = 'owner'
        self.authority.current_state.update(policy_generation=self.authority.generation,
                                            fence=self.authority.fence)
        self.pin()
        self.frame = replace(self.frame, owner_binding=(sha256(encode(self.policy)),
                             self.authority.generation,self.authority.fence))
        self.request['release']['joint_index_ref'].update(bytes=len(self.raw),sha256=sha256(self.raw))
        self.request['release']['joint_signature_ref'].update(bytes=len(self.signature),sha256=sha256(self.signature))
        self.request['release']['artifact_refs'] = [dict(path='runtime.lock.json',
            bytes=len(self.bodies['runtime.lock.json']),sha256=sha256(self.bodies['runtime.lock.json']))]
        scope = self.records[self.records[self.context['consent_ref']['path']]['scope_ref']['path']]
        self.records[scope['release_binding_ref']['path']] = copy.deepcopy(self.request['release'])
        self.observations = []
        self.owner_hook = None
        return api

    def inputs(self, *, frame=True):
        # Recompute the acyclic original-reference graph without rewriting any
        # inherited fixture on disk. No product code supplies a test signer.
        cache = {}
        def visit(value):
            if type(value) is dict:
                if set(value) == {'path','bytes','sha256'} and value['path'] in self.records:
                    raw = original(value['path'])
                    return dict(path=value['path'],bytes=len(raw),sha256=sha256(raw))
                return {k:visit(v) for k,v in value.items()}
            if type(value) is list:
                return [visit(v) for v in value]
            return value
        def original(path):
            if path not in cache:
                cache[path] = encode(visit(self.records[path]))
            return cache[path]
        for path in self.records:
            original(path)
        raw_context = encode(visit(self.context))
        inputs = owner._Inputs(raw_context,cache,encode(visit(self.clock)),self.aggregate)
        # The baseline carrier has no declared field. Keep all baseline controls
        # executable BODY assertions (not fixture constructor errors).
        if frame:
            object.__setattr__(inputs,'release_startup',self.frame)
        return inputs

    def observed(self):
        self.observations.append('fixed-owner')
        return self.owner_hook() if self.owner_hook else self.inputs()

    def composition(self):
        from contextlib import ExitStack
        stack = ExitStack()
        stack.enter_context(patch.object(owner,'_installed_owner_inputs',self.observed))
        stack.enter_context(patch.object(self.api(),'_startup_observation',lambda:self.observation))
        return stack

    def call(self, function):
        try:
            return function(), None
        except port.Refusal as error:
            return None, error

    def test_private_carrier_declares_bootstrap_data_without_public_binder(self):
        self.assertIn('release_startup',owner._Inputs.__dataclass_fields__)
        self.assertIsNone(owner._Inputs(b'',{},b'','').release_startup)
        self.assertEqual(set(owner.__all__),{'OwnerRefusal','OwnerProposal','ProposedDeadline','propose_effect'})

    def test_fixed_owner_originals_reach_real_R01_and_existing_sealed_port(self):
        self.prepare()
        with self.composition():
            value,error = self.call(lambda:port.request_member(self.aggregate,'runtime.lock.json'))
        self.assertIsNone(error, str(error))
        self.assertIs(type(value),port.OriginalMember)
        self.assertEqual(port.read_member(value,aggregate_id=self.aggregate,member_name='runtime.lock.json'),
                         self.bodies['runtime.lock.json'])
        self.assertEqual(self.transports,[self.aggregate]*2)
        self.assertGreater(len(self.observations),2)
        self.assertFalse(hasattr(value,'effect_authority'))
        self.assertFalse(hasattr(value,'native_qualified'))
        self.assert_closed()

    def test_real_effect_owner_proposal_uses_the_same_R01_originals(self):
        self.prepare()
        request = self.inputs().originals[self.context['request_ref']['path']]
        with self.composition():
            try:
                proposal = owner.propose_effect(request)
                error = None
            except owner.OwnerRefusal as caught:
                proposal,error = None,caught
        self.assertIsNone(error,str(error))
        self.assertEqual(proposal.request_sha256,sha256(request))
        self.assertFalse(proposal.effect_authority)
        self.assertFalse(proposal.native_qualified)
        self.assertFalse(proposal.reservation_created)
        self.assertFalse(proposal.consumed)
        self.assertEqual(self.transports,[self.aggregate]*2)
        self.assert_closed()

    def test_bound_arguments_still_refuse_before_original_transport(self):
        self.prepare()
        class Trap:
            def __eq__(self,other): raise AssertionError('candidate compared')
            def __str__(self): raise AssertionError('candidate text')
        with self.composition():
            _,error = self.call(lambda:port.request_member(Trap(),Trap()))
        self.assertIsNotNone(error)
        self.assertEqual(error.reason,'member_identity_mismatch')
        self.assertEqual(self.transports,[])

    def test_missing_startup_refuses_before_candidate(self):
        self.prepare();self.owner_hook=lambda:self.inputs(frame=False)
        with self.composition():
            _,error = self.call(lambda:port.request_member(object(),object()))
        self.assertIsNotNone(error)
        self.assertEqual(error.reason,'required_binding_gap')
        self.assertEqual(self.transports,[])

    def test_actual_peer_refusal_precedes_candidate_and_is_sanitized(self):
        self.prepare()
        def denied(): raise owner.OwnerRefusal('peer_denied')
        self.owner_hook=denied
        with self.composition():
            _,error = self.call(lambda:port.request_member(object(),object()))
        self.assertIsNotNone(error)
        self.assertEqual(error.reason,'member_authority_unavailable')
        self.assertEqual(self.transports,[])

    def test_unexpected_owner_failure_is_sanitized_before_candidate(self):
        self.prepare()
        def failed(): raise RuntimeError('SENTINEL private source')
        self.owner_hook=failed
        with self.composition():
            _,error=self.call(lambda:port.request_member(object(),object()))
        self.assertIsNotNone(error)
        self.assertEqual(error.reason,'member_authority_unavailable')
        self.assertNotIn('SENTINEL',str(error))
        self.assertEqual(self.transports,[])

    def test_wrong_operation_original_cannot_supply_release_context(self):
        self.prepare()
        self.policy['owner_binding']['operation_id']='00000000-0000-4000-8000-000000000099'
        self.pin();self.frame=replace(self.frame,owner_binding=(sha256(encode(self.policy)),11,27))
        with self.composition():
            _,error=self.call(lambda:port.request_member(self.aggregate,'runtime.lock.json'))
        self.assertIsNotNone(error)
        self.assertEqual(error.reason,'aggregate_scope_mismatch')
        self.assertEqual(self.transports,[])

    def test_wrong_current_generation_or_host_fence_cannot_supply_release_context(self):
        for field in ('generation','fence'):
            with self.subTest(field=field):
                self.prepare();setattr(self.authority,field,getattr(self.authority,field)+1)
                self.frame=replace(self.frame,owner_binding=(sha256(encode(self.policy)),
                    self.authority.generation,self.authority.fence))
                with self.composition():
                    _,error=self.call(lambda:port.request_member(self.aggregate,'runtime.lock.json'))
                self.assertIsNotNone(error)
                self.assertEqual(error.reason,'aggregate_replay')
                self.assertEqual(self.transports,[])

    def test_owner_target_is_not_selected_by_the_candidate(self):
        self.prepare()
        original=self.inputs()
        self.owner_hook=lambda:replace(original,aggregate_id='sha256:'+'f'*64)
        with self.composition():
            _,error=self.call(lambda:port.request_member(self.aggregate,'runtime.lock.json'))
        self.assertIsNotNone(error)
        self.assertEqual(error.reason,'aggregate_identity_mismatch')
        self.assertEqual(self.transports,[])

    def test_missing_current_original_refuses_without_transport(self):
        self.prepare();self.records.pop(self.context['current_state_ref']['path'])
        with self.composition():
            _,error=self.call(lambda:port.request_member(self.aggregate,'runtime.lock.json'))
        self.assertIsNotNone(error)
        self.assertEqual(error.reason,'member_authority_unavailable')
        self.assertEqual(self.transports,[])

    def test_current_clock_original_drives_R01_not_callback_clock(self):
        self.prepare();self.authority.now=999999999
        with self.composition():
            value,error=self.call(lambda:port.request_member(self.aggregate,'runtime.lock.json'))
        self.assertIsNone(error,str(error))
        self.assertIs(type(value),port.OriginalMember)
        self.assert_closed()

    def test_expired_current_clock_refuses_without_transport(self):
        self.prepare();self.clock['now_utc']+=30;self.clock['monotonic_ns']+=30_000_000_000
        with self.composition():
            _,error=self.call(lambda:port.request_member(self.aggregate,'runtime.lock.json'))
        self.assertIsNotNone(error)
        self.assertEqual(error.reason,'member_authority_unavailable')
        self.assertEqual(self.transports,[])

    def test_transport_reloads_current_originals_instead_of_capturing_first_callback(self):
        api=self.prepare();first=self.inputs();seen=[]
        original=self.frame.originals
        def fresh(aggregate): seen.append('fresh');return original(aggregate)
        fresh_frame=replace(self.frame,originals=fresh)
        values=[first,replace(first,release_startup=fresh_frame)] if 'release_startup' in owner._Inputs.__dataclass_fields__ else [first,first]
        self.owner_hook=lambda:values[min(len(self.observations)-1,1)]
        with self.composition():
            value,error=self.call(lambda:port.request_member(self.aggregate,'runtime.lock.json'))
        self.assertIsNone(error,str(error))
        self.assertEqual(seen,['fresh','fresh'])
        self.assertIs(type(value),port.OriginalMember)
        self.assert_closed()

    def test_nonce_change_after_binding_cannot_be_hidden_by_same_policy_pin(self):
        api=self.prepare();first=self.inputs()
        def current():
            if len(self.observations)>1:self.context['nonce']='different-current-nonce'
            return self.inputs()
        self.owner_hook=current
        with self.composition():
            _,error=self.call(lambda:port.request_member(self.aggregate,'runtime.lock.json'))
        self.assertIsNotNone(error)
        self.assertEqual(error.reason,'aggregate_replay')
        self.assertEqual(self.transports,[])

    def test_original_start_deadline_is_retained_when_owner_clock_ref_advances(self):
        api=self.prepare()
        with self.composition():
            startup,error=self.call(api._installed_startup)
            self.assertIsNone(error,str(error))
            # A new start observation cannot renew this original owner's 5s host bound.
            self.clock['now_utc']+=6;self.clock['monotonic_ns']+=6_000_000_000
            _,error=self.call(startup.current)
        self.assertIsNotNone(error)
        self.assertEqual(error.reason,'member_authority_unavailable')
        self.assertEqual(self.transports,[])


class PlatformStartupCarrier(unittest.TestCase):
    def test_linux_actual_collector_preserves_private_startup_data(self):
        from test_x2_linux_observer import X2LinuxCollector, _FakeLinuxSyscalls
        fixture=X2LinuxCollector();fixture.setUp()
        marker=object();object.__setattr__(fixture.base,'release_startup',marker)
        result=fixture.collect(_FakeLinuxSyscalls(fixture.receipt))
        self.assertIs(getattr(result,'release_startup',None),marker)

    def test_macos_actual_collector_preserves_private_startup_data(self):
        from test_x2_macos_collector import snapshot, collector
        value=snapshot();marker=object()
        object.__setattr__(value['admitted']['owner_inputs'],'release_startup',marker)
        api=collector()
        with patch.object(api,'_native_snapshot',lambda:value):
            result=api.collect().owner_inputs
        self.assertIs(getattr(result,'release_startup',None),marker)

    def test_macos_actual_observer_preserves_private_startup_data(self):
        from test_gate_b_macos import MacOSObserver
        fixture=MacOSObserver();api=fixture.api();data=fixture.data()
        carrier=fixture.carrier(api,data);marker=object()
        object.__setattr__(carrier.owner_inputs,'release_startup',marker)
        result=fixture.observe(api,data,supplied=carrier)
        self.assertIs(getattr(result,'release_startup',None),marker)
