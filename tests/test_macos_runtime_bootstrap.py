"""R70 focused bootstrap contract; no network, compiler or product state."""
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/release/bootstrap-macos-runtime.py'


class BootstrapContract(unittest.TestCase):
    def module(self):
        spec = importlib.util.spec_from_file_location('native_bootstrap', SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_workflow_uses_native_pinned_runtime(self):
        workflow = (ROOT / '.github/workflows/cortex-candidate.yml').read_text()
        self.assertNotIn('actions/setup-python', workflow)
        self.assertIn('bootstrap-macos-runtime.py', workflow)
        self.assertIn('$RUNNER_TEMP/cortex-native-runtime/venv/bin/python', workflow)
        self.assertIn('PIP_CERT: /etc/ssl/cert.pem', workflow)
        host = (ROOT / 'scripts/release/build-candidate.py').read_text().split('def host(', 1)[1].split('def compose_projection', 1)[0]
        self.assertNotIn('["python3"', host)
        self.assertIn('sys.executable', host)
        self.assertIn('runtime-inventory.json', host)

    def test_inputs_are_exact_and_describable_without_build(self):
        result = subprocess.run([sys.executable, str(SCRIPT), '--describe'], capture_output=True, text=True, check=True)
        inputs = json.loads(result.stdout)
        self.assertEqual(inputs['deployment_target'], '14.0')
        self.assertEqual(inputs['target'], 'macos-arm64')
        self.assertEqual(inputs['python']['version'], '3.12.14')
        self.assertEqual(inputs['python']['sha256'], '5c8462af5790baf43a321a1559dbe0db06d1be4300fb85fb53c40060668e548a')
        self.assertEqual(inputs['openssl']['version'], '3.5.8')
        self.assertEqual(inputs['openssl']['sha256'], 'a8f84a39918ec6415ce765d9b429d313ba97b8143169c172e734b9514464f5b2')

    def test_bad_download_digest_refuses_before_extraction(self):
        module = self.module()
        with tempfile.TemporaryDirectory(dir=os.environ['TMPDIR']) as directory:
            path = Path(directory) / 'source.tar'
            path.write_bytes(b'public invalid fixture archive')
            with self.assertRaisesRegex(RuntimeError, 'digest'):
                module.verify_archive(path, '0' * 64)
            module.verify_archive(path, hashlib.sha256(path.read_bytes()).hexdigest())

    def test_native_tool_environment_has_no_homebrew_discovery(self):
        env = self.module().build_environment(Path('/tmp/public-bootstrap'), {'PATH': '/opt/homebrew/bin', 'PYTHONPATH': '/opt/homebrew/python', 'LDFLAGS': '-L/opt/homebrew/lib', 'CPATH': '/opt/homebrew/include'})
        self.assertEqual(env['PATH'], '/usr/bin:/bin:/usr/sbin:/sbin')
        self.assertEqual(env['MACOSX_DEPLOYMENT_TARGET'], '14.0')
        self.assertEqual(env['PKG_CONFIG'], '/usr/bin/false')
        for key in ('PYTHONPATH', 'LDFLAGS', 'CPATH'):
            self.assertNotIn(key, env)


if __name__ == '__main__':
    unittest.main()
