"""P2b part1 SOURCE DATA: real owned files/history, no native/provider credit.

01 exact APPLIED lineage; 05 unchanged/unshared finite inverse; 06 exact purge
and current cleanup bounds; 07 independently bound child bytes + fsync before
the parent source consumer records advancement. Unknown/reconciliation is part2.
Existing P1 ResourceEntry/RecordRef and C0 opaque observation refs are reused.
"""
import copy
import hashlib
import inspect
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from cortex_core.install import contracts, lifecycle, owner, receipt
from cortex_core.release.codec import canonical_bytes
from cortex_core.release.contracts import thaw
from cortex_core.runtime import signed_member
from non_authoritative_signed_member_double import NonAuthoritativeSignedMemberDouble
import test_lifecycle_receipt as storage
import test_owner_proposal as proposals

FIX = Path(__file__).parent / 'fixtures'
F = json.loads((FIX / 'p2b-subjects.json').read_bytes())
P1 = json.loads((FIX / 'p1-subjects-pgc20-v1.json').read_bytes())


class AppliedSubject:
    """Test-owned original creation and an independently fresh cleanup request."""

    def __init__(self, root, *, operation='uninstall', classes=('runtime',),
                 policies=None, route='manual', target='linux/amd64', before=None,
                 retain_receipt=True, ownerships=None):
        self.root = Path(root)
        self.home = self.root / 'resources'; self.home.mkdir()
        self.history = lifecycle._History(self.root / 'history')
        self.check = storage.LifecycleReceiptOracle()
        self.helper, self.old, _ = self.check.setup_data()
        # Reuse P1's original DATA members for the complete creation-ref check.
        self.old['originals'].update(P1['members'])
        self.files = {}; self.resources = []; self.calls = []; self.dispatched = 0
        self.measured = 0; self.expire_after_consume = False
        self.monotonic_expiry = False; self.delete_kept = False
        q = json.loads(self.old['request_raw'])
        q['roots'][0]['absolute_path'] = str(self.home)
        q['roots'][1]['absolute_path'] = str(self.root / 'history')
        q['invocation']['cwd_root'] = str(self.home)
        if route == 'kos_prerequisite':
            q['caller'].update(route=route, parent_product='kos',
                               parent_transaction_id='source-parent-transaction')
        q['target'].update(host_os='darwin' if target == 'linux/arm64' else 'linux',
                           host_arch='arm64' if target == 'linux/arm64' else 'x86_64',
                           backend_platform=target)
        self.ids = ['owned-' + str(i) for i in range(len(classes))]
        self._request(self.old, q)
        self.helper.context(self.old, lambda c: c['effect'].update(resource_kind='file'))
        self.helper.scope(self.old, lambda s: s.update(resource_ids=self.ids,
            effect=json.loads(self.old['context_raw'])['effect']))
        first, _ = self.helper.run_gate(owner, self.old)
        self.creation_context = thaw(first.context)
        self.reserved = lifecycle._reserve(self.history, self.old['request_raw'].encode(), first,
            self._bytes(self.old), self.check.preimages(),
            clock_raw=self.old['current_clock_raw'].encode())
        current, clock = self.check.current(self.helper, self.old, self.history.current_revision())
        consumed = lifecycle._consume(self.history, self.reserved, current, clock,
                                      originals=self.check.current_originals)
        for i, data_class in enumerate(classes):
            rid = self.ids[i]; p = self.home / rid; self.files[rid] = p
            if before is None:
                prior = {'kind': 'absent', 'locator_ref': self.history.retain(canonical_bytes({'resource': rid})),
                    'parent_witness_ref': self.history.retain(canonical_bytes({'parent': str(self.home)})),
                    'observed_at': '2023-11-14T22:13:20Z'}
            else:
                p.write_bytes(before); prior = self.witness(p)
            p.write_bytes(('APPLIED-' + rid).encode())
            consumers = self.history.retain(canonical_bytes({'resource_id': rid, 'consumers': []}))
            self.resources.append({'resource_id': rid, 'product': 'cortex',
                'installation_id': q['identity']['installation_id'], 'kind': 'file',
                'role': 'source-owned', 'ownership': (ownerships or ['created_by_this_transaction'] * len(classes))[i],
                'created_by': None if ownerships and ownerships[i] == 'external_read_only' else self.creation_context['request_ref'],
                'intent_event_ref': None if ownerships and ownerships[i] == 'external_read_only' else self.reserved,
                'result_event_ref': None, 'before_witness': prior, 'after_witness': self.witness(p, content=data_class != 'credential_custody'),
                'source_installer_ref': q['release']['installer_ref'], 'source_receipt_ref': None,
                'consumers_ref': consumers, 'data_class': data_class,
                'removal_policy': (policies or ['remove_if_unchanged_unshared'] * len(classes))[i]})
        measurement = canonical_bytes({'identity': self.creation_context['identity'],
            'effect': self.creation_context['effect'], 'request_ref': self.creation_context['request_ref'],
            'state': 'applied_verified',
            'observed': {'resource_witnesses': {r['resource_id']: r['after_witness'] for r in self.resources},
                'resource_metadata': {r['resource_id']: {k: v for k, v in r.items()
                    if k not in ('after_witness', 'result_event_ref')} for r in self.resources}}})
        self.created = lifecycle._record_result(self.history, consumed, measurement)
        for r in self.resources:
            if r['ownership'] != 'external_read_only': r['result_event_ref'] = self.created
        child = copy.deepcopy(P1['receipt'])
        for key in ('identity', 'caller', 'target', 'release', 'profile', 'roots'):
            child[key] = q[key]
        child.update(request_ref=self.creation_context['request_ref'], journal_ref=self.created,
                     phase='enrollment_required', reason_code='enrollment_required',
                     disposition='created_by_this_transaction', resources=self.resources)
        for key in ('entrypoint_ref', 'argv', 'cwd_root', 'environment_policy_ref'):
            child['invocation_result'][key] = q['invocation'][key]
        self.child = child; self.child_raw = canonical_bytes(child)
        receipt.verify_product_receipt(self.child_raw, self.old['request_raw'].encode(),
                                       request_ref=self.creation_context['request_ref'])
        self.child_ref = lifecycle._ref('original-child-receipt.json', self.child_raw)
        if retain_receipt: self.history.retain(self.child_raw)
        self.data = copy.deepcopy(self.old)
        self.current_resources = copy.deepcopy(self.resources)
        self.operation = operation
        self.cleanup = {'expires_at': F['cleanup_expires_at'],
            'expires_monotonic_ns': F['cleanup_expires_monotonic_ns'], 'purge_selection': None}
        self._fresh_authority(self.data)
        q = json.loads(self.data['request_raw'])
        old_id = copy.deepcopy(q['identity'])
        q['identity'].update(request_id='cleanup-request', attempt_id='cleanup-attempt',
                             parent_attempt_id=old_id['attempt_id'])
        if operation in ('uninstall', 'purge'):
            q['identity'].update(operation_id=F['removal_operation_id'], transaction_id='cleanup-transaction')
        q.update(operation=operation, journal_ref=self.created, previous_receipt_ref=self.child_ref)
        if operation == 'purge':
            confirmation = self.fact(self.data, 'purge-confirmation.json', {'product': 'cortex', 'roots': [str(p) for p in self.files.values()]})
            selection = {'product': 'cortex', 'exact_data_roots': [str(p) for p in self.files.values()],
                         'confirmation_refs': [confirmation]}
            q['purge_selection'] = selection; self.cleanup['purge_selection'] = copy.deepcopy(selection)
        self._request(self.data, q)
        self.refresh()
        self.base_revision = self.history.current_revision()

    @staticmethod
    def _bytes(data): return {k: v.encode() for k, v in data['originals'].items()}

    def fact(self, data, name, value):
        raw = canonical_bytes(value)
        data['originals'][name] = raw.decode()
        return lifecycle._ref(name, raw)

    def witness(self, p, *, content=True):
        st = p.stat()
        return {'kind': 'file', 'canonical_path': str(p), 'backend_id': None,
            'filesystem_identity': {'device': st.st_dev, 'inode': st.st_ino},
            'uid': st.st_uid, 'mode': stat.S_IMODE(st.st_mode),
            'content_ref': self.history.retain(p.read_bytes()) if content else None, 'executable_ref': None,
            'link_target': None, 'backend_witness_ref': None}

    def _request(self, data, q):
        raw = canonical_bytes(q); ref = json.loads(data['context_raw'])['request_ref']
        data['request_raw'] = raw.decode(); data['originals'][ref['path']] = raw.decode()
        self.helper.context(data, lambda c: c.update(identity=q['identity'], target=q['target'],
            request_ref={**ref, 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}))
        self.helper.scope(data, lambda s: s.update(identity=q['identity'], target=q['target'], operation=q['operation']))

    def _fresh_authority(self, data):
        self.helper.context(data, lambda c: c.update(owner_epoch='cleanup-epoch',
            session_id='cleanup-session', nonce='cleanup-nonce'))
        self.helper.edit_record(data, 'peer_ref', lambda r: r.update(owner_epoch='cleanup-epoch', session_id='cleanup-session'))
        self.helper.clock_receipt(data, lambda r: r.update(owner_epoch='cleanup-epoch'))
        self.helper.edit_record(data, 'clock_ref', lambda r: r.update(owner_epoch='cleanup-epoch'))
        self.helper.clock(data, lambda r: r.update(owner_epoch='cleanup-epoch'))
        self.helper.context(data, lambda c: c['host_lease'].update(fence=c['host_lease']['fence'] + 1))
        self.helper.edit_record(data, 'current_state_ref', lambda r: r.update(host_fence=r['host_fence'] + 1))

    def refresh(self):
        before = self.fact(self.data, 'current-resources.json', {'resources': self.current_resources})
        cleanup = self.fact(self.data, 'current-cleanup.json', self.cleanup)
        self.helper.context(self.data, lambda c: c['effect'].update(
            effect_id='finite-' + self.operation, expected_before_ref=before))
        self.helper.scope(self.data, lambda s: s.update(effect=json.loads(self.data['context_raw'])['effect'],
            cleanup_restrictions_ref=cleanup, resource_ids=[r['resource_id'] for r in self.current_resources]))
        q = json.loads(self.data['request_raw'])
        q['authority_ref'] = json.loads(self.data['context_raw'])['consent_ref']
        self._request(self.data, q)
        # _request updates the scope's byte identity only when it changes.
        q = json.loads(self.data['request_raw']); q['authority_ref'] = json.loads(self.data['context_raw'])['consent_ref']
        raw = canonical_bytes(q); ref = json.loads(self.data['context_raw'])['request_ref']
        self.data['request_raw'] = raw.decode(); self.data['originals'][ref['path']] = raw.decode()
        self.helper.context(self.data, lambda c: c.update(request_ref={**ref, 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}))

    def change_request(self, change):
        q = json.loads(self.data['request_raw']); change(q); self._request(self.data, q); self.refresh()

    def inputs(self, *, parent=False):
        data = copy.deepcopy(self.parent_data if parent else self.data)
        revision = self.history.current_revision()
        self.helper.context(data, lambda c: c.update(state_revision=revision))
        self.helper.edit_record(data, 'current_state_ref', lambda s: s.update(revision=revision))
        if not parent and self.expire_after_consume and revision >= self.base_revision + 2:
            self.helper.clock(data, lambda c: c.update(**(
                {'monotonic_ns': F['cleanup_expires_monotonic_ns']} if self.monotonic_expiry
                else {'now_utc': F['cleanup_expires_at']})))
        if parent and getattr(self, 'expired_parent', False):
            self.helper.clock(data, lambda c: c.update(now_utc=1700000005))
        return self.helper.inputs(owner, data)

    def run(self):
        subject = self
        class Recorder:
            def snapshot(self): return subject.check.preimages()
            def dispatch(self, effect):
                subject.dispatched += 1; subject.calls.append('dispatch')
                for rid, target in effect['cleanup']['targets'].items():
                    if target is None: subject.files[rid].unlink()
                    else: subject.files[rid].write_bytes(subject.history.read(thaw(target['content_ref'])))
                if subject.delete_kept:
                    for item in effect['cleanup']['retained_items']:
                        subject.files[item['resource_id']].unlink()
            def measure(self, effect):
                subject.measured += 1
                resources = copy.deepcopy(subject.current_resources)
                for r in resources:
                    p = subject.files[r['resource_id']]
                    r['after_witness'] = subject.witness(p, content=r['data_class'] != 'credential_custody') if p.exists() else None
                c = thaw(effect['context'])
                return canonical_bytes({'identity': c['identity'], 'effect': c['effect'],
                    'request_ref': c['request_ref'], 'state': 'applied_verified', 'observed': {'resources': resources}})
        bindings = lifecycle._Bindings(self.history, {'file': Recorder()})
        double = NonAuthoritativeSignedMemberDouble(self.data['runtime_lock_raw'].encode(),
            aggregate_id=self.data['aggregate_id'], member_name='runtime.lock.json')
        with patch.object(owner, '_installed_owner_inputs', self.inputs), double.installed(signed_member), \
                patch.object(lifecycle, '_installed_lifecycle', return_value=bindings):
            return lifecycle.execute_effect(self.data['request_raw'].encode())

    def parent(self):
        self.parent_data = copy.deepcopy(self.old)
        self._fresh_authority(self.parent_data)
        ref = self.fact(self.parent_data, 'independent-child-original.json', self.child)
        self.helper.context(self.parent_data, lambda c: c['effect'].update(expected_before_ref=ref))
        self.helper.scope(self.parent_data, lambda s: s.update(effect=json.loads(self.parent_data['context_raw'])['effect']))
        api = getattr(receipt, '_persist_child_receipt', None)
        if api is None: raise AssertionError('P2B07_SURFACE_ABSENT: fixed child persistence')
        double = NonAuthoritativeSignedMemberDouble(self.parent_data['runtime_lock_raw'].encode(),
            aggregate_id=self.parent_data['aggregate_id'], member_name='runtime.lock.json')
        with patch.object(owner, '_installed_owner_inputs', lambda: self.inputs(parent=True)), \
                patch.object(lifecycle, '_installed_lifecycle', return_value=lifecycle._Bindings(self.history, {})), \
                double.installed(signed_member):
            return api(self.old['request_raw'].encode(), self.child_raw)

    def add_applied_resource(self):
        """A later finite install phase; the product receipt retains both creators."""
        data = copy.deepcopy(self.old); q = json.loads(data['request_raw'])
        q['identity'].update(request_id='second-creation-request', attempt_id='second-creation-attempt',
                             parent_attempt_id=q['identity']['attempt_id'])
        self._request(data, q)
        self.helper.context(data, lambda c: c.update(nonce='second-creation-nonce',
                                                     state_revision=self.history.current_revision()))
        self.helper.context(data, lambda c: c['effect'].update(effect_id='second-creation-effect'))
        self.helper.scope(data, lambda scope: scope.update(resource_ids=['owned-extra'],
            effect=json.loads(data['context_raw'])['effect']))
        self.helper.edit_record(data, 'current_state_ref', lambda state: state.update(revision=self.history.current_revision()))
        proposal, _ = self.helper.run_gate(owner, data)
        with patch.object(self.check, 'setup_data', return_value=(self.helper, data, proposal)):
            data, proposal, reserved, consumed = self.check.consume(lifecycle, self.history)
        context = thaw(proposal.context); rid = 'owned-extra'; p = self.home / rid
        p.write_bytes(b'APPLIED-extra'); self.files[rid] = p
        resource = copy.deepcopy(self.resources[0])
        resource.update(resource_id=rid, created_by=context['request_ref'], intent_event_ref=reserved,
            result_event_ref=None, after_witness=self.witness(p),
            before_witness={'kind': 'absent', 'locator_ref': self.history.retain(canonical_bytes({'resource': rid})),
                'parent_witness_ref': self.history.retain(canonical_bytes({'parent': str(self.home)})),
                'observed_at': '2023-11-14T22:13:20Z'},
            consumers_ref=self.history.retain(canonical_bytes({'resource_id': rid, 'consumers': []})))
        raw = canonical_bytes({'identity': context['identity'], 'effect': context['effect'],
            'request_ref': context['request_ref'], 'state': 'applied_verified', 'observed': {
                'resource_witnesses': {rid: resource['after_witness']},
                'resource_metadata': {rid: {k: v for k, v in resource.items()
                    if k not in ('after_witness', 'result_event_ref')}}}})
        completed = lifecycle._record_result(self.history, consumed, raw)
        resource['result_event_ref'] = completed; self.resources.append(resource)
        child = copy.deepcopy(self.child)
        child.update(identity=q['identity'], request_ref=context['request_ref'], journal_ref=completed,
                     resources=self.resources)
        self.old = data; self.creation_context = context; self.created = completed; self.reserved = reserved
        self.child = child; self.child_raw = canonical_bytes(child)
        self.child_ref = self.history.retain(self.child_raw)
        self.current_resources = copy.deepcopy(self.resources); self.ids.append(rid)
        self.change_request(lambda request: request.update(journal_ref=completed, previous_receipt_ref=self.child_ref,
            identity={**request['identity'], 'parent_attempt_id': q['identity']['attempt_id']}))
        self.base_revision = self.history.current_revision()


class RecoveryUninstallOracle(unittest.TestCase):
    def run_ok(self, subject):
        try: return subject.run()
        except owner.OwnerRefusal as error:
            self.fail('P2B_APPLIED_OPERATION_NOT_IMPLEMENTED: ' + error.detail)

    def refuse(self, subject, detail):
        before = {rid: p.read_bytes() for rid, p in subject.files.items()}
        with self.assertRaises(owner.OwnerRefusal) as caught: subject.run()
        self.assertEqual(caught.exception.detail, detail)
        self.assertEqual(subject.dispatched, 0)
        self.assertEqual({rid: p.read_bytes() for rid, p in subject.files.items()}, before)

    def test_P2B01_exact_applied_lineage_is_recorded_in_original_history(self):
        with tempfile.TemporaryDirectory() as root:
            s = AppliedSubject(root); result = self.run_ok(s)
            event, obs = lifecycle._load_entry(s.history, result)
            attempt = json.loads(s.history.read(obs['attempt_ref']))
            self.assertEqual(event['origin_request_ref'], s.creation_context['request_ref'])
            self.assertEqual(attempt['origin_request_ref'], s.creation_context['request_ref'])
            self.assertEqual(attempt['predecessor_result_refs'], [lifecycle._load_entry(s.history, s.created)[0]['result_ref']])
            self.assertEqual(event['phase'], 'finished')
            self.assertEqual(s.dispatched, 1); self.assertFalse(next(iter(s.files.values())).exists())

    def test_P2B01_wrong_parent_attempt_is_not_adopted(self):
        with tempfile.TemporaryDirectory() as root:
            s = AppliedSubject(root); s.change_request(lambda q: q['identity'].update(parent_attempt_id='foreign-parent'))
            self.refuse(s, 'scope_denied')

    def test_P2B01_foreign_source_or_roots_refuses(self):
        for field in ('target', 'release', 'roots'):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as root:
                s = AppliedSubject(root)
                def change(q):
                    if field == 'target': q['target'].update(host_os='darwin', host_arch='arm64', backend_platform='linux/arm64')
                    elif field == 'release': q['release']['product_tuple_ref'] = q['release']['product_manifest_ref']
                    else: q['roots'][0]['absolute_path'] += '-foreign'
                s.change_request(change)
                self.refuse(s, 'scope_denied')

    def test_P2B01_receipt_cannot_replace_original_measured_resource(self):
        with tempfile.TemporaryDirectory() as root:
            s = AppliedSubject(root); forged = copy.deepcopy(s.child)
            forged['resources'][0]['after_witness']['filesystem_identity']['inode'] += 1
            ref = s.history.retain(canonical_bytes(forged))
            s.change_request(lambda q: q.update(previous_receipt_ref=ref))
            self.refuse(s, 'resource_changed')

    def test_P2B01_missing_or_corrupt_journal_has_no_empty_fallback(self):
        for missing in (False, True):
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as root:
                s = AppliedSubject(root); p = s.history.root / s.created['path']
                if missing: p.unlink()
                else: p.write_bytes(b'CORRUPT')
                self.refuse(s, 'journal_invalid')

    def test_P2B01_receipt_cannot_promote_data_to_removable_runtime(self):
        with tempfile.TemporaryDirectory() as root:
            s = AppliedSubject(root, classes=('authoritative_data',))
            forged = copy.deepcopy(s.child)
            forged['resources'][0]['data_class'] = 'runtime'
            ref = s.history.retain(canonical_bytes(forged))
            s.current_resources[0]['data_class'] = 'runtime'
            s.change_request(lambda q: q.update(previous_receipt_ref=ref))
            self.refuse(s, 'resource_changed')

    def test_P2B01_product_receipt_joins_each_original_applied_creator(self):
        with tempfile.TemporaryDirectory() as root:
            s = AppliedSubject(root); first = s.created; s.add_applied_resource()
            second = s.created; result = self.run_ok(s)
            event, observation = lifecycle._load_entry(s.history, result)
            attempt = json.loads(s.history.read(observation['attempt_ref']))
            expected = [lifecycle._load_entry(s.history, ref)[0]['result_ref'] for ref in (second, first)]
            self.assertEqual(attempt['predecessor_result_refs'], expected)
            self.assertTrue(all(not path.exists() for path in s.files.values()))
            self.assertEqual(event['phase'], 'finished')

    def test_P2B01_unknown_original_is_held_for_part2(self):
        with tempfile.TemporaryDirectory() as root:
            s = AppliedSubject(root); event, _ = lifecycle._load_entry(s.history, s.created)
            s.change_request(lambda q: q.update(journal_ref=event['previous_event_ref']))
            self.refuse(s, 'attempt_ambiguous')

    def test_P2B01_new_operation_and_explicit_current_consent_are_required(self):
        for mode in ('same-operation', 'no-authority'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as root:
                s = AppliedSubject(root)
                if mode == 'same-operation':
                    s.change_request(lambda q: q['identity'].update(operation_id=s.creation_context['identity']['operation_id']))
                else:
                    q = json.loads(s.data['request_raw']); q['authority_ref'] = None; s._request(s.data, q)
                self.refuse(s, 'scope_denied')

    def test_P2B05_modified_shared_data_private_and_external_resources_are_kept(self):
        with tempfile.TemporaryDirectory() as root:
            s = AppliedSubject(root, classes=('runtime', 'runtime', 'authoritative_data', 'credential_custody', 'shared_support'))
            s.files['owned-0'].write_bytes(b'USER-MODIFIED')
            s.current_resources[0]['after_witness'] = s.witness(s.files['owned-0'])
            ref = s.fact(s.data, 'shared-consumers.json', {'resource_id': 'owned-1', 'consumers': ['other-product']})
            s.current_resources[1]['consumers_ref'] = ref; s.refresh()
            result = self.run_ok(s)
            from cortex_core.install import uninstall
            projection = uninstall._cleanup_projection(s.history, result)
            self.assertEqual(s.dispatched, 0)
            self.assertEqual(len(projection['retained_items']), 5)
            self.assertTrue(all(p.exists() for p in s.files.values()))
            self.assertEqual(s.files['owned-0'].read_bytes(), b'USER-MODIFIED')

    def test_P2B05_external_read_only_is_kept_even_under_purge(self):
        with tempfile.TemporaryDirectory() as root:
            s = AppliedSubject(root, operation='purge', classes=('authoritative_data',),
                policies=['retain_external'], ownerships=['external_read_only'])
            before = s.files['owned-0'].read_bytes(); result = self.run_ok(s)
            from cortex_core.install import uninstall
            projected = uninstall._cleanup_projection(s.history, result)
            self.assertEqual(s.dispatched, 0)
            self.assertEqual(s.files['owned-0'].read_bytes(), before)
            self.assertEqual(projected['retained_items'][0]['reason'], 'external_read_only')

    def test_P2B01_unknown_cleanup_of_any_original_creator_stays_held(self):
        with tempfile.TemporaryDirectory() as root:
            s = AppliedSubject(root); s.expire_after_consume = True
            self.refuse(s, 'reservation_expired')
            s.expire_after_consume = False; s.add_applied_resource()
            # A fresh request/operation/nonce must not borrow a different phase's
            # APPLIED result to skip an earlier creator's unresolved inverse.
            s.helper.context(s.data, lambda c: c.update(nonce='second-cleanup-nonce'))
            s.change_request(lambda q: q.update(identity={**q['identity'],
                'request_id': 'second-cleanup-request', 'attempt_id': 'second-cleanup-attempt',
                'operation_id': '44444444-4444-4444-8444-444444444444',
                'transaction_id': 'second-cleanup-transaction'}))
            self.refuse(s, 'attempt_ambiguous')

    def test_P2B05_only_unchanged_unshared_owned_subset_is_removed(self):
        with tempfile.TemporaryDirectory() as root:
            s = AppliedSubject(root, classes=('runtime', 'state'))
            unlisted = s.home / 'unlisted'; unlisted.write_bytes(b'UNLISTED')
            result = self.run_ok(s)
            self.assertFalse(s.files['owned-0'].exists()); self.assertTrue(s.files['owned-1'].exists())
            self.assertEqual(unlisted.read_bytes(), b'UNLISTED')
            from cortex_core.install import uninstall
            p = uninstall._cleanup_projection(s.history, result)
            self.assertEqual([(a['resource_id'], a['outcome']) for a in p['cleanup']['actions']], [('owned-0', 'removed'), ('owned-1', 'kept')])
            self.assertEqual(p['cleanup']['remaining_owned_ids'], ['owned-1'])

    def test_P2B05_unknown_consumers_or_null_identity_refuses_before_any_delete(self):
        for field in ('consumers', 'inode', 'null'):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as root:
                s = AppliedSubject(root, classes=('runtime', 'runtime'))
                r = s.current_resources[1]
                if field == 'consumers': r['consumers_ref'] = s.fact(s.data, 'unknown-consumers.json', {'resource_id': r['resource_id'], 'consumers': None})
                elif field == 'null': r['after_witness']['filesystem_identity'] = None
                else: r['after_witness']['filesystem_identity']['inode'] += 1
                s.refresh(); self.refuse(s, 'resource_changed')

    def test_P2B05_unlisted_scope_resource_is_not_adopted(self):
        with tempfile.TemporaryDirectory() as root:
            s = AppliedSubject(root); s.current_resources[0]['resource_id'] = 'unlisted'
            s.refresh(); self.refuse(s, 'resource_changed')

    def test_P2B05_rollback_restores_exact_retained_before_bytes(self):
        with tempfile.TemporaryDirectory() as root:
            s = AppliedSubject(root, operation='rollback', policies=['reverse_if_unchanged'], before=b'ORIGINAL-BEFORE')
            result = self.run_ok(s)
            self.assertEqual(s.files['owned-0'].read_bytes(), b'ORIGINAL-BEFORE')
            from cortex_core.install import uninstall
            self.assertEqual(uninstall._cleanup_projection(s.history, result)['cleanup']['actions'][0]['outcome'], 'restored')

    def test_P2B05_replayed_or_intersecting_cleanup_cannot_delete_again(self):
        with tempfile.TemporaryDirectory() as root:
            s = AppliedSubject(root); self.run_ok(s); old = s.dispatched
            with self.assertRaises(owner.OwnerRefusal): s.run()
            self.assertEqual(s.dispatched, old)

    def test_P2B05_independent_measurement_cannot_claim_a_deleted_kept_resource(self):
        with tempfile.TemporaryDirectory() as root:
            s = AppliedSubject(root, classes=('runtime', 'state')); s.delete_kept = True
            try: s.run()
            except owner.OwnerRefusal as error: self.assertEqual(error.detail, 'resource_changed')
            else: self.fail('cleanup published success after a kept resource disappeared')
            _, obs = lifecycle._load_entry(s.history, s.history.head()[0])
            self.assertEqual(json.loads(s.history.read(obs['attempt_ref']))['dispatch_state'], 'consumed_result_unknown')

    def test_P2B06_exact_purge_grant_removes_selected_data_only(self):
        with tempfile.TemporaryDirectory() as root:
            s = AppliedSubject(root, operation='purge', classes=('authoritative_data',))
            result = self.run_ok(s); self.assertFalse(s.files['owned-0'].exists())
            from cortex_core.install import uninstall
            self.assertEqual(uninstall._cleanup_projection(s.history, result)['cleanup']['state'], 'complete')

    def test_P2B06_missing_wrong_or_unconfirmed_purge_scope_refuses(self):
        for mode in ('absent', 'wrong-root', 'no-confirmation'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as root:
                s = AppliedSubject(root, operation='purge', classes=('authoritative_data',))
                if mode == 'absent': s.cleanup['purge_selection'] = None
                elif mode == 'wrong-root': s.cleanup['purge_selection']['exact_data_roots'] = ['/foreign']
                else: s.cleanup['purge_selection']['confirmation_refs'] = []
                s.refresh(); self.refuse(s, 'scope_denied')

    def test_P2B06_cleanup_utc_and_monotonic_equal_expiry_refuse(self):
        for field, value in [('expires_at', 1700000001), ('expires_monotonic_ns', 101000000000)]:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as root:
                s = AppliedSubject(root); s.cleanup[field] = value; s.refresh()
                self.refuse(s, 'reservation_expired')

    def test_P2B06_cleanup_expiry_during_consume_persistence_prevents_dispatch(self):
        for mono in (False, True):
            with self.subTest(monotonic=mono), tempfile.TemporaryDirectory() as root:
                s = AppliedSubject(root); s.expire_after_consume = True; s.monotonic_expiry = mono
                self.refuse(s, 'reservation_expired')
                _, obs = lifecycle._load_entry(s.history, s.history.head()[0])
                self.assertEqual(json.loads(s.history.read(obs['attempt_ref']))['dispatch_state'], 'consumed_result_unknown')

    def test_P2B06_clock_is_reobserved_after_original_lineage_audit(self):
        from cortex_core.install import uninstall
        with tempfile.TemporaryDirectory() as root:
            s = AppliedSubject(root); actual = uninstall._plan
            def audited(history, request, context, original):
                result = actual(history, request, context, original)
                _, observation = lifecycle._load_entry(history, history.head()[0])
                attempt = json.loads(history.read(observation['attempt_ref']))
                if attempt['dispatch_state'] == 'consumed_result_unknown':
                    s.expire_after_consume = True
                return result
            with patch.object(uninstall, '_plan', audited):
                self.refuse(s, 'reservation_expired')

    def test_P2B07_changed_child_body_cannot_select_its_own_expected_hash(self):
        with tempfile.TemporaryDirectory() as root:
            s = AppliedSubject(root, route='kos_prerequisite', retain_receipt=False)
            changed = copy.deepcopy(s.child); changed['cleanup']['state'] = 'hold'
            with self.assertRaises(contracts.InstallRefusal):
                receipt.verify_product_receipt(canonical_bytes(changed), s.old['request_raw'].encode(),
                    request_ref=s.creation_context['request_ref'], original_evidence={'expected_child_ref': s.child_ref})

    def test_P2B07_child_is_fsynced_before_parent_source_consumer_advances(self):
        with tempfile.TemporaryDirectory() as root:
            s = AppliedSubject(root, route='kos_prerequisite', retain_receipt=False)
            calls = []; actual = lifecycle.os.fsync
            def synced(fd): calls.append(stat.S_ISREG(os.fstat(fd).st_mode)); return actual(fd)
            with patch.object(lifecycle.os, 'fsync', side_effect=synced): ref = s.parent()
            self.assertEqual(s.history.read(ref), s.child_raw)
            self.assertIn(True, calls); self.assertIn(False, calls)
            parent = Path(root) / 'parent-source-advance'; parent.write_bytes(canonical_bytes(thaw(ref)))
            self.assertEqual(json.loads(parent.read_bytes()), thaw(ref))

    def test_P2B07_fsync_failure_or_final_expiry_prevents_parent_advance(self):
        for failure in ('fsync', 'expiry'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as root:
                s = AppliedSubject(root, route='kos_prerequisite', retain_receipt=False)
                parent = Path(root) / 'parent-source-advance'
                actual = lifecycle._History.retain
                def retained(history, raw):
                    ref = actual(history, raw)
                    if raw == s.child_raw: s.expired_parent = True
                    return ref
                context = patch.object(lifecycle.os, 'fsync', side_effect=OSError('TEST_DISK')) if failure == 'fsync' else patch.object(lifecycle._History, 'retain', retained)
                with context:
                    with self.assertRaises(owner.OwnerRefusal):
                        ref = s.parent(); parent.write_bytes(canonical_bytes(thaw(ref)))
                self.assertFalse(parent.exists())

    def test_P2B07_manual_wrong_parent_or_foreign_parser_does_not_advance(self):
        for mode in ('manual', 'parent', 'parser'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as root:
                s = AppliedSubject(root, route='manual' if mode == 'manual' else 'kos_prerequisite', retain_receipt=False)
                if mode == 'parent':
                    altered = copy.deepcopy(s.child); altered['caller']['parent_transaction_id'] = 'other-parent'
                    s.child_raw = canonical_bytes(altered)
                if mode == 'parser':
                    def foreign(*args, **kwargs): return contracts.parse_install_message(*args, **kwargs)
                    with patch.object(receipt, 'parse_install_message', foreign):
                        with self.assertRaises(contracts.InstallRefusal): s.parent()
                else:
                    with self.assertRaises(contracts.InstallRefusal): s.parent()

    def test_P2B07_default_owner_gap_precedes_child_or_request_access(self):
        api = getattr(receipt, '_persist_child_receipt', None)
        self.assertIsNotNone(api, 'P2B07_SURFACE_ABSENT')
        class Trap:
            def __getattribute__(self, name): raise AssertionError('candidate accessed before owner gap')
        with self.assertRaises(owner.OwnerRefusal) as caught: api(Trap(), Trap())
        self.assertEqual(caught.exception.detail, 'required_binding_gap')
        self.assertEqual(list(inspect.signature(api).parameters), ['original_request', 'original_child'])


class JoinedSubject:
    """Real Cox recovery measurement; subsequent effects are source recorders.

    Explicit P1 truth setup is NOT a producer for a real crash. Catalog rows and
    connection handles remain nonauthoritative; the marker is an owned file.
    """
    def __init__(self, root, *, private=False):
        from test_b2_recovery_join import RecoveryFixture
        from cortex_core.runtime import role_grants
        from cortex_core.release.contracts import freeze
        class AbsentProbe(RecoveryFixture):
            def original(inner, root):
                rows = json.loads(inner.before['role'])
                inner.probe = next(r for r in rows['roles'] if r[1].endswith('_probe'))
                rows['roles'].remove(inner.probe)
                inner.before['role'] = canonical_bytes(rows)
                return super().original(root)
        self.oracle = AbsentProbe(); self.root = Path(root); self.private = private
        original_setup = storage.LifecycleReceiptOracle.setup_data
        def prepared(check):
            helper,data,proposal = original_setup(check)
            if private:
                c=json.loads(data['context_raw'])
                value={'kind':'runtime','phase':'runtime','generation':c['policy_generation'],
                    'holder':'private-owner','fence':52,'expires_at':1700000003,
                    'installation_id':c['identity']['installation_id'],'operation_id':c['identity']['operation_id'],
                    'session_user_oid':16384,'session_user_name':'private-owner','map_revision':4}
                raw=canonical_bytes(value);data['originals']['joined-private.json']=raw.decode()
                helper.context(data,lambda c:c.update(private_binding={'kind':'private',
                    'binding_ref':lifecycle._ref('joined-private.json',raw)}))
                helper.clock_receipt(data,lambda r:r['monotonic_deadlines'].update(private=103000000000))
                proposal,_=helper.run_gate(owner,data)
            return helper,data,proposal
        with patch.object(storage.LifecycleReceiptOracle,'setup_data',prepared):
            _, self.helper, self.history, self.old, self.current, self.effect, self.adapter = self.oracle.fixture(root)
        self.role_grants = role_grants; self.freeze = freeze
        # Initialize the existing exclusion file during fixture setup. HOLD
        # checks freeze all history bytes after the owner root is prepared.
        with self.history.operation(): pass
        self.old['originals'].update(P1['members']); self.current['originals'].update(P1['members'])
        for raw in P1['members'].values(): self.history.retain(raw.encode())
        self.marker = self.root / 'finite-effects'
        self.dispatches = 0; self.snapshots = 0; self.recovery_reads = 0
        self.expire_on_measure = False; self.lose_after_publication = False
        self.resources = []; self.measured_raw = None; self.mode = 'recover'
        self.data = self.authority()

    def authority(self, prefix='recover'):
        data = self.oracle.authority(self.helper, self.current, self.history, self.effect)
        self.oracle.rewrite(self.helper, data, lambda q: q['identity'].update(
            request_id=prefix+'-request', attempt_id=prefix+'-attempt'))
        self.helper.context(data, lambda c: c.update(nonce=prefix+'-nonce'))
        if self.private: self.private_fact(data,lambda value:value.update(fence=value['fence']+1))
        return data

    def private_fact(self, data, change):
        ref=json.loads(data['context_raw'])['private_binding']['binding_ref']
        value=json.loads(data['originals'][ref['path']]);change(value);raw=canonical_bytes(value)
        data['originals'][ref['path']]=raw.decode()
        self.helper.context(data,lambda c:c['private_binding'].update(binding_ref=lifecycle._ref(ref['path'],raw)))

    def tree(self):
        return {p.relative_to(self.history.root).as_posix(): p.read_bytes()
                for p in self.history.root.rglob('*') if p.is_file()}

    def context(self): return json.loads(self.data['context_raw'])

    def inputs(self):
        data = copy.deepcopy(self.data); revision = self.history.current_revision()
        self.helper.context(data, lambda c: c.update(state_revision=revision))
        self.helper.edit_record(data, 'current_state_ref', lambda s: s.update(revision=revision))
        if getattr(self, 'expired', False):
            self.helper.clock(data, lambda c: c.update(now_utc=1700000005))
        if (getattr(self,'expire_cleanup',False) and self.mode=='uninstall'
                and revision >= self.cleanup_base_revision+2):
            self.helper.clock(data,lambda c:c.update(now_utc=F['cleanup_expires_at']))
        return self.helper.inputs(owner, data)

    def witness(self):
        rows = json.loads(self.adapter.live['role'])['roles']
        row = next((r for r in rows if r[1] == self.oracle.probe[1]), None)
        if row is None: return None
        return {'kind': 'database_role', 'canonical_path': None,
            'backend_id': str(row[0])+':'+row[1], 'filesystem_identity': None,
            'uid': None, 'mode': None, 'content_ref': None, 'executable_ref': None,
            'link_target': None, 'backend_witness_ref': self.history.retain(canonical_bytes(row))}

    def created_resource(self, context, reserved):
        return {'resource_id': 'owned-probe', 'product': 'cortex',
            'installation_id': context['identity']['installation_id'], 'kind': 'database_role',
            'role': 'source-owned', 'ownership': 'created_by_this_transaction',
            'created_by': context['request_ref'], 'intent_event_ref': reserved, 'result_event_ref': None,
            'before_witness': {'kind': 'absent',
                'locator_ref': self.history.retain(canonical_bytes({'role': self.oracle.probe[1]})),
                'parent_witness_ref': self.history.retain(canonical_bytes({'database': 55})),
                'observed_at': '2023-11-14T22:13:20Z'},
            'after_witness': self.witness(),
            'source_installer_ref': json.loads(self.old['request_raw'])['release']['installer_ref'],
            'source_receipt_ref': None,
            'consumers_ref': self.history.retain(canonical_bytes({'resource_id': 'owned-probe', 'consumers': []})),
            'data_class': 'runtime', 'removal_policy': 'remove_if_unchanged_unshared'}

    def create_role(self):
        rows = json.loads(self.adapter.live['role'])
        if not any(r[1] == self.oracle.probe[1] for r in rows['roles']):
            rows['roles'].append(copy.deepcopy(self.oracle.probe))
        self.adapter.live['role'] = canonical_bytes(rows)
        self.adapter.created[self.oracle.probe[1]] = self.oracle.probe[0]

    def truth(self, state='applied_verified', *, reconciliation=True):
        if state == 'applied_verified':
            self.create_role()
            event, _ = lifecycle._load_entry(self.history, thaw(self.effect['journal_ref']))
            self.resources = [self.created_resource(thaw(self.effect['context']), event['previous_event_ref'])]
        def extra(result):
            if self.resources:
                result['observed']['resource_witnesses'] = {r['resource_id']: r['after_witness'] for r in self.resources}
                result['observed']['resource_metadata'] = {r['resource_id']: {k: v for k, v in r.items()
                    if k not in ('after_witness', 'result_event_ref')} for r in self.resources}
        raw = self.oracle.truth(self.adapter, self.effect, state=state,
                               reconciliation=reconciliation, edit=extra)
        self.data = self.authority()
        return raw

    def child(self, parent, prefix='child'):
        self.mode = 'install'; self.data = self.authority(prefix)
        self.oracle.rewrite(self.helper, self.data, lambda q: q.update(operation='install', journal_ref=thaw(parent)))

    def cleanup(self, completed, *, shared=False):
        event, observation = lifecycle._load_entry(self.history, thaw(completed))
        raw_request = self.history.read(event['request_ref']); q = json.loads(raw_request)
        context = json.loads(self.history.read(observation['context_ref']))
        if not self.resources:
            raise AssertionError('fixture lacks original resource proof')
        self.resources[0]['result_event_ref'] = thaw(completed)
        child = copy.deepcopy(P1['receipt'])
        for key in ('identity','caller','target','release','profile','roots'): child[key] = q[key]
        child.update(request_ref=event['request_ref'], journal_ref=thaw(completed), resources=self.resources,
            phase='enrollment_required', reason_code='enrollment_required', disposition='created_by_this_transaction')
        for key in ('entrypoint_ref','argv','cwd_root','environment_policy_ref'):
            child['invocation_result'][key] = q['invocation'][key]
        ref = self.history.retain(canonical_bytes(child))
        self.mode = 'uninstall'; self.data = self.authority('cleanup')
        self.oracle.rewrite(self.helper, self.data, lambda request: request.update(
            operation='uninstall', journal_ref=thaw(completed), previous_receipt_ref=ref,
            identity={**q['identity'], 'operation_id': F['removal_operation_id'],
                'transaction_id':'joined-cleanup', 'request_id':'joined-cleanup-request',
                'attempt_id':'joined-cleanup-attempt','parent_attempt_id':q['identity']['attempt_id']}))
        self.current_resources = copy.deepcopy(self.resources)
        self.cleanup_base_revision = self.history.current_revision()
        if shared:
            self.current_resources[0]['consumers_ref'] = self.history.retain(canonical_bytes(
                {'resource_id':'owned-probe','consumers':['another-product']}))
        for path in self.history.root.joinpath('facts').iterdir():
            self.data['originals'][path.name] = path.read_text()
        # Only the current resources/restriction facts need provider-original names.
        def fact(name, value):
            raw=canonical_bytes(value); self.data['originals'][name]=raw.decode()
            return lifecycle._ref(name,raw)
        before = fact('joined-current-resources.json', {'resources': self.current_resources})
        restrictions = fact('joined-cleanup-bounds.json', {'expires_at':F['cleanup_expires_at'],
            'expires_monotonic_ns':F['cleanup_expires_monotonic_ns'],'purge_selection':None})
        self.helper.context(self.data, lambda c: c['effect'].update(effect_id='joined-uninstall', expected_before_ref=before))
        self.helper.scope(self.data, lambda s: s.update(resource_ids=['owned-probe'],
            effect=self.context()['effect'], cleanup_restrictions_ref=restrictions))
        self.oracle.rewrite(self.helper, self.data, lambda request: request.update(authority_ref=self.context()['consent_ref']))
        # Rewriting changes only request/identity, leaving the scope byte identity stable.

    def run(self):
        subject = self
        class Slot:
            def snapshot(inner):
                subject.snapshots += 1
                return dict(subject.adapter.live)
            def dispatch(inner, effect):
                subject.dispatches += 1
                _, obs = lifecycle._load_entry(subject.history, effect['journal_ref'])
                attempt = json.loads(subject.history.read(obs['attempt_ref']))
                if attempt['dispatch_state'] != 'consumed_result_unknown':
                    raise AssertionError('child effect before durable consumption')
                if subject.mode == 'install': subject.create_role()
                else:
                    rows = json.loads(subject.adapter.live['role'])
                    if 'owned-probe' in effect['cleanup']['targets']:
                        rows['roles'] = [r for r in rows['roles'] if r[1] != subject.oracle.probe[1]]
                    subject.adapter.live['role'] = canonical_bytes(rows)
                with subject.marker.open('ab') as out: out.write((subject.mode+'\n').encode())
            def measure(inner, effect):
                if subject.mode == 'recover':
                    subject.recovery_reads += 1
                    result = subject.role_grants._Primitive(subject.adapter,'prepare',subject.adapter.connection,{}).measure(effect)
                    if subject.expire_on_measure: subject.expired = True
                    if getattr(subject,'tamper_answer',False) and result[1] is not None:
                        changed=json.loads(result[1]);changed['observed']['acceptance_note']='unlinked replacement'
                        result=(result[0],canonical_bytes(changed))
                    return result
                context = thaw(effect['context'])
                _, observation = lifecycle._load_entry(subject.history, effect['journal_ref'])
                event, _ = lifecycle._load_entry(subject.history, effect['journal_ref'])
                if subject.mode == 'install':
                    subject.resources = [subject.created_resource(context,event['previous_event_ref'])]
                    observed = {'adapter':'finite_pg_roles',
                        'originals':{k:subject.history.retain(v) for k,v in subject.adapter.live.items()},
                        'resource_witnesses':{r['resource_id']:r['after_witness'] for r in subject.resources},
                        'resource_metadata':{r['resource_id']:{k:v for k,v in r.items()
                            if k not in ('after_witness','result_event_ref')} for r in subject.resources}}
                else:
                    measured = copy.deepcopy(subject.current_resources)
                    measured[0]['after_witness'] = subject.witness()
                    observed = {'resources': measured}
                raw = canonical_bytes({'identity':context['identity'],'effect':context['effect'],
                    'request_ref':context['request_ref'],'state':'applied_verified','observed':observed})
                subject.measured_raw = raw
                return raw
        double = NonAuthoritativeSignedMemberDouble(self.data['runtime_lock_raw'].encode(),
            aggregate_id=self.data['aggregate_id'],member_name='runtime.lock.json')
        with patch.object(owner,'_installed_owner_inputs',self.inputs), double.installed(signed_member), \
                patch.object(lifecycle,'_installed_lifecycle',return_value=lifecycle._Bindings(self.history,{'database_role':Slot()})):
            return lifecycle.execute_effect(self.data['request_raw'].encode())


class ContinuationOverlapOracle(unittest.TestCase):
    def continuation(self, subject):
        s=subject; data=copy.deepcopy(s.old); q=json.loads(data['request_raw'])
        q['identity'].update(request_id='continuation-request',attempt_id='continuation-attempt',
                             parent_attempt_id=q['identity']['attempt_id'])
        s._request(data,q)
        s.helper.context(data,lambda c:c.update(nonce='continuation-nonce',state_revision=s.history.current_revision()))
        s.helper.context(data,lambda c:c['effect'].update(effect_id='continuation-effect'))
        s.helper.scope(data,lambda scope:scope.update(resource_ids=s.ids,effect=json.loads(data['context_raw'])['effect']))
        s.helper.edit_record(data,'current_state_ref',lambda state:state.update(revision=s.history.current_revision()))
        proposal,_=s.helper.run_gate(owner,data)
        with patch.object(s.check,'setup_data',return_value=(s.helper,data,proposal)):
            data,proposal,reserved,consumed=s.check.consume(lifecycle,s.history)
        c=thaw(proposal.context)
        raw=canonical_bytes({'identity':c['identity'],'effect':c['effect'],'request_ref':c['request_ref'],
            'state':'applied_verified','observed':{
                'resource_witnesses':{r['resource_id']:r['after_witness'] for r in s.resources},
                'resource_metadata':{r['resource_id']:{k:v for k,v in r.items()
                    if k not in ('after_witness','result_event_ref')} for r in s.resources}}})
        completed=lifecycle._record_result(s.history,consumed,raw)
        child=copy.deepcopy(s.child)
        child.update(identity=q['identity'],request_ref=c['request_ref'],journal_ref=completed,
                     disposition='verified_owned_continuation')
        s.old=data;s.creation_context=c;s.created=completed;s.reserved=reserved
        s.child=child;s.child_raw=canonical_bytes(child);s.child_ref=s.history.retain(s.child_raw)
        receipt.verify_product_receipt(s.child_raw,s.old['request_raw'].encode(),request_ref=c['request_ref'])
        s.change_request(lambda request:request.update(journal_ref=completed,previous_receipt_ref=s.child_ref,
            identity={**request['identity'],'parent_attempt_id':q['identity']['attempt_id']}))
        s.base_revision=s.history.current_revision()

    def unresolved(self, s):
        s.expire_after_consume=True
        with self.assertRaises(owner.OwnerRefusal) as caught:s.run()
        self.assertEqual(caught.exception.detail,'reservation_expired');self.assertEqual(s.dispatched,0)
        ref=s.history.head()[0];_,obs=lifecycle._load_entry(s.history,ref)
        attempt=json.loads(s.history.read(obs['attempt_ref']))
        self.assertEqual(attempt['dispatch_state'],'consumed_result_unknown')
        s.expire_after_consume=False
        return ref,attempt

    def fresh_inverse(self, s, prefix):
        s.helper.context(s.data,lambda c:c.update(nonce=prefix+'-nonce'))
        s.change_request(lambda q:q.update(identity={**q['identity'],
            'request_id':prefix+'-request','attempt_id':prefix+'-attempt',
            'operation_id':'66666666-6666-4666-8666-666666666666','transaction_id':prefix+'-transaction'}))

    def held(self,s,prior,attempt):
        before={k:p.read_bytes() for k,p in s.files.items()}
        with self.assertRaises(owner.OwnerRefusal) as caught:s.run()
        self.assertEqual(caught.exception.detail,'attempt_ambiguous')
        self.assertEqual(s.dispatched,0)
        self.assertEqual({k:p.read_bytes() for k,p in s.files.items()},before)
        _,obs=lifecycle._load_entry(s.history,prior)
        self.assertEqual(json.loads(s.history.read(obs['attempt_ref'])),attempt)

    def test_P2B01_owned_continuation_retains_original_overlap_anchor(self):
        with tempfile.TemporaryDirectory() as root:
            s=AppliedSubject(root);self.continuation(s)
            self.assertNotEqual(s.creation_context['request_ref'],s.resources[0]['created_by'])
            prior,attempt=self.unresolved(s);self.fresh_inverse(s,'continuation-cleanup')
            self.held(s,prior,attempt)

    def test_P2B01_older_receipt_cannot_backtrack_past_overlapping_unknown(self):
        with tempfile.TemporaryDirectory() as root:
            s=AppliedSubject(root)
            initial=copy.deepcopy((s.created,s.child_ref,s.creation_context['identity']['attempt_id'],s.current_resources))
            s.add_applied_resource();prior,attempt=self.unresolved(s)
            s.current_resources=initial[3];self.fresh_inverse(s,'backtrack-cleanup')
            s.change_request(lambda q:q.update(journal_ref=initial[0],previous_receipt_ref=initial[1],
                identity={**q['identity'],'parent_attempt_id':initial[2]}))
            self.held(s,prior,attempt)

    def test_P2B01_owned_continuation_without_prior_inverse_remains_admitted(self):
        with tempfile.TemporaryDirectory() as root:
            s=AppliedSubject(root);self.continuation(s)
            try:result=s.run()
            except owner.OwnerRefusal as error:self.fail('healthy continuation refused:'+error.detail)
            self.assertEqual(lifecycle._load_entry(s.history,result)[0]['phase'],'finished')
            self.assertEqual(s.dispatched,1);self.assertFalse(s.files['owned-0'].exists())


class RecoveryJoinOracle(unittest.TestCase):
    def run_ok(self, subject):
        try: return subject.run()
        except owner.OwnerRefusal as error: self.fail('P2B_JOIN_NOT_IMPLEMENTED:'+error.detail)

    def hold(self, subject, *, detail='attempt_ambiguous'):
        before=subject.tree(); calls=subject.dispatches
        with patch.object(subject.history,'retain',side_effect=AssertionError('HOLD_WROTE_FACT')), \
                patch.object(subject.history,'append',side_effect=AssertionError('HOLD_WROTE_EVENT')):
            with self.assertRaises(owner.OwnerRefusal) as caught: subject.run()
        self.assertEqual(caught.exception.detail,detail)
        self.assertEqual(subject.tree(),before); self.assertEqual(subject.dispatches,calls)

    def test_P2B02_durable_applied_reply_loss_returns_original_without_publication(self):
        with tempfile.TemporaryDirectory() as root:
            s=JoinedSubject(root); raw=s.truth(reconciliation=False); before=s.tree()
            completed=self.run_ok(s); event,_=lifecycle._load_entry(s.history,completed)
            self.assertEqual(s.history.read(event['result_ref']),raw)
            self.assertEqual(s.tree(),before); self.assertEqual(s.dispatches,0); self.assertEqual(s.snapshots,0)
            self.assertEqual(s.recovery_reads,1)

    def test_P2B02_reconciled_applied_is_published_once_with_exact_bytes(self):
        with tempfile.TemporaryDirectory() as root:
            s=JoinedSubject(root); raw=s.truth(); count=len(s.history.events())
            first=self.run_ok(s); again=self.run_ok(s)
            self.assertEqual(first,again); self.assertEqual(len(s.history.events()),count+1)
            event,obs=lifecycle._load_entry(s.history,first)
            self.assertEqual(s.history.read(event['result_ref']),raw)
            self.assertEqual(json.loads(s.history.read(obs['attempt_ref']))['reconciliation_ref'],event['result_ref'])
            self.assertEqual(s.dispatches,0); self.assertEqual(s.snapshots,0)

    def test_P2B02_expiry_after_owner_answer_prevents_publication(self):
        with tempfile.TemporaryDirectory() as root:
            s=JoinedSubject(root); s.truth(); s.expire_on_measure=True
            self.hold(s,detail='reservation_expired')

    def test_P2B02_truth_publication_advances_the_current_revision_floor(self):
        with tempfile.TemporaryDirectory() as root:
            s=JoinedSubject(root);s.truth();before=s.history.current_revision()
            try:result=s.run()
            except owner.OwnerRefusal:result=None
            self.assertEqual(s.history.current_revision(),before+1,'recovery reused an old revision floor')
            self.assertIsNotNone(result,'checked durable truth did not reach the source consumer')

    def test_P2B03_child_uses_its_own_deadline_after_original_expiry(self):
        with tempfile.TemporaryDirectory() as root:
            s=JoinedSubject(root);s.truth('not_applied_verified');parent=self.run_ok(s);s.child(parent)
            h,d=s.helper,s.data;delta=100
            h.context(d,lambda c:c['host_lease'].update(expires_at=c['host_lease']['expires_at']+delta))
            for key in ('peer_ref','consent_ref'):
                h.edit_record(d,key,lambda r:r.update(not_before=r['not_before']+delta,expires_at=r['expires_at']+delta))
            h.edit_record(d,'policy_ref',lambda r:r['validity'].update(
                not_before=r['validity']['not_before']+delta,expires_at=r['validity']['expires_at']+delta))
            h.clock_receipt(d,lambda r:r.update(monotonic_deadlines={k:v+delta*1000000000 for k,v in r['monotonic_deadlines'].items()}))
            h.edit_record(d,'clock_ref',lambda r:r.update(now_utc=r['now_utc']+delta,monotonic_ns=r['monotonic_ns']+delta*1000000000))
            h.clock(d,lambda r:r.update(now_utc=r['now_utc']+delta,monotonic_ns=r['monotonic_ns']+delta*1000000000))
            child=self.run_ok(s);_,obs=lifecycle._load_entry(s.history,child)
            attempt=json.loads(s.history.read(obs['attempt_ref']))
            reservation=json.loads(s.history.read(attempt['reservation_ref']))
            self.assertEqual(reservation['state'],'consumed')
            self.assertGreater(reservation['expires_at'],1700000005)
            self.assertEqual(s.marker.read_bytes(),b'install\n')

    def test_P2B02_other_attempt_history_survives_original_result_publication(self):
        with tempfile.TemporaryDirectory() as root:
            s=JoinedSubject(root);raw=s.truth();data=copy.deepcopy(s.old)
            s.oracle.rewrite(s.helper,data,lambda q:q['identity'].update(
                operation_id='55555555-5555-4555-8555-555555555555',
                transaction_id='other-transaction',request_id='other-request',attempt_id='other-attempt'))
            revision=s.history.current_revision()
            s.helper.context(data,lambda c:c.update(nonce='other-nonce',state_revision=revision))
            s.helper.edit_record(data,'current_state_ref',lambda c:c.update(revision=revision))
            proposal,_=s.helper.run_gate(owner,data)
            other=lifecycle._reserve(s.history,data['request_raw'].encode(),proposal,
                AppliedSubject._bytes(data),s.oracle.before,clock_raw=data['current_clock_raw'].encode())
            before=s.tree();result=self.run_ok(s)
            for p,b in before.items():self.assertEqual((s.history.root/p).read_bytes(),b)
            event,_=lifecycle._load_entry(s.history,result)
            self.assertEqual(event['previous_event_ref'],other)
            self.assertEqual(s.history.read(event['result_ref']),raw)
            self.assertEqual(s.dispatches,0)

    def test_P2B03_definitive_not_applied_then_fresh_child_consumes_once(self):
        with tempfile.TemporaryDirectory() as root:
            s=JoinedSubject(root); raw=s.truth('not_applied_verified'); parent=self.run_ok(s)
            preserved=s.tree(); s.child(parent); child=self.run_ok(s)
            for p,b in preserved.items(): self.assertEqual((s.history.root/p).read_bytes(),b)
            event,obs=lifecycle._load_entry(s.history,child); attempt=json.loads(s.history.read(obs['attempt_ref']))
            self.assertEqual(attempt['origin_request_ref'],thaw(s.effect['context']['request_ref']))
            self.assertEqual(attempt['predecessor_result_refs'],[lifecycle._load_entry(s.history,parent)[0]['result_ref']])
            self.assertEqual(attempt['dispatch_state'],'applied_verified'); self.assertEqual(s.dispatches,1)
            self.assertEqual(s.marker.read_bytes(),b'install\n')
            self.assertEqual(s.history.read(attempt['predecessor_result_refs'][0]),raw)
            with self.assertRaises(owner.OwnerRefusal): s.run()
            self.assertEqual(s.dispatches,1)

    def test_P2B03_retry_preserves_all_first_preimages(self):
        with tempfile.TemporaryDirectory() as root:
            s=JoinedSubject(root); s.truth('not_applied_verified'); parent=self.run_ok(s)
            original=thaw(s.effect['preimages_ref']); s.child(parent); child=self.run_ok(s)
            _,obs=lifecycle._load_entry(s.history,child)
            self.assertEqual(obs['preimages_ref'],original)

    def test_P2B03_reused_ids_and_nonce_cannot_replace_old_attempt(self):
        for field in ('request_id','attempt_id','nonce'):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as root:
                s=JoinedSubject(root); s.truth('not_applied_verified'); parent=self.run_ok(s); s.child(parent)
                if field=='nonce': s.helper.context(s.data,lambda c:c.update(nonce=s.effect['context']['nonce']))
                else:s.oracle.rewrite(s.helper,s.data,lambda q:q['identity'].update({field:s.effect['context']['identity'][field]}))
                before=s.tree()
                with self.assertRaises(owner.OwnerRefusal):s.run()
                self.assertEqual(s.tree(),before);self.assertEqual(s.dispatches,0)

    def test_P2B03_stale_authority_does_not_renew_the_parent(self):
        for field in ('owner_epoch','session_id','fence'):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as root:
                s=JoinedSubject(root); s.truth('not_applied_verified'); parent=self.run_ok(s); s.child(parent)
                if field=='fence':
                    fence=s.effect['context']['host_lease']['fence']
                    s.helper.context(s.data,lambda c:c['host_lease'].update(fence=fence))
                    s.helper.edit_record(s.data,'current_state_ref',lambda r:r.update(host_fence=fence))
                elif field=='session_id':
                    value=s.effect['context'][field];s.helper.context(s.data,lambda c:c.update(session_id=value))
                    s.helper.edit_record(s.data,'peer_ref',lambda r:r.update(session_id=value))
                else:
                    value=s.effect['context'][field];s.helper.context(s.data,lambda c:c.update(owner_epoch=value))
                    s.helper.edit_record(s.data,'peer_ref',lambda r:r.update(owner_epoch=value))
                    s.helper.clock_receipt(s.data,lambda r:r.update(owner_epoch=value))
                    s.helper.edit_record(s.data,'clock_ref',lambda r:r.update(owner_epoch=value))
                    s.helper.clock(s.data,lambda r:r.update(owner_epoch=value))
                before=s.tree()
                with self.assertRaises(owner.OwnerRefusal):s.run()
                self.assertEqual(s.tree(),before);self.assertEqual(s.dispatches,0)

    def test_P2B03_wrong_parent_or_effect_cannot_start_child(self):
        for field in ('parent','effect'):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as root:
                s=JoinedSubject(root);s.truth('not_applied_verified');parent=self.run_ok(s);s.child(parent)
                if field=='parent':s.oracle.rewrite(s.helper,s.data,lambda q:q['identity'].update(parent_attempt_id='foreign'))
                else:
                    s.helper.context(s.data,lambda c:c['effect'].update(effect_id='foreign'))
                    s.helper.scope(s.data,lambda r:r.update(effect=s.context()['effect']))
                before=s.tree()
                with self.assertRaises(owner.OwnerRefusal):s.run()
                self.assertEqual(s.tree(),before);self.assertEqual(s.dispatches,0)

    def test_P2B04_common_crash_points_without_linked_truth_stay_hold(self):
        for point in ('before-dispatch','after-dispatch','orphan-result'):
            with self.subTest(point=point), tempfile.TemporaryDirectory() as root:
                s=JoinedSubject(root)
                if point=='after-dispatch':s.create_role()
                elif point=='orphan-result':
                    c=thaw(s.effect['context']);s.history.retain(canonical_bytes({'identity':c['identity'],
                        'effect':c['effect'],'request_ref':c['request_ref'],'state':'applied_verified',
                        'observed':{'adapter':'finite_pg_roles','originals':{k:s.history.retain(v) for k,v in s.adapter.live.items()}}}))
                self.hold(s)

    def test_P2B04_null_or_changed_live_identity_stays_hold(self):
        for drift in ('null','oid','hba'):
            with self.subTest(drift=drift), tempfile.TemporaryDirectory() as root:
                s=JoinedSubject(root);s.truth()
                if drift=='hba':s.adapter.live['hba']+=b'changed'
                else:
                    rows=json.loads(s.adapter.live['role']);rows['roles'][0][0]=None if drift=='null' else 900001
                    s.adapter.live['role']=canonical_bytes(rows)
                self.hold(s)

    def test_P2B04_absence_is_not_definitive_not_applied(self):
        with tempfile.TemporaryDirectory() as root:
            s=JoinedSubject(root)
            self.assertIsNone(s.witness());self.hold(s)

    def test_P2B05_reconciled_applied_creation_can_be_removed_from_exact_proof(self):
        with tempfile.TemporaryDirectory() as root:
            s=JoinedSubject(root);s.truth();completed=self.run_ok(s);s.cleanup(completed)
            result=self.run_ok(s)
            from cortex_core.install import uninstall
            self.assertIsNone(s.witness());self.assertEqual(s.dispatches,1)
            self.assertEqual(uninstall._cleanup_projection(s.history,result)['cleanup']['actions'][0]['outcome'],'removed')

    def test_P2B05_retry_child_creation_keeps_root_lineage_for_removal(self):
        with tempfile.TemporaryDirectory() as root:
            s=JoinedSubject(root);s.truth('not_applied_verified');parent=self.run_ok(s);s.child(parent)
            child=self.run_ok(s);s.cleanup(child);result=self.run_ok(s)
            event,_=lifecycle._load_entry(s.history,result)
            self.assertEqual(event['origin_request_ref'],thaw(s.effect['context']['request_ref']))
            self.assertIsNone(s.witness());self.assertEqual(s.marker.read_bytes(),b'install\nuninstall\n')

    def test_P2B05_reconciled_shared_creation_is_kept(self):
        with tempfile.TemporaryDirectory() as root:
            s=JoinedSubject(root);s.truth();completed=self.run_ok(s);s.cleanup(completed,shared=True)
            result=self.run_ok(s)
            from cortex_core.install import uninstall
            self.assertIsNotNone(s.witness());self.assertEqual(s.dispatches,0)
            self.assertEqual(uninstall._cleanup_projection(s.history,result)['retained_items'][0]['reason'],'shared_consumer')

    def test_P2B02_lost_reply_after_publication_reuses_the_exact_result(self):
        with tempfile.TemporaryDirectory() as root:
            s=JoinedSubject(root);raw=s.truth();actual=s.history.append
            def published(record,*,expected_ref):
                ref=actual(record,expected_ref=expected_ref)
                if record['result_ref'] is not None:s.expired=True
                return ref
            with patch.object(s.history,'append',published):
                with self.assertRaises(owner.OwnerRefusal) as caught:s.run()
            self.assertEqual(caught.exception.detail,'reservation_expired')
            completed=s.history.head()[0];event,_=lifecycle._load_entry(s.history,completed)
            self.assertEqual(s.history.read(event['result_ref']),raw)
            before=s.tree();s.expired=False
            self.assertEqual(self.run_ok(s),completed);self.assertEqual(s.tree(),before)
            self.assertEqual(s.dispatches,0)

    def test_P2B02_unlinked_answer_bytes_refuse_before_publication(self):
        with tempfile.TemporaryDirectory() as root:
            s=JoinedSubject(root);s.truth();s.tamper_answer=True;before=s.tree()
            with self.assertRaises(owner.OwnerRefusal) as caught:s.run()
            self.assertEqual(caught.exception.detail,'journal_invalid')
            self.assertEqual(s.tree(),before);self.assertEqual(s.dispatches,0)

    def test_P2B03_applied_parent_cannot_be_retried_from_resource_absence(self):
        with tempfile.TemporaryDirectory() as root:
            s=JoinedSubject(root);s.truth();parent=self.run_ok(s)
            s.adapter.live=dict(s.oracle.before);s.child(parent);before=s.tree()
            with self.assertRaises(owner.OwnerRefusal):s.run()
            self.assertEqual(s.tree(),before);self.assertEqual(s.dispatches,0)

    def test_P2B03_terminal_not_applied_is_checked_once_before_child(self):
        with tempfile.TemporaryDirectory() as root:
            s=JoinedSubject(root);raw=s.truth('not_applied_verified',reconciliation=False)
            count=len(s.history.events());parent=self.run_ok(s)
            self.assertEqual(self.run_ok(s),parent);self.assertEqual(len(s.history.events()),count+1)
            _,obs=lifecycle._load_entry(s.history,parent);attempt=json.loads(s.history.read(obs['attempt_ref']))
            self.assertEqual(attempt['result_ref'],attempt['reconciliation_ref'])
            self.assertEqual(s.history.read(attempt['result_ref']),raw)
            s.child(parent);self.run_ok(s);self.assertEqual(s.dispatches,1)

    def test_P2B03_unmeasured_terminal_truth_does_not_start_a_child(self):
        with tempfile.TemporaryDirectory() as root:
            s=JoinedSubject(root);s.truth('not_applied_verified',reconciliation=False)
            s.child(s.history.head()[0]);before=s.tree()
            with self.assertRaises(owner.OwnerRefusal):s.run()
            self.assertEqual(s.tree(),before);self.assertEqual(s.dispatches,0)

    def test_P2B03_changed_first_originals_refuse_without_replacement(self):
        for field in ('role','acl','database','hba','carrier'):
            with self.subTest(field=field),tempfile.TemporaryDirectory() as root:
                s=JoinedSubject(root);s.truth('not_applied_verified');parent=self.run_ok(s);s.child(parent)
                original=thaw(s.effect['preimages_ref']);before=s.history.read(original);head=s.history.head()[0]
                s.adapter.live[field]+=b' '
                with self.assertRaises(owner.OwnerRefusal) as caught:s.run()
                self.assertEqual(caught.exception.detail,'resource_changed')
                self.assertEqual(s.history.head()[0],head);self.assertEqual(s.history.read(original),before)
                self.assertEqual(s.dispatches,0)

    def test_P2B03_applied_descendant_blocks_old_parent_even_after_resource_absence(self):
        with tempfile.TemporaryDirectory() as root:
            s=JoinedSubject(root);s.truth('not_applied_verified');parent=self.run_ok(s)
            s.child(parent);self.run_ok(s);self.assertEqual(s.dispatches,1)
            s.adapter.live=dict(s.oracle.before);s.child(parent,'another-child');before=s.tree()
            with self.assertRaises(owner.OwnerRefusal):s.run()
            self.assertEqual(s.dispatches,1);self.assertEqual(s.tree(),before)
            self.assertEqual(s.marker.read_bytes(),b'install\n')

    def test_P2B03_ids_from_another_recorded_operation_cannot_be_reused(self):
        for field in ('request_id','attempt_id'):
            with self.subTest(field=field),tempfile.TemporaryDirectory() as root:
                s=JoinedSubject(root);s.truth('not_applied_verified');parent=self.run_ok(s)
                data=copy.deepcopy(s.old)
                s.oracle.rewrite(s.helper,data,lambda q:q['identity'].update(
                    operation_id='55555555-5555-4555-8555-555555555555',
                    transaction_id='other-transaction',request_id='other-request',attempt_id='other-attempt'))
                revision=s.history.current_revision()
                s.helper.context(data,lambda c:c.update(nonce='other-nonce',state_revision=revision))
                s.helper.edit_record(data,'current_state_ref',lambda c:c.update(revision=revision))
                proposal,_=s.helper.run_gate(owner,data)
                lifecycle._reserve(s.history,data['request_raw'].encode(),proposal,
                    AppliedSubject._bytes(data),s.oracle.before,clock_raw=data['current_clock_raw'].encode())
                s.child(parent)
                s.oracle.rewrite(s.helper,s.data,lambda q:q['identity'].update({field:'other-request' if field=='request_id' else 'other-attempt'}))
                before=s.tree()
                with self.assertRaises(owner.OwnerRefusal):s.run()
                self.assertEqual(s.tree(),before);self.assertEqual(s.dispatches,0)

    def test_P2B03_private_child_requires_independent_fence_and_identity(self):
        for drift in ('fence','holder','session_user_oid'):
            with self.subTest(drift=drift),tempfile.TemporaryDirectory() as root:
                s=JoinedSubject(root,private=True);s.truth('not_applied_verified');parent=self.run_ok(s);s.child(parent)
                s.private_fact(s.data,lambda value:value.update({drift:
                    52 if drift=='fence' else 'foreign' if drift=='holder' else 16385}))
                before=s.tree()
                with self.assertRaises(owner.OwnerRefusal):s.run()
                self.assertEqual(s.tree(),before);self.assertEqual(s.dispatches,0)

    def test_P2B03_renewed_private_child_still_consumes_normally(self):
        with tempfile.TemporaryDirectory() as root:
            s=JoinedSubject(root,private=True);s.truth('not_applied_verified');parent=self.run_ok(s)
            s.child(parent);result=self.run_ok(s);_,obs=lifecycle._load_entry(s.history,result)
            self.assertEqual(json.loads(s.history.read(obs['attempt_ref']))['dispatch_state'],'applied_verified')
            self.assertEqual(s.marker.read_bytes(),b'install\n')

    def test_P2B05_unknown_inverse_of_retry_child_keeps_the_root_anchor(self):
        with tempfile.TemporaryDirectory() as root:
            s=JoinedSubject(root);s.truth('not_applied_verified');parent=self.run_ok(s)
            s.child(parent);child=self.run_ok(s);s.cleanup(child);s.expire_cleanup=True
            with self.assertRaises(owner.OwnerRefusal) as caught:s.run()
            self.assertEqual(caught.exception.detail,'reservation_expired')
            prior=s.history.head()[0];event,obs=lifecycle._load_entry(s.history,prior)
            attempt=json.loads(s.history.read(obs['attempt_ref']))
            self.assertEqual(event['origin_request_ref'],thaw(s.effect['context']['request_ref']))
            self.assertEqual(attempt['dispatch_state'],'consumed_result_unknown')
            s.expire_cleanup=False;s.helper.context(s.data,lambda c:c.update(nonce='another-inverse-nonce'))
            s.oracle.rewrite(s.helper,s.data,lambda q:q['identity'].update(request_id='another-inverse-request',
                attempt_id='another-inverse-attempt',operation_id='77777777-7777-4777-8777-777777777777',
                transaction_id='another-inverse-transaction'))
            s.oracle.rewrite(s.helper,s.data,lambda q:q.update(authority_ref=s.context()['consent_ref']))
            head=s.history.head()[0]
            with self.assertRaises(owner.OwnerRefusal) as caught:s.run()
            self.assertEqual(caught.exception.detail,'attempt_ambiguous')
            self.assertEqual(s.history.head()[0],head);self.assertIsNotNone(s.witness())
            self.assertEqual(s.dispatches,1);self.assertEqual(s.marker.read_bytes(),b'install\n')
            _,obs=lifecycle._load_entry(s.history,prior)
            self.assertEqual(json.loads(s.history.read(obs['attempt_ref'])),attempt)
