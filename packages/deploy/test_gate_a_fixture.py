import importlib.util
from pathlib import Path
import unittest

spec=importlib.util.spec_from_file_location('fixture',Path(__file__).with_name('gate-a-fixture.py'))
f=importlib.util.module_from_spec(spec);spec.loader.exec_module(f)

class FixtureTests(unittest.TestCase):
    def test_serialized_fixture_has_no_private_key_and_is_labelled(self):
        value=f.sign_test_manifest(b'{}')
        self.assertEqual(set(value),{'public_key','signature','trust','qualification'})
        self.assertEqual(value['trust'],'TEST_ONLY_GATE_A')
        self.assertIs(value['qualification'],False)
        self.assertIn('TEST ONLY',value['signature'])

if __name__=='__main__':unittest.main()
