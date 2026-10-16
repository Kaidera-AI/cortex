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
                    expected_revision=ctx['state_revision'] if boundary['reservation_stage']=='proposed' else r['expected_state_revision']+(r['state']=='consumed')
                    self.assertEqual(boundary['state_revision'],expected_revision)
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
                   if c['group']=='C0-11' and c['kind']=='crash_control'}
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
        self.assertEqual(frozen.SUBJECTS['Q3_acceptance_ref']['sha256'],'8fb4badfba7d8fa4c7ecbfe54b247e9a23fdfa7061eee2b29f44fff15bf00c32')
        self.assertEqual(frozen.SUBJECTS['fold'],'existing-P2-event-history-only')


class OwnerSessionSurface(unittest.TestCase):
    pass


class RecoveryAndDeadlineRequirements(unittest.TestCase):
    """Independent C0 requirement assertions, frozen RED before literal repair."""
    def original(self, reference):
        self.assertIsInstance(reference, dict, 'required original is absent')
        self.assertEqual(set(reference), {'path','bytes','sha256'})
        raw = frozen.SUBJECTS['original_owner_fact_bytes'].get(reference['path'])
        self.assertIsNotNone(raw, 'original referenced bytes are absent')
        self.assertEqual(len(raw.encode()), reference['bytes'])
        self.assertEqual(frozen.hashlib.sha256(raw.encode()).hexdigest(), reference['sha256'])
        return frozen.json.loads(raw)

    def case(self, identity):
        value = frozen.CASES.get(identity)
        self.assertIsNotNone(value, 'missing literal control '+identity)
        return value

    def test_every_consumed_original_has_sequence_clock_and_consumed_reservation(self):
        for case in frozen.CASES.values():
            if case['expected']['history']['owner_boundary']['consumed_grants']:
                with self.subTest(case=case['id']):
                    role = case['input']['boundary'].get('current_attempt_role')
                    self.assertIn(role, ['original','fresh_child','fresh_fix_forward'])
                    facts = case['input']['facts']
                    if role == 'original':
                        attempt = facts['attempt']
                        self.assertNotEqual(attempt['dispatch_state'], 'not_consumed')
                        self.assertIsNotNone(attempt['consumption_sequence'])
                        clock = self.original(attempt['consume_clock_ref'])
                        self.assertEqual(clock['owner_epoch'], facts['context']['owner_epoch'])
                        self.assertEqual(facts['reservation']['state'], 'consumed')
                        self.assertEqual(case['input']['boundary']['state_revision'], facts['reservation']['expected_state_revision']+1)
                    else:
                        parents = case['input'].get('original_history_refs')
                        self.assertTrue(parents)
                        parent = self.original(parents[-1])
                        self.assertEqual(facts['attempt']['identity']['parent_attempt_id'], parent['identity']['attempt_id'])
                        self.assertIsNotNone(parent['consumption_sequence'])
                        self.original(parent['consume_clock_ref'])
                        self.assertEqual(facts['attempt']['dispatch_state'], 'not_consumed')
                        self.assertIsNone(facts['attempt']['consumption_sequence'])
                        self.assertIsNone(facts['attempt']['consume_clock_ref'])
                        self.assertNotEqual(facts['context']['owner_epoch'], self.original(parent['context_ref'])['owner_epoch'])

    def test_completed_readonly_recovery_has_original_durable_applied_result(self):
        for case in frozen.CASES.values():
            if case['expected']['action'] == 'READ_ONLY_ORIGINAL_RESULT':
                with self.subTest(case=case['id']):
                    attempt = case['input']['facts']['attempt']
                    self.assertEqual(attempt['dispatch_state'], 'applied_verified')
                    result = self.original(attempt['result_ref'])
                    self.assertEqual(result['outcome'], 'APPLIED')
                    self.assertEqual(result['identity'], attempt['identity'])
                    self.assertEqual(result['effect'], attempt['effect'])
                    self.assertEqual(case['expected']['delta']['owner_boundary'], {'reserved_intents':0,'consumed_grants':0})
                    self.assertEqual(case['expected']['delta']['primitive_boundary']['dispatches'], 0)

    def test_durable_reply_loss_reads_the_same_result_without_any_new_effect(self):
        positive = self.case('C0-11-reply-lost-P')
        lost = self.case('C0-11-reply-lost-N')
        self.assertEqual(lost['expected']['action'], 'READ_ONLY_ORIGINAL_RESULT')
        self.assertEqual(lost['expected'], positive['expected'])
        self.assertEqual((lost['expected']['result'],lost['expected']['reason_code'],lost['expected']['detail']), ('PASS','completed',None))
        self.assertEqual(lost['input']['facts']['attempt']['result_ref'], positive['input']['facts']['attempt']['result_ref'])
        self.original(lost['input']['facts']['attempt']['result_ref'])
        self.assertEqual(frozen.changed_paths(positive['input'],lost['input']), ['boundary.crash'])

    def test_all_eight_crash_dispositions_follow_original_state(self):
        expected = {
            'before-intent': ('BLOCKED','dependency_not_run','required_binding_gap','FRESH_CURRENT_RESERVE_REQUIRED'),
            'intent-no-consume': ('FAIL','external_conflict','reservation_invalidated','INVALIDATE_AND_FRESH_RESERVE'),
            'consume-uncertain': ('HOLD','ambiguous_create','attempt_ambiguous','HOLD_ORIGINAL_EFFECT'),
            'commit-no-result': ('PASS','completed',None,'APPEND_ORIGINAL_RESULT_ONCE'),
            'not-applied': ('PASS','completed',None,'FRESH_CHILD_SAME_LOGICAL_EFFECT'),
            'reply-lost': ('PASS','completed',None,'READ_ONLY_ORIGINAL_RESULT'),
            'corrupt-history': ('HOLD','state_drift','journal_invalid','HOLD_ORIGINAL_LINEAGE'),
            'accepted-activation-failed': ('PASS','completed',None,'FRESH_FIX_FORWARD_ONLY')}
        for point, want in expected.items():
            with self.subTest(point=point):
                ex = self.case('C0-11-'+point+'-N')['expected']
                self.assertEqual(tuple(ex[k] for k in ['result','reason_code','detail','action']), want)

    def test_unknown_original_cannot_claim_completed_receipt_or_erase_history(self):
        for identity in ['C0-11-consume-uncertain-P','C0-14-missing-P']:
            case = self.case(identity)
            self.assertEqual((case['expected']['result'],case['expected']['detail']), ('HOLD','attempt_ambiguous'))
            self.assertEqual(case['input']['facts']['attempt']['dispatch_state'], 'consumed_result_unknown')
            self.assertIsNone(case['input']['facts']['attempt']['result_ref'])
            self.assertEqual(case['expected']['history']['owner_boundary']['consumed_grants'], 1)
            self.assertEqual(case['expected']['delta']['primitive_boundary']['dispatches'], 0)

    def test_not_applied_child_is_fresh_and_preserves_original_effect(self):
        for identity in ['C0-11-not-applied-P','C0-13-fresh-child-P']:
            case = self.case(identity); facts = case['input']['facts']
            self.assertEqual(case['input']['boundary'].get('current_attempt_role'), 'fresh_child')
            parents = case['input'].get('original_history_refs'); self.assertTrue(parents)
            parent = self.original(parents[-1]); result = self.original(parent['result_ref'])
            self.assertEqual(result['outcome'], 'NOT_APPLIED')
            self.assertEqual(parent['effect'], facts['attempt']['effect'])
            self.assertEqual(parent['origin_request_ref'], facts['attempt']['origin_request_ref'])
            self.assertEqual(facts['attempt']['identity']['parent_attempt_id'], parent['identity']['attempt_id'])
            self.assertNotEqual(parent['reservation_ref'], facts['attempt']['reservation_ref'])
            self.assertGreater(facts['context']['host_lease']['fence'], self.original(parent['context_ref'])['host_lease']['fence'])

    def test_every_positive_reservation_is_exact_minimum_and_mint_interval_valid(self):
        for case in frozen.CASES.values():
            if case['kind'] != 'conditional_positive': continue
            with self.subTest(case=case['id']):
                facts=case['input']['facts']; context=facts['context']; clock=facts['clock']; reservation=facts['reservation']
                policy=self.original(context['policy_ref']); peer=self.original(context['peer_ref'])
                self.assertIn('validity',policy); self.assertIn('expires_at',peer)
                clock_receipt=self.original(clock['clock_receipt_ref'])
                self.assertIn('monotonic_deadlines',clock_receipt)
                deadlines=[reservation['reserved_at']+30,facts['consent']['expires_at'],context['host_lease']['expires_at'],
                           policy['validity']['expires_at'],peer['expires_at']]
                if context['private_binding']['kind']=='private':
                    deadlines.append(self.original(context['private_binding']['binding_ref'])['expires_at'])
                applicable={'consent','host','policy','session'}
                if context['private_binding']['kind']=='private': applicable.add('private')
                self.assertEqual(set(clock_receipt['monotonic_deadlines']),applicable)
                self.assertEqual(reservation['expires_at'],min(deadlines))
                self.assertEqual(reservation['expires_monotonic_ns'],min([clock['monotonic_ns']+30_000_000_000]+list(clock_receipt['monotonic_deadlines'].values())))
                self.assertEqual(clock_receipt['owner_epoch'],context['owner_epoch'])
                lower=clock['now_utc']-clock['uncertainty_seconds']; upper=clock['now_utc']+clock['uncertainty_seconds']
                self.assertGreaterEqual(lower,max(facts['consent']['not_before'],policy['validity']['not_before'],peer['not_before']))
                self.assertLess(upper,reservation['expires_at'])

    def test_each_authority_can_shorten_and_equal_or_after_utc_deadline_refuses(self):
        reasons={'consent':'consent_expired','host':'reservation_expired','private':'reservation_expired',
                 'policy':'reservation_expired','session':'session_stale'}
        for winner, reason in reasons.items():
            with self.subTest(winner=winner):
                p=self.case('F2-'+winner+'-P'); r=p['input']['facts']['reservation']
                self.assertEqual(p['input']['boundary'].get('shorter_deadline_winner'),winner)
                self.assertLess(r['expires_at'],r['reserved_at']+30)
                for suffix,offset in [('utc-equal',0),('utc-after',1)]:
                    n=self.case('F2-'+winner+'-'+suffix+'-N')
                    self.assertEqual(n['input']['boundary']['final_utc'],r['expires_at']+offset)
                    self.assertEqual(n['input']['boundary']['final_uncertainty_seconds'],0)
                    self.assertEqual(n['expected']['detail'],reason)
                    self.assertEqual(n['expected']['history']['owner_boundary'],{'reserved_intents':1,'consumed_grants':0})
                    self.assertEqual(n['expected']['delta']['primitive_boundary']['dispatches'],0)
                    self.assertEqual(frozen.changed_paths(p['input'],n['input']),['boundary.final_utc'])

    def test_each_same_epoch_monotonic_equality_or_after_refuses(self):
        for winner in ['consent','host','private','policy','session','monotonic']:
            p=self.case('F2-'+winner+'-P'); r=p['input']['facts']['reservation']
            for suffix,offset in [('mono-equal',0),('mono-after',1)]:
                with self.subTest(winner=winner,suffix=suffix):
                    n=self.case('F2-'+winner+'-'+suffix+'-N')
                    self.assertEqual(n['input']['boundary']['final_monotonic_ns'],r['expires_monotonic_ns']+offset)
                    self.assertEqual(n['expected']['detail'],'reservation_expired')
                    self.assertEqual(n['expected']['delta']['owner_boundary']['consumed_grants'],0)
                    self.assertEqual(frozen.changed_paths(p['input'],n['input']),['boundary.final_monotonic_ns'])

    def test_uncertainty_lower_not_before_boundary_is_inclusive_and_outside_unavailable(self):
        p=self.case('F2-not-before-P'); n=self.case('F2-not-before-N')
        boundary=p['input']['boundary']; not_before=p['input']['facts']['consent']['not_before']
        self.assertEqual(boundary['final_utc']-boundary['final_uncertainty_seconds'],not_before)
        self.assertLess(n['input']['boundary']['final_utc']-n['input']['boundary']['final_uncertainty_seconds'],not_before)
        self.assertEqual(n['expected']['detail'],'time_unavailable')
        self.assertEqual(frozen.changed_paths(p['input'],n['input']),['boundary.final_uncertainty_seconds'])

    def test_q3_is_immutable_accepted_reference_not_historical_open_questions(self):
        reference=frozen.SUBJECTS.get('Q3_acceptance_ref'); self.assertIsNotNone(reference)
        self.assertEqual(reference['sha256'],'8fb4badfba7d8fa4c7ecbfe54b247e9a23fdfa7061eee2b29f44fff15bf00c32')
        self.assertEqual(reference['bytes'],20401)
        self.assertEqual(reference['items'],['Q3-'+str(i) for i in range(1,7)])
        self.assertNotIn('Vera_open',frozen.SUBJECTS)

    def test_all_control_differences_remain_one_declared_boundary(self):
        for case in frozen.CASES.values():
            if case.get('control_id') and type(case.get('fault')) is dict:
                with self.subTest(case=case['id']):
                    self.assertEqual(frozen.changed_paths(self.case(case['control_id'])['input'],case['input']),[case['fault']['path']])

    def test_current_attempt_and_current_revision_are_bound_original_observations(self):
        for case in frozen.CASES.values():
            with self.subTest(case=case['id']):
                self.assertEqual(self.original(case['input'].get('original_attempt_ref')),case['input']['facts']['attempt'])
                states=case['input']['boundary'].get('current_state_refs'); self.assertTrue(states)
                state=self.original(states[-1])
                if case.get('fault') and type(case['fault']) is dict and case['fault']['path']=='boundary.current_state_refs':
                    self.assertNotEqual(state['revision'],case['input']['boundary']['state_revision'])
                    self.assertEqual(case['expected']['detail'],'reservation_invalidated')
                else:
                    self.assertEqual(state['revision'],case['input']['boundary']['state_revision'])

    def test_all_current_and_parent_effect_idempotency_subjects_correspond(self):
        for case in frozen.CASES.values():
            with self.subTest(case=case['id']):
                attempts=[case['input']['facts']['attempt']]+[self.original(ref) for ref in case['input'].get('original_history_refs',[])]
                for attempt in attempts:
                    original=self.original(attempt['effect']['idempotency_ref'])
                    self.assertEqual(original['effect_id'],attempt['effect']['effect_id'])

    def test_proposal_reserved_and_consumed_revisions_match_the_durable_phase(self):
        for case in frozen.CASES.values():
            if case['kind']!='conditional_positive':continue
            with self.subTest(case=case['id']):
                facts=case['input']['facts']; boundary=case['input']['boundary']
                state=self.original(boundary['current_state_refs'][-1])
                stage=boundary['reservation_stage']; r=facts['reservation']; context=facts['context']
                want=context['state_revision'] if stage=='proposed' else r['expected_state_revision']+(stage=='durable_consumed')
                self.assertEqual(state['revision'],want)


for _case in frozen.CASES.values():
    if _case['group'] not in frozen.ADMISSION_GROUPS:
        def _body(self, case=_case):
            frozen.compare_execution(self, case)
        setattr(OwnerSessionSurface,'test_'+_case['id'].replace('-','_'),_body)


if __name__ == '__main__':
    unittest.main()


class X1ClientWireContract(unittest.TestCase):
    def test_x1_unbound_client_cannot_turn_r01_data_into_execution(self):
        from unittest.mock import patch
        from cortex_core.install import __main__ as client, lifecycle
        from cortex_core.runtime import signed_member
        with patch.object(signed_member,'request_member',return_value=object()) as r01, \
                patch.object(lifecycle,'execute_effect') as execute:
            result=client.main()
        self.assertEqual((result.result,result.reason_code,result.unit),
            ('BLOCKED','dependency_not_run','P1 installation owner composition'))
        self.assertFalse(result.effect_authority);self.assertFalse(result.native_qualified)
        r01.assert_called_once_with('','runtime.lock.json');execute.assert_not_called()

    def test_x1_original_closed_wire_rejects_caller_selected_supplier(self):
        import json
        import test_owner_proposal as original
        from cortex_core.install.contracts import InstallRefusal,parse_install_message
        data=original.OwnerProposalOracle().data(); q=json.loads(data['request_raw'])
        self.assertEqual(parse_install_message(data['request_raw'].encode(),expected_kind='request').raw,
            data['request_raw'].encode())
        q['owner_supplier']='caller-selected'
        with self.assertRaises(InstallRefusal):
            parse_install_message(original.encode(q),expected_kind='request')
