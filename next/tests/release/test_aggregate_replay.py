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

    def test_owner_tighter_deadline_is_enforced(self):
        api = self.api(); clock = [100.0]; self.policy['limits']['read_seconds'] = 1
        with patch.object(api.time, 'monotonic', lambda: clock[0]):
            delivery = self.backend(); clock[0] = 101.01
            self.refusal('raw_limit', lambda: delivery.read_original(262144))
        self.assert_closed()

if __name__ == '__main__': unittest.main()
