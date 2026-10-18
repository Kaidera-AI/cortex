"""J1 Core source controls; memory IO/noncryptographic cipher, no actual key.

The real decoder, owner, adapter and record code execute. These controls are not
host identity, cryptography, native process isolation, restart or installed proof.
"""
import contextvars
import copy
from contextlib import ExitStack
import hashlib
import json
import pickle
import unittest
from unittest.mock import patch

from cortex_core.release.codec import canonical_bytes
from cortex_core.runtime import bootstrap_config, custody
from cortex_core.runtime.bootstrap_config import decode_config
from cortex_core.runtime.signed_member import Refusal
from cortex_core.runtime.tests.test_bootstrap_config import sample
from cortex_core.runtime.tests.test_installer_custody import SourceAdapters, PURPOSES

SUBJECT = '10000000-0000-0000-0000-000000000004'
OTHER = '10000000-0000-0000-0000-000000000005'
PREIMAGE = b'a' * 64  # public NONSECRET fixture, never an issued credential


class ProjectCustodyTests(unittest.TestCase):
    def setUp(self):
        self.value = sample('/source-only/NONSECRET-project-vault')
        self.value['custody_unlock'] = dict(kind='project-key-file',
            path='/source-only/NONSECRET-key/NONSECRET-project.key',
            key_id='NONSECRET-project', identity='NONSECRET_project',
            project_id=self.value['project_id'])
        self.adapters = SourceAdapters()
        self.stack = ExitStack()
        self.stack.enter_context(self.adapters.installed())
        self.addCleanup(self.stack.close)

    def config(self, value=None, mode='project'):
        try:
            return decode_config(json.dumps(self.value if value is None else value).encode(), mode=mode)
        except Refusal as error:
            self.fail('closed project custody carrier must be implemented: ' + str(error))

    def opened(self, config=None, **kwargs):
        opener = getattr(custody, 'open_project_custody', None)
        self.assertTrue(callable(opener), 'finite project owner must be implemented')
        return opener(self.config() if config is None else config, unlock_fd=99,
            key_id=kwargs.pop('key_id', 'NONSECRET-project'),
            installation_key_id=kwargs.pop('installation_key_id', 'NONSECRET-installation'),
            deadline=kwargs.pop('deadline', 200.0), **kwargs)

    def intent(self, purpose='project_worker', subject=SUBJECT):
        return dict(installation_id=self.value['installation_id'], tenant_id=self.value['tenant_id'],
            project_id=self.value['project_id'], purpose=purpose, subject_id=subject,
            operation_key='NONSECRET-operation', credential_sha256=hashlib.sha256(PREIMAGE).hexdigest())

    def receipt(self, intent):
        return {**intent, 'intent_sha256': hashlib.sha256(canonical_bytes(intent)).hexdigest()}

    def prepare(self, owner, purpose='project_worker', subject=SUBJECT):
        intent = self.intent(purpose, subject)
        with owner.record(purpose, subject_id=subject) as record:
            material = record.create(intent=intent, credential=PREIMAGE)
        return intent, material

    def test_decoder_carries_exact_immutable_reference_without_effect(self):
        config = self.config()
        self.assertEqual(dict(config.custody_unlock), self.value['custody_unlock'])
        self.assertEqual(dict(config.roles), {})
        self.assertEqual(config.source_sha256, hashlib.sha256(canonical_bytes(self.value)).hexdigest())
        with self.assertRaises(TypeError): config.custody_unlock['identity'] = 'changed'
        self.assertEqual(self.adapters.events, [])
        self.assertEqual(self.config(mode='install').source_sha256, config.source_sha256)

    def test_reference_closed_shape_and_project_binding_refuse_before_io(self):
        self.config()
        variants = []
        for key in self.value['custody_unlock']:
            value = copy.deepcopy(self.value); del value['custody_unlock'][key]; variants.append(value)
        for key, value in [('extra', True), ('kind', 'installation-key-file'), ('key_id', '../bad'),
                           ('identity', 'name with space'), ('project_id', OTHER), ('identity', None)]:
            changed = copy.deepcopy(self.value); changed['custody_unlock'][key] = value; variants.append(changed)
        for path in ['relative/NONSECRET-project.key', '//keys/NONSECRET-project.key',
                     '/keys/../NONSECRET-project.key', '/keys/./NONSECRET-project.key',
                     '/keys//NONSECRET-project.key', '/keys/wrong.key',
                     '/keys/NONSECRET-project.key/', '/keys/\x00/NONSECRET-project.key']:
            changed = copy.deepcopy(self.value); changed['custody_unlock']['path'] = path; variants.append(changed)
        changed = copy.deepcopy(self.value); del changed['custody_unlock']; variants.append(changed)
        for value in variants:
            with self.subTest(value=value), self.assertRaises(Refusal):
                decode_config(json.dumps(value).encode(), mode='project')
        self.assertEqual(self.adapters.events, [])

    def test_legacy_config_remains_exact_and_unknown_key_refused(self):
        value = sample('/source-only/NONSECRET-installation')
        config = decode_config(json.dumps(value).encode(), mode='serve')
        self.assertEqual(set(config.roles), {'api_request', 'probe'})
        value['unknown'] = True
        with self.assertRaises(Refusal): decode_config(json.dumps(value).encode(), mode='serve')

    def test_finite_owner_uses_one_actual_adapter_and_no_public_birth(self):
        config = self.config()
        with self.assertRaises(Refusal): custody.CustodyVault(config, unlock_fd=99, key_id='NONSECRET-project')
        with self.opened(config) as owner:
            self.assertIs(type(owner), custody.ProjectCustody)
            self.assertIs(type(owner._vault), custody.CustodyVault)
            self.assertIs(owner._config, config)
            with self.assertRaises((TypeError, Refusal)): custody.ProjectCustody()
            with self.assertRaises((TypeError, Refusal)): pickle.dumps(owner)
            with self.assertRaises(AttributeError): owner._deadline = 999.0
        self.assertEqual(self.adapters.events.count('unlock-double'), 1)
        self.assertTrue(all(directory.closed for directory in self.adapters.directories))

    def test_project_handover_one_session_private_records(self):
        with self.opened() as owner:
            for purpose, subject in [('project_enrollment', self.value['project_id']),
                                     ('project_lead', SUBJECT), ('project_worker', OTHER)]:
                expected, material = self.prepare(owner, purpose, subject)
                self.assertEqual(material.state, 'prepared')
                with owner.record(purpose, subject_id=subject) as record:
                    saved = record.load()
                    self.assertEqual(saved.intent, expected)
                    self.assertEqual(saved.credential, PREIMAGE)
                    receipt = self.receipt(expected)
                    committed = record.commit(receipt=receipt)
                    self.assertEqual(committed.state, 'committed')
                    self.assertEqual(record.commit(receipt=receipt).generation, committed.generation)
        self.assertEqual(len(self.adapters.records), 3)
        self.assertTrue(all(PREIMAGE not in raw for raw in self.adapters.records.values()))

    def test_installation_role_seed_client_and_unknown_purposes_deny_without_io(self):
        with self.opened() as owner:
            before = list(self.adapters.events)
            for purpose in (*PURPOSES, 'seed', 'client', 'other', None):
                with self.subTest(purpose=purpose), self.assertRaises(Refusal):
                    with owner.record(purpose, subject_id=SUBJECT): self.fail('foreign purpose admitted')
            self.assertEqual(self.adapters.events, before)
        self.assertEqual(self.adapters.records, {})

    def test_wrong_key_or_installation_key_refuses_before_unlock(self):
        for kwargs in [dict(key_id='wrong'), dict(installation_key_id='NONSECRET-project'),
                       dict(installation_key_id=''), dict(installation_key_id=None)]:
            with self.subTest(kwargs=kwargs), self.assertRaises(Refusal):
                with self.opened(**kwargs): self.fail('wrong key admission')
        self.assertEqual(self.adapters.events, [])

    def test_original_deadline_closed_owner_and_retained_record_refuse_without_io(self):
        with self.opened() as owner:
            expected, _ = self.prepare(owner)
            with owner.record('project_worker', subject_id=SUBJECT) as record:
                record.load(); self.adapters.clock = 200.0
                before = list(self.adapters.events)
                with self.assertRaises(Refusal): record.commit(receipt=self.receipt(expected))
                self.assertEqual(self.adapters.events, before)
        before = list(self.adapters.events)
        with self.assertRaises(Refusal): record.load()
        with self.assertRaises(Refusal):
            with owner.record('project_worker', subject_id=SUBJECT): self.fail('closed owner')
        self.assertEqual(self.adapters.events, before)

    def test_copied_context_and_foreign_process_refuse_before_io(self):
        for foreign in ('context', 'process'):
            with self.subTest(foreign=foreign), self.opened() as owner:
                before = list(self.adapters.events)
                def use():
                    with owner.record('project_worker', subject_id=SUBJECT): self.fail('foreign context')
                with self.assertRaises(Refusal):
                    if foreign == 'context': contextvars.copy_context().run(use)
                    else:
                        with patch.object(custody.os, 'getpid', return_value=-1): use()
                self.assertEqual(self.adapters.events, before)

    def test_project_and_subject_intent_confusion_refuse_without_write(self):
        with self.opened() as owner:
            for key in ('installation_id', 'tenant_id', 'project_id', 'purpose', 'subject_id', 'credential_sha256'):
                changed = self.intent(); changed[key] = 'changed'
                with self.subTest(key=key), owner.record('project_worker', subject_id=SUBJECT) as record:
                    with self.assertRaises(Refusal): record.create(intent=changed, credential=PREIMAGE)
        self.assertEqual(self.adapters.records, {})

    def test_changed_receipt_does_not_commit_preimage(self):
        with self.opened() as owner:
            intent, _ = self.prepare(owner)
            for key in (*intent, 'intent_sha256'):
                receipt = self.receipt(intent); receipt[key] = 'changed'
                with self.subTest(key=key), owner.record('project_worker', subject_id=SUBJECT) as record:
                    self.assertEqual(record.load().state, 'prepared')
                    with self.assertRaises(Refusal): record.commit(receipt=receipt)
            with owner.record('project_worker', subject_id=SUBJECT) as record:
                self.assertEqual(record.load().state, 'prepared')

    def test_foreign_project_ciphertext_cannot_be_opened(self):
        with self.opened() as owner: self.prepare(owner)
        changed = copy.deepcopy(self.value)
        changed['project_id'] = OTHER; changed['project_names'] = {'other': OTHER}
        changed['custody_unlock']['project_id'] = OTHER
        with self.opened(self.config(changed)) as owner:
            with owner.record('project_worker', subject_id=SUBJECT) as record:
                with self.assertRaises(Refusal): record.load()

    def test_reentry_expiry_after_lock_and_failed_write_taint(self):
        original_lock = self.adapters.lock
        def expiry_lock(directory, name):
            context = original_lock(directory, name)
            class Expiry:
                def __enter__(self): context.__enter__(); self_clock = 200.0; self_outer.adapters.clock = self_clock
                def __exit__(self, *args): return context.__exit__(*args)
            self_outer = self
            return Expiry()
        with self.opened() as owner:
            with owner.record('project_worker', subject_id=SUBJECT):
                before = list(self.adapters.events)
                with self.assertRaises(Refusal):
                    with owner.record('project_worker', subject_id=OTHER): self.fail('nested record')
                self.assertEqual(self.adapters.events, before)
        self.adapters.clock = 100.0
        with self.opened() as owner, patch.object(custody.custody_fs, 'record_lock', expiry_lock):
            with self.assertRaises(Refusal):
                with owner.record('project_worker', subject_id=SUBJECT): self.fail('expired lock')
        self.assertNotIn('read-double', self.adapters.events)
        self.adapters.clock = 100.0
        with self.opened() as owner:
            self.adapters.write_error = Refusal('synthetic_write')
            with self.assertRaises(Refusal): self.prepare(owner)
            before = list(self.adapters.events)
            with self.assertRaises(Refusal): self.prepare(owner)
            self.assertEqual(self.adapters.events, before)

    def test_closed_record_handle_and_changed_replay_deny(self):
        with self.opened() as owner:
            intent, _ = self.prepare(owner)
            with owner.record('project_worker', subject_id=SUBJECT) as record:
                saved = record.load()
                committed = record.commit(receipt=self.receipt(intent))
                with self.assertRaises(Refusal): record.create(intent=intent, credential=PREIMAGE)
                self.assertNotIn('aaaa', repr(saved))
                self.assertEqual(committed.generation, 1)
            before = list(self.adapters.events)
            with self.assertRaises(Refusal): record.load()
            self.assertEqual(self.adapters.events, before)


if __name__ == '__main__': unittest.main()
