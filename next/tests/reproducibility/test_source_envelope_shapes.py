"""Malformed source observations must stay typed; no runtime/host is used."""
import copy
import unittest
from unittest.mock import patch
from cortex_core.release import codec
import test_source_envelopes as source_tests


class SourceShapes(unittest.TestCase):
    def replace_value(self, case, row, value):
        row['value'] = value
        raw = codec.canonical_bytes(value)
        case[7][row['evidence']['path']] = raw
        row['evidence'].update(bytes=len(raw), sha256=codec.sha256(raw))

    def caught(self, action):
        try:
            action()
        except Exception as error:
            return error
        return None

    def test_material_value_shape_is_a_typed_refusal(self):
        helper = source_tests.SourceEnvelopes(); case = helper.case(); m = case[0]
        _, facts, material, verified = helper.parsed(case)
        bad = dict(material); old = bad['toolchain']
        bad['toolchain'] = m.BoundFact(old.owner, 'not-a-toolchain', old.evidence)
        self.assertIsInstance(self.caught(lambda: m._target(verified,
            ('api', 'linux/amd64'), facts, bad)), m.SourceRefusal)

    def test_environment_nonstring_is_a_typed_refusal(self):
        helper = source_tests.SourceEnvelopes(); case = helper.case(); m = case[0]
        bad = copy.deepcopy(case[5]); bad['environment_names'] = [{'SECRET': True}]
        self.assertIsInstance(self.caught(lambda: m._limits(bad)), m.SourceRefusal)

    def test_boolean_cannot_fill_an_unbound_runtime_fact(self):
        helper = source_tests.SourceEnvelopes(); case = helper.case(); m = case[0]
        bad = copy.deepcopy(case[1]['api'])
        bad['targets']['linux/amd64']['runtime_facts']['service_factory']['value'] = True
        self.assertIsInstance(self.caught(lambda: m.parse_source_envelope(
            codec.canonical_bytes(bad))), m.SourceRefusal)

    def test_source_read_failure_preserves_typed_custody_and_never_plans(self):
        helper = source_tests.SourceEnvelopes(); case = helper.case(); m = case[0]
        def failed(path):
            raise OSError('synthetic interruption')
        with self.assertRaises(m.SourceRefusal) as caught:
            m.plan_source_recipes({k: codec.canonical_bytes(v) for k, v in case[1].items()},
                case[2], failed, case[4], case[5])
        self.assertEqual(caught.exception.code, 'r02_fact_custody')

    def test_reader_cannot_replace_checked_policy_roots_or_limits(self):
        helper = source_tests.SourceEnvelopes(); case = helper.case()
        original_root = case[4][('api', 'linux/amd64')]['A']['cache']
        changed = False
        def observe(path):
            nonlocal changed
            if not changed:
                changed = True
                case[2][('api', 'linux/amd64')].policy['qualification_scope'] = 'native'
                case[4][('api', 'linux/amd64')]['A']['cache'] = '/changed/source'
                case[5]['output_bytes'] = 0
            return case[3](path)
        result = case[0].plan_source_recipes(
            {k: codec.canonical_bytes(v) for k, v in case[1].items()},
            case[2], observe, case[4], case[5])
        request = next(x for x in result.requests
                       if (x.module, x.platform) == ('api', 'linux/amd64'))
        self.assertTrue(changed)
        self.assertEqual(request.roots['A']['cache'], original_root)
        self.assertEqual(result.limits['output_bytes'], 1048576)
        self.assertFalse(result.native_qualified)

    def test_fact_change_after_all_four_selections_refuses(self):
        helper = source_tests.SourceEnvelopes(); case = helper.case(); m = case[0]
        key = 'facts/api/amd64/config.json'; observations = 0
        def observe(path):
            nonlocal observations
            if path == key:
                observations += 1
                if observations == 2:
                    case[7][key] = b'"changed-after-selection"\n'
            return case[3](path)
        with self.assertRaises(m.SourceRefusal) as caught:
            m.plan_source_recipes({k: codec.canonical_bytes(v) for k, v in case[1].items()},
                case[2], observe, case[4], case[5])
        self.assertEqual(observations, 2)
        self.assertEqual(caught.exception.code, 'r02_fact_custody')

    def test_unknown_envelope_obligation_refuses_before_decode(self):
        helper = source_tests.SourceEnvelopes(); case = helper.case(); m = case[0]
        raw = {k: codec.canonical_bytes(v) for k, v in case[1].items()}
        raw['extra'] = b'not an admitted envelope'
        with patch.object(m, 'parse_source_envelope', side_effect=AssertionError('decode ran')) as decode:
            error = self.caught(lambda: m.plan_source_recipes(
                raw, case[2], case[3], case[4], case[5]))
        self.assertIsInstance(error, m.SourceRefusal)
        self.assertEqual(decode.call_count, 0)

    def test_engine_platform_and_abi_bindings_refuse_at_public_entry(self):
        changes = (('base', 'platform', 'linux/arm64'),
                   ('toolchain', 'engine_id', 'foreign-engine'),
                   ('toolchain', 'sha256', '0' * 64),
                   ('toolchain', 'abi', 'foreign-abi'),
                   ('image_members', 'platform', 'linux/arm64'),
                   ('image_members', 'abi', 'foreign-abi'))
        for material, field, value in changes:
            with self.subTest(material=material, field=field):
                helper = source_tests.SourceEnvelopes(); case = helper.case(); m = case[0]
                row = case[1]['api']['targets']['linux/amd64']['materials'][material]
                self.replace_value(case, row, {**row['value'], field: value})
                with self.assertRaises(m.SourceRefusal) as caught:
                    helper.plan(case)
                self.assertEqual(caught.exception.code, 'r02_target_binding')

    def test_abi_and_members_cannot_be_boolean_claims(self):
        for field in ('abi', 'members'):
            with self.subTest(field=field):
                helper = source_tests.SourceEnvelopes(); case = helper.case(); m = case[0]
                rows = case[1]['api']['targets']['linux/amd64']['materials']
                row = rows['image_members']
                self.replace_value(case, row, {**row['value'], field: True})
                if field == 'abi':
                    row = rows['toolchain']
                    self.replace_value(case, row, {**row['value'], 'abi': True})
                with self.assertRaises(m.SourceRefusal):
                    helper.plan(case)

    def test_nested_roots_and_secret_environment_refuse_before_readers(self):
        for kind in ('nested', 'environment'):
            with self.subTest(kind=kind):
                helper = source_tests.SourceEnvelopes(); case = helper.case(); m = case[0]
                if kind == 'nested':
                    roots = case[4][('api', 'linux/amd64')]
                    roots['B']['store'] = roots['A']['store'] + '/child'
                else:
                    case[5]['environment_names'] = ['API_KEY']
                with self.assertRaises(m.SourceRefusal):
                    helper.plan(case)
                self.assertEqual(case[6], [])

    def test_missing_material_and_owner_refuse_before_readers(self):
        for kind in ('material', 'owner'):
            with self.subTest(kind=kind):
                helper = source_tests.SourceEnvelopes(); case = helper.case(); m = case[0]
                target = case[1]['api']['targets']['linux/amd64']
                if kind == 'material':
                    target['materials']['base'].update(state='UNBOUND', value=None, evidence=None)
                else:
                    target['runtime_facts']['service_factory']['owner'] = 'caller'
                with self.assertRaises(m.SourceRefusal):
                    helper.plan(case)
                self.assertEqual(case[6], [])


if __name__ == '__main__':
    unittest.main()
