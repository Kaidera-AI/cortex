"""RED-first synthetic minisign admission on the current Core source line."""

import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


SCHEMA = Path(__file__).resolve().parents[2] / 'schema' / 'manifest.json'
STEPS = ['auth-0004', 'auth-0005', 'auth-0006', 'ingest-head-0006']


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


class SignedAdmission(unittest.TestCase):
    def setUp(self):
        cases = Path(os.environ['SIGNED_CASE_ROOT'])
        self.temp = tempfile.TemporaryDirectory(prefix='case-', dir=cases)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.public = self.root / 'public.key'
        self.secret = self.root / 'secret.key'
        self.binary = Path(os.environ['SIGNED_MINISIGN_BIN'])
        self.home = Path(os.environ['HOME'])
        log = Path(os.environ['SIGNED_KEY_PATH_LOG'])
        with log.open('a') as output:
            output.write(json.dumps({'test': self.id(), 'public': str(self.public),
                                     'secret': str(self.secret), 'home': str(self.home),
                                     'binary': str(self.binary)}, sort_keys=True) + '\n')
        self.minisign('-G', '-W', '-p', self.public, '-s', self.secret)
        self.counter = 0
        self.old = {'format': 'cortex-aggregate-v1', 'release': 'v0.1.002',
                    'edition': 'standalone', 'platform': 'darwin-arm64',
                    'schema_version': 1, 'event_version': 1,
                    'modules': {'core': 'sha256:' + '1' * 64,
                                'search': 'sha256:' + '2' * 64}}
        self.new = {'format': 'cortex-aggregate-v1', 'release': 'v0.1.020',
                    'edition': 'standalone', 'platform': 'darwin-arm64',
                    'schema_version': 2, 'event_version': 1,
                    'modules': {'core': 'sha256:' + '3' * 64,
                                'search': 'sha256:' + '4' * 64}}

    def minisign(self, *args):
        command = [str(self.binary), *(str(value) for value in args)]
        if '-W' not in command or '-f' in command or not ({'-s', '-p'} & set(command)):
            raise RuntimeError('minisign test command omitted isolated key flags')
        result = subprocess.run(command, capture_output=True, timeout=10,
                                env=os.environ.copy())
        if result.returncode:
            raise RuntimeError(f'minisign fixture failed: {result.stderr[-300:]}')
        return result

    def api(self):
        try:
            from cortex_core.upgrade import UpgradeRefusal, admit_signed_pair
        except ImportError:
            self.fail('current Core signed admission port is absent')
        return UpgradeRefusal, admit_signed_pair

    def sign(self, name, value):
        path = self.root / f'{name}.json'
        signature = self.root / f'{name}.minisig'
        path.write_bytes(canonical(value))
        self.minisign('-S', '-W', '-s', self.secret,
                      '-m', path, '-x', signature)
        return path, signature

    def pair(self, *, old=None, new=None):
        self.counter += 1
        old = copy.deepcopy(self.old if old is None else old)
        new = copy.deepcopy(self.new if new is None else new)
        old_path, old_sig = self.sign(f'old-{self.counter}', old)
        new_path, new_sig = self.sign(f'new-{self.counter}', new)
        policy = {
            'trusted_key_sha256': hashlib.sha256(self.public.read_bytes()).hexdigest(),
            'old_release': old['release'], 'new_release': new['release'],
            'old_manifest_sha256': hashlib.sha256(old_path.read_bytes()).hexdigest(),
            'new_manifest_sha256': hashlib.sha256(new_path.read_bytes()).hexdigest(),
            'edition': 'standalone', 'platform': 'darwin-arm64',
            'old_schema': 1, 'new_schema': 2, 'old_event': 1, 'new_event': 1,
            'old_modules': copy.deepcopy(self.old['modules']),
            'new_modules': copy.deepcopy(self.new['modules']),
            'expand_steps': STEPS.copy(),
            'readers': [['v0.1.002', 1], ['v0.1.020', 2]],
            'writers': [['v0.1.002', 1], ['v0.1.020', 2]],
        }
        return policy, (old_path, old_sig), (new_path, new_sig)

    def admit(self, policy, old, new, *, binary=None):
        _, function = self.api()
        try:
            return function(policy, old[0], old[1], new[0], new[1],
                            self.public, self.binary if binary is None else binary)
        except TypeError as error:
            self.fail(f'explicit minisign binary port is absent: {error}')

    def refusal(self, policy, old, new, code, *, binary=None):
        error, _ = self.api()
        with self.assertRaises(error) as caught:
            self.admit(policy, old, new, binary=binary)
        self.assertEqual(caught.exception.code, code)

    def test_exact_synthetic_signatures_admit_current_steps(self):
        policy, old, new = self.pair()
        manifest = json.loads(SCHEMA.read_text())
        self.assertEqual([row['id'] for row in manifest['migrations'][15:]], STEPS)
        real_run = subprocess.run
        verify_argv = []
        def observe(command, *args, **kwargs):
            if '-V' in command:
                verify_argv.append(tuple(command))
            return real_run(command, *args, **kwargs)
        with patch('cortex_core.upgrade.subprocess.run', side_effect=observe):
            admission = self.admit(policy, old, new)
        self.assertEqual(len(verify_argv), 2)
        secret_flags = {'-W', '-s', '-S', '-G', '-f'}
        for index, command in enumerate(verify_argv):
            with self.subTest(verification=index):
                self.assertFalse(secret_flags.intersection(command),
                                 'verify argv contains a secret-key flag')
        self.assertEqual(admission.expand_steps, tuple(STEPS))
        self.assertEqual((admission.old_release, admission.new_release),
                         ('v0.1.002', 'v0.1.020'))
        self.assertEqual((admission.old_schema, admission.new_schema), (1, 2))
        self.assertEqual((getattr(admission, 'old_event', None),
                          getattr(admission, 'new_event', None)), (1, 1))
        self.assertEqual(admission.readers,
                         (('v0.1.002', 1), ('v0.1.020', 2)))
        self.assertEqual(admission.writers,
                         (('v0.1.002', 1), ('v0.1.020', 2)))
        self.assertTrue(admission.reader_allowed('v0.1.002', 1))
        self.assertFalse(admission.reader_allowed('v0.1.020', 1))
        self.assertTrue(admission.writer_allowed('v0.1.020', 2))
        self.assertFalse(admission.writer_allowed('v0.1.020', 1))

    def test_absent_old_signed_origin_and_policy_fail_closed(self):
        policy, old, new = self.pair()
        self.refusal(None, old, new, 'invalid_policy')
        old[0].unlink()  # A launcher checksum is not an old signed aggregate.
        self.refusal(policy, old, new, 'signature_unavailable')

    def test_relative_or_missing_minisign_binary_refuses_without_path_lookup(self):
        policy, old, new = self.pair()
        self.refusal(policy, old, new, 'signature_unavailable', binary='minisign')
        self.refusal(policy, old, new, 'signature_unavailable',
                     binary=self.root / 'missing-minisign')

    def test_wrong_signature_refuses(self):
        policy, old, new = self.pair()
        lines = new[1].read_text().splitlines()
        lines[1] = ('A' if lines[1][0] != 'A' else 'B') + lines[1][1:]
        new[1].write_text('\n'.join(lines) + '\n')
        self.refusal(policy, old, new, 'signature_invalid')

    def test_changed_digest_even_if_resigned_refuses(self):
        policy, old, _ = self.pair()
        changed = copy.deepcopy(self.new)
        changed['modules']['core'] = 'sha256:' + '5' * 64
        new = self.sign('changed-new', changed)
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
        self.refusal(policy, old, new, 'downgrade')

    def test_mixed_platform_refuses(self):
        changed = copy.deepcopy(self.new)
        changed['platform'] = 'linux-amd64'
        policy, old, new = self.pair(new=changed)
        self.refusal(policy, old, new, 'mixed_platform')

    def test_incompatible_module_set_refuses(self):
        changed = copy.deepcopy(self.new)
        changed['modules']['search'] = 'sha256:' + '7' * 64
        policy, old, new = self.pair(new=changed)
        self.refusal(policy, old, new, 'module_set_incompatible')

    def test_untrusted_key_refuses(self):
        policy, old, new = self.pair()
        policy['trusted_key_sha256'] = '0' * 64
        self.refusal(policy, old, new, 'untrusted_key')

    def test_new_writer_on_old_schema_refuses(self):
        policy, old, new = self.pair()
        policy['writers'].append(['v0.1.020', 1])
        self.refusal(policy, old, new, 'incompatible_clients')


if __name__ == '__main__':
    unittest.main()
