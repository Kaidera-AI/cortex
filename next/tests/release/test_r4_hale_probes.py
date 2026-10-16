"""Hale's expectation REDs. Owned fixture effects only; never an engine."""
import copy
import json
import time
import unittest
from unittest.mock import patch
import test_r4_native_lane as r4

WITNESSES = {}


class Boundaries(unittest.TestCase):
    def test_OWN_no_rm_before_replaced_store_identity_is_proven(self):
        lane = r4.Lane(self)
        fake = r4.Engine(lane, replace_store=True, ps=[['f' * 64], []])
        record = lane.run(fake)
        marker = list(lane.parent.glob('*-store/foreign-owned-marker'))
        WITNESSES['OWN'] = {'calls': fake.calls, 'marker_survived': bool(marker),
                            'result': record['result'], 'cleanup': record['lifecycle']['cleanup']}
        self.assertTrue(marker)
        self.assertNotIn('unshare', fake.calls)
        self.assertNotIn('rm', fake.calls, 'replacement must never receive a destructive command')

    def budget_case(self):
        clock = r4.Clock()
        caps = {'input': 10, 'parser': 60, 'build': 120, 'export': 60, 'audit': 60, 'cleanup': 30}
        lane = r4.Lane(self, absolute=400, caps=caps)
        fake = r4.Engine(lane, clock=clock, costs={'load': 9, 'inspect': 100})
        at_cleanup = []
        def child(*args):
            if args[0][8] == 'ps':
                at_cleanup.append(clock.now)
            return fake(*args)
        with patch.object(time, 'monotonic', clock), patch.object(r4.runner, '_child', child):
            record = r4.runner.native_lane('A', r4.verified(lane.fx, lane.value), lane.materialize,
                lane.base, lane.closure, r4.HOST, lane.parent, lane.outputs, lane.evidence, clock=clock)
        WITNESSES['BUDGET'] = {'cleanup_start': at_cleanup[0], 'timeouts': fake.timeouts,
                               'phase_cap': 10, 'cleanup_cap': 30, 'kill_seconds': r4.runner.KILL_SECONDS}
        return fake, at_cleanup[0]

    def test_BUDGET_phase_cap_includes_kill_wait(self):
        _, cleanup_start = self.budget_case()
        self.assertLessEqual(cleanup_start, 10, 'input + termination must fit the input cap')

    def test_BUDGET_cleanup_cap_includes_kill_wait(self):
        fake, _ = self.budget_case()
        first_ps = next(timeout for verb, timeout in fake.timeouts if verb == 'ps')
        self.assertLessEqual(first_ps + r4.runner.KILL_SECONDS, 30, 'cleanup gets at most its cap')

    def complete(self):
        lane = r4.Lane(self)
        inventory = [{'path': 'app/module.py', 'bytes': 5, 'sha256': '0' * 64,
                      'mode': 420, 'uid': 1001, 'gid': 1001, 'mtime': 0}]
        with patch.object(r4.product('artifacts'), 'inspect_oci', return_value=((), inventory)), \
             patch.object(r4.runner, '_runtime_smoke', return_value=('pass', 'fixture contract')), \
             patch.object(r4.runner, '_child', r4.Engine(lane, ps=[[], [], [], []])), \
             patch.object(r4.build_receipts, 'verify_shared_receipt', side_effect=AssertionError('verified')):
            result = r4.runner.native_lanes(r4.verified(lane.fx, lane.value), lane.materialize,
                lane.base, lane.closure, r4.HOST, lane.parent, lane.outputs, lane.evidence,
                r4.codec.canonical_bytes(r4.qualification(lane)))
        self.assertEqual(result['result'], 'RAW')
        return lane, json.loads(result['receipt'])

    def emit(self, lane, value):
        return r4.build_receipts.emit(r4.verified(lane.fx, lane.value), value['builds'], value['audit'],
            value['evidence'], value['custody'], value['cleanup'],
            lambda path: (lane.outputs / path).read_bytes(),
            lambda path: (lane.evidence / path).read_bytes(), None)

    def test_EMIT_missing_required_runtime_class_refuses(self):
        lane, value = self.complete()
        for slot in ('A', 'B'):
            del value['audit']['reports'][slot]['runtime']
        with self.assertRaisesRegex(ValueError, 'r02_native_emission_unqualified'):
            raw, verified = self.emit(lane, value)
            WITNESSES['EMIT_missing_class'] = {'result': json.loads(raw)['result'], 'verified': verified,
                'required': value['audit']['required_classes'],
                'present_A': sorted(value['audit']['reports']['A'])}

    def test_EMIT_incomplete_cleanup_and_custody_refuse(self):
        lane, value = self.complete()
        value['cleanup']['complete'] = False
        value['custody']['complete'] = False
        with self.assertRaisesRegex(ValueError, 'r02_native_emission_unqualified'):
            raw, verified = self.emit(lane, value)
            WITNESSES['EMIT_incomplete'] = {'result': json.loads(raw)['result'], 'verified': verified,
                                         'cleanup': value['cleanup'], 'custody': value['custody']}

    def test_MAP_timed_out_child_remains_in_ordered_evidence(self):
        lane = r4.Lane(self)
        fake = r4.Engine(lane, fail='inspect')
        record = lane.run(fake)
        children = record['phases']['input']['children']
        WITNESSES['MAP_timeout'] = {'calls': fake.calls, 'recorded': [c['argv'][8:] for c in children]}
        self.assertEqual(len(children), 2, 'load and failed inspect must both be retained')

    def test_MAP_owner_verifier_refuses_empty_input_children(self):
        lane, value = self.complete()
        build = value['builds']['A']
        measured = {row['path']: (lane.evidence / row['path']).read_bytes()
                    for row in value['evidence']['inventory']}
        op = next(op for op in build['operations'] if op['phase'] == 'input')
        path = op['evidence']['path'][:-5] + '.children.json'
        raw = r4.codec.canonical_bytes([])
        measured[path] = raw
        inventory = copy.deepcopy(value['evidence']['inventory'])
        row = next(row for row in inventory if row['path'] == path)
        row.update(bytes=len(raw), sha256=r4.sha(raw))
        doc = json.loads((lane.evidence / 'qualification.json').read_bytes())
        # Confirm every unmodified child first; only the input list is the fault.
        original = dict(measured, **{path: (lane.evidence / path).read_bytes()})
        r4.receipts._native_children(build, original, value['evidence']['inventory'], doc)
        with self.assertRaisesRegex(ValueError, 'native_child_evidence'):
            found = r4.receipts._native_children(build, measured, inventory, doc)
            WITNESSES['MAP_empty_input'] = {'accepted': path in found, 'path': path}

    def test_MAP_evidence_audit_retains_full_child_metadata(self):
        lane = r4.Lane(self)
        report = {'closure_sha256': lane.closure.closure_sha256,
            'request_sha256': '1' * 64, 'reply_sha256': '2' * 64,
            'argv': ['fixture-evidence-run'], 'verdicts': [{'cache': 'a', 'equal': True}]}
        def inspect(*args, inspection, **kwargs):
            inspection.check([{'cache': 'a'}])
            return (), []
        with patch.object(r4.product('artifacts'), 'inspect_oci', inspect), \
             patch.object(r4.inspector, 'collect_inspection_evidence', return_value=report):
            record = lane.run(r4.Engine(lane))
        child = record['phases']['audit']['children'][0]
        WITNESSES['MAP_audit'] = {'recorded_keys': sorted(child), 'child': child}
        self.assertTrue({'started_at_unix_ns', 'finished_at_unix_ns', 'stdout', 'stderr', 'journal'} <= set(child),
                        'evidence-only inspection must retain its actual child execution evidence')
