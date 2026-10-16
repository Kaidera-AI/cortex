"""B1-CLOCK-01 same-call DATA regressions; no protected or durable execution.

Vera's three probes and Hale's three witnesses share the deciding assertions.
Each monotonic test retains both reviewer setups as separately named subtests.
"""
import copy
import hashlib
import json
import unittest

import test_owner_proposal as proposals


class OwnerDeadlineRetentionOracle(unittest.TestCase):
    def helper(self):
        return proposals.OwnerProposalOracle()

    def provider(self, helper, data, path, bound, now):
        clock = json.loads(data['current_clock_raw'])
        receipt = json.loads(data['originals'][clock['clock_receipt_ref']['path']])
        receipt['monotonic_deadlines']['host'] = bound
        raw = proposals.encode(receipt)
        data['originals'][path] = raw.decode()
        helper.clock(data, lambda c: c.update(monotonic_ns=now, clock_receipt_ref={
            'path': path, 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}))

    def monotonic_pair(self, reviewer, now):
        helper = self.helper()
        first = helper.data()
        initial_now = 101000000000 if reviewer == 'Vera' else 100000000000
        self.provider(helper, first, 'first-current-clock.json', 103000000000, initial_now)
        fresh = helper.data() if reviewer == 'Vera' else copy.deepcopy(first)
        later_bound = 105000000000 if reviewer == 'Vera' else 107000000000
        self.provider(helper, fresh, 'fresh-current-clock.json', later_bound, now)
        self.assertEqual(first['context_raw'], fresh['context_raw'])
        return helper, first, fresh

    def test_reobservation_retains_first_monotonic_deadline(self):
        for reviewer in ('Vera', 'Hale'):
            with self.subTest(reviewer=reviewer):
                helper, first, fresh = self.monotonic_pair(reviewer, 102000000000)
                proposal, calls = helper.run_gate(helper.api(), first, fresh=fresh)
                self.assertEqual(len(calls), 2)
                self.assertEqual(proposal.deadline.expires_monotonic_ns, 103000000000)
                self.assertFalse(proposal.effect_authority)

    def test_reobservation_at_first_monotonic_deadline_refuses(self):
        for reviewer in ('Vera', 'Hale'):
            with self.subTest(reviewer=reviewer):
                helper, first, fresh = self.monotonic_pair(reviewer, 103000000000)
                api = helper.api()
                helper.refusal(api, 'reservation_expired',
                               lambda: helper.run_gate(api, first, fresh=fresh))

    def test_reobservation_after_first_monotonic_deadline_refuses(self):
        for reviewer in ('Vera', 'Hale'):
            with self.subTest(reviewer=reviewer):
                helper, first, fresh = self.monotonic_pair(reviewer, 104000000000)
                api = helper.api()
                helper.refusal(api, 'reservation_expired',
                               lambda: helper.run_gate(api, first, fresh=fresh))

    def utc_pair(self, now):
        helper = self.helper()
        first = helper.data()
        helper.context(first, lambda c: c['host_lease'].update(expires_at=1700000300))
        helper.clock_receipt(first, lambda r: r['monotonic_deadlines'].update(
            {name: 400000000000 for name in r['monotonic_deadlines']}))
        fresh = copy.deepcopy(first)
        # Only the explicitly refreshable clock_ref changes; all other context
        # facts stay identical. A later proposal start cannot renew this call.
        helper.edit_record(fresh, 'clock_ref', lambda c: c.update(
            now_utc=1700000002, monotonic_ns=102000000000))
        helper.clock(fresh, lambda c: c.update(now_utc=now, monotonic_ns=103000000000))
        a, b = json.loads(first['context_raw']), json.loads(fresh['context_raw'])
        self.assertEqual({k: v for k, v in a.items() if k != 'clock_ref'},
                         {k: v for k, v in b.items() if k != 'clock_ref'})
        return helper, first, fresh

    def test_reobservation_retains_first_utc_deadline_and_proposal_origin(self):
        helper, first, fresh = self.utc_pair(1700000029)
        proposal, calls = helper.run_gate(helper.api(), first, fresh=fresh)
        self.assertEqual(len(calls), 2)
        self.assertEqual((proposal.deadline.expires_at, proposal.deadline.expires_monotonic_ns),
                         (1700000030, 130000000000))
        self.assertEqual((proposal.deadline.proposed_at_utc, proposal.deadline.proposed_at_monotonic_ns),
                         (1700000000, 100000000000))

    def test_reobservation_at_first_utc_deadline_refuses(self):
        helper, first, fresh = self.utc_pair(1700000030)
        api = helper.api()
        helper.refusal(api, 'reservation_expired', lambda: helper.run_gate(api, first, fresh=fresh))

    def test_reobservation_after_first_utc_deadline_refuses(self):
        helper, first, fresh = self.utc_pair(1700000031)
        api = helper.api()
        helper.refusal(api, 'reservation_expired', lambda: helper.run_gate(api, first, fresh=fresh))

    def test_reobservation_fresh_shorter_deadline_still_applies(self):
        helper = self.helper()
        first = helper.data()
        self.provider(helper, first, 'first-current-clock.json', 107000000000, 100000000000)
        fresh = copy.deepcopy(first)
        self.provider(helper, fresh, 'fresh-current-clock.json', 103000000000, 102000000000)
        self.assertEqual(first['context_raw'], fresh['context_raw'])
        proposal, _ = helper.run_gate(helper.api(), first, fresh=fresh)
        self.assertEqual(proposal.deadline.expires_monotonic_ns, 103000000000)
        self.assertFalse(proposal.effect_authority)
