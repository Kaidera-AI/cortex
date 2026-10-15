"""Fixed lifecycle seam source recorder; all storage belongs to the test."""
import copy
from collections.abc import Mapping
import inspect
import hashlib
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

    def run_recorder(self, *, lose_result=False, expire_after_consume=False, not_applied=False,
                     snapshot_failure=False, owner_failure_after_reserve=False, result_owner_failure=False):
        api = self.api()
        check = storage.LifecycleReceiptOracle()
        helper, data, proposal = check.setup_data()
        with tempfile.TemporaryDirectory() as root:
            history = api._History(Path(root)/'history')
            marker = Path(root)/'source-recorder-marker'
            counts = {'dispatches': 0, 'measurements': 0}
            def inputs():
                revision = history.current_revision()
                if owner_failure_after_reserve and revision is not None:
                    raise RuntimeError('TEST_SECRET_OWNER_ERROR')
                if result_owner_failure and counts['measurements']:
                    raise RuntimeError('TEST_SECRET_RESULT_CLOCK_ERROR')
                fresh = copy.deepcopy(data)
                if revision is not None:
                    helper.context(fresh, lambda c: c.update(state_revision=revision))
                    helper.edit_record(fresh, 'current_state_ref', lambda s: s.update(revision=revision))
                if expire_after_consume and revision is not None and revision >= 9:
                    helper.clock(fresh, lambda c: c.update(now_utc=1700000005))
                return helper.inputs(owner, fresh)
            class Recorder:
                def snapshot(self):
                    if snapshot_failure:
                        raise RuntimeError('TEST_SECRET_BACKEND_ERROR')
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
                if result_owner_failure:
                    with self.assertRaises(owner.OwnerRefusal) as caught:
                        api.execute_effect(data['request_raw'].encode())
                    original = check.measurement(data, proposal)
                    ref = {'path':'original-measured-result.json','bytes':len(original),
                           'sha256':hashlib.sha256(original).hexdigest()}
                    try:
                        kept = history.read(ref)
                    except owner.OwnerRefusal as error:
                        self.fail('measured original was lost on later collector failure: '+error.detail)
                    self.assertEqual(kept, original)
                    self.assertEqual(caught.exception.detail, 'owner_unavailable')
                    _, obs = api._load_entry(history, history.head()[0])
                    attempt = json.loads(history.read(obs['attempt_ref']))
                    self.assertEqual(attempt['dispatch_state'], 'consumed_result_unknown')
                    self.assertIsNone(attempt['result_ref'])
                elif snapshot_failure or owner_failure_after_reserve:
                    try:
                        api.execute_effect(data['request_raw'].encode())
                    except owner.OwnerRefusal as caught:
                        self.assertEqual(caught.detail, 'backend_unavailable' if snapshot_failure else 'owner_unavailable')
                        self.assertNotIn('TEST_SECRET', str(caught))
                    except Exception as error:
                        self.fail('fixed composition error escaped its typed boundary: '+type(error).__name__)
                    else:
                        self.fail('unavailable fixed composition was admitted')
                    if snapshot_failure:
                        self.assertIsNone(history.head()[0])
                    else:
                        _, obs = api._load_entry(history, history.head()[0])
                        attempt = json.loads(history.read(obs['attempt_ref']))
                        self.assertEqual(attempt['dispatch_state'], 'not_consumed')
                elif expire_after_consume:
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
                    self.assertIsInstance(result, Mapping, 'return the existing P1 RecordRef, not another result record')
                    self.assertEqual(set(result), {'path','bytes','sha256'})
                    self.assertEqual(result, history.head()[0])
                    with self.assertRaises(TypeError):
                        result['path'] = 'forged'
                    event, _ = api._load_entry(history, result)
                    self.assertEqual(event['phase'], 'held' if not_applied else 'created')
                    result_bytes = history.read(event['result_ref'])
                    if not_applied:
                        self.assertEqual(json.loads(result_bytes)['state'], 'not_applied_verified')
                    else:
                        self.assertEqual(result_bytes, check.measurement(data, proposal))
            stopped = expire_after_consume or snapshot_failure or owner_failure_after_reserve
            self.assertEqual(counts, {'dispatches': 0, 'measurements': 0} if stopped
                             else {'dispatches': 1, 'measurements': 1})
            self.assertEqual(marker.is_file(), not stopped and not not_applied)

    def test_P2A13_dispatch_requires_intent_and_consume_then_independent_result(self):
        self.run_recorder()

    def test_P2A12_unknown_result_retains_consumption_and_never_redispatches(self):
        self.run_recorder(lose_result=True)

    def test_P2A20_expiry_during_consume_persistence_refuses_before_dispatch(self):
        self.run_recorder(expire_after_consume=True)

    def test_P2A21_known_not_applied_result_remains_held(self):
        self.run_recorder(not_applied=True)

    def test_P2A24_snapshot_unavailable_is_typed_before_intent(self):
        self.run_recorder(snapshot_failure=True)

    def test_P2A25_owner_unavailable_retains_reserved_intent_without_dispatch(self):
        self.run_recorder(owner_failure_after_reserve=True)

    def test_P2A26_measured_original_survives_later_owner_clock_unavailability(self):
        self.run_recorder(result_owner_failure=True)
