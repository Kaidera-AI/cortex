"""B0 frozen contract/receipt oracle, NOT a verifier or owner test factory.

Until accepted original R01 material arrives these are labelled surface-absence
BODY controls, never behavioural positive/crypto/native credit. Future execution
receipts must come from a separately authorised common-owner runner; this module
only compares independent observed evidence with the literal frozen expectations.
"""
import hashlib
import json
from pathlib import Path
import unittest

NEXT = Path(__file__).resolve().parents[2]
FIX = Path(__file__).parent / 'fixtures'
RAW = (FIX / 'gate-b-subjects.json').read_bytes()
SUBJECTS = json.loads(RAW)
CASES = {c['id']: c for c in SUBJECTS['cases']}
OWNER = NEXT / 'src/cortex_core/install/owner.py'

REFUSALS = {
    'required_binding_gap': ('BLOCKED', 'dependency_not_run'),
    'owner_unavailable': ('BLOCKED', 'dependency_not_run'),
    'time_unavailable': ('BLOCKED', 'dependency_not_run'),
    'atomicity_unavailable': ('BLOCKED', 'dependency_not_run'),
    'backend_unavailable': ('BLOCKED', 'dependency_not_run'),
    'peer_denied': ('FAIL', 'unsafe_custody'),
    'invalid_request': ('FAIL', 'bad_shape'),
    **{k: ('FAIL', 'external_conflict') for k in (
        'scope_denied', 'consent_denied', 'consent_expired', 'consent_revoked',
        'session_stale', 'nonce_replay', 'reservation_used', 'reservation_expired',
        'reservation_invalidated')},
    **{k: ('HOLD', 'state_drift') for k in (
        'policy_changed', 'generation_changed', 'fence_changed', 'custody_changed',
        'resource_changed', 'journal_invalid')},
    'attempt_ambiguous': ('HOLD', 'ambiguous_create'),
    'r01_required_binding_gap': ('BLOCKED', 'dependency_not_run'),
    'r01_other_refusal': ('FAIL', 'wrong_release'),
}


def changed_paths(a, b, prefix=''):
    """Fixture-only structural comparison, never a wire decoder or admission."""
    if type(a) is dict and type(b) is dict and set(a) == set(b):
        return [p for key in a for p in changed_paths(a[key], b[key], prefix+key+'.')]
    return [] if type(a) is type(b) and a == b else [prefix.rstrip('.')]


def compare_execution(test, case):
    # Deciding assertion is in the test BODY; no missing import/setup/skip earns RED.
    test.assertTrue(OWNER.is_file(), 'GATE_B_SURFACE_ABSENT ['+case['id']+']: protected owner.py NOT_DELIVERED')
    index = json.loads((FIX / 'subjects.json').read_bytes())
    receipt_ref = index.get('GateB_accepted_execution')
    test.assertIsNotNone(receipt_ref, 'NOT_RUN_MISSING_ORIGINAL_CORPUS ['+case['id']+']: see missing_R01_cases and missing_composed_positive_material')
    receipt_path = FIX / receipt_ref['path']
    test.assertEqual(receipt_path.parent.resolve(), FIX.resolve())
    evidence_raw = receipt_path.read_bytes()
    test.assertEqual(len(evidence_raw), receipt_ref['bytes'])
    test.assertEqual(hashlib.sha256(evidence_raw).hexdigest(), receipt_ref['sha256'])
    evidence = json.loads(evidence_raw)
    test.assertEqual(evidence['frozen_fixture_sha256'], hashlib.sha256(RAW).hexdigest())
    test.assertEqual(evidence['owner_source_sha256'], hashlib.sha256(OWNER.read_bytes()).hexdigest())
    observed = evidence['cases'][case['id']]
    test.assertGreaterEqual(observed['common_owner_invocations'], 1)
    test.assertNotEqual(observed['owner_probe_identity'], observed['primitive_probe_identity'])
    test.assertEqual(observed['observation_origin'], 'independent-executed-boundaries')
    test.assertEqual(observed['observed'], case['expected'])
    test.assertEqual(observed['original_corpus_state'], 'accepted-originals-executed')
    # Presence or exact-reason-only dummies cannot qualify a causal execution.
    control = observed['causal_control']
    test.assertGreaterEqual(control['intended_body_failures'], 1)
    test.assertEqual((control['errors'], control['skips']), (0, 0))
    test.assertEqual(control['restored_owner_sha256'], evidence['owner_source_sha256'])
    test.assertTrue(control['full_restore_passed'])


class FrozenAdmissionContract(unittest.TestCase):
    def test_literal_groups_and_material_classification(self):
        self.assertEqual({c['group'] for c in CASES.values()} - {'PRECEDENCE'},
                         {'C0-'+str(i).zfill(2) for i in range(1, 21)})
        self.assertEqual({c['proof_group'] for c in CASES.values()} - {None},
                         {'B-N'+str(i) for i in range(1, 10)})
        self.assertEqual(len(CASES), len(SUBJECTS['cases']))
        for case in CASES.values():
            self.assertEqual(case['execution'], 'NOT_RUN_MISSING_ORIGINAL_CORPUS')
            self.assertEqual(set(case['input']['originals_required'].values()), {None})
            self.assertFalse(case['expected']['native_credit'])
            self.assertFalse(case['expected']['effect_authority_to_client'])

    def test_every_sole_fault_has_complete_literal_control(self):
        for case in CASES.values():
            if case['kind'] == 'sole_fault_negative':
                with self.subTest(case=case['id']):
                    control = CASES[case['control_id']]
                    self.assertEqual(control['kind'], 'conditional_positive')
                    self.assertEqual(changed_paths(control['input'], case['input']), [case['fault']['path']])
                    self.assertEqual(set(case['input']['facts']), {'context','scope','consent','clock','reservation','attempt'})

    def test_exact_refusals_and_outward_mapping(self):
        for case in CASES.values():
            expected = case['expected']
            with self.subTest(case=case['id']):
                pair = REFUSALS[expected['detail']] if expected['detail'] else ('PASS','completed')
                self.assertEqual((expected['result'], expected['reason_code']), pair)
                if expected['detail']:
                    self.assertEqual(expected['delta']['owner_boundary'], {'reserved_intents':0,'consumed_grants':0})
                    self.assertEqual(expected['delta']['primitive_boundary'], {'dispatches':0,'verified_effects':0})

    def test_positive_owner_fact_original_refs_are_complete_and_bounded(self):
        originals = SUBJECTS['original_owner_fact_bytes']
        def check_refs(value):
            if type(value) is dict:
                if set(value) == {'path','bytes','sha256'}:
                    raw = originals[value['path']].encode()
                    self.assertLessEqual(len(raw), 262144)
                    self.assertEqual(len(raw), value['bytes'])
                    self.assertEqual(hashlib.sha256(raw).hexdigest(), value['sha256'])
                else:
                    for item in value.values(): check_refs(item)
            elif type(value) is list:
                for item in value: check_refs(item)
        schema = json.loads((NEXT/'contracts/install/operation.schema.json').read_bytes())
        for case in CASES.values():
            if case['kind'] == 'conditional_positive':
                with self.subTest(case=case['id']):
                    facts = case['input']['facts']; check_refs(facts)
                    context = facts['context']; consent = facts['consent']; scope = facts['scope']
                    self.assertEqual(json.loads(originals[context['consent_ref']['path']]), consent)
                    self.assertEqual(json.loads(originals[consent['scope_ref']['path']]), scope)
                    self.assertEqual(json.loads(originals[facts['reservation']['context_ref']['path']]), context)
                    self.assertEqual(context['identity'], scope['identity'])
                    self.assertEqual(context['identity'], facts['attempt']['identity'])
                    self.assertEqual(set(context['effect']), set(schema['$defs']['JournalEffect']['required']))
                    self.assertEqual(set(context['identity']), set(schema['$defs']['OperationIdentity']['required']))
                    self.assertEqual(set(context['target']), set(schema['$defs']['TargetBinding']['required']))

    def test_additive_index_binds_literal_fixture(self):
        index = json.loads((FIX/'subjects.json').read_bytes())
        self.assertEqual(index['P1'], {'path': 'p1-subjects.json', 'bytes': 87342, 'sha256': '698303043bdaf3550e543b096dcd5fa75fb36e32c9eca5b742ebd3075e263883', 'revision': 'e2-runtime-tuple-v1'})
        self.assertEqual(index['case_groups'], ['P1-0'+str(i) for i in range(1,9)])
        self.assertEqual(index['GateB'], {'path':'gate-b-subjects.json','bytes':len(RAW),
                                       'sha256':hashlib.sha256(RAW).hexdigest()})
        self.assertEqual(index['gate_b_case_groups'], ['C0-'+str(i).zfill(2) for i in range(1,21)])

    def test_exact_corrected_missing_R01_case_ids(self):
        expected = {'SIG'+str(i).zfill(2) for i in range(1,16)} | {
            'SEM'+str(i).zfill(2) for i in range(1,7)} | {'ID01','ID02','ID03','BOUNDARY01','BOUNDARY02',
            'SIG02-ID','SIG07-FIRST','SIG07-GLOBAL','SIG08-REQUEST-ID','SIG08-ORIGINAL','SIG08-REENCODING',
            'SIG10-TRANSPORT','SIG10-SIGNED-SHAPE','SIG10-PHYSICAL','SIG11-AUTHENTIC-CONTEXT','SIG14-DOMAIN','CURRENT-HASH'}
        self.assertEqual({c['id'] for c in SUBJECTS['missing_R01_cases']}, expected)
        self.assertEqual(len(SUBJECTS['missing_R01_cases']), 38)
        self.assertEqual({c['status'] for c in SUBJECTS['missing_R01_cases']}, {'NOT_RUN_UNBOUND'})


class OwnerAdmissionSurface(unittest.TestCase):
    pass


ADMISSION_GROUPS = {'C0-01','C0-02','C0-03','C0-04','C0-05','C0-15','C0-16','C0-17','C0-18'}
for _case in CASES.values():
    if _case['group'] in ADMISSION_GROUPS:
        def _body(self, case=_case):
            compare_execution(self, case)
        setattr(OwnerAdmissionSurface, 'test_'+_case['id'].replace('-','_'), _body)


if __name__ == '__main__':
    unittest.main()
