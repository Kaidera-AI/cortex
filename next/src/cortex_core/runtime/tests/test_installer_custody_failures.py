"""Additional Core1 fault controls; same named NONSECRET adapter boundaries."""
import contextvars
from contextlib import contextmanager
import unittest
from unittest.mock import patch

from cortex_core.runtime import custody, custody_fs
from cortex_core.runtime.signed_member import Refusal
from cortex_core.runtime.tests import test_installer_custody as core1

PURPOSES = core1.PURPOSES


class InstallerFailureTests(unittest.TestCase):
    # Reuse only the source adapter fixture, without inheriting deciding tests.
    setUp = core1.InstallerCore1Tests.setUp
    config = core1.InstallerCore1Tests.config
    opened = core1.InstallerCore1Tests.opened

    def test_ordinary_seed_prepared_commit_and_client_replay_with_adapters(self):
        from cortex_core.runtime.tests.test_custody import intent, receipt, SECRET
        expected = intent(self.value)
        ack = receipt(expected)
        with custody.open_custody(self.config('seed'), unlock_fd=99, key_id='NONSECRET-id') as vault:
            with vault.seed_record() as record:
                material = record.create(intent=expected, credential=SECRET)
                self.assertEqual(material.state, 'prepared')
            with self.assertRaises(Refusal):
                record.load()
        with custody.open_custody(self.config('client'), unlock_fd=99, key_id='NONSECRET-id') as vault:
            with self.assertRaisesRegex(Refusal, 'custody_uncommitted'):
                vault.resolve('client')
        with custody.open_custody(self.config('seed'), unlock_fd=99, key_id='NONSECRET-id') as vault:
            with vault.seed_record() as record:
                self.assertEqual(record.load().intent, expected)
                material = record.commit(receipt=ack)
                self.assertEqual(material.state, 'committed')
                self.assertEqual(record.commit(receipt=ack).receipt, ack)
        with custody.open_custody(self.config('client'), unlock_fd=99, key_id='NONSECRET-id') as vault:
            self.assertEqual(vault.resolve('client'), SECRET)

    def test_invalid_descriptor_and_key_id_are_denied_before_input_adapter(self):
        config = self.config()
        for fd, key_id in [(True, 'ok'), (-1, 'ok'), (2, 'ok'), (99, ''), (99, '../bad'), (99, None)]:
            with self.subTest(fd=fd, key_id=key_id), self.assertRaises(Refusal):
                with custody.open_installer_custody(config, unlock_fd=fd, key_id=key_id, deadline=200.0):
                    self.fail('bad input admission')
        self.assertEqual(self.adapters.events, [])

    def test_owner_and_vault_subclasses_cannot_gain_authority(self):
        class ForeignOwner(custody.InstallerCustody):
            pass
        class ForeignVault(custody.CustodyVault):
            pass
        with self.opened() as owner:
            before = list(self.adapters.events)
            forged = object.__new__(ForeignOwner)
            with self.assertRaises(Refusal):
                forged.import_credential('probe', b'NONSECRET')
            with self.assertRaises(Refusal):
                custody._current_installer(owner._config, object.__new__(ForeignVault), owner._deadline)
            self.assertEqual(before, self.adapters.events)

    def test_private_unlock_helper_cannot_birth_install_without_owner(self):
        config = self.config()
        vault = object.__new__(custody.CustodyVault)
        with self.assertRaises(Refusal):
            custody._unlock_vault(vault, config, unlock_fd=99, key_id='NONSECRET-id')
        self.assertEqual(self.adapters.events, [])
        with self.opened() as owner:
            before = list(self.adapters.events)
            with self.assertRaises(Refusal):
                custody._unlock_vault(owner._vault, owner._config, unlock_fd=99, key_id='NONSECRET-id')
            self.assertEqual(before, self.adapters.events)

    def test_private_role_path_requires_actual_busy_owner_import(self):
        with self.opened() as owner:
            before = list(self.adapters.events)
            with self.assertRaises(Refusal):
                owner._vault._import_role(owner._config.roles['probe'].secret_ref, 'probe', b'NONSECRET')
            self.assertEqual(before, self.adapters.events)
            self.assertEqual(owner._imported, frozenset())

    def test_partial_directory_and_cipher_failure_reset_context(self):
        for boundary, target in [('directory', custody_fs), ('cipher', None)]:
            with self.subTest(boundary=boundary):
                if target:
                    failing = patch.object(target, 'PrivateDirectory', side_effect=SystemExit(8))
                else:
                    import sys
                    module = sys.modules['cryptography.hazmat.primitives.ciphers.aead']
                    failing = patch.object(module, 'AESGCM', side_effect=ValueError('NONSECRET-cipher'))
                with failing, self.assertRaises((Refusal, SystemExit)):
                    with self.opened():
                        self.fail('partial birth returned')
                with self.opened():
                    pass

    def test_import_interrupts_invalidate_retained_owner(self):
        for error in [KeyboardInterrupt(), SystemExit(9), RuntimeError('NONSECRET-io')]:
            with self.subTest(error=type(error)), self.opened() as owner:
                self.adapters.write_error = error
                with self.assertRaises(type(error)):
                    owner.import_credential('probe', b'NONSECRET')
                self.assertEqual(owner._imported, frozenset())
                self.adapters.write_error = None
                before = list(self.adapters.events)
                with self.assertRaises(Refusal):
                    owner.import_credential('probe', b'NONSECRET')
                self.assertEqual(before, self.adapters.events)

    def test_import_credit_requires_authenticated_post_write_readback(self):
        original = self.adapters.write
        def corrupt(directory, name, raw, *, expected):
            stamp = original(directory, name, raw, expected=expected)
            self.adapters.records[directory.root, name] = b'NONSECRET-corrupt'
            return stamp
        with self.opened() as owner:
            with patch.object(custody_fs, 'write_record', corrupt), self.assertRaises(Refusal):
                owner.import_credential('probe', b'NONSECRET')
            self.assertEqual(owner._imported, frozenset())

    def test_lock_cleanup_failure_does_not_grant_import_credit(self):
        @contextmanager
        def failing_lock(directory, name):
            with self.adapters.lock(directory, name):
                yield
            raise Refusal('NONSECRET-lock-cleanup')
        with self.opened() as owner:
            with patch.object(custody_fs, 'record_lock', failing_lock), self.assertRaises(Refusal):
                owner.import_credential('probe', b'NONSECRET')
            self.assertEqual(owner._imported, frozenset())

    def test_deadline_elapsed_during_import_cannot_receive_credit(self):
        original = self.adapters.write
        def expiry(directory, name, raw, *, expected):
            result = original(directory, name, raw, expected=expected)
            self.adapters.clock = 201.0
            return result
        with self.opened() as owner:
            with patch.object(custody_fs, 'write_record', expiry), self.assertRaisesRegex(Refusal, 'installer_deadline'):
                owner.import_credential('probe', b'NONSECRET')
            self.assertEqual(owner._imported, frozenset())

    def test_reentrant_import_is_denied_and_outer_operation_is_tainted(self):
        original = self.adapters.write
        with self.opened() as owner:
            def reentry(directory, name, raw, *, expected):
                before = list(self.adapters.events)
                with self.assertRaises(Refusal):
                    owner.import_credential('migrator', b'NONSECRET')
                self.assertEqual(before, self.adapters.events)
                return original(directory, name, raw, expected=expected)
            with patch.object(custody_fs, 'write_record', reentry), self.assertRaises(Refusal):
                owner.import_credential('probe', b'NONSECRET')
            self.assertEqual(owner._imported, frozenset())

    def test_cleanup_failure_during_body_error_still_invalidates(self):
        self.adapters.close_error = OSError('NONSECRET-close')
        with self.assertRaisesRegex(Refusal, 'installer_cleanup'):
            with self.opened() as owner:
                raise KeyboardInterrupt()
        self.assertTrue(owner._closed)
        self.assertTrue(owner._vault._closed)
        self.assertIsNone(owner._vault._directory)
        self.adapters.close_error = None
        with self.opened():
            pass

    def test_retained_vault_in_foreign_context_denied_before_directory_check(self):
        with self.opened() as owner:
            before = list(self.adapters.events)
            for context in [contextvars.Context(), contextvars.copy_context()]:
                with self.assertRaises(Refusal):
                    context.run(owner._vault._live)
                self.assertEqual(before, self.adapters.events)

    def test_no_successful_run_even_for_other_operation_inputs_or_repeats(self):
        for operation in [None, True, {}, lambda: True, 'NONSECRET-operation']:
            with self.opened() as owner:
                for purpose in PURPOSES:
                    owner.import_credential(purpose, b'NONSECRET-' + purpose.encode())
                with self.assertRaises(Refusal):
                    owner.run(operation)
                before = list(self.adapters.events)
                with self.assertRaises(Refusal):
                    owner.run('NONSECRET-operation')
                self.assertEqual(before, self.adapters.events)


if __name__ == '__main__':
    unittest.main()
