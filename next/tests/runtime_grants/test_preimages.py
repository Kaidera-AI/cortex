"""Frozen PG-B2 source-DATA admission and exact-byte restoration oracle.

These tests exercise real P2a retained history; ByteRecorder only proves fixture
plumbing. Candidate six-port/finite-role SQL remains the original PG-O gate.
"""
import copy
import json
import tempfile
import unittest
from unittest.mock import patch

from cortex_core.install import owner
from cortex_core.runtime import signed_member
from cortex_core.release.codec import canonical_bytes
from cortex_core.release.contracts import thaw
from pgb2_preimages import PGPreimageFixture, ByteRecorder, FIELDS
from non_authoritative_signed_member_double import NonAuthoritativeSignedMemberDouble


class Preimages(unittest.TestCase):
    def test_fixed_dispatcher_admits_the_original_snapshot_and_frozen_context(self):
        with tempfile.TemporaryDirectory() as root:
            f = PGPreimageFixture(self, root, reserve=False)
            snapshots = []
            class Adapter(ByteRecorder):
                def snapshot(adapter):
                    raw = super().snapshot()
                    snapshots.append(raw)
                    return raw
                def dispatch(adapter, context):
                    f.context = context
                    self.assertEqual(set(context), {'context', 'journal_ref', 'preimages_ref'})
                    with self.assertRaises(TypeError): context['journal_ref'] = None
                    f.preimages_ref = f.api._retained_preimages(f.history,
                        thaw(context['context'])['identity'])
                    self.assertEqual(context['preimages_ref'], f.preimages_ref)
                    f.assert_restored(f.retained_originals())
                    # Simulate a changed owned byte store, then exercise the
                    # test adapter's retained-original restoration boundary.
                    adapter.change()
                    super().dispatch(context)
            adapter = Adapter(f, root)
            def inputs():
                revision = f.history.current_revision()
                fresh = copy.deepcopy(f.data)
                if revision is not None:
                    f.owner_helper.context(fresh, lambda c: c.update(state_revision=revision))
                    f.owner_helper.edit_record(fresh, 'current_state_ref', lambda s: s.update(revision=revision))
                return f.owner_helper.inputs(owner, fresh)
            double = NonAuthoritativeSignedMemberDouble(f.data['runtime_lock_raw'].encode(),
                aggregate_id=f.data['aggregate_id'], member_name='runtime.lock.json')
            kind = thaw(f.proposal.context)['effect']['resource_kind']
            with patch.object(owner, '_installed_owner_inputs', inputs), double.installed(signed_member), \
                    patch.object(f.api, '_installed_lifecycle', return_value=f.api._Bindings(f.history, {kind: adapter})):
                completed = f.api.execute_effect(f.data['request_raw'].encode())
            self.assertEqual(adapter.dispatches, 1)
            self.assertGreaterEqual(len(snapshots), 2)  # pre-intent + independent measurement
            for snapshot in snapshots: f.assert_restored(snapshot)
            f.reload()
            self.assertEqual(completed, f.history.head()[0])
            event, _ = f.api._load_entry(f.history, completed)
            self.assertEqual(event['phase'], 'created')
            result = json.loads(f.history.read(event['result_ref']))
            c = thaw(f.context['context'])
            for key in ('identity', 'effect', 'request_ref'): self.assertEqual(result[key], c[key])
            f.assert_restored({k: f.history.read(ref) for k, ref in result['observed']['originals'].items()})

    def test_originals_are_durable_and_match_private_bundle_after_reload(self):
        with tempfile.TemporaryDirectory() as root:
            f = PGPreimageFixture(self, root)
            before = f.preimages_ref
            f.reload().assert_restored(f.retained_originals())
            self.assertEqual(f.api._retained_preimages(f.history, f.identity), before)
            event, obs = f.api._load_entry(f.history, f.reserved)
            self.assertEqual(event['phase'], 'create_intent')
            self.assertEqual(obs['preimages_ref'], before)
            self.assertIsNone(event['result_ref'])

    def test_reference_adapter_restores_all_original_bytes_and_records_measured_result(self):
        with tempfile.TemporaryDirectory() as root:
            f = PGPreimageFixture(self, root)
            adapter = ByteRecorder(f, root)
            f.assert_restored(adapter.snapshot())
            adapter.change()
            self.assertTrue(all(adapter.snapshot()[k] != f.originals[k] for k in FIELDS))
            context = f.consume()
            with self.assertRaises(TypeError):
                context['preimages_ref'] = None
            with self.assertRaises(TypeError):
                context['context']['identity']['operation_id'] = 'foreign'
            f.reload()
            adapter.dispatch(context)
            self.assertEqual(adapter.dispatches, 1)
            f.assert_restored(adapter.snapshot())
            raw = adapter.measure(context)
            completed = f.api._record_result(f.history, f.consumed, raw)
            f.reload()
            event, obs = f.api._load_entry(f.history, completed)
            self.assertEqual(event['phase'], 'created')
            self.assertEqual(f.history.read(event['result_ref']), raw)
            self.assertEqual(obs['preimages_ref'], f.preimages_ref)
            measured = json.loads(raw)['observed']['originals']
            f.assert_restored({k: f.history.read(measured[k]) for k in FIELDS})

    def test_foreign_operation_cannot_select_originals(self):
        with tempfile.TemporaryDirectory() as root:
            f = PGPreimageFixture(self, root).reload()
            foreign = {**f.identity, 'operation_id': '00000000-0000-4000-8000-000000000099'}
            with self.assertRaises(owner.OwnerRefusal) as caught:
                f.retained_originals(foreign)
            self.assertEqual(caught.exception.detail, 'scope_denied')
            f.assert_restored(f.retained_originals())

    def damage(self, field, tamper):
        with tempfile.TemporaryDirectory() as root:
            f = PGPreimageFixture(self, root)
            context = f.consume()
            adapter = ByteRecorder(f, root)
            adapter.change()
            unchanged = adapter.snapshot()
            bundle = json.loads(f.history.read(f.preimages_ref))
            ref = f.preimages_ref if field == 'bundle' else bundle['originals'][field]
            target = f.history.root/'facts'/(ref['sha256']+'.json')
            original = target.read_bytes()
            if tamper:
                changed = bytearray(original); changed[0] ^= 1
                target.write_bytes(changed)
                self.assertEqual(target.stat().st_size, len(original))
            else:
                target.unlink()
            with self.assertRaises(owner.OwnerRefusal) as caught:
                adapter.dispatch(context)
            self.assertEqual(caught.exception.detail, 'journal_invalid')
            self.assertEqual(adapter.dispatches, 0)
            self.assertEqual(adapter.snapshot(), unchanged)
            self.assertEqual(f.history.head()[0], f.consumed)
            target.write_bytes(original); target.chmod(0o600)
            f.reload()
            adapter.dispatch(context)
            self.assertEqual(adapter.dispatches, 1)
            f.assert_restored(adapter.snapshot())

    def test_replayed_consume_after_reload_cannot_dispatch_twice(self):
        with tempfile.TemporaryDirectory() as root:
            f = PGPreimageFixture(self, root)
            adapter = ByteRecorder(f, root)
            context = f.consume()
            adapter.change(); adapter.dispatch(context)
            before = adapter.snapshot()
            f.reload()
            with self.assertRaises(owner.OwnerRefusal) as caught:
                replay = f.api._consume(f.history, f.reserved, f.current, f.clock,
                                       originals=f.helper.current_originals)
                adapter.dispatch(replay)
            self.assertEqual(caught.exception.detail, 'reservation_used')
            self.assertEqual(adapter.dispatches, 1)
            self.assertEqual(adapter.snapshot(), before)
            self.assertEqual(f.history.head()[0], f.consumed)

    def wrong_result(self, field):
        with tempfile.TemporaryDirectory() as root:
            f = PGPreimageFixture(self, root)
            adapter = ByteRecorder(f, root)
            context = f.consume(); adapter.change(); adapter.dispatch(context)
            result = json.loads(adapter.measure(context))
            result[field] = copy.deepcopy(result[field])
            if field == 'identity': result[field]['operation_id'] = '00000000-0000-4000-8000-000000000099'
            elif field == 'effect': result[field]['effect_id'] = 'foreign-effect'
            else: result[field]['sha256'] = '0'*64
            with self.assertRaises(owner.OwnerRefusal) as caught:
                f.api._record_result(f.history, f.consumed, canonical_bytes(result))
            self.assertEqual(caught.exception.detail, 'resource_changed')
            self.assertEqual(f.history.head()[0], f.consumed)
            event, obs = f.api._load_entry(f.history, f.consumed)
            self.assertIsNone(event['result_ref'])
            self.assertEqual(json.loads(f.history.read(obs['attempt_ref']))['dispatch_state'],
                             'consumed_result_unknown')

    def wrong_original(self, field):
        with tempfile.TemporaryDirectory() as root:
            f = PGPreimageFixture(self, root)
            wrong = dict(f.originals)
            wrong[field] += b' '
            with self.assertRaises(AssertionError) as caught:
                f.assert_restored(wrong)
            self.assertIn('PG-B2 original bytes: '+field, str(caught.exception))


def bind_cases():
    # Exact static suffix set is frozen in PGB2_FREEZE.json before handoff.
    for field in ('bundle', *FIELDS):
        for tamper in (False, True):
            def case(self, field=field, tamper=tamper): self.damage(field, tamper)
            setattr(Preimages, 'test_'+('tampered_' if tamper else 'missing_')+field, case)
    for field in FIELDS:
        def case(self, field=field): self.wrong_original(field)
        setattr(Preimages, 'test_exact_restore_rejects_changed_'+field, case)
    for field in ('identity', 'effect', 'request_ref'):
        def case(self, field=field): self.wrong_result(field)
        setattr(Preimages, 'test_measured_result_rejects_foreign_'+field, case)


bind_cases()
