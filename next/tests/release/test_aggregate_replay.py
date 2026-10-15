"""Current/replay/EOF controls against actual U3-D read-only admission body."""
from dataclasses import replace
import copy
import unittest
from unittest.mock import patch

from test_original_member_bridge import Fixture, MEMBER
from cortex_core.release.contracts import freeze
from cortex_core.release.custody import RootReader


class Replay(Fixture, unittest.TestCase):
    def test_valid_older_signed_subject_refuses_before_current_digest(self):
        self.policy['sequence_floor'] = self.value['sequence']+1
        self.policy['expected_aggregate_sha256'] = 'f'*64
        self.refusal('aggregate_replay', self.backend); self.assert_closed()

    def test_current_digest_differs_after_actual_valid_crypto(self):
        self.policy['expected_aggregate_sha256'] = 'f'*64
        self.refusal('aggregate_identity_mismatch', self.backend); self.assert_closed()

    def test_expired_current_time_before_member_transport_open(self):
        self.authority.now = 9999
        with patch.object(RootReader, 'aggregate_begin', side_effect=AssertionError('expired physical read')):
            self.refusal('aggregate_replay', self.backend)
        self.assert_closed()

    def test_generation_changes_during_pair_audit(self):
        self.current_hook = lambda c: replace(c, generation=2) if len(self.contexts)>1 else c
        self.refusal('aggregate_replay', self.backend); self.assert_closed()

    def test_fence_changes_during_pair_audit(self):
        self.current_hook = lambda c: replace(c, fence=2) if len(self.contexts)>1 else c
        self.refusal('aggregate_replay', self.backend); self.assert_closed()

    def test_time_expires_during_pair_audit(self):
        self.current_hook = lambda c: replace(c, now=9999) if len(self.contexts)>1 else c
        self.refusal('aggregate_replay', self.backend); self.assert_closed()

    def test_missing_current_state_after_pair_audit(self):
        self.current_hook = lambda c: replace(c, current_state=None) if len(self.contexts)>1 else c
        self.refusal('required_binding_gap', self.backend); self.assert_closed()

    def test_revoked_policy_after_pair_audit(self):
        self.current_hook = lambda c: None if len(self.contexts)>1 else c
        self.refusal('required_binding_gap', self.backend); self.assert_closed()

    def test_freshness_is_checked_before_EOF_work(self):
        self.current_hook = lambda c: replace(c, now=9999) if len(self.contexts)>1 else c
        with patch.object(RootReader, 'aggregate_finish', side_effect=AssertionError('EOF work before current refusal')):
            self.refusal('aggregate_replay', self.backend)
        self.assert_closed()

    def test_coherent_new_lease_during_audit_cannot_replace_original_context(self):
        from cortex_core.release.trust import _OwnerContext
        from cortex_core.release.contracts import thaw
        from aggregate_fixture import encode, sha256
        def changed(c):
            if len(self.contexts)==1: return c
            policy = thaw(c.policy); policy['owner_binding']['policy_generation'] = 2
            state = thaw(c.current_state); state['policy_generation'] = 2
            raw = encode(policy)
            return _OwnerContext(freeze(policy),raw,sha256(raw),c.public_root,c.now,2,c.fence,c.placement,freeze(state))
        self.current_hook = changed
        self.refusal('aggregate_replay', self.backend); self.assert_closed()

    def test_missing_current_state_after_EOF(self):
        original = RootReader.aggregate_finish
        def finish(reader):
            original(reader); self.authority.current_state = None
        with patch.object(RootReader, 'aggregate_finish', finish):
            self.refusal('required_binding_gap', self.backend)
        self.assert_closed()

    def test_fence_changes_after_EOF(self):
        original = RootReader.aggregate_finish
        def finish(reader): original(reader); self.authority.fence = 2
        with patch.object(RootReader, 'aggregate_finish', finish): self.refusal('aggregate_replay', self.backend)
        self.assert_closed()

    def test_missing_current_state_before_delivery_reread(self):
        delivery = self.backend(); self.authority.current_state = None
        self.refusal('required_binding_gap', lambda: delivery.read_original(262144)); self.assert_closed()

    def test_generation_changes_before_delivery_reread(self):
        delivery = self.backend(); self.authority.generation = 2
        self.refusal('aggregate_replay', lambda: delivery.read_original(262144)); self.assert_closed()

    def test_delivery_shared_read_deadline_cannot_reset(self):
        api = self.api(); clock = [100.0]
        with patch.object(api.time, 'monotonic', lambda: clock[0]):
            delivery = self.backend(); clock[0] = 221.0
            self.refusal('raw_limit', lambda: delivery.read_original(262144))
        self.assert_closed()

    def test_deadline_exceeded_during_pair_audit_no_value(self):
        import cortex_core.release.aggregate as aggregate
        api = self.api(); clock = [100.0]; original = aggregate._check_pairs
        def after(*args): original(*args); clock[0] = 221.0
        with patch.object(api.time, 'monotonic', lambda: clock[0]), patch.object(aggregate, '_check_pairs', after):
            self.refusal('raw_limit', self.backend)
        self.assert_closed()

    def test_delivery_remaining_budget_reaches_every_original_read(self):
        import cortex_core.release.aggregate as aggregate
        api = self.api(); clock = [100.0]; read_calls = []
        original_pairs = aggregate._check_pairs
        def pairs(*args):
            original_pairs(*args); clock[0] = 219.0
        with patch.object(api.time,'monotonic',lambda: clock[0]),patch.object(aggregate,'_check_pairs',pairs):
            delivery = self.backend()
        def reader_hook(reader):
            body = reader.aggregate_bytes
            def late(path):
                read_calls.append(path); clock[0] = 221.0; return body(path)
            reader.aggregate_bytes = late
        self.transport_hook = reader_hook
        with patch.object(api.time,'monotonic',lambda: clock[0]):
            self.refusal('raw_limit',lambda: delivery.read_original(262144))
        # A shared expired budget refuses at the first custody/read check,
        # before proceeding through the remaining original graph.
        self.assertEqual(len(read_calls),1)
        self.assert_closed()

    def test_exhausted_budget_refuses_before_physical_member_open(self):
        import cortex_core.release.aggregate as aggregate
        api = self.api(); clock = [100.0]; original = aggregate._validate_manifest
        def exhausted(*args):
            result = original(*args); clock[0] = 220.0; return result
        with patch.object(api.time,'monotonic',lambda: clock[0]),patch.object(aggregate,'_validate_manifest',exhausted),patch.object(RootReader,'aggregate_begin',side_effect=AssertionError('physical open with exhausted budget')):
            self.refusal('raw_limit',self.backend)
        self.assert_closed()

    def test_elapsed_measurement_refuses_before_pair_parser(self):
        import cortex_core.release.aggregate as aggregate
        api = self.api(); clock = [100.0]; original = aggregate._measure_members
        def measured(*args,**kwargs):
            result = original(*args,**kwargs); clock[0] = 221.0; return result
        with patch.object(api.time,'monotonic',lambda: clock[0]),patch.object(aggregate,'_measure_members',measured),patch.object(aggregate,'_check_pairs',side_effect=AssertionError('pair parser after expired budget')):
            self.refusal('raw_limit',self.backend)
        self.assert_closed()

    def test_deadline_exceeded_after_EOF_no_partial_delivery(self):
        api = self.api(); clock = [100.0]; original = RootReader.aggregate_finish
        def finish(reader):
            original(reader); clock[0] = 221.0
        with patch.object(api.time,'monotonic',lambda: clock[0]),patch.object(RootReader,'aggregate_finish',finish):
            self.refusal('raw_limit',self.backend)
        self.assert_closed()

    def test_original_replaced_during_final_current_reload_refuses(self):
        def changed(c):
            if len(self.contexts)==3:
                replacement = self.root/'replacement.tmp'; replacement.write_bytes(self.bodies[MEMBER])
                replacement.replace(self.root/MEMBER)
            return c
        self.current_hook = changed
        self.refusal('member_custody_changed',self.backend); self.assert_closed()

    def test_expired_delivery_cannot_reach_original_transport(self):
        api = self.api(); clock = [100.0]
        with patch.object(api.time,'monotonic',lambda: clock[0]):
            delivery = self.backend()
            self.transport_hook = lambda reader: self.fail('expired delivery reached original transport')
            clock[0] = 221.0
            self.refusal('raw_limit',lambda: delivery.read_original(262144))
        self.assertEqual(self.transports,[self.aggregate]); self.assert_closed()

    def test_owner_tighter_deadline_is_enforced(self):
        api = self.api(); clock = [100.0]; self.policy['limits']['read_seconds'] = 1
        with patch.object(api.time, 'monotonic', lambda: clock[0]):
            delivery = self.backend(); clock[0] = 101.01
            self.refusal('raw_limit', lambda: delivery.read_original(262144))
        self.assert_closed()

if __name__ == '__main__': unittest.main()
