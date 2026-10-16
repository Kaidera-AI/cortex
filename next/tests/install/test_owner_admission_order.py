"""C0 composition precedence over original BM/BL DATA, never native authority.

The actual propose_effect entry is exercised with private simulated suppliers.
Existing fixture originals and the sole nonauthoritative R01 double are reused.
No collector, host, process, effect primitive or production binding is installed.
"""
import copy
from contextlib import ExitStack
import json
import unittest
from unittest.mock import patch

from cortex_core.install import owner
from cortex_core.install.platforms import linux, macos
from cortex_core.runtime import signed_member
from non_authoritative_signed_member_double import NonAuthoritativeSignedMemberDouble
import test_gate_b_linux as bl
import test_gate_b_macos as bm


class AdmissionOrdering(unittest.TestCase):
    def graph(self, platform, fault=None, scope_fault=False):
        if platform == 'linux':
            case = {'custody': 'caller_write', 'peer': 'transport',
                    'code': 'peer_code', 'startup': 'startup'}.get(fault, 'positive')
            data = copy.deepcopy(bl.SUBJECTS['cases'][case]['inputs'])
            data.update(request_raw=bl.SUBJECTS['request_raw'],
                        runtime_lock_raw=bl.SUBJECTS['runtime_lock_raw'])
            api, supply = linux, '_installed_linux_inputs'
        else:
            data = copy.deepcopy(bm.FIXTURE)
            oracle = bm.MacOSObserver()
            if fault in ('peer', 'code'):
                key = 'peer_lifetime_substitution' if fault == 'peer' else 'code_owner_requirement_satisfied'
                cases = [c for c in data['cases'] if c['id'] == key]
                self.assertEqual(len(cases), 1, 'missing matched original BM fault')
                oracle.change(data, cases[0])
            elif fault == 'custody':
                observed = json.loads(data['owner_inputs']['originals'][data['observation_ref']['path']])
                chains = observed['custody_before']['chains']
                index = next(i for i, c in enumerate(chains) if c['role'] == 'code')
                nodes = chains[index]['nodes']
                oracle.change(data, dict(record='observation', operation='set',
                    path=['custody_before', 'chains', index, 'nodes', len(nodes)-1, 'mode'],
                    value=nodes[-1]['mode'] | 0o002))
            api, supply = macos, '_installed_macos_inputs'
        b = data if platform == 'linux' else data['owner_inputs']
        context = json.loads(b['context_raw'])
        if fault == 'missing_state':
            del b['originals'][context['current_state_ref']['path']]
        if scope_fault:
            context['identity']['operation_id'] = '00000000-0000-4000-8000-000000000099'
            b['context_raw'] = bm.encode(context).decode()
        if platform == 'linux':
            supplied = owner._Inputs(b['context_raw'].encode(),
                {k: v.encode() for k, v in b['originals'].items()},
                b['current_clock_raw'].encode(), b['aggregate_id'])
        else:
            supplied = bm.MacOSObserver().carrier(api, data)
        return api, supply, supplied, b

    def run_composed(self, platform, fault=None, *, malformed=False,
                     fresh_fault=None, scope_fault=False, r01_reason=None):
        api, supply, first, b = self.graph(platform, fault, scope_fault)
        fresh = first if fresh_fault is None else self.graph(platform, fresh_fault, scope_fault)[2]
        values = [first, fresh]
        observations = []
        def collect():
            observations.append('observe')
            return values[min(len(observations)-1, 1)]
        double = NonAuthoritativeSignedMemberDouble(b['runtime_lock_raw'].encode(),
            aggregate_id=b['aggregate_id'], member_name='runtime.lock.json')
        with ExitStack() as stack:
            stack.enter_context(patch.object(api, supply, collect))
            stack.enter_context(patch.object(owner, '_installed_owner_inputs', api._installed_owner_inputs))
            stack.enter_context(double.installed(signed_member))
            r01 = None
            if r01_reason:
                r01 = stack.enter_context(patch.object(signed_member, 'request_member',
                    side_effect=signed_member.Refusal(r01_reason)))
            parser = stack.enter_context(patch.object(owner, 'parse_install_message',
                                                      wraps=owner.parse_install_message))
            proposals = stack.enter_context(patch.object(owner, 'OwnerProposal', wraps=owner.OwnerProposal))
            try:
                value = owner.propose_effect(b'{}\n' if malformed else b['request_raw'].encode())
            except owner.OwnerRefusal as error:
                value = error
        return dict(value=value, parser=parser.call_count, proposals=proposals.call_count,
            observations=len(observations), r01=len(double.calls) if r01 is None else r01.call_count,
            reads=len(double.reads))

    def check_refusal(self, result, detail, *, parser=None, r01=0, observations=1):
        error = result['value']
        self.assertIsInstance(error, owner.OwnerRefusal)
        self.assertEqual(error.detail, detail, 'C0_DECIDING_PRECEDENCE:'+detail)
        expected = owner.OwnerRefusal(detail)
        self.assertEqual((error.result, error.reason_code), (expected.result, expected.reason_code))
        if parser is not None:
            self.assertEqual(result['parser'], parser)
        self.assertEqual(result['r01'], r01)
        self.assertEqual(result['observations'], observations)
        return error

    def malformed_current(self, platform, fault):
        result = self.run_composed(platform, fault, malformed=True)
        error = self.check_refusal(result, 'invalid_request', parser=1)
        self.assertEqual((error.result, error.reason_code), ('FAIL', 'bad_shape'))
        self.assertEqual(result['proposals'], 0)

    def test_linux_malformed_request_before_absent_current_state(self):
        self.malformed_current('linux', 'missing_state')

    def test_linux_malformed_request_before_caller_writable_source(self):
        self.malformed_current('linux', 'custody')

    def test_macos_malformed_request_before_absent_current_state(self):
        self.malformed_current('macos', 'missing_state')

    def test_macos_malformed_request_before_caller_writable_source(self):
        self.malformed_current('macos', 'custody')

    def test_matched_valid_requests_remain_readonly_preconsume_data(self):
        for platform in ('linux', 'macos'):
            with self.subTest(platform=platform):
                r = self.run_composed(platform)
                self.assertIs(type(r['value']), owner.OwnerProposal)
                self.assertEqual((r['r01'], r['reads'], r['observations']), (1, 1, 2))
                self.assertEqual(r['value'].stage, 'PRECONSUME_DATA')
                for flag in ('effect_authority', 'native_qualified', 'reservation_created', 'consumed'):
                    self.assertIs(getattr(r['value'], flag), False)

    def test_fixed_binding_gap_precedes_candidate_access(self):
        for api in (owner, linux, macos):
            with self.subTest(api=api.__name__), \
                    patch.object(owner, '_installed_owner_inputs', api._installed_owner_inputs), \
                    patch.object(owner, 'parse_install_message') as parser:
                with self.assertRaises(owner.OwnerRefusal) as got:
                    owner.propose_effect(object())
                self.assertEqual(got.exception.detail, 'required_binding_gap')
                self.assertEqual(parser.call_count, 0)

    def test_actual_peer_denial_precedes_malformed_candidate_access(self):
        for platform in ('linux', 'macos'):
            with self.subTest(platform=platform):
                self.check_refusal(self.run_composed(platform, 'peer', malformed=True),
                                   'peer_denied', parser=0)

    def test_actual_code_denial_precedes_malformed_candidate_access(self):
        for platform in ('linux', 'macos'):
            with self.subTest(platform=platform):
                self.check_refusal(self.run_composed(platform, 'code', malformed=True),
                                   'peer_denied', parser=0)
        self.check_refusal(self.run_composed('linux', 'startup', malformed=True),
                           'custody_changed', parser=0)

    def test_valid_request_keeps_current_state_unavailability(self):
        for platform in ('linux', 'macos'):
            with self.subTest(platform=platform):
                self.check_refusal(self.run_composed(platform, 'missing_state'),
                                   'owner_unavailable', parser=1)

    def test_valid_request_keeps_custody_drift_after_r01_and_reobservation(self):
        for platform in ('linux', 'macos'):
            with self.subTest(platform=platform):
                self.check_refusal(self.run_composed(platform, 'custody'),
                                   'custody_changed', r01=1, observations=2)

    def test_post_audit_current_failures_prevent_data_return(self):
        for platform in ('linux', 'macos'):
            for fault, detail in (('missing_state', 'owner_unavailable'), ('custody', 'custody_changed')):
                with self.subTest(platform=platform, fault=fault):
                    self.check_refusal(self.run_composed(platform, fresh_fault=fault),
                                       detail, r01=1, observations=2)

    def test_current_unavailability_precedes_later_scope_denial(self):
        for platform in ('linux', 'macos'):
            with self.subTest(platform=platform):
                self.check_refusal(self.run_composed(platform, 'missing_state', scope_fault=True),
                                   'owner_unavailable', parser=1)

    def test_scope_denial_precedes_r01_and_current_custody_drift(self):
        for platform in ('linux', 'macos'):
            with self.subTest(platform=platform):
                self.check_refusal(self.run_composed(platform, 'custody', scope_fault=True),
                                   'scope_denied', parser=1)

    def test_exact_r01_refusal_precedes_current_custody_drift(self):
        for platform in ('linux', 'macos'):
            with self.subTest(platform=platform):
                error = self.check_refusal(self.run_composed(platform, 'custody',
                    r01_reason='member_digest_mismatch'), 'r01_other_refusal', r01=1)
                self.assertEqual(error.r01_reason, 'member_digest_mismatch')

    def test_unmarked_supplier_failure_does_not_claim_peer_authentication(self):
        with patch.object(owner, '_installed_owner_inputs', side_effect=RuntimeError('SENTINEL')), \
                patch.object(owner, 'parse_install_message') as parser:
            with self.assertRaises(owner.OwnerRefusal) as got:
                owner.propose_effect(b'{}\n')
        self.assertEqual(got.exception.detail, 'owner_unavailable')
        self.assertEqual(parser.call_count, 0)

    def test_direct_common_owner_still_parses_before_missing_state(self):
        _, _, inputs, _ = self.graph('linux', 'missing_state')
        with patch.object(owner, '_installed_owner_inputs', return_value=inputs):
            with self.assertRaises(owner.OwnerRefusal) as got:
                owner.propose_effect(b'{}\n')
        self.assertEqual(got.exception.detail, 'invalid_request')


class X1OriginalAdmission(unittest.TestCase):
    """Fixture-only full-chain refusals; existing owner/codec remain unchanged."""

    def test_x1_authority_faults_never_reach_lifecycle_or_dispatch(self):
        import test_owner_proposal as original
        from cortex_core.install import lifecycle
        helper = original.OwnerProposalOracle()
        changes = [
            ('peer_denied', lambda d: helper.edit_record(d, 'peer_ref', lambda p: p.update(owner_uid=502))),
            ('peer_denied', lambda d: helper.edit_record(d, 'peer_ref', lambda p: p.update(owner_epoch='forged'))),
            ('session_stale', lambda d: helper.edit_record(d, 'peer_ref', lambda p: p.update(expires_at=1700000001))),
            ('consent_revoked', lambda d: helper.edit_record(d, 'consent_ref', lambda c: c.update(revoked=True))),
            ('time_unavailable', lambda d: helper.clock(d, lambda c: c.update(owner_epoch='forged'))),
            ('reservation_expired', lambda d: helper.clock(d, lambda c: c.update(monotonic_ns=105000000000))),
            ('fence_changed', lambda d: helper.edit_record(d, 'current_state_ref', lambda s: s.update(host_fence=999))),
            ('scope_denied', lambda d: helper.scope(d, lambda s: s['identity'].update(request_id='forged'))),
        ]
        for detail, change in changes:
            with self.subTest(detail=detail):
                data = helper.data(); change(data)
                double = NonAuthoritativeSignedMemberDouble(data['runtime_lock_raw'].encode(),
                    aggregate_id=data['aggregate_id'], member_name='runtime.lock.json')
                with patch.object(owner, '_installed_owner_inputs', lambda: helper.inputs(owner, data)),                         double.installed(signed_member),                         patch.object(lifecycle, '_installed_lifecycle') as backend:
                    with self.assertRaises(owner.OwnerRefusal) as caught:
                        lifecycle.execute_effect(data['request_raw'].encode())
                self.assertEqual(caught.exception.detail, detail, 'X1A_ORIGINAL_AUTHORITY')
                backend.assert_not_called()

    def test_x1_changed_original_after_r01_has_no_lifecycle_binding(self):
        import test_owner_proposal as original
        from cortex_core.install import lifecycle
        helper = original.OwnerProposalOracle(); data = helper.data(); fresh = copy.deepcopy(data)
        helper.edit_record(fresh, 'peer_ref', lambda p: p.update(lifetime='replacement-process'))
        values = iter([helper.inputs(owner, data), helper.inputs(owner, fresh)])
        double = NonAuthoritativeSignedMemberDouble(data['runtime_lock_raw'].encode(),
            aggregate_id=data['aggregate_id'], member_name='runtime.lock.json')
        with patch.object(owner, '_installed_owner_inputs', lambda: next(values)),                 double.installed(signed_member), patch.object(lifecycle, '_installed_lifecycle') as backend:
            with self.assertRaises(owner.OwnerRefusal) as caught:
                lifecycle.execute_effect(data['request_raw'].encode())
        self.assertEqual(caught.exception.detail, 'resource_changed', 'X1A_ORIGINAL_JOIN')
        backend.assert_not_called()
