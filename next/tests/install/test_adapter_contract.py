"""Fixed lifecycle seam source recorder; all storage belongs to the test."""
import copy
import inspect
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cortex_core.install import owner
from cortex_core.runtime import signed_member
import test_lifecycle_receipt as storage
from non_authoritative_signed_member_double import NonAuthoritativeSignedMemberDouble


class LifecycleAdapterOracle(unittest.TestCase):
    def api(self):
        return storage.LifecycleReceiptOracle().api()

    def test_P2A14_default_binding_gap_precedes_candidate_access(self):
        api = self.api()
        class Untouchable:
            def __bytes__(self):
                raise AssertionError('candidate accessed before default owner gap')
        with self.assertRaises(owner.OwnerRefusal) as caught:
            api.execute_effect(Untouchable())
        self.assertEqual(caught.exception.detail, 'required_binding_gap')
        self.assertEqual(list(inspect.signature(api.execute_effect).parameters), ['original_request'])

    def test_P2A14_dispatcher_consumes_the_existing_B1_entry(self):
        api = self.api()
        with patch.object(owner, 'propose_effect', side_effect=owner.OwnerRefusal('required_binding_gap')) as gate:
            with patch.object(api, '_installed_lifecycle', side_effect=AssertionError('backend before B1')):
                with self.assertRaises(owner.OwnerRefusal):
                    api.execute_effect(b'candidate')
        gate.assert_called_once_with(b'candidate')

    def run_recorder(self, *, lose_result=False, expire_after_consume=False, not_applied=False):
        api = self.api()
        check = storage.LifecycleReceiptOracle()
        helper, data, proposal = check.setup_data()
        with tempfile.TemporaryDirectory() as root:
            history = api._History(Path(root)/'history')
            marker = Path(root)/'source-recorder-marker'
            counts = {'dispatches': 0, 'measurements': 0}
            def inputs():
                revision = history.current_revision()
                fresh = copy.deepcopy(data)
                if revision is not None:
                    helper.context(fresh, lambda c: c.update(state_revision=revision))
                    helper.edit_record(fresh, 'current_state_ref', lambda s: s.update(revision=revision))
                if expire_after_consume and revision is not None and revision >= 9:
                    helper.clock(fresh, lambda c: c.update(now_utc=1700000005))
                return helper.inputs(owner, fresh)
            class Recorder:
                def snapshot(self):
                    return check.preimages()
                def dispatch(self, context):
                    counts['dispatches'] += 1
                    _, obs = api._load_entry(history, history.head()[0])
                    attempt = json.loads(history.read(obs['attempt_ref']))
                    check.assertEqual(attempt['dispatch_state'], 'consumed_result_unknown')
                    if not not_applied:
                        marker.write_bytes(b'measured-after')
                def measure(self, context):
                    counts['measurements'] += 1
                    if lose_result:
                        raise OSError('TEST_RESULT_UNAVAILABLE')
                    if not_applied:
                        check.assertFalse(marker.exists())
                        result = json.loads(check.measurement(data, proposal))
                        result.update(state='not_applied_verified', observed={'test_owned_marker':'measured-absent'})
                        return storage.canonical_bytes(result)
                    check.assertEqual(marker.read_bytes(), b'measured-after')
                    return check.measurement(data, proposal)
            bindings = api._Bindings(history, {'directory': Recorder()})
            double = NonAuthoritativeSignedMemberDouble(data['runtime_lock_raw'].encode(),
                aggregate_id=data['aggregate_id'], member_name='runtime.lock.json')
            with patch.object(owner, '_installed_owner_inputs', inputs), double.installed(signed_member), \
                    patch.object(api, '_installed_lifecycle', return_value=bindings):
                if expire_after_consume:
                    with self.assertRaises(owner.OwnerRefusal) as caught:
                        api.execute_effect(data['request_raw'].encode())
                    self.assertEqual(caught.exception.detail, 'reservation_expired')
                    _, obs = api._load_entry(history, history.head()[0])
                    attempt = json.loads(history.read(obs['attempt_ref']))
                    self.assertEqual(attempt['dispatch_state'], 'consumed_result_unknown')
                    self.assertIsNone(attempt['result_ref'])
                elif lose_result:
                    with self.assertRaises(owner.OwnerRefusal) as caught:
                        api.execute_effect(data['request_raw'].encode())
                    self.assertEqual(caught.exception.detail, 'attempt_ambiguous')
                    _, obs = api._load_entry(history, history.head()[0])
                    attempt = json.loads(history.read(obs['attempt_ref']))
                    self.assertEqual(attempt['dispatch_state'], 'consumed_result_unknown')
                    self.assertIsNone(attempt['result_ref'])
                    with self.assertRaises(owner.OwnerRefusal):
                        api.execute_effect(data['request_raw'].encode())
                else:
                    result = api.execute_effect(data['request_raw'].encode())
                    self.assertEqual(result.journal_ref, history.head()[0])
                    self.assertFalse(result.native_credit)
                    self.assertFalse(result.effect_authority)
                    self.assertEqual(result.phase, 'held' if not_applied else 'enrollment_required')
                    event, _ = api._load_entry(history, result.journal_ref)
                    result_bytes = history.read(event['result_ref'])
                    if not_applied:
                        self.assertEqual(json.loads(result_bytes)['state'], 'not_applied_verified')
                    else:
                        self.assertEqual(result_bytes, check.measurement(data, proposal))
            self.assertEqual(counts, {'dispatches': 0, 'measurements': 0} if expire_after_consume
                             else {'dispatches': 1, 'measurements': 1})
            self.assertEqual(marker.is_file(), not expire_after_consume and not not_applied)

    def test_P2A13_dispatch_requires_intent_and_consume_then_independent_result(self):
        self.run_recorder()

    def test_P2A12_unknown_result_retains_consumption_and_never_redispatches(self):
        self.run_recorder(lose_result=True)

    def test_P2A20_expiry_during_consume_persistence_refuses_before_dispatch(self):
        self.run_recorder(expire_after_consume=True)

    def test_P2A21_known_not_applied_result_remains_held(self):
        self.run_recorder(not_applied=True)
