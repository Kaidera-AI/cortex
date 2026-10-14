"""B0 frozen temporal/recovery oracle. Original executions remain NOT_RUN."""
import unittest
import test_owner_admission as frozen


class FrozenSessionContract(unittest.TestCase):
    def test_finite_strict_deadlines_clock_advance_and_post_reserve_revision(self):
        self.assertEqual(frozen.SUBJECTS['reservation_max_seconds'], 30)
        for case in frozen.CASES.values():
            if case['kind'] == 'conditional_positive':
                with self.subTest(case=case['id']):
                    facts = case['input']['facts']; boundary = case['input']['boundary']
                    r = facts['reservation']; ctx = facts['context']; clock = facts['clock']
                    self.assertEqual(r['expected_state_revision'], ctx['state_revision']+1)
                    self.assertEqual(boundary['state_revision'], r['expected_state_revision'])
                    self.assertLessEqual(r['expires_at']-r['reserved_at'], 30)
                    self.assertLessEqual(r['expires_monotonic_ns']-clock['monotonic_ns'], 30_000_000_000)
                    self.assertGreater(boundary['final_monotonic_ns'], clock['monotonic_ns'])
                    self.assertLess(boundary['final_monotonic_ns'], r['expires_monotonic_ns'])
                    self.assertLess(boundary['final_utc']+boundary['final_uncertainty_seconds'], r['expires_at'])

    def test_two_independent_boundaries_preserve_historical_totals(self):
        for case in frozen.CASES.values():
            ex = case['expected']
            for boundary in ['owner_boundary','primitive_boundary']:
                with self.subTest(case=case['id'],boundary=boundary):
                    self.assertEqual(set(ex['history'][boundary]),set(ex['delta'][boundary]))
                    for name in ex['history'][boundary]:
                        self.assertEqual(ex['totals'][boundary][name], ex['history'][boundary][name]+ex['delta'][boundary][name])
                        self.assertGreaterEqual(ex['history'][boundary][name],0)
                        self.assertGreaterEqual(ex['delta'][boundary][name],0)
        late = [c for c in frozen.CASES.values() if c['kind']=='sole_fault_negative' and c['phase']=='after_reserve']
        self.assertGreater(len(late),0)
        for case in late:
            self.assertEqual(case['expected']['history']['owner_boundary'],{'reserved_intents':1,'consumed_grants':0})
            self.assertEqual(case['expected']['delta']['primitive_boundary']['dispatches'],0)

    def test_all_crash_points_and_no_blind_retry_contract(self):
        crashes = {c['input']['boundary']['crash'] for c in frozen.CASES.values()
                   if c['group']=='C0-11' and c['kind']=='sole_fault_negative'}
        self.assertEqual(crashes, {'before-intent','intent-no-consume','consume-uncertain','commit-no-result',
                                  'not-applied','reply-lost','corrupt-history','accepted-activation-failed'})
        for case in frozen.CASES.values():
            if case['expected']['detail']=='attempt_ambiguous':
                self.assertEqual(case['expected']['result'],'HOLD')
                self.assertEqual(case['expected']['delta']['primitive_boundary']['dispatches'],0)

    def test_precedence_and_single_provider_open_physical_contract(self):
        precedence = {c['id']:c['expected']['detail'] for c in frozen.CASES.values() if c['group']=='PRECEDENCE'}
        self.assertEqual(precedence, {'PRECEDENCE-fixed-gap':'required_binding_gap','PRECEDENCE-peer':'peer_denied',
            'PRECEDENCE-shape':'invalid_request','PRECEDENCE-unavailable':'owner_unavailable',
            'PRECEDENCE-scope':'scope_denied','PRECEDENCE-r01':'r01_other_refusal'})
        self.assertEqual(frozen.SUBJECTS['interpreter_contract'],'KOS 249cc66c bundled CPython 3.12.15')
        self.assertEqual(frozen.SUBJECTS['provider_contract'], {'function':'R01.verify_ed25519','provider':'cryptography',
            'version':'50.0.0','transitives':{'cffi':'2.1.1','pycparser':'3.0'},'PyNaCl':'excluded','fallback':False})
        self.assertEqual(frozen.SUBJECTS['physical_bindings'],'UNBOUND')
        self.assertEqual(len(frozen.SUBJECTS['Vera_open']),2)
        self.assertEqual(frozen.SUBJECTS['fold'],'existing-P2-event-history-only')


class OwnerSessionSurface(unittest.TestCase):
    pass


for _case in frozen.CASES.values():
    if _case['group'] not in frozen.ADMISSION_GROUPS:
        def _body(self, case=_case):
            frozen.compare_execution(self, case)
        setattr(OwnerSessionSurface,'test_'+_case['id'].replace('-','_'),_body)


if __name__ == '__main__':
    unittest.main()
