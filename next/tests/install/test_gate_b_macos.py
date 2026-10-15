"""BM SOURCE SIMULATION oracle; no actual Mac/kernel/effect qualification.

Literal observer subjects are frozen before the adapter. Native providers stay
UNBOUND. Private unittest patching supplies DATA, never installed authority.
"""
import ast
import copy
from contextlib import ExitStack
from dataclasses import FrozenInstanceError
import hashlib
import importlib
import inspect
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from cortex_core.install import owner
from cortex_core.runtime import signed_member
from non_authoritative_signed_member_double import NonAuthoritativeSignedMemberDouble

NEXT = Path(__file__).resolve().parents[2]
SUBJECT = NEXT / 'src/cortex_core/install/platforms/macos.py'
FIXTURE = json.loads((Path(__file__).parent / 'fixtures/gate-b-macos.json').read_bytes())


def encode(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False) + '\n').encode()


class MacOSObserver(unittest.TestCase):
    def api(self):
        self.assertTrue(SUBJECT.is_file(), 'BM_SURFACE_ABSENT: fixed macOS observer DATA contracts')
        return importlib.import_module('cortex_core.install.platforms.macos')

    def data(self):
        return copy.deepcopy(FIXTURE)

    def carrier(self, api, data):
        b = data['owner_inputs']
        inputs = owner._Inputs(b['context_raw'].encode(),
            {k: v.encode() for k, v in b['originals'].items()},
            b['current_clock_raw'].encode(), b['aggregate_id'])
        return api._MacOSInputs(inputs, data['profile_ref'], data['observation_ref'])

    def edit_peer(self, data, change):
        b = data['owner_inputs']; context = json.loads(b['context_raw'])
        reference = context['peer_ref']; peer = json.loads(b['originals'][reference['path']])
        change(peer)
        raw = encode(peer); b['originals'][reference['path']] = raw.decode()
        context['peer_ref'] = {**reference, 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
        consent_ref = context['consent_ref']; consent = json.loads(b['originals'][consent_ref['path']])
        consent['subject_ref'] = context['peer_ref']
        raw = encode(consent); b['originals'][consent_ref['path']] = raw.decode()
        context['consent_ref'] = {**consent_ref, 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
        b['context_raw'] = encode(context).decode()

    def change(self, data, case, *, rebind=True):
        if case['record'] == 'context':
            raw = data['owner_inputs']['context_raw']
        else:
            reference = data[case['record'] + '_ref']
            raw = data['owner_inputs']['originals'][reference['path']]
        value = json.loads(raw)
        parent = value
        for key in case['path'][:-1]:
            parent = parent[key]
        key = case['path'][-1]
        if case['operation'] == 'delete':
            del parent[key]
        else:
            parent[key] = case['value']
        raw = encode(value)
        if case['record'] == 'context':
            data['owner_inputs']['context_raw'] = raw.decode()
        else:
            data['owner_inputs']['originals'][reference['path']] = raw.decode()
            data[case['record'] + '_ref'] = {**reference, 'bytes': len(raw),
                'sha256': hashlib.sha256(raw).hexdigest()}
            if rebind:
                self.edit_peer(data, lambda p: p.update(macos_profile_ref=data['profile_ref'],
                    macos_observation_ref=data['observation_ref']))

    def observe(self, api, data, supplied=None):
        supplied = self.carrier(api, data) if supplied is None else supplied
        with patch.object(api, '_installed_macos_inputs', return_value=supplied):
            return api._installed_owner_inputs()

    def refusal(self, detail, operation):
        with self.assertRaises(owner.OwnerRefusal) as got:
            operation()
        self.assertEqual(got.exception.detail, detail)
        self.assertNotIn('SENTINEL', str(got.exception))
        return got.exception

    def integration(self, api, data, fresh=None):
        values = [self.carrier(api, data), self.carrier(api, data if fresh is None else fresh)]
        calls = []
        def installed():
            calls.append('fixed-macos-observation')
            return values[min(len(calls) - 1, 1)]
        b = data['owner_inputs']
        double = NonAuthoritativeSignedMemberDouble(b['runtime_lock_raw'].encode(),
            aggregate_id=b['aggregate_id'], member_name='runtime.lock.json')
        with patch.object(api, '_installed_macos_inputs', installed), \
                patch.object(owner, '_installed_owner_inputs', api._installed_owner_inputs), \
                double.installed(signed_member):
            result = owner.propose_effect(b['request_raw'].encode())
        return result, calls

    def negative_case(self, case):
        api = self.api()
        data = self.data()
        # Every semantic negative first has a usable matched actual-source control.
        positive = self.observe(api, data)
        self.assertIs(type(positive), owner._Inputs)
        self.change(data, case)
        with patch.object(signed_member, 'request_member') as candidates:
            self.refusal(case['expected_detail'], lambda: self.observe(api, data))
        self.assertEqual(candidates.call_count, case['candidate_reads'])
        self.assertEqual(case['physical'], 'NOT_RUN')

    def test_default_binding_gap_before_any_original_or_native_access(self):
        api = self.api()
        def forbidden(*args, **kwargs):
            self.fail('unbound observer reached original/native/candidate access')
        with patch.object(owner, '_original', forbidden), patch.object(owner, '_decode', forbidden), \
                patch('builtins.open', forbidden), patch.object(Path, 'read_bytes', forbidden), \
                patch('socket.socket', forbidden), patch('subprocess.Popen', forbidden):
            error = self.refusal('required_binding_gap', api._installed_owner_inputs)
        self.assertEqual((error.result, error.reason_code, error.unit),
            ('BLOCKED', 'dependency_not_run', owner.MISSING_UNIT))

    def test_no_public_provider_binder_or_new_P2a_interface(self):
        api = self.api()
        self.assertEqual(tuple(api.__all__), ())
        self.assertEqual(list(inspect.signature(api._installed_owner_inputs).parameters), [])
        self.assertEqual(list(inspect.signature(api._installed_macos_inputs).parameters), [])
        self.assertEqual(list(inspect.signature(owner.propose_effect).parameters), ['original_request'])

    def test_matched_observer_returns_only_B1_data_and_copies_original_map(self):
        api = self.api(); data = self.data(); carrier = self.carrier(api, data)
        result = self.observe(api, data, carrier)
        self.assertIs(type(result), owner._Inputs)
        self.assertEqual(result, carrier.owner_inputs)
        self.assertIsNot(result.originals, carrier.owner_inputs.originals)
        carrier.owner_inputs.originals.clear()
        self.assertTrue(result.originals)
        with self.assertRaises(FrozenInstanceError):
            result.context_raw = b'changed'

    def test_carrier_exact_type_is_not_a_provenance_or_subclass_shortcut(self):
        api = self.api(); data = self.data(); value = self.carrier(api, data)
        fake = SimpleNamespace(owner_inputs=value.owner_inputs,
            profile_ref=value.profile_ref, observation_ref=value.observation_ref)
        self.observe(api, data)
        self.refusal('owner_unavailable', lambda: self.observe(api, data, fake))

    def test_existing_original_reference_integrity_is_required(self):
        api = self.api(); data = self.data(); self.observe(api, data)
        data['observation_ref']['sha256'] = '0' * 64
        self.refusal('owner_unavailable', lambda: self.observe(api, data))

    def test_missing_original_is_unavailable_not_an_implicit_observation(self):
        api = self.api(); data = self.data(); self.observe(api, data)
        del data['owner_inputs']['originals'][data['profile_ref']['path']]
        self.refusal('owner_unavailable', lambda: self.observe(api, data))

    def test_provider_failure_is_sanitized_and_has_no_fallback(self):
        api = self.api()
        with patch.object(api, '_installed_macos_inputs', side_effect=RuntimeError('SENTINEL/private-root')):
            error = self.refusal('owner_unavailable', api._installed_owner_inputs)
        self.assertEqual((error.result, error.reason_code), ('BLOCKED', 'dependency_not_run'))

    def test_actual_B1_entry_consumes_mac_observer_twice_and_returns_DATA(self):
        api = self.api(); result, calls = self.integration(api, self.data())
        self.assertEqual(calls, ['fixed-macos-observation'] * 2)
        self.assertEqual(result.stage, 'PRECONSUME_DATA')
        self.assertEqual(result.observed_state_revision, 7)
        self.assertEqual((result.deadline.expires_at, result.deadline.expires_monotonic_ns),
            (1700000005, 105000000000))
        for name in ('effect_authority', 'native_qualified', 'reservation_created', 'consumed'):
            self.assertIs(getattr(result, name), False)

    def test_actual_B1_entry_refuses_bad_mac_peer_before_R01_candidate(self):
        api = self.api(); data = self.data()
        case = next(c for c in data['cases'] if c['id'] == 'peer_lifetime_substitution')
        self.integration(api, data)
        self.change(data, case)
        with patch.object(signed_member, 'request_member') as candidates:
            self.refusal('peer_denied', lambda: self.integration(api, data))
        candidates.assert_not_called()

    def test_actual_B1_post_audit_observation_is_not_cached(self):
        api = self.api(); first = self.data(); fresh = self.data()
        case = next(c for c in first['cases'] if c['id'] == 'launch_caller_controls_launch')
        self.integration(api, first)
        self.change(fresh, case)
        self.refusal('peer_denied', lambda: self.integration(api, first, fresh))

    def test_peer_original_must_bind_both_platform_original_refs(self):
        api = self.api(); self.observe(api, self.data())
        for field in ('macos_profile_ref', 'macos_observation_ref'):
            with self.subTest(field=field):
                data = self.data(); self.edit_peer(data, lambda p: p.pop(field))
                self.refusal('peer_denied', lambda: self.observe(api, data))

    def test_peer_original_cannot_substitute_platform_ref(self):
        api = self.api(); self.observe(api, self.data())
        for field, other in (('macos_profile_ref', 'observation_ref'),
                ('macos_observation_ref', 'profile_ref')):
            with self.subTest(field=field):
                data = self.data(); self.edit_peer(data, lambda p: p.update({field: data[other]}))
                self.refusal('peer_denied', lambda: self.observe(api, data))

    def test_post_audit_coherent_native_reprofile_cannot_keep_old_peer_subject(self):
        api = self.api(); first = self.data(); fresh = self.data()
        self.integration(api, first)
        self.change(fresh, {'record': 'profile', 'path': ['launch_id'], 'operation': 'set',
            'value': 'SOURCE_SIMULATION-new-admitted-launch'}, rebind=False)
        self.change(fresh, {'record': 'observation', 'path': ['launch', 'launch_id'], 'operation': 'set',
            'value': 'SOURCE_SIMULATION-new-admitted-launch'}, rebind=False)
        self.refusal('peer_denied', lambda: self.integration(api, first, fresh))

    def test_source_positive_calls_no_native_effect_canary(self):
        api = self.api(); calls = []
        def forbidden(*args, **kwargs):
            calls.append('native-effect-canary')
            self.fail('source observer attempted a native effect')
        with ExitStack() as stack:
            for name in ('socket.socket', 'subprocess.Popen', 'os.system',
                    'os.mkdir', 'os.chown', 'os.chmod', 'os.posix_spawn'):
                stack.enter_context(patch(name, forbidden))
            self.observe(api, self.data())
            self.integration(api, self.data())
        self.assertEqual(calls, [])

    def test_source_has_no_platform_probe_effect_or_configuration_path(self):
        api = self.api(); tree = ast.parse(SUBJECT.read_text())
        forbidden_imports = {'os', 'sys', 'socket', 'subprocess', 'ctypes', 'platform', 'shutil', 'urllib'}
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(n.name.split('.')[0] for n in node.names)
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split('.')[0])
            if isinstance(node, ast.Call):
                name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, 'attr', '')
                self.assertNotIn(name, {'open', 'read_bytes', 'write_bytes', 'write_text', 'resolve',
                    'stat', 'lstat', 'getenv', 'getuid', 'geteuid', 'system', 'Popen', 'exec', 'eval', '__import__'})
        self.assertFalse(imported & forbidden_imports)
        self.refusal('required_binding_gap', lambda: owner.propose_effect(object()))
        self.assertEqual(api.__all__, ())

    def test_literal_fixture_never_claims_physical_or_GateB_acceptance(self):
        api = self.api()
        self.assertEqual(FIXTURE['scope'], 'SOURCE_SIMULATION_ONLY')
        self.assertEqual(FIXTURE['physical']['GateB'], 'NOT_ACCEPTED')
        self.assertTrue(all(v == 'NOT_RUN' for k, v in FIXTURE['physical'].items() if k != 'GateB'))
        self.assertEqual(len(FIXTURE['cases']), 70)
        self.assertEqual(len({c['id'] for c in FIXTURE['cases']}), 70)
        self.assertEqual({c['guard'] for c in FIXTURE['cases']},
            {'profile', 'shape', 'binding', 'launch', 'peer', 'code', 'custody'})


def case_method(case):
    def test(self):
        self.negative_case(case)
    return test


for _case in FIXTURE['cases']:
    setattr(MacOSObserver, 'test_literal_' + _case['id'], case_method(_case))
