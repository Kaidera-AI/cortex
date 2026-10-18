"""Core1 source controls: real owner/vault; NONSECRET input/FS/cipher doubles.

No real unlock, descriptor, key, cipher, encrypted record or install effect runs.
The memory and cipher adapters prove source composition only, never J06 custody.
"""
import contextvars
from contextlib import contextmanager, ExitStack
from dataclasses import replace
import hashlib
import json
import pickle
import sys
from types import ModuleType
import unittest
from unittest.mock import patch

from cortex_core.release.codec import canonical_bytes
from cortex_core.runtime import bootstrap_config, custody, custody_fs, credentials
from cortex_core.runtime.bootstrap_config import BootstrapConfig, decode_config
from cortex_core.runtime.signed_member import Refusal
from cortex_core.runtime.tests.test_bootstrap_config import sample

PURPOSES = ('bootstrap', 'migrator', 'api_request', 'probe', 'installer_enrollment')


class SourceAdapters:
    """Explicit NONSECRET adapter doubles; no owner/vault/decoder substitution."""
    def __init__(self):
        self.events = []
        self.records = {}
        self.payloads = {}
        self.directories = []
        self.write_error = self.close_error = self.unlock_error = None
        self.clock = 100.0
        adapters = self

        class MemoryDirectory:
            def __init__(self, root):
                self.root, self.closed = root, False
                adapters.events.append('directory')
                adapters.directories.append(self)

            def check(self):
                adapters.events.append('check')
                if self.closed:
                    raise Refusal('custody_closed')

            def close(self):
                self.closed = True
                adapters.events.append('close')
                if adapters.close_error:
                    raise adapters.close_error

        class NonCryptographicCipher:
            def __init__(self, nonsecret_input):
                assert nonsecret_input == b'NONSECRET-input-adapter'
                adapters.events.append('cipher')

            def encrypt(self, nonce, plain, aad):
                adapters.events.append('seal-double')
                token = b'NONSECRET-cipher-double-' + str(len(adapters.payloads)).encode()
                adapters.payloads[token] = (nonce, plain, aad)
                return token

            def decrypt(self, nonce, token, aad):
                adapters.events.append('open-double')
                expected_nonce, plain, expected_aad = adapters.payloads[token]
                if (nonce, aad) != (expected_nonce, expected_aad):
                    raise ValueError('synthetic binding mismatch')
                return plain

        self.directory_class = MemoryDirectory
        self.cipher_class = NonCryptographicCipher

    def unlock(self, fd):
        self.events.append('unlock-double')
        if self.unlock_error:
            raise self.unlock_error
        assert fd == 99  # sentinel only: never opened or read
        return b'NONSECRET-input-adapter'

    @contextmanager
    def lock(self, directory, name):
        directory.check()
        self.events.append('lock-double')
        yield

    def read(self, directory, name):
        directory.check()
        self.events.append('read-double')
        raw = self.records.get((directory.root, name))
        return None if raw is None else (raw, hashlib.sha256(raw).digest())

    def write(self, directory, name, raw, *, expected):
        directory.check()
        self.events.append('write-double')
        if self.write_error:
            raise self.write_error
        current = self.read(directory, name)
        if (None if current is None else current[1]) != expected:
            raise Refusal('custody_changed')
        self.records[directory.root, name] = raw
        return hashlib.sha256(raw).digest()

    @contextmanager
    def installed(self):
        module = ModuleType('cryptography.hazmat.primitives.ciphers.aead')
        module.AESGCM = self.cipher_class
        with ExitStack() as stack:
            stack.enter_context(patch.dict(sys.modules, {module.__name__: module}))
            for name, double in [('read_unlock', self.unlock), ('PrivateDirectory', self.directory_class),
                                 ('record_lock', self.lock), ('read_record', self.read), ('write_record', self.write)]:
                stack.enter_context(patch.object(custody_fs, name, double))
            stack.enter_context(patch.object(custody.os, 'urandom', lambda n: b'N' * n))
            # Deterministic monotonic input is not a renewed installer budget.
            stack.enter_context(patch.object(custody, 'time', create=True))
            custody.time.monotonic.side_effect = lambda: self.clock
            yield self


class InstallerCore1Tests(unittest.TestCase):
    def setUp(self):
        self.value = sample('/source-only/NONSECRET-vault')
        self.raw = json.dumps(self.value).encode()
        self.adapters = SourceAdapters()
        self.stack = ExitStack()
        self.stack.enter_context(self.adapters.installed())
        self.addCleanup(self.stack.close)

    def config(self, mode='install'):
        return decode_config(self.raw, mode=mode)

    def opened(self, config=None, deadline=200.0):
        self.assertTrue(callable(getattr(custody, 'open_installer_custody', None)),
                        'finite private installer owner is required')
        return custody.open_installer_custody(self.config() if config is None else config,
                            unlock_fd=99, key_id='NONSECRET-id', deadline=deadline)

    def test_install_decoder_retains_exact_five_and_identity(self):
        try:
            config = self.config()
        except Refusal as error:
            self.fail('existing closed decoder must admit install metadata: ' + str(error))
        self.assertIs(type(config), BootstrapConfig)
        self.assertEqual(config.mode, 'install')
        self.assertEqual(set(config.roles), set(PURPOSES))
        self.assertEqual(config.source_sha256, self.config('serve').source_sha256)
        self.assertEqual(config.source_sha256, hashlib.sha256(canonical_bytes(self.value)).hexdigest())
        self.assertEqual(dict(config.role_logins), {n: r['login'] for n, r in self.value['roles'].items()})
        for purpose in PURPOSES:
            self.assertEqual(config.roles[purpose].secret_ref, self.value['roles'][purpose]['secret_ref'])
        self.assertEqual(self.adapters.events, [])

    def test_install_file_decoder_is_same_closed_decoder(self):
        with patch.object(bootstrap_config, 'read_private_file', return_value=self.raw) as reader:
            config = bootstrap_config.read_config_file('/NONSECRET/config', mode='install')
        reader.assert_called_once()
        self.assertEqual(config, self.config())
        changed = {**self.value, 'approval': True}
        with self.assertRaises(Refusal):
            decode_config(json.dumps(changed).encode(), mode='install')
        with self.assertRaises(TypeError):
            config.roles['extra'] = config.roles['probe']

    def test_no_plaintext_resolver_or_public_vault_unlock_for_install(self):
        config = self.config()
        with patch.object(credentials, 'read_private_file', side_effect=AssertionError('plaintext read')):
            for purpose in (*PURPOSES, 'client', 'other'):
                with self.subTest(purpose=purpose), self.assertRaises(Refusal):
                    credentials.resolve_credential(config, purpose)
        with self.assertRaises(Refusal):
            custody.CustodyVault(config, unlock_fd=99, key_id='NONSECRET-id')
        with self.assertRaises(Refusal):
            with custody.open_custody(config, unlock_fd=99, key_id='NONSECRET-id'):
                self.fail('public install vault')
        self.assertEqual(self.adapters.events, [])

    def test_exact_private_owner_and_one_real_vault(self):
        config = self.config()
        with self.opened(config) as owner:
            self.assertIs(type(owner), custody.InstallerCustody)
            self.assertIs(type(owner._vault), custody.CustodyVault)
            self.assertIs(owner._config, config)
            self.assertIs(owner._vault._config, config)
            self.assertIs(custody._current_installer(config, owner._vault, owner._deadline), owner)
            self.assertEqual(self.adapters.events.count('unlock-double'), 1)
            self.assertEqual(self.adapters.events.count('directory'), 1)
            self.assertNotIn('NONSECRET', repr(owner))
            with self.assertRaises((Refusal, TypeError)):
                pickle.dumps(owner)
            with self.assertRaises((Refusal, TypeError)):
                custody.InstallerCustody()
            for name in ('resolve', 'seed_record', 'connection', 'set_phase', 'close'):
                self.assertFalse(hasattr(owner, name), name)
            with self.assertRaises(AttributeError):
                owner._deadline = 999.0

    def test_foreign_dict_subclass_and_wrong_mode_refuse_before_unlock(self):
        config = self.config()
        class ForeignConfig(BootstrapConfig):
            pass
        variants = [{}, object(), replace(config, mode='serve'),
                    ForeignConfig(**config.__dict__)]
        for candidate in variants:
            with self.subTest(candidate=type(candidate)), self.assertRaises(Refusal):
                with self.opened(candidate):
                    self.fail('foreign config admitted')
        self.assertEqual(self.adapters.events, [])

    def test_deadline_is_finite_nonbool_original_absolute_and_not_renewed(self):
        for deadline in [True, False, None, '200', float('nan'), float('inf'), -float('inf'), 99.0, 100.0]:
            with self.subTest(deadline=deadline), self.assertRaises(Refusal):
                with self.opened(deadline=deadline):
                    self.fail('bad deadline')
        self.assertEqual(self.adapters.events, [])
        with self.opened() as owner:
            before = list(self.adapters.events)
            with self.assertRaises(Refusal):
                custody._current_installer(owner._config, owner._vault, 201.0)
            self.assertEqual(before, self.adapters.events)
            self.adapters.clock = 200.0
            with self.assertRaises(Refusal):
                owner.import_credential('probe', b'NONSECRET-probe')
            self.assertEqual(before, self.adapters.events)

    def test_nesting_is_refused_before_second_unlock(self):
        with self.opened():
            before = list(self.adapters.events)
            with self.assertRaises(Refusal):
                with self.opened():
                    self.fail('nested context')
            self.assertEqual(before, self.adapters.events)

    def test_foreign_equal_config_vault_and_forged_owner_refuse_before_io(self):
        with self.opened() as owner:
            config, vault, deadline = owner._config, owner._vault, owner._deadline
            other_config = self.config()
            self.assertEqual(config, other_config)
            for c, v in [(other_config, vault), (config, {}), (config, object.__new__(custody.CustodyVault))]:
                before = list(self.adapters.events)
                with self.subTest(vault_type=type(v)), self.assertRaises(Refusal):
                    custody._current_installer(c, v, deadline)
                self.assertEqual(before, self.adapters.events)
            forged = object.__new__(custody.InstallerCustody)
            with self.assertRaises(Refusal):
                forged.import_credential('probe', b'NONSECRET')
            with self.assertRaises(Refusal):
                custody.InstallerCustody.import_credential({}, 'probe', b'NONSECRET')

    def test_copied_or_foreign_context_denied_before_io(self):
        with self.opened() as owner:
            before = list(self.adapters.events)
            for context in [contextvars.Context(), contextvars.copy_context()]:
                with self.assertRaises(Refusal):
                    context.run(custody._current_installer, owner._config, owner._vault, owner._deadline)
                self.assertEqual(before, self.adapters.events)
            self.assertIs(custody._current_installer(owner._config, owner._vault, owner._deadline), owner)

    def test_all_five_actual_imports_and_authenticated_replay(self):
        with self.opened() as owner:
            for purpose in PURPOSES:
                owner.import_credential(purpose, b'NONSECRET-' + purpose.encode())
            self.assertEqual(owner._imported, frozenset(PURPOSES))
            self.assertEqual(len(self.adapters.records), 5)
            self.assertEqual(self.adapters.events.count('write-double'), 5)
            saved = dict(self.adapters.records)
            for purpose in PURPOSES:
                owner.import_credential(purpose, b'NONSECRET-' + purpose.encode())
            self.assertEqual(saved, self.adapters.records)
            self.assertEqual(self.adapters.events.count('write-double'), 5)
            self.assertGreaterEqual(self.adapters.events.count('open-double'), 5)
        # A fresh context must authenticate the actual stored records again.
        with self.opened() as owner:
            self.assertEqual(owner._imported, frozenset())
            for purpose in PURPOSES:
                owner.import_credential(purpose, b'NONSECRET-' + purpose.encode())
            self.assertEqual(owner._imported, frozenset(PURPOSES))

    def test_changed_bytes_taint_owner_and_keep_original_record(self):
        with self.opened() as owner:
            owner.import_credential('probe', b'NONSECRET-original')
            saved = dict(self.adapters.records)
            with self.assertRaisesRegex(Refusal, 'custody_changed'):
                owner.import_credential('probe', b'NONSECRET-changed')
            self.assertEqual(saved, self.adapters.records)
            before = list(self.adapters.events)
            with self.assertRaises(Refusal):
                owner.import_credential('probe', b'NONSECRET-original')
            self.assertEqual(before, self.adapters.events)

    def test_invalid_purpose_and_bytes_refuse_before_record_io(self):
        for purpose, data in [('client', b'N'), ('other', b'N'), (True, b'N'),
                              ('probe', ''), ('probe', b''), ('probe', b'N' * 4097)]:
            with self.subTest(purpose=purpose, size=len(data)), self.opened() as owner:
                before = list(self.adapters.events)
                with self.assertRaises(Refusal):
                    owner.import_credential(purpose, data)
                self.assertEqual(before, self.adapters.events)
                self.assertEqual(owner._imported, frozenset())

    def test_import_cannot_use_public_resolution_seed_or_resources(self):
        from cortex_core.runtime import resources
        with self.opened() as owner:
            vault = owner._vault
            before = list(self.adapters.events)
            for purpose in (*PURPOSES, 'client'):
                with self.assertRaises(Refusal):
                    vault.resolve(purpose)
                with self.assertRaises(Refusal):
                    vault.import_credential(purpose, b'NONSECRET')
            with self.assertRaises(Refusal):
                with vault.seed_record():
                    self.fail('seed during IMPORT')
            # Existing resource exact config-mode gates must refuse before connector IO.
            with patch.object(resources, '_connect', side_effect=AssertionError('DB reached')):
                for opener in (resources.open_bootstrap_resources, resources.open_seed_resources,
                               resources.open_migration_resources, resources.open_core_resources):
                    with self.assertRaises(Refusal):
                        with opener(owner._config, custody=vault, deadline=owner._deadline):
                            self.fail('IMPORT connection')
            self.assertEqual(before, self.adapters.events)
            # Actual private replay must not delegate to the public resolver.
            with patch.object(custody.CustodyVault, 'resolve', side_effect=AssertionError('public resolve')):
                owner.import_credential('probe', b'NONSECRET-probe')
                owner.import_credential('probe', b'NONSECRET-probe')

    def test_failed_import_never_receives_credit_and_cannot_retry(self):
        with self.opened() as owner:
            self.adapters.write_error = Refusal('custody_write_failed')
            with self.assertRaises(Refusal):
                owner.import_credential('probe', b'NONSECRET')
            self.assertEqual(owner._imported, frozenset())
            self.adapters.write_error = None
            before = list(self.adapters.events)
            with self.assertRaises(Refusal):
                owner.import_credential('probe', b'NONSECRET')
            self.assertEqual(before, self.adapters.events)

    def test_corrupt_replay_never_receives_credit(self):
        with self.opened() as owner:
            owner.import_credential('probe', b'NONSECRET')
        self.adapters.records[self.value['vault_root'], 'probe'] = b'NONSECRET-corrupt-record'
        with self.opened() as owner:
            with self.assertRaisesRegex(Refusal, 'custody_invalid'):
                owner.import_credential('probe', b'NONSECRET')
            self.assertEqual(owner._imported, frozenset())

    def test_run_is_typed_non_success_with_zero_and_all_imports(self):
        for complete in (False, True):
            with self.opened() as owner:
                if complete:
                    for purpose in PURPOSES:
                        owner.import_credential(purpose, b'NONSECRET-' + purpose.encode())
                before = list(self.adapters.events)
                with self.assertRaisesRegex(Refusal, 'installer_(imports_incomplete|incomplete)'):
                    owner.run('NONSECRET-operation')
                self.assertEqual(before, self.adapters.events)

    def test_ordinary_error_interrupt_and_systemexit_close_context_and_handles(self):
        for error in (RuntimeError('NONSECRET-body'), KeyboardInterrupt(), SystemExit(7)):
            with self.subTest(error=type(error)), self.assertRaises(type(error)):
                with self.opened() as owner:
                    vault = owner._vault
                    raise error
            self.assertTrue(vault._closed)
            self.assertIsNone(vault._directory)
            before = list(self.adapters.events)
            with self.assertRaises(Refusal):
                owner.import_credential('probe', b'NONSECRET')
            with self.assertRaises(Refusal):
                owner.run('NONSECRET-operation')
            self.assertEqual(before, self.adapters.events)
            with self.opened():
                pass  # original context reset permits a fresh independent invocation

    def test_cleanup_failure_is_typed_non_success_and_context_is_reset(self):
        self.adapters.close_error = OSError('NONSECRET-close-error')
        with self.assertRaisesRegex(Refusal, 'installer_cleanup'):
            with self.opened() as owner:
                vault = owner._vault
        self.assertTrue(vault._closed)
        self.assertIsNone(vault._directory)
        before = list(self.adapters.events)
        with self.assertRaises(Refusal):
            owner.import_credential('probe', b'NONSECRET')
        self.assertEqual(before, self.adapters.events)
        self.adapters.close_error = None
        with self.opened():
            pass

    def test_partial_unlock_failure_resets_context_without_false_owner(self):
        self.adapters.unlock_error = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            with self.opened():
                self.fail('unlock failed')
        self.adapters.unlock_error = None
        with self.opened() as owner:
            self.assertIs(type(owner), custody.InstallerCustody)

    def test_ordinary_vault_source_regression_with_same_adapter_doubles(self):
        for mode, allowed in [('seed', ('bootstrap', 'installer_enrollment', 'api_request')),
                              ('migrate', ('migrator',)), ('serve', ('api_request', 'probe')),
                              ('status', ()), ('client', ())]:
            config = self.config(mode)
            with custody.open_custody(config, unlock_fd=99, key_id='NONSECRET-id') as vault:
                for purpose in allowed:
                    data = b'NONSECRET-' + purpose.encode()
                    vault.import_credential(purpose, data)
                    vault.import_credential(purpose, data)
                    self.assertEqual(vault.resolve(purpose), data)
                for purpose in set(PURPOSES) - set(allowed):
                    before = list(self.adapters.events)
                    with self.assertRaises(Refusal):
                        vault.resolve(purpose)
                    self.assertEqual(before, self.adapters.events)
                if mode != 'seed':
                    with self.assertRaises(Refusal):
                        with vault.seed_record():
                            self.fail('ordinary seed widening')
            with self.assertRaises(Refusal):
                vault.resolve('api_request')


if __name__ == '__main__':
    unittest.main()
