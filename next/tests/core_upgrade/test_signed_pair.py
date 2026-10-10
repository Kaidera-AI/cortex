"""Signed compatibility admission must finish before the migration port opens."""
import copy
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


class SignedPair(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if shutil.which('minisign') is None:
            raise unittest.SkipTest('minisign required for real signature controls')
        cls.keys = tempfile.TemporaryDirectory(prefix='cox-core-upgrade-key-')
        cls.key_dir = Path(cls.keys.name)
        result = subprocess.run(['minisign', '-G', '-W', '-p', str(cls.key_dir/'public.key'),
                                 '-s', str(cls.key_dir/'secret.key')], capture_output=True)
        assert result.returncode == 0, result.stderr

    @classmethod
    def tearDownClass(cls):
        cls.keys.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cox-core-upgrade-manifest-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.counter = 0
        self.old = {'format': 'cortex-aggregate-v1', 'release': 'v0.1.002',
                    'edition': 'standalone', 'platform': 'darwin-arm64',
                    'schema_version': 1, 'event_version': 1,
                    'modules': {'core': 'sha256:'+'1'*64, 'search': 'sha256:'+'2'*64}}
        self.new = {'format': 'cortex-aggregate-v1', 'release': 'v0.1.020',
                    'edition': 'standalone', 'platform': 'darwin-arm64',
                    'schema_version': 2, 'event_version': 1,
                    'modules': {'core': 'sha256:'+'3'*64, 'search': 'sha256:'+'4'*64}}

    def api(self):
        try:
            from cortex_core.upgrade import admit_signed_pair, UpgradeRefusal
        except ImportError:
            self.fail('Core signed-upgrade admission port is absent')
        return admit_signed_pair, UpgradeRefusal

    def sign(self, name, value):
        path = self.root / (name+'.json')
        signature = self.root / (name+'.minisig')
        path.write_bytes(canonical(value))
        result = subprocess.run(['minisign', '-S', '-W', '-s',
            str(self.key_dir/'secret.key'), '-m', str(path), '-x', str(signature)],
            capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return path, signature

    def pair(self, old=None, new=None):
        old, new = copy.deepcopy(old or self.old), copy.deepcopy(new or self.new)
        self.counter += 1
        old_path, old_sig = self.sign(f'old-{self.counter}', old)
        new_path, new_sig = self.sign(f'new-{self.counter}', new)
        policy = {'trusted_key_sha256': hashlib.sha256(
                      (self.key_dir/'public.key').read_bytes()).hexdigest(),
                  'old_release': 'v0.1.002', 'new_release': 'v0.1.020',
                  'old_manifest_sha256': hashlib.sha256(old_path.read_bytes()).hexdigest(),
                  'new_manifest_sha256': hashlib.sha256(new_path.read_bytes()).hexdigest(),
                  'edition': 'standalone', 'platform': 'darwin-arm64',
                  'old_schema': 1, 'new_schema': 2,
                  'old_event': 1, 'new_event': 1,
                  'old_modules': copy.deepcopy(self.old['modules']),
                  'new_modules': copy.deepcopy(self.new['modules']),
                  'expand_steps': ['upgrade-probe-0001', 'upgrade-probe-0002'],
                  'readers': [['v0.1.002', 1], ['v0.1.020', 2]],
                  'writers': [['v0.1.002', 1], ['v0.1.020', 2]]}
        return policy, (old_path, old_sig), (new_path, new_sig)

    def admit(self, policy, old, new):
        function, _ = self.api()
        return function(policy, old[0], old[1], new[0], new[1],
                        self.key_dir/'public.key')

    def refusal(self, policy, old, new, code):
        _, error = self.api()
        with self.assertRaises(error) as seen:
            self.admit(policy, old, new)
        self.assertEqual(seen.exception.code, code)

    def test_exact_synthetic_signatures_admit_and_writer_pairs_are_scoped(self):
        policy, old, new = self.pair()
        result = self.admit(policy, old, new)
        self.assertEqual(result.expand_steps,
                         ('upgrade-probe-0001', 'upgrade-probe-0002'))
        self.assertTrue(result.reader_allowed('v0.1.002', 1))
        self.assertTrue(result.reader_allowed('v0.1.020', 2))
        self.assertFalse(result.writer_allowed('v0.1.020', 1))
        self.assertTrue(result.writer_allowed('v0.1.020', 2))

    def test_wrong_signature_refuses(self):
        policy, old, new = self.pair()
        lines = new[1].read_text().splitlines()
        lines[1] = ('A' if lines[1][0] != 'A' else 'B') + lines[1][1:]
        new[1].write_text('\n'.join(lines)+'\n')
        self.refusal(policy, old, new, 'signature_invalid')

    def test_changed_digest_even_if_resigned_refuses(self):
        policy, old, _ = self.pair()
        changed = copy.deepcopy(self.new)
        changed['modules']['core'] = 'sha256:'+'5'*64
        new = self.sign('new', changed)
        self.refusal(policy, old, new, 'digest_mismatch')

    def test_unknown_schema_and_event_refuse(self):
        for field, code in (('schema_version', 'unknown_schema'),
                            ('event_version', 'unknown_event')):
            with self.subTest(field=field):
                changed = copy.deepcopy(self.new)
                changed[field] = 99
                policy, old, new = self.pair(new=changed)
                self.refusal(policy, old, new, code)

    def test_downgrade_refuses(self):
        changed = copy.deepcopy(self.new)
        changed['release'] = 'v0.1.001'
        policy, old, new = self.pair(new=changed)
        policy['new_release'] = 'v0.1.001'
        self.refusal(policy, old, new, 'downgrade')

    def test_mixed_platform_refuses(self):
        changed = copy.deepcopy(self.new)
        changed['platform'] = 'linux-amd64'
        policy, old, new = self.pair(new=changed)
        self.refusal(policy, old, new, 'mixed_platform')

    def test_incompatible_module_set_refuses(self):
        changed = copy.deepcopy(self.new)
        changed['modules']['search'] = 'sha256:'+'7'*64
        policy, old, new = self.pair(new=changed)
        self.refusal(policy, old, new, 'module_set_incompatible')

    def test_untrusted_key_refuses(self):
        policy, old, new = self.pair()
        policy['trusted_key_sha256'] = '0'*64
        self.refusal(policy, old, new, 'untrusted_key')


if __name__ == '__main__':
    unittest.main()
