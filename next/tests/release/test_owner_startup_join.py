"""Cross-owner joins: both original sides are individually valid SOURCE DATA."""
import unittest
import test_owner_startup_binding as support
from cortex_core.runtime import signed_member as port


class CrossOwnerOriginals(unittest.TestCase):
    def fixture(self):
        f=support.OwnerStartupBinding(methodName='runTest');f.setUp()
        self.addCleanup(f.doCleanups);f.prepare()
        return f

    def refuse(self,f):
        with f.composition():
            _,error=f.call(lambda:port.request_member(f.aggregate,'runtime.lock.json'))
        return error

    def test_valid_independent_R01_generation_cannot_replace_current_effect_generation(self):
        f=self.fixture()
        f.context['policy_generation']+=1
        f.records[f.context['policy_ref']['path']]['generation']+=1
        f.records[f.context['consent_ref']['path']]['generation']+=1
        f.records[f.context['current_state_ref']['path']]['generation']+=1
        error=self.refuse(f)
        self.assertIsNotNone(error)
        self.assertEqual(error.reason,'aggregate_replay')
        self.assertEqual(f.transports,[])

    def test_valid_independent_R01_fence_cannot_replace_current_host_fence(self):
        f=self.fixture();f.context['host_lease']['fence']+=1
        f.records[f.context['current_state_ref']['path']]['host_fence']+=1
        error=self.refuse(f)
        self.assertIsNotNone(error)
        self.assertEqual(error.reason,'aggregate_replay')
        self.assertEqual(f.transports,[])

    def test_installer_placement_cannot_stand_in_for_the_effect_owner(self):
        f=self.fixture();f.authority.placement='installer'
        error=self.refuse(f)
        self.assertIsNotNone(error)
        self.assertEqual(error.reason,'aggregate_scope_mismatch')
        self.assertEqual(f.transports,[])

    def test_fresh_frame_cannot_extend_audit_when_all_other_leases_remain_valid(self):
        f=self.fixture();f.context['host_lease']['expires_at']+=100
        receipt=f.records[f.clock['clock_receipt_ref']['path']]
        receipt['monotonic_deadlines']['host']+=100_000_000_000
        with f.composition():
            pair,error=f.call(f.api()._owner_originals)
            self.assertIsNone(error,str(error))
            current,_=pair
            f.clock['now_utc']+=31;f.clock['monotonic_ns']+=31_000_000_000
            start=f.records[f.context['clock_ref']['path']]
            start['now_utc']+=31;start['monotonic_ns']+=31_000_000_000
            # Independent positive control: these refreshed originals and every
            # other finite bound still admit a new audit at the advanced clock.
            _,fresh_error=f.call(lambda:f.api()._installed_startup().current())
            self.assertIsNone(fresh_error,str(fresh_error))
            _,error=f.call(current)
        self.assertIsNotNone(error)
        self.assertEqual(error.reason,'member_authority_unavailable')
        self.assertEqual(f.transports,[])
