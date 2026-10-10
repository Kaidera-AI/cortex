"""C06 publication completeness controls; no product imports or live target."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


HERE = Path(__file__).resolve().parent
WT = HERE.parents[3]
VERIFY = HERE / 'verify-publication.py'
INDEX = HERE / 'publication-index-005.json'


class PublicationCompleteness(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder = tempfile.TemporaryDirectory(prefix='c06-publication-reference-')
        cls.clean = Path(cls.folder.name) / 'clean'
        subprocess.run(['git', 'clone', '--quiet', '--no-checkout', '--shared', str(WT), str(cls.clean)],
                       check=True, capture_output=True, text=True)
        subprocess.run(['git', '-C', str(cls.clean), 'checkout', '--quiet', '--detach', 'HEAD'],
                       check=True, capture_output=True, text=True)

    @classmethod
    def tearDownClass(cls):
        cls.folder.cleanup()

    def run_index(self, index):
        return subprocess.run([sys.executable, str(VERIFY), str(index), str(self.clean), str(WT)],
                              capture_output=True, text=True)

    def test_full_published_inventory_passes(self):
        result = self.run_index(INDEX)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_omitted_member_refuses_even_when_supplied_digests_match(self):
        data = json.loads(INDEX.read_text())
        omitted = 'docs/next/evidence/c06-source/reproduce.md'
        self.assertIn(omitted, data['members'])
        data['members'].pop(omitted)
        with tempfile.TemporaryDirectory() as folder:
            copied = Path(folder) / 'index.json'
            copied.write_text(json.dumps(data))
            result = self.run_index(copied)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn('expected_inventory_mismatch', result.stderr)

    def test_tampered_member_digest_refuses(self):
        data = json.loads(INDEX.read_text())
        member = 'next/src/cortex_core/records.py'
        self.assertIn(member, data['members'])
        data['members'][member]['sha256'] = '0' * 64
        with tempfile.TemporaryDirectory() as folder:
            copied = Path(folder) / 'index.json'
            copied.write_text(json.dumps(data))
            result = self.run_index(copied)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn('published_digest', result.stderr)


if __name__ == '__main__':
    unittest.main()
