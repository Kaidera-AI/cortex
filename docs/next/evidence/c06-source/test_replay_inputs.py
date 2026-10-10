"""No-resource Git custody probes for the C06 replay controller inputs."""
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


HERE = Path(__file__).resolve().parent
WT = HERE.parents[3]


class ReplayInputCustody(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder = tempfile.TemporaryDirectory(prefix='c06-replay-inputs-')
        cls.clean = Path(cls.folder.name) / 'clean'
        subprocess.run(['git', 'clone', '--quiet', '--no-checkout', '--shared', str(WT), str(cls.clean)],
                       check=True, capture_output=True, text=True)
        subprocess.run(['git', '-C', str(cls.clean), 'checkout', '--quiet', '--detach', 'HEAD'],
                       check=True, capture_output=True, text=True)

    @classmethod
    def tearDownClass(cls):
        cls.folder.cleanup()

    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix='c06-replay-bundle-')
        self.addCleanup(folder.cleanup)
        self.bundle = Path(folder.name)
        for source in HERE.glob('*.py'):
            shutil.copy2(source, self.bundle / source.name)
        shutil.copy2(HERE / 'offline-wheel-inputs.json', self.bundle / 'offline-wheel-inputs.json')

    def preflight(self):
        return subprocess.run([sys.executable, str(self.bundle / 'verify-replay-inputs.py'), str(self.clean)],
                              capture_output=True, text=True)

    def tamper(self, name):
        path = self.bundle / name
        path.write_bytes(path.read_bytes() + b'\n')
        result = self.preflight()
        self.assertNotEqual(result.returncode, 0, name + ': ' + result.stdout)

    def test_clean_committed_input_bundle_passes(self):
        result = self.preflight()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_changed_lifecycle_helper_refuses_before_resources(self):
        self.tamper('replay_lifecycle.py')

    def test_changed_comparison_script_refuses_before_resources(self):
        self.tamper('verify-fixture-contract.py')

    def test_changed_wheel_manifest_refuses_before_resources(self):
        self.tamper('offline-wheel-inputs.json')


if __name__ == '__main__':
    unittest.main()
