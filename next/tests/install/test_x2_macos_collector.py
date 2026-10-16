"""X2 macOS collector behind ONE native seam (accepted X2 note c7fa754c, nix slice).

SOURCE SIMULATION only: fake native snapshots stand in at the one zero-argument
seam `_macos_collector._native_snapshot`; no XPC service, launchd, code signing,
host or backend is touched. The authentic positive is derived from the frozen Gate B
macOS fixture and must map BYTE-FOR-BYTE onto its accepted observation original.
Real macOS evidence waits for the test MacBook (CTO item R); nothing here is
native qualification.
"""
import ast
import copy
import hashlib
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from cortex_core.install import owner
from cortex_core.install.platforms import macos

NEXT = Path(__file__).resolve().parents[2]
COLLECTOR = NEXT / 'src/cortex_core/install/platforms/_macos_collector.py'
FIXTURE = json.loads((Path(__file__).parent / 'fixtures/gate-b-macos.json').read_bytes())
ARGV = ['/Library/Application Support/Kaidera/Cortex/runtime/bin/python3.12', '-I', '-S', '-B',
        '/Library/Application Support/Kaidera/Cortex/runtime/libexec/cortex-owner.py']


def collector():
    from cortex_core.install.platforms import _macos_collector
    return _macos_collector


def snapshot():
    """The authentic snapshot the native helper would return for the fixture's world."""
    data = copy.deepcopy(FIXTURE)
    b = data['owner_inputs']
    originals = {k: v.encode() for k, v in b['originals'].items()}
    observation = json.loads(originals.pop(data['observation_ref']['path']))
    peer_code = json.loads(originals['bm-peer-code.json'])
    launch, owner_process, peer_process = observation['launch'], observation['owner_process'], observation['peer_process']
    plist = next(c for c in observation['custody_before']['chains'] if c['role'] == 'launch')['nodes'][-1]['sha256']
    code = {k: v for k, v in observation['code'].items() if k not in ('peer_requirement_ref', 'peer_requirement_satisfied')}
    sample = {'platform': observation['platform'], 'arch': observation['arch'],
              'host_identity_sha256': observation['host_identity_ref']['sha256'],
              'owner_epoch': observation['owner_epoch'], 'session_id': observation['session_id'],
              'launchd': [{'label': launch['launch_id'], 'domain': 'system', 'uid': launch['owner_uid'],
                           'pid': owner_process['pid'], 'mach_services': [launch['endpoint_id']], 'argv': list(ARGV),
                           'environment': [], 'plist_sha256': plist}],
              'owner': {k: owner_process[k] for k in ('pid', 'uids', 'groups', 'lifetime')},
              'xpc': {'requirement_rc': 0, 'requirement': peer_code, 'sender_valid': True,
                      'peer': {k: peer_process[k] for k in ('pid', 'uids', 'groups', 'lifetime')}},
              'code': dict(code, entitlements={}),
              'denials': {'caller_launch': 'denied', 'caller_debug': 'denied', 'caller_elevate': 'denied',
                          'backend_bypass': 'denied'}}
    inputs = owner._Inputs(b['context_raw'].encode(), originals, b['current_clock_raw'].encode(), b['aggregate_id'])
    admitted = {'owner_inputs': inputs, 'profile_ref': data['profile_ref'],
                'observation_path': data['observation_ref']['path'],
                'launch': {'label': launch['launch_id'], 'mach_service': launch['endpoint_id'], 'domain': 'system',
                           'uid': launch['owner_uid'], 'argv': list(ARGV), 'environment': [], 'plist_sha256': plist},
                'entitlements': {}}
    return {'schema': 'cortex.macos-native-snapshot.v1', 'admitted': admitted,
            'samples': [dict(copy.deepcopy(sample), custody=observation['custody_before']),
                        dict(copy.deepcopy(sample), custody=observation['custody_after'])]}


def both(change):
    """Apply one change to BOTH samples (a coherent world, not drift)."""
    def apply(value):
        for sample in value['samples']:
            change(sample)
    return apply


def encode(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False) + '\n').encode()


def rebound(value, observation_ref):
    """The admitted session pins THIS observation (peer.macos_observation_ref, as the
    Gate B oracle's rebind does), so macos.py's semantic guards decide, not the pin."""
    value = dict(value, admitted=dict(value['admitted']))
    inputs = value['admitted']['owner_inputs']
    originals = dict(inputs.originals)
    context = json.loads(inputs.context_raw)

    def rewrite(key, change):
        reference = context[key]
        record = json.loads(originals[reference['path']])
        change(record)
        raw = encode(record)
        originals[reference['path']] = raw
        context[key] = dict(reference, bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
    rewrite('peer_ref', lambda peer: peer.__setitem__('macos_observation_ref', observation_ref))
    rewrite('consent_ref', lambda consent: consent.__setitem__('subject_ref', context['peer_ref']))
    value['admitted']['owner_inputs'] = owner._Inputs(encode(context), originals, inputs.current_clock_raw,
                                                      inputs.aggregate_id)
    return value


class Base(unittest.TestCase):
    def observe(self, value, *, rebind=True):
        if rebind:
            with patch.object(collector(), '_native_snapshot', lambda: value):
                try:
                    carrier = collector().collect()
                except owner.OwnerRefusal:
                    carrier = None  # the collector's own refusal decides; macos re-raises it unchanged
            if carrier is not None:
                value = rebound(value, carrier.observation_ref)
        with patch.object(collector(), '_native_snapshot', lambda: value):
            return macos._installed_owner_inputs()

    def refuses(self, detail, change):
        positive = snapshot()
        self.assertIs(type(self.observe(positive)), owner._Inputs)  # matched control first
        value = snapshot()
        change(value)
        with self.assertRaises(owner.OwnerRefusal) as got:
            self.observe(value)
        self.assertEqual(got.exception.detail, detail)
        self.assertIs(type(self.observe(snapshot())), owner._Inputs)  # authentic restore
        return got.exception


class Seam(Base):
    """One zero-argument native seam; UNBOUND by default; the collector is pure."""

    def test_unbound_seam_refuses_before_any_original_native_or_file_access(self):
        def forbidden(*args, **kwargs):
            self.fail('unbound collector reached original/native/file access')
        with patch.object(owner, '_original', forbidden), patch.object(owner, '_decode', forbidden), \
                patch('builtins.open', forbidden), patch.object(Path, 'read_bytes', forbidden), \
                patch('socket.socket', forbidden), patch('subprocess.Popen', forbidden):
            with self.assertRaises(owner.OwnerRefusal) as got:
                macos._installed_owner_inputs()
        self.assertEqual((got.exception.detail, got.exception.result), ('required_binding_gap', 'BLOCKED'))

    def test_authentic_snapshot_maps_byte_for_byte_onto_the_accepted_observation(self):
        result = self.observe(snapshot())
        reference = FIXTURE['observation_ref']
        raw = result.originals[reference['path']]
        self.assertEqual(raw, FIXTURE['owner_inputs']['originals'][reference['path']].encode())
        self.assertEqual(hashlib.sha256(raw).hexdigest(), reference['sha256'])
        self.assertIs(type(result), owner._Inputs)  # DATA only, never an authority object

    def test_darwin_owner_entry_reaches_the_collector_once_per_observation(self):
        calls = []
        with patch.object(sys, 'platform', 'darwin'), \
                patch.object(collector(), '_native_snapshot', lambda: calls.append('native-snapshot') or snapshot()):
            first = owner._installed_owner_inputs()
            second = owner._installed_owner_inputs()
        self.assertEqual(calls, ['native-snapshot'] * 2)  # re-observed, never cached
        self.assertEqual(first, second)

    def test_a_coherent_but_different_observation_is_refused_by_the_session_pin(self):
        value = snapshot()
        both(lambda s: s['owner'].__setitem__('groups', [602, 80]))(value)  # valid shape, not the pinned world
        with self.assertRaises(owner.OwnerRefusal) as got:
            self.observe(value, rebind=False)
        self.assertEqual(got.exception.detail, 'peer_denied')
        self.assertIs(type(self.observe(value)), owner._Inputs)  # the same world, pinned, passes every guard

    def test_collector_and_provider_stay_probe_free_and_zero_argument(self):
        import inspect
        self.assertEqual(list(inspect.signature(collector()._native_snapshot).parameters), [])
        self.assertEqual(list(inspect.signature(collector().collect).parameters), [])
        tree = ast.parse(COLLECTOR.read_text())
        imported = {n.name.split('.')[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for n in node.names}
        imported |= {node.module.split('.')[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
        self.assertFalse(imported & {'os', 'sys', 'socket', 'subprocess', 'ctypes', 'platform', 'shutil', 'urllib'})
        calls = {node.func.id if isinstance(node.func, ast.Name) else node.func.attr
                 for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, (ast.Name, ast.Attribute))}
        self.assertFalse(calls & {'open', 'read_bytes', 'stat', 'lstat', 'getenv', 'getuid', 'system', 'Popen', 'eval', 'exec'})


def stale_observation(value):
    inputs = value['admitted']['owner_inputs']
    originals = dict(inputs.originals, **{value['admitted']['observation_path']: b'{"stale":true}\n'})
    value['admitted']['owner_inputs'] = owner._Inputs(inputs.context_raw, originals, inputs.current_clock_raw,
                                                      inputs.aggregate_id)


class Shape(Base):
    """Missing or malformed observations BLOCK (owner_unavailable); no implicit default."""

    def test_missing_or_malformed_snapshot_blocks(self):
        cases = {'extra snapshot key': lambda v: v.__setitem__('extra', 1),
                 'wrong schema': lambda v: v.__setitem__('schema', 'cortex.macos-native-snapshot.v0'),
                 'one sample only': lambda v: v['samples'].pop(),
                 'sample key missing': lambda v: v['samples'][1].pop('denials'),
                 'sample extra key': lambda v: v['samples'][0].__setitem__('note', 'x'),
                 'admitted inputs not the carrier type': lambda v: v['admitted'].__setitem__('owner_inputs', {}),
                 'a stale observation already admitted (never overwritten)': stale_observation,
                 'launchd job absent': both(lambda s: s.__setitem__('launchd', [])),
                 'no audit-token peer lifetime (no PID-only fallback)': both(lambda s: s['xpc'].__setitem__('peer', None))}
        for name, change in cases.items():
            with self.subTest(case=name):
                self.refuses('owner_unavailable', change)


class Launch(Base):
    """The fixed system launchd job and its admitted launch descriptor."""

    def test_launch_job_must_be_the_one_admitted_system_job(self):
        def duplicate(sample):
            sample['launchd'].append(dict(sample['launchd'][0], pid=9999))
        cases = {'ambiguous: two jobs with the label': both(duplicate),
                 'user domain': both(lambda s: s['launchd'][0].__setitem__('domain', 'gui/501')),
                 'another executable': both(lambda s: s['launchd'][0]['argv'].__setitem__(0, '/tmp/python3.12')),
                 'extra startup flag': both(lambda s: s['launchd'][0]['argv'].insert(1, '-X')),
                 'startup environment': both(lambda s: s['launchd'][0].__setitem__('environment', ['PYTHONPATH'])),
                 'second mach service': both(lambda s: s['launchd'][0]['mach_services'].append('other.service')),
                 'job runs as another uid': both(lambda s: s['launchd'][0].__setitem__('uid', 0))}
        for name, change in cases.items():
            with self.subTest(case=name):
                self.refuses('peer_denied', change)

    def test_launch_descriptor_drift_from_the_admitted_original_holds(self):
        self.refuses('custody_changed', both(lambda s: s['launchd'][0].__setitem__('plist_sha256', '0' * 64)))


class Code(Base):
    """XPC peer code requirement of the caller + owner code/entitlement custody."""

    def test_peer_requirement_and_entitlements_are_enforced(self):
        cases = {'requirement call failed': both(lambda s: s['xpc'].__setitem__('requirement_rc', -1)),
                 'requirement substituted': both(lambda s: s['xpc'].__setitem__(
                     'requirement', {'requirement_id': 'anyone', 'revision': 1})),
                 'sender failed the requirement': both(lambda s: s['xpc'].__setitem__('sender_valid', False)),
                 'get-task-allow': both(lambda s: s['code']['entitlements'].__setitem__(
                     'com.apple.security.get-task-allow', True)),
                 'any entitlement (accepted set is EMPTY)': both(lambda s: s['code']['entitlements'].__setitem__(
                     'com.apple.security.cs.disable-library-validation', True)),
                 'owner code requirement unsatisfied': both(lambda s: s['code'].__setitem__('owner_requirement_satisfied', False)),
                 'candidate imports': both(lambda s: s['code'].__setitem__('candidate_imports', True))}
        for name, change in cases.items():
            with self.subTest(case=name):
                self.refuses('peer_denied', change)


class Lifetime(Base):
    """Owner = the launchd job's process; peer bound to its audit-token lifetime."""

    def test_process_lifetimes_bind_across_both_samples(self):
        cases = {'owner is not the launchd job process': both(lambda s: s['owner'].__setitem__('pid', 701)),
                 'peer replaced between samples': lambda v: v['samples'][1]['xpc']['peer'].__setitem__('lifetime', 'other'),
                 'peer pid reused between samples': lambda v: v['samples'][1]['xpc']['peer'].__setitem__('pid', 801),
                 'owner replaced between samples': lambda v: v['samples'][1]['owner'].__setitem__('lifetime', 'other'),
                 'peer is the owner pid': both(lambda s: s['xpc']['peer'].__setitem__('pid', s['owner']['pid']))}
        for name, change in cases.items():
            with self.subTest(case=name):
                self.refuses('peer_denied', change)


class Backend(Base):
    """Backend exclusion and caller powers come from measured denials, never assumed."""

    def test_any_power_not_measured_as_denied_refuses(self):
        for probe in ('caller_launch', 'caller_debug', 'caller_elevate', 'backend_bypass'):
            for outcome in ('allowed', 'unknown'):
                with self.subTest(probe=probe, outcome=outcome):
                    self.refuses('peer_denied', both(lambda s: s['denials'].__setitem__(probe, outcome)))


class Stale(Base):
    """A world that moved between the two samples is drift (HOLD); another host FAILS."""

    def test_drift_between_samples_holds(self):
        cases = {'epoch moved': lambda v: v['samples'][1].__setitem__('owner_epoch', 'test-epoch-2'),
                 'session moved': lambda v: v['samples'][1].__setitem__('session_id', 'test-session-2'),
                 'denials moved': lambda v: v['samples'][1]['denials'].__setitem__('caller_debug', 'allowed'),
                 'custody moved': lambda v: v['samples'][1]['custody']['chains'][0]['nodes'][-1].__setitem__('inode', 99)}
        for name, change in cases.items():
            with self.subTest(case=name):
                self.refuses('custody_changed', change)

    def test_substituted_scalar_types_refuse_and_are_never_erased(self):
        """Vera X2M-001: True == 1 == 1.0 in Python; a native fact of the wrong exact type
        is malformed (BLOCK), never mapped away into the unchanged pinned observation."""
        def second(change):
            return lambda v: change(v['samples'][1])
        cases = {'second sender_valid True -> 1': second(lambda s: s['xpc'].__setitem__('sender_valid', 1)),
                 'second owner_requirement_satisfied True -> 1': second(lambda s: s['code'].__setitem__(
                     'owner_requirement_satisfied', 1)),
                 'second launchd pid int -> float': second(lambda s: s['launchd'][0].__setitem__(
                     'pid', float(s['launchd'][0]['pid']))),
                 'second requirement_rc 0 -> False': second(lambda s: s['xpc'].__setitem__('requirement_rc', False)),
                 'both requirement revision 1 -> True': both(lambda s: s['xpc']['requirement'].__setitem__('revision', True)),
                 'both requirement_rc 0 -> False': both(lambda s: s['xpc'].__setitem__('requirement_rc', False)),
                 'admitted uid int -> float': lambda v: v['admitted']['launch'].__setitem__(
                     'uid', float(v['admitted']['launch']['uid'])),
                 'denial outcome bool': both(lambda s: s['denials'].__setitem__('backend_bypass', True))}
        for name, change in cases.items():
            for rebind in (False, True):
                with self.subTest(case=name, rebind=rebind):
                    value = snapshot()
                    change(value)
                    with self.assertRaises(owner.OwnerRefusal) as got:
                        self.observe(value, rebind=rebind)
                    self.assertEqual(got.exception.detail, 'owner_unavailable')
        self.assertIs(type(self.observe(snapshot(), rebind=False)), owner._Inputs)  # authentic control

    def test_another_host_or_stale_session_fails(self):
        self.refuses('peer_denied', both(lambda s: s.__setitem__('host_identity_sha256', '1' * 64)))
        self.refuses('peer_denied', both(lambda s: s.__setitem__('session_id', 'old-session')))


if __name__ == '__main__':
    unittest.main(verbosity=2)
