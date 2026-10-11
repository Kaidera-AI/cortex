"""Local regression controls for checked effects and native receipt admission.

External effects are spies. The signature adapter supplies store only when that
argument exists, so the same assertions exercise the pre-fix admission function.
"""
import copy
import inspect
from pathlib import Path
import sys
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import dispatch
import native_receipts
from custody import Refusal, bound_json, json_bytes, measured_file
from observations import ObservationStore
from support import Fixture, INSTANCE, VOLUME


class DispatchReceiptCase(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)
        self.invocations = self.f.root / 'invocations'
        self.invocations.mkdir(mode=0o700)

    def attempt(self, fn):
        try:
            return fn()
        except Refusal as error:
            return error.code

    def spec(self, role, phase):
        source = self.f.B['sealed_files'][self.f.B['command_inventory']['root_roles'][role]]
        spec = {'phase': phase, 'argv': [source['path']], 'shell': False, 'timeout_sec': 30,
                'input_digests': {'authority': '9' * 64}, 'output_contract': 'native-facts-json-v2'}
        # Input compatibility only; verdicts below do not branch on implementation.
        self.f.B['commands'][role] = spec
        self.f.B['commands'].setdefault(phase, {})[role] = spec
        self.f.B['phase_command_inputs'][phase][role] = spec['input_digests']
        return spec

    def store(self, phase='PRE_ALLOC_CHECKED'):
        self.S = ObservationStore(self.f.root / 'observations', self.f.A, create=True)
        self.S.append('PRE_ALLOC_CHECKED', {'checks': self.f.checks,
            'admitted_command_inputs': self.f.B['phase_command_inputs']['PRE_ALLOC_CHECKED']})
        if phase != 'PRE_ALLOC_CHECKED':
            self.S.append('ALLOCATED', {'instance_id': INSTANCE, 'volume_id': VOLUME,
                'admitted_command_inputs': self.f.B['phase_command_inputs']['ALLOCATED']})
        return self.S

    def call(self, role, runner):
        return dispatch.execute(self.f.A, self.S, role, runner=runner,
                                invocation_directory=self.invocations)

    def invocation(self):
        return measured_file(next(self.invocations.glob('*.json')))

    def successful_output(self, role):
        rows, records = self.S.read()
        phase = rows[-1]['phase']
        target = {'launcher': 'ALLOCATED', 'manager': 'HOST_BOUND', 'retirer': 'RETIRED'}[role]
        payload = {'instance_id': INSTANCE, 'volume_id': VOLUME}
        raw = {'schema': 'cortex.gate-a.run8.native-facts.v2', **self.f.A.context,
               'execution_scope': 'native', 'phase': target, 'from_phase': phase,
               'observation_predecessor': records[-1], 'input_digests': {'authority': '9' * 64}, 'facts': payload}
        return SimpleNamespace(returncode=0, stdout=json_bytes(raw), stderr=b'')

    def native(self, row, phase='ALLOCATED'):
        kwargs = {'store': self.S} if 'store' in inspect.signature(native_receipts.admit).parameters else {}
        return native_receipts.admit(self.f.A, phase, row, **kwargs)

    def test_expired_admission_refuses_before_launcher_effect(self):
        self.spec('launcher', 'PRE_ALLOC_CHECKED'); self.f.refresh(); self.f.checks = self.f.measured_checks()
        self.store(); effects = []
        runner = lambda *a, **k: (effects.append(a) or SimpleNamespace(returncode=0, stdout=b'', stderr=b''))
        with patch('admission.time.time', return_value=time.time() + 700):
            result = self.attempt(lambda: self.call('launcher', runner))
        self.assertEqual(result, 'check_not_fresh')
        self.assertEqual(effects, [])
        self.assertEqual(list(self.invocations.glob('*.json')), [])

    def test_phase_specific_command_and_invocation_prefix(self):
        self.spec('manager', 'ALLOCATED'); self.spec('manager', 'PRE_ALLOC_CHECKED')
        self.f.refresh(); self.store('ALLOCATED')
        result = self.attempt(lambda: self.call('manager', lambda *a, **k: self.successful_output('manager')))
        self.assertFalse(isinstance(result, str), result)
        invocation = bound_json(self.invocation()); _, records = self.S.read()
        self.assertEqual(invocation.get('from_phase'), 'ALLOCATED')
        self.assertEqual(invocation.get('observation_predecessor'), records[-1])
        self.assertEqual(invocation.get('input_digests'), {'authority': '9' * 64})

    def test_failed_early_command_enters_owned_retirement(self):
        self.spec('manager', 'ALLOCATED'); self.spec('retirer', 'FAILURE')
        self.f.refresh(); self.store('ALLOCATED')
        self.assertEqual(self.attempt(lambda: self.call('manager', lambda *a, **k: SimpleNamespace(returncode=23, stdout=b'', stderr=b''))), 'checked_command_failed')
        rows, _ = self.S.read()
        self.assertEqual(rows[-1]['phase'], 'FAILURE')
        self.assertEqual(rows[-1]['payload']['instance_id'], INSTANCE)
        self.assertEqual(rows[-1]['payload']['volume_id'], VOLUME)
        result = self.attempt(lambda: self.call('retirer', lambda *a, **k: self.successful_output('retirer')))
        self.assertFalse(isinstance(result, str), result)

    def test_header_claim_cannot_substitute_arbitrary_raw_file(self):
        self.spec('launcher', 'PRE_ALLOC_CHECKED'); self.f.refresh(); self.f.checks = self.f.measured_checks(); self.store()
        self.call('launcher', lambda *a, **k: self.successful_output('launcher'))
        row = {'schema': 'cortex.gate-a.run8.native-output.v2', **self.f.A.context,
               'execution_scope': 'native', 'phase': 'ALLOCATED', 'checked_invocation': self.invocation(),
               'raw_evidence': {'facts': self.f.file('unrelated.json', b'{}\n')},
               'payload': {'instance_id': INSTANCE, 'volume_id': VOLUME}}
        result = self.attempt(lambda: self.native(self.f.file('native.json', json_bytes(row))))
        self.assertEqual(result, 'native_output_not_bound_to_invocation')

    def test_native_output_derivation_and_replay_refusal(self):
        self.spec('launcher', 'PRE_ALLOC_CHECKED'); self.f.refresh(); self.f.checks = self.f.measured_checks(); self.store()
        _, records = self.S.read()
        payload = {'instance_id': INSTANCE, 'volume_id': VOLUME}
        facts = {'schema': 'cortex.gate-a.run8.native-facts.v2', **self.f.A.context,
                 'execution_scope': 'native', 'phase': 'ALLOCATED', 'from_phase': 'PRE_ALLOC_CHECKED',
                 'observation_predecessor': records[-1], 'input_digests': {'authority': '9' * 64}, 'facts': payload}
        self.call('launcher', lambda *a, **k: SimpleNamespace(returncode=0, stdout=json_bytes(facts), stderr=b''))
        invocation = bound_json(self.invocation())
        output = invocation.get('public_output')
        self.assertIsInstance(output, dict)
        row = {'schema': 'cortex.gate-a.run8.native-output.v2', **self.f.A.context,
               'execution_scope': 'native', 'phase': 'ALLOCATED', 'checked_invocation': self.invocation(),
               'raw_evidence': {'facts': output}, 'payload': payload}
        receipt = self.f.file('native.json', json_bytes(row))
        self.assertEqual(self.native(receipt), payload)
        bad = copy.deepcopy(row); bad['payload']['volume_id'] = 'vol-' + 'c' * 17
        self.assertEqual(self.attempt(lambda: self.native(self.f.file('bad-native.json', json_bytes(bad)))), 'native_payload_not_derived')
        self.S.append('ALLOCATED', {**payload, 'admitted_command_inputs': self.f.B['phase_command_inputs']['ALLOCATED']})
        self.assertEqual(self.attempt(lambda: self.native(receipt)), 'native_invocation_predecessor')


if __name__ == '__main__':
    unittest.main(verbosity=2)
