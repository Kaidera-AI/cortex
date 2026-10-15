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
P1 = json.loads((FIX / 'p1-subjects.json').read_bytes())


class AppliedSubject:
    """Test-owned original creation and an independently fresh cleanup request."""

    def __init__(self, root, *, operation='uninstall', classes=('runtime',),
                 policies=None, route='manual', target='linux/amd64', before=None,
                 retain_receipt=True):
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
                'role': 'source-owned', 'ownership': 'created_by_this_transaction',
                'created_by': self.creation_context['request_ref'], 'intent_event_ref': self.reserved,
                'result_event_ref': None, 'before_witness': prior, 'after_witness': self.witness(p, content=data_class != 'credential_custody'),
                'source_installer_ref': q['release']['installer_ref'], 'source_receipt_ref': None,
                'consumers_ref': consumers, 'data_class': data_class,
                'removal_policy': (policies or ['remove_if_unchanged_unshared'] * len(classes))[i]})
        measurement = canonical_bytes({'identity': self.creation_context['identity'],
            'effect': self.creation_context['effect'], 'request_ref': self.creation_context['request_ref'],
            'state': 'applied_verified',
            'observed': {'resource_witnesses': {r['resource_id']: r['after_witness'] for r in self.resources}}})
        self.created = lifecycle._record_result(self.history, consumed, measurement)
        for r in self.resources: r['result_event_ref'] = self.created
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
