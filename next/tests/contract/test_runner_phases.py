"""An assertion in a fixture is not a causal test-body mutation kill."""
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'tests'))
from test_receipts import classify, suite


class RunnerPhases(unittest.TestCase):
    def probe(self, source):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / 'test_probe.py').write_text(source)
            return suite(directory)

    def test_setup_and_teardown_assertions_are_inconclusive(self):
        for method in ('setUp', 'tearDown'):
            result = self.probe(f'''import unittest
class Probe(unittest.TestCase):
    def {method}(self):
        self.fail('fixture assertion')
    def test_expected(self):
        self.assertTrue(True)
''')
            with self.subTest(method=method):
                self.assertEqual(classify(result, {'test_probe.Probe.test_expected'}), 'inconclusive')

    def test_body_assertion_is_causal(self):
        result = self.probe('''import unittest
class Probe(unittest.TestCase):
    def test_expected(self):
        self.fail('body assertion')
''')
        self.assertEqual(classify(result, {'test_probe.Probe.test_expected'}), 'killed')
