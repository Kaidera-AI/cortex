"""Core2 SOURCE controls. Actual owner/vault/admission; named NONSECRET IO doubles.

No provision_sequence replacement, fake phase success or real custody/DB effects.
The unavailable fixed Nix composition is NOT RUN, not a synthetic positive.
"""
from contextlib import ExitStack
from dataclasses import replace
import contextvars
import unittest
from unittest.mock import patch

from cortex_core.runtime import custody, resources
from cortex_core.runtime.signed_member import Refusal
from cortex_core.runtime.tests.test_installer_custody import InstallerCore1Tests, PURPOSES


class InstallerStageTests(unittest.TestCase):
    setUp = InstallerCore1Tests.setUp
    config = InstallerCore1Tests.config
    opened = InstallerCore1Tests.opened

    def imported(self, owner):
        for purpose in PURPOSES:
            owner.import_credential(purpose, b'NONSECRET-' + purpose.encode())

    def test_run_rejects_non_key_before_fixed_caller(self):
        with self.opened() as owner:
            self.imported(owner)
            before = list(self.adapters.events)
            with self.assertRaises(Refusal) as caught:
                owner.run(True)
            self.assertEqual(caught.exception.reason, 'seed_operation_key')
            self.assertEqual(self.adapters.events, before)
            self.assertTrue(owner._tainted)

    def test_run_input_variants_cannot_be_callbacks_or_renew_budget(self):
        called = []
        for key in (None, 1, {}, lambda: called.append(True), '', 'x'*257, '\n', '\x7f', '\ud800'):
            with self.subTest(key_type=type(key)), self.opened() as owner:
                self.imported(owner)
                before = list(self.adapters.events)
                with self.assertRaisesRegex(Refusal, 'seed_operation_key'):
                    owner.run(key)
                self.assertEqual(before, self.adapters.events)
        self.assertEqual(called, [])

    def test_phase_guard_cannot_be_used_in_import_or_to_skip_or_repeat(self):
        self.assertTrue(callable(getattr(custody, '_installer_phase', None)))
        for phase in ('PRE','MIGRATE','POST','LEDGER','SEED','ACK','CLOSE','COMPLETE','pre',True,{}):
            with self.subTest(phase=phase), self.opened() as owner:
                self.imported(owner)
                before = list(self.adapters.events)
                with self.assertRaises(Refusal):
                    with custody._installer_phase(owner._config, custody=owner._vault,
                            deadline=owner._deadline, phase=phase):
                        self.fail('phase cannot be caller authority')
                self.assertEqual(before, self.adapters.events)
                self.assertTrue(owner._tainted)

    def test_resource_openers_and_passwords_deny_import_before_connector_or_record(self):
        for opener in (resources.open_bootstrap_resources, resources.open_migration_resources,
                       resources.open_seed_resources, resources.open_core_resources):
            with self.subTest(opener=opener.__name__), self.opened() as owner:
                self.imported(owner)
                before = list(self.adapters.events)
                with patch.object(resources, '_connect', side_effect=AssertionError('connector reached')):
                    with self.assertRaises(Refusal):
                        with opener(owner._config, custody=owner._vault, deadline=owner._deadline):
                            self.fail('IMPORT resources')
                self.assertEqual(before, self.adapters.events)
        for purpose in (*PURPOSES, 'client', True, {}):
            with self.subTest(purpose=purpose), self.opened() as owner:
                before = list(self.adapters.events)
                with self.assertRaises(Refusal):
                    custody._installer_password(owner._config, custody=owner._vault,
                            deadline=owner._deadline, purpose=purpose)
                self.assertEqual(before, self.adapters.events)

    def test_no_install_public_resolver_or_seed_accessor_after_imports(self):
        with self.opened() as owner:
            self.imported(owner)
            before = list(self.adapters.events)
            for purpose in (*PURPOSES, 'client'):
                with self.assertRaises(Refusal): owner._vault.resolve(purpose)
            with self.assertRaises(Refusal):
                with owner._vault.seed_record(): self.fail('IMPORT seed record')
            self.assertEqual(before, self.adapters.events)

    def test_identity_deadline_and_copied_context_refuse_without_io(self):
        for variant in ('config','vault','deadline','context'):
            with self.subTest(variant=variant), self.opened() as owner:
                before = list(self.adapters.events)
                config = self.config() if variant=='config' else owner._config
                vault = {} if variant=='vault' else owner._vault
                deadline = 201.0 if variant=='deadline' else owner._deadline
                def attempt():
                    with custody._installer_phase(config, custody=vault, deadline=deadline, phase='PRE'):
                        self.fail('foreign phase')
                with self.assertRaises(Refusal):
                    if variant=='context': contextvars.copy_context().run(attempt)
                    else: attempt()
                self.assertEqual(before, self.adapters.events)

    def test_expiry_blocks_run_before_caller_and_finally_closes_context(self):
        with self.opened() as owner:
            self.imported(owner)
            before = list(self.adapters.events)
            self.adapters.clock = owner._deadline
            with self.assertRaises(Refusal): owner.run('NONSECRET-operation')
            self.assertEqual(before, self.adapters.events)
        self.assertTrue(owner._closed)
        self.assertTrue(owner._vault._closed)
        self.assertIsNone(custody._INSTALLER.get())

    def test_missing_fixed_package_is_typed_non_success_and_cannot_retry(self):
        # No module/caller substitution. This checkout does not package cortex_ship.
        with self.opened() as owner:
            self.imported(owner)
            before = list(self.adapters.events)
            with self.assertRaisesRegex(Refusal, 'installer_incomplete'):
                owner.run('NONSECRET-operation')
            self.assertEqual(before, self.adapters.events)
            self.assertTrue(owner._tainted)
            with self.assertRaises(Refusal): owner.run('NONSECRET-operation')
        self.assertIsNone(custody._INSTALLER.get())

    def test_seed_and_serve_ordinary_wrong_modes_still_deny_before_io(self):
        for mode in ('seed', 'serve', 'migrate'):
            config = self.config(mode)
            for opener in (resources.open_seed_resources, resources.open_migration_resources,
                           resources.open_bootstrap_resources, resources.open_core_resources):
                expected = {'seed':'seed','migrate':'migrate','bootstrap':'seed','serve':'serve'}
                desired = opener.__name__.split('_')[1]
                if expected[desired] == mode: continue
                with self.subTest(mode=mode, opener=desired), self.assertRaises(Refusal):
                    with opener(config, custody=None, deadline=200.0): self.fail('wrong ordinary mode')
        self.assertEqual(self.adapters.events, [])


if __name__ == '__main__': unittest.main()
