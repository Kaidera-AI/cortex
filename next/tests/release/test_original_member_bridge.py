"""U3-D original-byte source oracle; unrelated RAM roots, no native/effects."""
import copy
from dataclasses import replace
import importlib
import json
from pathlib import Path
from r01u3_support.workspace import workspace_output
import tempfile
import unittest
from unittest.mock import patch

from aggregate_fixture import materials, write, encode, sha256, repin, update
from r01u3_support.factory import test_authority
from cortex_core.runtime import signed_member as port
from cortex_core.release.custody import RootReader

NEXT = Path(__file__).resolve().parents[2]
MISSING = 'R01 unit 3: signed Core/API aggregate and member admission'
MEMBER = 'runtime.lock.json'


MAIN=workspace_output()

class Fixture:
    def api(self):
        self.assertTrue((NEXT/'src/cortex_core/release/member_admission.py').is_file(),
                        'SURFACE_ABSENCE_BODY: U3-D original-member source absent')
        return importlib.import_module('cortex_core.release.member_admission')

    def setUp(self):
        MAIN.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(prefix='bridge-inert-', dir=MAIN.resolve())
        self.addCleanup(self.tmp.cleanup); self.root = Path(self.tmp.name)
        self.value, self.bodies, self.builds, self.policy = materials()
        self.authority = test_authority()
        self.policy['root'] = copy.deepcopy(self.authority.policy['root'])
        self.authority.policy = self.policy
        self.authority.current_state['installed_baseline'] = copy.deepcopy(self.policy['installed_baseline'])
        self.readers = []; self.transports = []; self.contexts = []
        self.current_hook = None; self.transport_hook = None
        self.pin()

    def pin(self):
        repin(self.value, self.policy); write(self.root, self.bodies)
        self.raw = encode(self.value); self.aggregate = 'sha256:'+sha256(self.raw)
        self.authority.raw = self.raw; self.authority.value = copy.deepcopy(self.value)
        self.signature = self.authority.signed()

    def current(self):
        from cortex_core.release.trust import _OwnerContext
        from cortex_core.release.contracts import freeze
        # Factory calls are forbidden from a product stack. This TEST adapter
        # copies owner DATA; it never calls a signer/factory from the backend.
        raw = encode(self.authority.policy)
        context = _OwnerContext(freeze(copy.deepcopy(self.authority.policy)), raw, sha256(raw),
                                self.authority.root_material, self.authority.now,
                                self.authority.generation, self.authority.fence,
                                self.authority.placement, freeze(copy.deepcopy(self.authority.current_state)))
        self.contexts.append(context)
        return self.current_hook(context) if self.current_hook else context

    def transport(self, aggregate):
        self.transports.append(aggregate)
        r = RootReader.for_aggregate(str(self.root), 8*1024**2)
        self.readers.append(r); self.addCleanup(r.close)
        if self.transport_hook: self.transport_hook(r)
        return self.raw, self.signature, r, self.builds

    def backend(self, aggregate=None, name=MEMBER):
        return self.api()._request_original(self.aggregate if aggregate is None else aggregate,
                                           name, self.current, self.transport)

    def sealed(self, name=MEMBER):
        self.api()  # surface absence stays BODY, never sanitized by RC2
        with patch.object(port, '_original_authority', self.backend):
            return port.request_member(self.aggregate, name)

    def refusal(self, reason, fn):
        with self.assertRaises(port.Refusal) as caught: fn()
        self.assertEqual(caught.exception.reason, reason)
        self.assertEqual(str(caught.exception), reason if caught.exception.unit is None else reason+': '+caught.exception.unit)
        return caught.exception

    def assert_closed(self):
        for r in self.readers:
            self.assertIsNone(r.fd)
            self.assertFalse(r._aggregate_files)
            self.assertFalse(r._aggregate_dirs)


class Bridge(Fixture, unittest.TestCase):
    def test_default_backend_and_actual_port_gap_before_any_candidate(self):
        api = self.api()
        class Trap:
            def __eq__(self, other): raise AssertionError('candidate equality')
            def __fspath__(self): raise AssertionError('candidate path')
            def __str__(self): raise AssertionError('candidate text')
        def fail(*a, **kw): self.fail('UNBOUND reached input or effect')
        with patch.object(Path, 'read_bytes', fail), patch('builtins.open', fail), patch('socket.socket', fail), patch('subprocess.Popen', fail):
            for fn in (api.request_original_member, port.request_member):
                error = self.refusal('required_binding_gap', lambda: fn(Trap(), Trap()))
                self.assertEqual(error.unit, MISSING)
        self.assertIs(port._original_authority, port._unbound)

    def test_sole_existing_port_delegates_to_fixed_R01_backend(self):
        api = self.api(); calls = []
        def missing(aggregate, name):
            calls.append((aggregate,name)); raise port.Refusal('member_not_in_aggregate')
        with patch.object(api,'request_original_member',missing):
            self.refusal('member_not_in_aggregate',lambda: port.request_member(self.aggregate,MEMBER))
        self.assertEqual(calls,[(self.aggregate,MEMBER)])

    def test_exact_original_lock_through_actual_port_and_verifier(self):
        import cortex_core.release.trust as trust
        import cortex_core.release.aggregate as aggregate
        primitive = trust.verify_ed25519; outcomes = []; receipts = []
        def crypto(*args):
            result = primitive(*args); outcomes.append(result); return result
        original = aggregate.verify_build_receipt
        def receipt(parsed, *args):
            result = original(parsed, *args); receipts.append(parsed.raw); return result
        with patch.object(trust, 'verify_ed25519', crypto), patch.object(aggregate, 'verify_build_receipt', receipt):
            member = self.sealed()
        self.assertIs(type(member), port.OriginalMember)
        self.assertEqual(port.read_member(member, aggregate_id=self.aggregate, member_name=MEMBER), self.bodies[MEMBER])
        self.assertEqual(member.signing_root_id, 'sha256:'+sha256(self.authority.root_material))
        self.assertEqual(member.member_sha256, sha256(self.bodies[MEMBER]))
        self.assertGreaterEqual(len(outcomes), 4); self.assertTrue(all(outcomes))
        self.assertEqual(receipts, [self.bodies[g['receipt']] for g in self.builds]*2)
        self.assertEqual(self.transports, [self.aggregate]*2)
        self.assert_closed()

    def test_fixed_private_port_composition_reuses_backend(self):
        api = self.api()
        with patch.object(api, '_owner_originals', lambda: (self.current, self.transport)):
            member = port.request_member(self.aggregate, MEMBER)
        self.assertEqual(port.read_member(member, aggregate_id=self.aggregate, member_name=MEMBER), self.bodies[MEMBER])

    def test_small_record_original_is_supported_without_widening_port(self):
        name = self.value['compatibility']['compatibility_member']
        member = self.sealed(name)
        self.assertEqual(port.read_member(member, aggregate_id=self.aggregate, member_name=name), self.bodies[name])

    def test_noncanonical_aggregate_id_refuses_before_current_and_transport(self):
        for aggregate in [sha256(self.raw), 'SHA256:'+sha256(self.raw), 'sha256:'+'A'*64, '', 'sha256:'+'a'*65]:
            self.refusal('member_identity_mismatch', lambda: self.backend(aggregate))
        self.assertEqual(self.contexts, []); self.assertEqual(self.transports, [])

    def test_noncanonical_member_refuses_before_current_and_transport(self):
        for name in ['Runtime.lock.json', './runtime.lock.json', 'members/../runtime.lock.json', '/runtime.lock.json', 'members/a%2fb', 'members/'+'a'*513]:
            self.refusal('member_identity_mismatch', lambda: self.backend(name=name))
        self.assertEqual(self.contexts, []); self.assertEqual(self.transports, [])

    def test_requested_raw_identity_before_crypto_or_physical_open(self):
        import cortex_core.release.trust as trust
        with patch.object(trust, 'verify_ed25519', side_effect=AssertionError('crypto before request identity')), patch.object(RootReader, 'aggregate_begin', side_effect=AssertionError('open before request identity')):
            self.refusal('aggregate_identity_mismatch', lambda: self.backend('sha256:'+'f'*64))
        self.assert_closed()

    def test_unsigned_refuses_before_member_open(self):
        self.signature = None
        with patch.object(RootReader, 'aggregate_begin', side_effect=AssertionError('unsigned physical read')):
            self.refusal('aggregate_unsigned', self.backend)
        self.assert_closed()

    def test_actual_bad_signature_refuses_before_member_open(self):
        import base64
        lines = self.signature.split(b'\n'); b = bytearray(base64.b64decode(lines[3])); b[0] ^= 1
        lines[3] = base64.b64encode(b); self.signature = b'\n'.join(lines)
        with patch.object(RootReader, 'aggregate_begin', side_effect=AssertionError('bad signature physical read')):
            self.refusal('aggregate_signature_invalid', self.backend)
        self.assert_closed()

    def test_unknown_member_returns_no_delivery(self):
        self.refusal('member_not_in_aggregate', lambda: self.backend(name='members/absent.json'))
        self.assert_closed()

    def test_image_cannot_use_small_snapshot_port(self):
        # Type, not the tiny synthetic archive length, decides this separation.
        image = next(r['path'] for r in self.value['members'] if r['kind']=='image')
        self.refusal('raw_limit', lambda: self.backend(name=image)); self.assert_closed()

    def test_original_digest_mismatch_before_R01_parser(self):
        import cortex_core.release.aggregate as aggregate
        raw = self.bodies[MEMBER]; (self.root/MEMBER).write_bytes(raw[:-1]+bytes([raw[-1]^1]))
        with patch.object(aggregate, '_check_pairs', side_effect=AssertionError('parser before digest')):
            self.refusal('member_digest_mismatch', self.backend)
        self.assert_closed()

    def test_unrelated_original_corruption_cannot_be_bypassed_by_lock_request(self):
        path = self.builds[-1]['paths']['B/config.bin']; (self.root/path).write_bytes(b'changed')
        self.refusal('member_digest_mismatch', self.backend); self.assert_closed()

    def test_missing_independent_pair_policy_cannot_be_replaced_by_signature(self):
        self.builds[0]['policy'] = None
        self.refusal('required_binding_gap', self.backend); self.assert_closed()

    def test_actual_pair_receipt_failure_cannot_be_bypassed_by_signature(self):
        g = self.builds[0]; d = json.loads(self.bodies[g['receipt']]); d['comparison']['raw_archives_equal'] = False
        update(self.value, self.bodies, g['receipt'], encode(d))
        next(r for r in self.value['members'] if r['path']==g['image'])['build_receipt_sha256'] = sha256(encode(d))
        self.pin(); self.refusal('aggregate_pair_mismatch', self.backend); self.assert_closed()

    def test_custody_replacement_after_pair_audit_is_detected_at_EOF(self):
        import cortex_core.release.aggregate as aggregate
        original = aggregate._check_pairs
        def after(*args):
            original(*args); path = self.root/MEMBER
            other = self.root/'replace.tmp'; other.write_bytes(self.bodies[MEMBER]); other.replace(path)
        with patch.object(aggregate, '_check_pairs', after): self.refusal('member_custody_changed', self.backend)
        self.assert_closed()

    def test_EOF_rehash_detects_actual_original_bytes_with_blinded_stat_checks(self):
        # Source fault injection: hide stat freshness observations, retain actual
        # held-FD reads/hashes. This is NOT evidence of native stale-stat behavior.
        import cortex_core.release.aggregate as aggregate
        original = aggregate._check_pairs
        path = self.builds[0]['paths']['B/config.bin']; raw = self.bodies[path]
        def after(*args):
            original(*args); (self.root/path).write_bytes(raw[:-1]+bytes([raw[-1]^1]))
        with patch.object(aggregate,'_check_pairs',after),patch.object(RootReader,'_aggregate_check',lambda *args: None):
            self.refusal('member_custody_changed',self.backend)
        self.assert_closed()

    def test_delivery_reobserves_physical_originals_before_snapshot(self):
        delivery = self.backend(); (self.root/MEMBER).write_bytes(b'changed')
        self.refusal('member_digest_mismatch', lambda: delivery.read_original(262144)); self.assert_closed()

    def test_read_limit_exact_type_and_measured_bound(self):
        delivery = self.backend()
        for limit in [True, 0, -1, 262145, 1.0, len(self.bodies[MEMBER])-1]:
            self.refusal('raw_limit', lambda: delivery.read_original(limit))
        self.assertEqual(delivery.read_original(len(self.bodies[MEMBER])), self.bodies[MEMBER]); self.assert_closed()

    def test_sealed_snapshot_does_not_become_live_authority(self):
        member = self.sealed(); self.authority.now = 9999; (self.root/MEMBER).write_bytes(b'changed')
        self.assertEqual(port.read_member(member, aggregate_id=self.aggregate, member_name=MEMBER), self.bodies[MEMBER])
        self.refusal('aggregate_replay', self.backend)
        self.assertFalse(hasattr(member, 'effect_authority')); self.assertFalse(hasattr(member, 'native_qualified'))

    def test_exception_text_is_closed_and_all_readers_close(self):
        self.transport_hook = lambda r: (_ for _ in ()).throw(ValueError('PRIVATE_SENTINEL'))
        error = self.refusal('member_authority_unavailable', self.backend)
        self.assertNotIn('PRIVATE_SENTINEL', str(error)); self.assert_closed()

    def test_missing_current_context_before_transport(self):
        self.current_hook = lambda c: None
        self.refusal('required_binding_gap', self.backend); self.assertEqual(self.transports, [])

    def test_runtime_admit_config_consumes_actual_original_without_effects(self):
        self.api()
        from cortex_core.runtime.config import admit_config
        cfg = json.loads((NEXT/'tests/install/fixtures/p1-subjects.json').read_bytes())['configs']['linux/arm64']
        cfg = json.loads(cfg)
        cfg['runtime_lock_sha256'] = sha256(self.bodies[MEMBER]); cfg['metrics_labels']['release'] = self.value['release']['release_id']
        member = self.sealed()
        plan = admit_config(member, encode(cfg), aggregate_id=self.aggregate, observed_target=cfg['target'], mode='inspect')
        self.assertEqual(plan.lock_sha256, sha256(self.bodies[MEMBER]))
        self.assertFalse(plan.effect_authority); self.assertFalse(plan.native_qualified); self.assertEqual(plan.authority_status, 'UNBOUND')

    def test_original_transport_reader_requires_actual_held_reader(self):
        class ForeignReader:
            def close(self): pass
        self.transport = lambda aggregate: (self.raw, self.signature, ForeignReader(), self.builds)
        self.refusal('required_binding_gap', self.backend)

    def test_manifest_raw_bound_precedes_hash_and_crypto(self):
        api = self.api(); self.raw = b' '*(1048576+1)
        self.aggregate = 'sha256:'+sha256(self.raw)
        with patch.object(api, 'sha256', side_effect=AssertionError('hash before raw bound')):
            self.refusal('raw_limit', self.backend)
        self.assert_closed()

    def test_owner_expected_inventory_must_match_original_signed_rows(self):
        self.policy['expected_members'] = self.policy['expected_members'][:-1]
        self.refusal('aggregate_pair_mismatch', self.backend); self.assert_closed()

    def test_requested_snapshot_measured_limit_cannot_be_enlarged(self):
        path = 'members/large-inert-record.json'; raw = encode({'inert':'x'*262144})
        self.bodies[path] = raw
        self.value['members'].append(dict(kind='record',role='evidence',path=path,
            media_type='application/json',bytes=len(raw),sha256=sha256(raw)))
        self.value['members'].sort(key=lambda r:r['path']); self.pin()
        self.refusal('raw_limit', lambda: self.backend(name=path)); self.assert_closed()

    def test_delivery_root_rotation_cannot_relabel_existing_metadata(self):
        from r01u3_support.factory import test_authority
        delivery = self.backend(); replacement = test_authority()
        replacement.raw = self.raw; replacement.value = copy.deepcopy(self.value)
        replacement.policy = copy.deepcopy(self.policy)
        replacement.policy['root'].update(key_id=replacement.key_id[::-1].hex().upper(),
            key_material_sha256=sha256(replacement.root_material),public_key_bytes=len(replacement.root_material))
        replacement.current_state['installed_baseline'] = copy.deepcopy(self.policy['installed_baseline'])
        self.authority = replacement; self.policy = replacement.policy; self.signature = replacement.signed()
        self.refusal('aggregate_root_mismatch', lambda: delivery.read_original(262144)); self.assert_closed()

    def installer_fixture(self, target, route):
        subjects = json.loads((NEXT/'tests/install/fixtures/p1-subjects.json').read_bytes())
        q = copy.deepcopy(subjects['requests'][target+'/'+route])
        names = {}
        for name, text in subjects['members'].items():
            if name == MEMBER: continue
            path = 'members/install/'+name
            raw = text.encode(); names[name] = dict(path=path, bytes=len(raw), sha256=sha256(raw))
            self.bodies[path] = raw
            self.value['members'].append(dict(kind='record', role='evidence', media_type='application/json', **names[name]))
        names[MEMBER] = dict(path=MEMBER, bytes=len(self.bodies[MEMBER]), sha256=sha256(self.bodies[MEMBER]))
        def mapped(value):
            if type(value) is dict:
                if set(value)=={'path','bytes','sha256'} and value['path'] in names: return copy.deepcopy(names[value['path']])
                return {k:mapped(v) for k,v in value.items()}
            if type(value) is list: return [mapped(v) for v in value]
            return value
        q = mapped(q); self.value['members'].sort(key=lambda r:r['path']); self.pin()
        cfg = json.loads(subjects['configs'][target]); cfg['runtime_lock_sha256'] = sha256(self.bodies[MEMBER])
        cfg['metrics_labels']['release'] = self.value['release']['release_id']
        return q, encode(cfg)

    def installer(self, q, cfg, target):
        self.api()
        from cortex_core.install.planner import plan
        calls = []
        def config(path, limit): calls.append((path,limit)); return cfg
        with patch.object(port, '_original_authority', self.backend):
            result = plan(encode(q), aggregate_id=self.aggregate, observed_target=target, owner_uid=501,
                          host_identity_ref=q['caller']['host_identity_ref'], environ={}, config_reader=config)
        return result, calls

    def test_finn_planner_real_bridge_rejects_signature_before_config(self):
        q, cfg = self.installer_fixture('linux/arm64', 'manual'); self.signature = b'invalid\n'
        result, calls = self.installer(q, cfg, 'linux/arm64')
        self.assertEqual(result.result, 'FAIL'); self.assertEqual(result.reason_code, 'wrong_release'); self.assertEqual(calls, [])


class EqualityObject:
    comparisons = 0
    def __eq__(self, other): self.comparisons += 1; return True
    def __ne__(self, other): self.comparisons += 1; return False
class EqualityString(str):
    comparisons = 0
    def __eq__(self, other): self.comparisons += 1; return True
    def __ne__(self, other): self.comparisons += 1; return False
class SameString(str): pass

def identity_case(field, factory):
    def case(self):
        api = self.api(); actual = factory(self.aggregate if field=='aggregate' else MEMBER)
        args = [self.aggregate, MEMBER]; args[0 if field=='aggregate' else 1] = actual
        def fail(*a): self.fail('crafted argument reached current/transport')
        self.refusal('member_identity_mismatch', lambda: api._request_original(*args, fail, fail))
        self.assertEqual(getattr(actual, 'comparisons', 0), 0)
    return case
for field in ('aggregate', 'member'):
    for label, factory in [('equality_object', lambda v: EqualityObject()), ('wrong_subclass', lambda v: EqualityString('different')), ('same_subclass', SameString)]:
        setattr(Bridge, 'test_exact_'+field+'_'+label, identity_case(field, factory))

def installer_case(target, route):
    def case(self):
        q, cfg = self.installer_fixture(target, route); result, calls = self.installer(q, cfg, target)
        self.assertEqual(result.result, 'PASS'); self.assertEqual(result.reason_code, 'completed')
        self.assertEqual(calls, [('/run/cortex/inspection.json',65536)])
        self.assertEqual(result.runtime_plan.lock_sha256, sha256(self.bodies[MEMBER]))
        self.assertFalse(result.effect_authority); self.assertFalse(result.native_qualified)
        self.assertEqual(result.authority_status, 'UNBOUND'); self.assertIsNone(result.receipt['journal_ref'])
        self.assertEqual(result.receipt['invocation_result']['outcome'], 'not_executed')
        self.assertGreater(len(self.transports), 2); self.assert_closed()
    return case
for target in ('linux/arm64', 'linux/amd64'):
    for route in ('manual', 'kos_prerequisite'):
        setattr(Bridge, 'test_actual_finn_'+target.replace('/','_')+'_'+route, installer_case(target, route))

if __name__ == '__main__': unittest.main()
