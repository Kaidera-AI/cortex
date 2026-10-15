"""P2a test-owned storage DATA oracle. No signature/native/effect acceptance.

Original frozen F2 tests are separate; their missing original corpus is not
supplied by these DATA subjects or the existing nonauthoritative R01 double.
"""
import copy
import importlib
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from cortex_core.install import owner
from cortex_core.release.codec import canonical_bytes
from cortex_core.release.contracts import freeze, thaw
import test_owner_proposal as proposals

NEXT = Path(__file__).resolve().parents[2]
FIXTURE = json.loads((Path(__file__).parent/'fixtures/p2a-subjects.json').read_bytes())


class LifecycleReceiptOracle(unittest.TestCase):
    def api(self):
        self.assertTrue((NEXT/'src/cortex_core/install/lifecycle.py').is_file(),
                        'P2A_SURFACE_ABSENT: durable single P1 history and preimages')
        return importlib.import_module('cortex_core.install.lifecycle')

    def setup_data(self):
        helper = proposals.OwnerProposalOracle()
        data = helper.data()
        proposal, _ = helper.run_gate(helper.api(), data)
        return helper, data, proposal

    def preimages(self):
        return {k: v.encode() for k, v in FIXTURE['preimages'].items()}

    def reserve(self, api, history):
        helper, data, proposal = self.setup_data()
        entry = api._reserve(history, data['request_raw'].encode(), proposal,
                             {k: v.encode() for k, v in data['originals'].items()}, self.preimages(),
                             clock_raw=data['current_clock_raw'].encode())
        return helper, data, proposal, entry

    def current(self, helper, data, revision):
        fresh = copy.deepcopy(data)
        helper.context(fresh, lambda c: c.update(state_revision=revision))
        helper.edit_record(fresh, 'current_state_ref', lambda s: s.update(revision=revision))
        self.current_originals = {k: v.encode() for k, v in fresh['originals'].items()}
        p, _ = helper.run_gate(helper.api(), fresh)
        return p, fresh['current_clock_raw'].encode()

    def consume(self, api, history):
        helper, data, proposal, reserved = self.reserve(api, history)
        current, clock = self.current(helper, data, proposal.observed_state_revision+1)
        consumed = api._consume(history, reserved, current, clock, originals=self.current_originals)
        return data, proposal, reserved, consumed

    def measurement(self, data, proposal):
        return canonical_bytes({'identity': thaw(proposal.context)['identity'],
            'effect': thaw(proposal.context)['effect'],
            'request_ref': thaw(proposal.context)['request_ref'],
            'state': 'applied_verified', 'observed': {'test_owned_marker': 'measured-after'}})

    def another_attempt(self, helper, data, revision, *, nonce):
        fresh = copy.deepcopy(data)
        request = json.loads(fresh['request_raw'])
        request['identity']['attempt_id'] = 'test-attempt-2'
        raw = proposals.encode(request)
        context = json.loads(fresh['context_raw'])
        ref = context['request_ref']
        fresh['request_raw'] = raw.decode()
        fresh['originals'][ref['path']] = raw.decode()
        import hashlib
        helper.context(fresh, lambda c: c.update(identity=request['identity'], nonce=nonce,
            state_revision=revision, request_ref={**ref, 'bytes': len(raw),
                                                   'sha256': hashlib.sha256(raw).hexdigest()}))
        helper.edit_record(fresh, 'current_state_ref', lambda s: s.update(revision=revision))
        helper.scope(fresh, lambda s: s.update(identity=request['identity']))
        proposal, _ = helper.run_gate(helper.api(), fresh)
        return proposal, fresh

    def test_P2A01_originals_and_preimages_are_retained_before_durable_intent(self):
        api = self.api()
        with tempfile.TemporaryDirectory() as root:
            history = api._History(Path(root))
            _, _, proposal, entry = self.reserve(api, history)
            event, observation = api._load_entry(history, entry)
            self.assertEqual(event['phase'], 'create_intent')
            self.assertIsNone(event['result_ref'])
            reservation = json.loads(history.read(observation['reservation_ref']))
            attempt = json.loads(history.read(observation['attempt_ref']))
            self.assertEqual(reservation['state'], 'reserved')
            self.assertEqual(reservation['expected_state_revision'], proposal.observed_state_revision+1)
            self.assertEqual(reservation['reserved_at'], 1700000001)
            self.assertEqual(attempt['dispatch_state'], 'not_consumed')
            self.assertIsNone(attempt['consumption_sequence'])
            bundle = json.loads(history.read(observation['preimages_ref']))
            self.assertEqual(set(bundle['originals']), set(self.preimages()))
            for name, ref in bundle['originals'].items():
                self.assertEqual(history.read(ref), self.preimages()[name])

    def test_P2A02_journal_compare_and_swap_refuses_stale_previous_head(self):
        api = self.api()
        with tempfile.TemporaryDirectory() as root:
            history = api._History(Path(root))
            *_, entry = self.reserve(api, history)
            event, _ = api._load_entry(history, entry)
            stale = {**event, 'sequence': event['sequence']+1, 'previous_event_ref': entry}
            with self.assertRaises(owner.OwnerRefusal) as caught:
                history.append(stale, expected_ref=None)
            self.assertEqual(caught.exception.detail, 'generation_changed')
            self.assertEqual(history.head()[0], entry)

    def test_P2A03_preimages_reload_exact_original_bytes_after_restart(self):
        api = self.api()
        with tempfile.TemporaryDirectory() as root:
            first = api._History(Path(root))
            _, _, proposal, entry = self.reserve(api, first)
            original = api._retained_preimages(first, thaw(proposal.context)['identity'])
            restarted = api._History(Path(root))
            again = api._retained_preimages(restarted, thaw(proposal.context)['identity'])
            self.assertEqual(again, original)
            bundle = json.loads(restarted.read(again))
            for name, ref in bundle['originals'].items():
                self.assertEqual(restarted.read(ref), self.preimages()[name])

    def test_P2A04_preimages_are_bound_to_the_original_operation(self):
        api = self.api()
        with tempfile.TemporaryDirectory() as root:
            history = api._History(Path(root))
            _, _, proposal, _ = self.reserve(api, history)
            foreign = thaw(proposal.context)['identity']
            foreign['operation_id'] = '00000000-0000-4000-8000-000000000099'
            with self.assertRaises(owner.OwnerRefusal) as caught:
                api._retained_preimages(history, foreign)
            self.assertEqual(caught.exception.detail, 'scope_denied')

    def test_P2A05_same_operation_never_replaces_original_preimages(self):
        api = self.api()
        with tempfile.TemporaryDirectory() as root:
            history = api._History(Path(root))
            _, data, proposal, _ = self.reserve(api, history)
            original = api._retained_preimages(history, thaw(proposal.context)['identity'])
            changed = self.preimages(); changed['role'] = b'REPLACEMENT'
            with self.assertRaises(owner.OwnerRefusal):
                api._reserve(history, data['request_raw'].encode(), proposal,
                             {k: v.encode() for k, v in data['originals'].items()}, changed,
                             clock_raw=data['current_clock_raw'].encode())
            self.assertEqual(api._retained_preimages(history, thaw(proposal.context)['identity']), original)
            self.assertEqual(json.loads(history.read(original))['identity'], thaw(proposal.context)['identity'])

    def test_P2A06_consumption_is_durable_and_one_shot(self):
        api = self.api()
        with tempfile.TemporaryDirectory() as root:
            history = api._History(Path(root))
            helper, data, proposal, reserved = self.reserve(api, history)
            current, clock = self.current(helper, data, proposal.observed_state_revision+1)
            consumed = api._consume(history, reserved, current, clock, originals=self.current_originals)
            restarted = api._History(Path(root))
            with self.assertRaises(owner.OwnerRefusal) as caught:
                api._consume(restarted, reserved, current, clock, originals=self.current_originals)
            self.assertEqual(caught.exception.detail, 'reservation_used')
            self.assertEqual(restarted.head()[0], consumed)
            _, obs = api._load_entry(restarted, consumed)
            attempt = json.loads(restarted.read(obs['attempt_ref']))
            self.assertEqual(attempt['dispatch_state'], 'consumed_result_unknown')
            self.assertIsNotNone(attempt['consumption_sequence'])
            self.assertIsNotNone(attempt['consume_clock_ref'])

    def test_P2A07_consumption_requires_post_reserve_revision(self):
        api = self.api()
        with tempfile.TemporaryDirectory() as root:
            history = api._History(Path(root))
            _, data, proposal, reserved = self.reserve(api, history)
            with self.assertRaises(owner.OwnerRefusal) as caught:
                api._consume(history, reserved, proposal, data['current_clock_raw'].encode(),
                             originals={k: v.encode() for k, v in data['originals'].items()})
            self.assertEqual(caught.exception.detail, 'generation_changed')
            self.assertEqual(history.head()[0], reserved)

    def test_P2A08_consumption_refuses_equal_and_after_both_original_deadlines(self):
        api = self.api()
        for field, values in [('now_utc', (1700000005, 1700000006)),
                              ('monotonic_ns', (105000000000, 106000000000))]:
            for value in values:
                with self.subTest(clock=field, value=value), tempfile.TemporaryDirectory() as root:
                    history = api._History(Path(root))
                    helper, data, proposal, reserved = self.reserve(api, history)
                    current, clock = self.current(helper, data, proposal.observed_state_revision+1)
                    now = json.loads(clock); now[field] = value
                    with self.assertRaises(owner.OwnerRefusal) as caught:
                        api._consume(history, reserved, current, canonical_bytes(now), originals=self.current_originals)
                    self.assertEqual(caught.exception.detail, 'reservation_expired')
                    self.assertEqual(history.head()[0], reserved)

    def test_P2A09_original_files_and_event_are_fsynced_before_return(self):
        api = self.api()
        with tempfile.TemporaryDirectory() as root:
            history = api._History(Path(root))
            calls = []
            actual = os.fsync
            def observed(fd):
                calls.append(stat.S_ISREG(os.fstat(fd).st_mode))
                return actual(fd)
            with patch.object(api.os, 'fsync', side_effect=observed):
                *_, entry = self.reserve(api, history)
            self.assertGreaterEqual(sum(calls), len(self.preimages())+1)
            self.assertIn(False, calls)
            self.assertEqual(history.head()[0], entry)

    def test_P2A09_fsync_failure_never_publishes_a_durable_intent(self):
        api = self.api()
        with tempfile.TemporaryDirectory() as root:
            history = api._History(Path(root))
            with patch.object(api.os, 'fsync', side_effect=OSError('TEST_DISK_FAILURE')):
                with self.assertRaises(owner.OwnerRefusal) as caught:
                    self.reserve(api, history)
            self.assertEqual(caught.exception.detail, 'atomicity_unavailable')
            self.assertIsNone(history.head()[0])

    def test_P2A10_independent_result_cannot_bind_another_effect(self):
        api = self.api()
        with tempfile.TemporaryDirectory() as root:
            history = api._History(Path(root))
            data, proposal, _, consumed = self.consume(api, history)
            result = json.loads(self.measurement(data, proposal))
            result['effect']['effect_id'] = 'foreign-effect'
            with self.assertRaises(owner.OwnerRefusal) as caught:
                api._record_result(history, consumed, canonical_bytes(result))
            self.assertEqual(caught.exception.detail, 'resource_changed')
            self.assertEqual(history.head()[0], consumed)

    def test_P2A11_original_result_is_retained_and_reloadable(self):
        api = self.api()
        with tempfile.TemporaryDirectory() as root:
            history = api._History(Path(root))
            data, proposal, _, consumed = self.consume(api, history)
            raw = self.measurement(data, proposal)
            complete = api._record_result(history, consumed, raw)
            restarted = api._History(Path(root))
            event, obs = api._load_entry(restarted, complete)
            self.assertEqual(restarted.read(event['result_ref']), raw)
            attempt = json.loads(restarted.read(obs['attempt_ref']))
            self.assertEqual(attempt['dispatch_state'], 'applied_verified')
            self.assertEqual(attempt['result_ref'], event['result_ref'])

    def test_P2A15_corrupt_history_refuses_without_empty_fallback(self):
        api = self.api()
        with tempfile.TemporaryDirectory() as root:
            history = api._History(Path(root))
            *_, entry = self.reserve(api, history)
            path = Path(root)/entry['path']
            path.write_bytes(b'BROKEN ORIGINAL')
            with self.assertRaises(owner.OwnerRefusal) as caught:
                api._History(Path(root)).head()
            self.assertEqual(caught.exception.detail, 'journal_invalid')
            self.assertEqual(path.read_bytes(), b'BROKEN ORIGINAL')

    def test_P2A16_symlink_original_is_not_followed(self):
        api = self.api()
        with tempfile.TemporaryDirectory() as root:
            history = api._History(Path(root))
            ref = history.retain(b'ORIGINAL')
            path = Path(root)/ref['path']
            other = Path(root)/'foreign'; other.write_bytes(b'ORIGINAL')
            path.unlink(); path.symlink_to(other)
            with self.assertRaises(owner.OwnerRefusal) as caught:
                history.read(ref)
            self.assertEqual(caught.exception.detail, 'custody_changed')

    def test_P2A17_consume_retains_new_current_owner_originals_before_publication(self):
        api = self.api()
        with tempfile.TemporaryDirectory() as root:
            history = api._History(Path(root))
            _, _, _, consumed = self.consume(api, history)
            _, obs = api._load_entry(history, consumed)
            context = json.loads(history.read(obs['context_ref']))
            try:
                raw = history.read(context['current_state_ref'])
            except owner.OwnerRefusal as error:
                self.fail('published current context lost its original state bytes: '+error.detail)
            self.assertEqual(json.loads(raw)['revision'], context['state_revision'])

    def test_P2A18_another_attempt_cannot_reuse_the_same_owner_nonce(self):
        api = self.api()
        with tempfile.TemporaryDirectory() as root:
            history = api._History(Path(root))
            helper, data, first, reserved = self.reserve(api, history)
            current, fresh = self.another_attempt(helper, data, first.observed_state_revision+1,
                                                  nonce=first.context['nonce'])
            with self.assertRaises(owner.OwnerRefusal) as caught:
                api._reserve(history, fresh['request_raw'].encode(), current,
                    {k: v.encode() for k, v in fresh['originals'].items()}, self.preimages(),
                    clock_raw=fresh['current_clock_raw'].encode())
            self.assertEqual(caught.exception.detail, 'nonce_replay')
            self.assertEqual(history.head()[0], reserved)

    def test_P2A19_new_caller_attempt_cannot_erase_consumed_unknown_effect(self):
        api = self.api()
        with tempfile.TemporaryDirectory() as root:
            history = api._History(Path(root))
            data, first, _, consumed = self.consume(api, history)
            helper = proposals.OwnerProposalOracle()
            current, fresh = self.another_attempt(helper, data, first.observed_state_revision+2,
                                                  nonce='test-new-nonce')
            with self.assertRaises(owner.OwnerRefusal) as caught:
                api._reserve(history, fresh['request_raw'].encode(), current,
                    {k: v.encode() for k, v in fresh['originals'].items()}, self.preimages(),
                    clock_raw=fresh['current_clock_raw'].encode())
            self.assertEqual(caught.exception.detail, 'attempt_ambiguous')
            self.assertEqual(history.head()[0], consumed)

    def test_P2A22_original_storage_retains_the_existing_P1_request_byte_limit(self):
        api = self.api()
        from cortex_core.install.contracts import LIMITS
        original = b'x'*LIMITS['request']
        with tempfile.TemporaryDirectory() as root:
            history = api._History(Path(root))
            try:
                ref = history.retain(original)
            except owner.OwnerRefusal as error:
                self.fail('P2 original storage narrowed the accepted P1 request bound: '+error.detail)
            self.assertEqual(history.read(ref), original)

    def test_P2A23_readonly_request_never_creates_an_effect_intent(self):
        api = self.api()
        helper = proposals.OwnerProposalOracle()
        data = helper.data()
        request = json.loads(data['request_raw']); request['operation'] = 'plan'
        raw = proposals.encode(request)
        context = json.loads(data['context_raw']); ref = context['request_ref']
        import hashlib
        data['request_raw'] = raw.decode(); data['originals'][ref['path']] = raw.decode()
        helper.context(data, lambda c: c.update(request_ref={**ref, 'bytes':len(raw),
                                                            'sha256':hashlib.sha256(raw).hexdigest()}))
        helper.scope(data, lambda s: s.update(operation='plan'))
        helper.edit_record(data, 'policy_ref', lambda p: p.update(operation='plan'))
        with tempfile.TemporaryDirectory() as root:
            history = api._History(Path(root))
            with patch.object(owner, '_installed_owner_inputs', lambda: helper.inputs(owner, data)):
                with patch.object(api, '_installed_lifecycle', side_effect=AssertionError('backend before readonly denial')):
                    with self.assertRaises(owner.OwnerRefusal) as caught:
                        api.execute_effect(raw)
            self.assertEqual(caught.exception.detail, 'scope_denied')
            self.assertIsNone(history.head()[0])
