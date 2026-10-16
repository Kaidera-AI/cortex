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


class X1NonSQLDeadline(unittest.TestCase):
    """Real test-owned flock/fsync and P1 chain; no installed/native credit."""

    def subject(self, root):
        from test_hold1_interface import Subject
        return Subject(root)

    def primitive(self, s):
        from cortex_core.release.codec import canonical_bytes
        class Recorder:
            def snapshot(self): return s.before
            def dispatch(self, context):
                s.effects += 1
                s.trace.append(('dispatch', context))
            def measure(self, context):
                s.barrier = True
                c = context['context']
                from cortex_core.release.contracts import thaw
                return canonical_bytes({'identity':thaw(c['identity']), 'effect':thaw(c['effect']),
                    'request_ref':thaw(c['request_ref']), 'state':'applied_verified',
                    'observed':{'source_data_only':True}})
        return Recorder()

    def execute(self, s):
        from cortex_core.install import lifecycle
        return lifecycle.execute_effect(s.data['request_raw'].encode())

    def test_x1_non_sql_operation_retains_original_deadline(self):
        import tempfile
        from unittest.mock import patch
        from cortex_core.install import lifecycle
        with tempfile.TemporaryDirectory() as root:
            s=self.subject(root); operation=s.history.operation; seen=[]
            def observed(**kwargs):
                seen.append(kwargs)
                return operation(**kwargs)
            with s.installed(self.primitive(s)), patch.object(s.history,'operation',observed):
                result=self.execute(s)
            self.assertEqual(seen[0].get('deadline'),s.deadline,'X1B_NONSQL_ORIGINAL_E')
            self.assertEqual(seen[0].get('expected_context'),dict(s.proposal.context))
            self.assertEqual(s.effects,1)
            self.assertEqual(result,s.history.head()[0])
            _,obs=lifecycle._load_entry(s.history,result)
            reservation=json.loads(s.history.read(obs['reservation_ref']))
            self.assertEqual((reservation['expires_at'],reservation['expires_monotonic_ns']),
                (s.deadline.expires_at,s.deadline.expires_monotonic_ns))

    def test_x1_revoke_during_intent_file_fsync_prevents_publication(self):
        import os, stat, tempfile
        from unittest.mock import patch
        from cortex_core.install import lifecycle, owner
        with tempfile.TemporaryDirectory() as root:
            s=self.subject(root); actual=os.fsync; fired=[]
            def delayed(fd):
                actual(fd)
                if not fired and stat.S_ISREG(os.fstat(fd).st_mode):
                    fired.append(True); s.revoked=True
            with s.installed(self.primitive(s)), patch.object(lifecycle.os,'fsync',delayed):
                with self.assertRaises(owner.OwnerRefusal):self.execute(s)
            self.assertTrue(fired)
            self.assertIsNone(s.history.head()[0],'X1B_STALE_INTENT_PUBLISHED')
            self.assertEqual(s.effects,0)

    def test_x1_expire_during_consume_fsync_keeps_only_reservation(self):
        import os, stat, tempfile
        from unittest.mock import patch
        from cortex_core.install import lifecycle, owner
        with tempfile.TemporaryDirectory() as root:
            s=self.subject(root); actual=os.fsync; fired=[]
            def delayed(fd):
                actual(fd)
                if not fired and s.history.current_revision()==s.initial_revision+1                         and stat.S_ISREG(os.fstat(fd).st_mode):
                    fired.append(True); s.expired=True
            with s.installed(self.primitive(s)), patch.object(lifecycle.os,'fsync',delayed):
                with self.assertRaises(owner.OwnerRefusal):self.execute(s)
            self.assertTrue(fired)
            self.assertEqual(len(s.history.events()),1,'X1B_STALE_CONSUME_PUBLISHED')
            self.assertEqual(s.effects,0)

    def test_x1_revoke_after_measure_never_publishes_result(self):
        import os, stat, tempfile
        from unittest.mock import patch
        from cortex_core.install import lifecycle, owner
        with tempfile.TemporaryDirectory() as root:
            s=self.subject(root); actual=os.fsync; fired=[]
            def delayed(fd):
                actual(fd)
                if not fired and s.barrier and stat.S_ISREG(os.fstat(fd).st_mode):
                    fired.append(True); s.revoked=True
            with s.installed(self.primitive(s)), patch.object(lifecycle.os,'fsync',delayed):
                with self.assertRaises(owner.OwnerRefusal,msg='X1B_STALE_RESULT_ACK'):self.execute(s)
            self.assertTrue(fired); self.assertEqual(s.effects,1)
            self.assertEqual(len(s.history.events()),2)
            event,obs=lifecycle._load_entry(s.history,s.history.head()[0])
            self.assertIsNone(event['result_ref'])
            self.assertEqual(json.loads(s.history.read(obs['attempt_ref']))['dispatch_state'],
                'consumed_result_unknown')

    def test_x1_lost_ack_keeps_one_consumption_and_no_replay(self):
        import tempfile
        from unittest.mock import patch
        from cortex_core.install import lifecycle, owner
        with tempfile.TemporaryDirectory() as root:
            s=self.subject(root); publish=s.history._publish; lost=[]
            def lose(folder,name,raw):
                publish(folder,name,raw)
                if folder=='events' and json.loads(raw)['result_ref'] is not None:
                    lost.append(True); raise owner.OwnerRefusal('atomicity_unavailable')
            with s.installed(self.primitive(s)), patch.object(s.history,'_publish',lose):
                with self.assertRaises(owner.OwnerRefusal):self.execute(s)
                before=s.history.head()[0]
                with self.assertRaises(owner.OwnerRefusal):self.execute(s)
            self.assertEqual(lost,[True]); self.assertEqual(s.effects,1)
            self.assertEqual(len(s.history.events()),3)
            self.assertEqual(s.history.head()[0],before)
            _,obs=lifecycle._load_entry(s.history,before)
            attempt=json.loads(s.history.read(obs['attempt_ref']))
            self.assertEqual(attempt['consumption_sequence'],1)
            self.assertEqual(attempt['dispatch_state'],'applied_verified')

    def test_x1_contended_operation_rechecks_revoked_owner_before_dispatch(self):
        import fcntl, os, tempfile
        from unittest.mock import patch
        from cortex_core.install import lifecycle, owner
        with tempfile.TemporaryDirectory() as root:
            s=self.subject(root); descriptor=os.open(s.root/'.operation.lock',os.O_RDWR)
            fcntl.flock(descriptor,fcntl.LOCK_EX)
            lock=lifecycle.fcntl.flock;waited=[];blocking=[]
            identity=os.fstat(descriptor)
            def acquire(fd, flags):
                current=os.fstat(fd)
                if flags == fcntl.LOCK_EX and (current.st_dev,current.st_ino) == (identity.st_dev,identity.st_ino):
                    blocking.append(True)
                    raise BlockingIOError('fixture prevented an unbounded wait')
                return lock(fd,flags)
            def wait(seconds):
                waited.append(seconds);s.revoked=True
            try:
                with s.installed(self.primitive(s)), patch.object(lifecycle.time,'sleep',wait), \
                        patch.object(lifecycle.fcntl,'flock',acquire):
                    with self.assertRaises(owner.OwnerRefusal):self.execute(s)
            finally:
                fcntl.flock(descriptor,fcntl.LOCK_UN);os.close(descriptor)
            self.assertFalse(blocking,'X1B_UNBOUNDED_NONSQL_LOCK')
            self.assertTrue(waited);self.assertEqual(s.effects,0)
            self.assertIsNone(s.history.head()[0])

    def test_x1_result_directory_fsync_loss_refuses_ack_with_durable_truth(self):
        import os,stat,tempfile
        from unittest.mock import patch
        from cortex_core.install import lifecycle,owner
        with tempfile.TemporaryDirectory() as root:
            s=self.subject(root);actual=os.fsync;fired=[]
            def delayed(fd):
                actual(fd)
                if not fired and stat.S_ISDIR(os.fstat(fd).st_mode):
                    events=s.history.events()
                    if events and events[-1][1]['result_ref'] is not None:
                        fired.append(True);s.revoked=True
            with s.installed(self.primitive(s)),patch.object(lifecycle.os,'fsync',delayed):
                with self.assertRaises(owner.OwnerRefusal,msg='X1B_STALE_POST_FSYNC_ACK'):
                    self.execute(s)
            self.assertEqual(fired,[True]);self.assertEqual(s.effects,1)
            self.assertEqual(len(s.history.events()),3)
            self.assertIsNotNone(s.history.head()[1]['result_ref'])
            reopened=lifecycle._History(s.root)
            self.assertEqual(reopened.head()[0],s.history.head()[0])
