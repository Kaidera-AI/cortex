import importlib.util
from pathlib import Path
import unittest

s = importlib.util.spec_from_file_location('identity_producer', Path(__file__).with_name('build-manual-linux.py'))
p = importlib.util.module_from_spec(s)
s.loader.exec_module(p)


class IdentityLabel(unittest.TestCase):
    def test_all_seven_builds_explicitly_keep_builder_identity(self):
        plan = p.make_plan(Path('/fixture/source'), 'a'*40)
        self.assertEqual(len(plan['images']), 7)
        for row in plan['images']:
            self.assertEqual(row['argv'].count('--identity-label=true'), 1, row['role'])


if __name__ == '__main__':
    unittest.main()
