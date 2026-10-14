"""Malformed source observations must stay typed; no runtime/host is used."""
import copy
import unittest
from cortex_core.release import codec
from test_source_envelopes import SourceEnvelopes


class SourceShapes(unittest.TestCase):
    def caught(self, action):
        try:
            action()
        except Exception as error:
            return error
        return None

    def test_material_value_shape_is_a_typed_refusal(self):
        helper = SourceEnvelopes(); case = helper.case(); m = case[0]
        _, facts, material, verified = helper.parsed(case)
        bad = dict(material); old = bad['toolchain']
        bad['toolchain'] = m.BoundFact(old.owner, 'not-a-toolchain', old.evidence)
        self.assertIsInstance(self.caught(lambda: m._target(verified,
            ('api', 'linux/amd64'), facts, bad)), m.SourceRefusal)

    def test_environment_nonstring_is_a_typed_refusal(self):
        helper = SourceEnvelopes(); case = helper.case(); m = case[0]
        bad = copy.deepcopy(case[5]); bad['environment_names'] = [{'SECRET': True}]
        self.assertIsInstance(self.caught(lambda: m._limits(bad)), m.SourceRefusal)

    def test_boolean_cannot_fill_an_unbound_runtime_fact(self):
        helper = SourceEnvelopes(); case = helper.case(); m = case[0]
        bad = copy.deepcopy(case[1]['api'])
        bad['targets']['linux/amd64']['runtime_facts']['service_factory']['value'] = True
        self.assertIsInstance(self.caught(lambda: m.parse_source_envelope(
            codec.canonical_bytes(bad))), m.SourceRefusal)

    def test_source_read_failure_preserves_typed_custody_and_never_plans(self):
        helper = SourceEnvelopes(); case = helper.case(); m = case[0]
        def failed(path):
            raise OSError('synthetic interruption')
        with self.assertRaises(m.SourceRefusal) as caught:
            m.plan_source_recipes({k: codec.canonical_bytes(v) for k, v in case[1].items()},
                case[2], failed, case[4], case[5])
        self.assertEqual(caught.exception.code, 'r02_fact_custody')


if __name__ == '__main__':
    unittest.main()
