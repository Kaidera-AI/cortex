"""Malformed source observations must stay typed; no runtime/host is used."""
import copy
import unittest
from unittest.mock import patch
from cortex_core.release import codec
import test_source_envelopes as source_tests


class SourceShapes(unittest.TestCase):
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


if __name__ == '__main__':
    unittest.main()
