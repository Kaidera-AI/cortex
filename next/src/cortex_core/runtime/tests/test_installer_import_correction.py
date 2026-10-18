"""Independent adversarial source controls; all I/O remains named NONSECRET doubles."""
import contextvars
from contextlib import contextmanager
from dataclasses import replace
import json
import unittest
from unittest.mock import patch
from cortex_core.runtime import custody, custody_fs, bootstrap_config
from cortex_core.runtime.signed_member import Refusal
from cortex_core.runtime.tests import test_installer_custody as fixture

class HaleCore1(unittest.TestCase):
    def witness(self, name, owner):
        print('WITNESS='+json.dumps({'case':name,'events':self.adapters.events,'record_names':[k[1] for k in self.adapters.records],'clock':self.adapters.clock,'imported':sorted(owner._imported),'tainted':owner._tainted}),flush=True)

    setUp=fixture.InstallerCore1Tests.setUp
    config=fixture.InstallerCore1Tests.config
    opened=fixture.InstallerCore1Tests.opened

    def test_expiry_after_lock_must_prevent_record_io(self):
        @contextmanager
        def expire(directory,name):
            with self.adapters.lock(directory,name):
                self.adapters.clock=201.0
                yield
        with self.opened() as owner:
            start=len(self.adapters.events)
            with patch.object(custody_fs,'record_lock',expire),self.assertRaisesRegex(Refusal,'installer_deadline'):
                owner.import_credential('probe',b'NONSECRET-probe')
            events=self.adapters.events[start:]
            self.witness('expiry_after_lock',owner)
            self.assertEqual(owner._imported,frozenset())
            self.assertNotIn('read-double',events,events)
            self.assertEqual(self.adapters.records,{},events)

    def test_expiry_after_absent_read_must_prevent_publication(self):
        original=self.adapters.read
        def expire(directory,name):
            result=original(directory,name)
            self.adapters.clock=201.0
            return result
        with self.opened() as owner:
            with patch.object(custody_fs,'read_record',expire),self.assertRaisesRegex(Refusal,'installer_deadline'):
                owner.import_credential('probe',b'NONSECRET-probe')
            self.witness(self._testMethodName,owner)
            self.assertEqual(self.adapters.records,{},self.adapters.events)

    def test_tainted_after_reentry_must_prevent_publication(self):
        @contextmanager
        def reenter(directory,name):
            with self.adapters.lock(directory,name):
                with self.assertRaises(Refusal):
                    owner.import_credential('migrator',b'NONSECRET-migrator')
                self.assertTrue(owner._tainted)
                yield
        with self.opened() as owner:
            with patch.object(custody_fs,'record_lock',reenter),self.assertRaises(Refusal):
                owner.import_credential('probe',b'NONSECRET-probe')
            self.witness(self._testMethodName,owner)
            self.assertEqual(self.adapters.records,{},self.adapters.events)

    def test_direct_private_import_reentry_must_refuse(self):
        original=self.adapters.write
        entered=False
        def reenter(directory,name,raw,*,expected):
            nonlocal entered
            if not entered:
                entered=True
                try:
                    owner._vault._import_role(owner._config.roles['migrator'].secret_ref,
                                             'migrator',b'NONSECRET-migrator')
                except Refusal:
                    pass
                else:
                    self.witness('internal_reentry_accepted',owner)
                    self.fail('private nested import returned normally; second record published')
            return original(directory,name,raw,expected=expected)
        with self.opened() as owner:
            with patch.object(custody_fs,'write_record',reenter):
                try: owner.import_credential('probe',b'NONSECRET-probe')
                except Refusal: pass
            self.assertNotIn((self.value['vault_root'],'migrator'),self.adapters.records)


class ImportBoundaryControls(unittest.TestCase):
    """Fault only the existing memory IO, noncrypto cipher and clock adapters."""
    setUp = fixture.InstallerCore1Tests.setUp
    config = fixture.InstallerCore1Tests.config
    opened = fixture.InstallerCore1Tests.opened

    def invalidate(self, owner, fault):
        if fault == 'expiry':
            self.adapters.clock = 201.0
        else:
            with self.assertRaises(Refusal):
                owner.import_credential('migrator', b'NONSECRET-migrator')
            self.assertTrue(owner._tainted)

    def assert_no_credit_or_retry(self, owner):
        self.assertEqual(owner._imported, frozenset())
        self.assertTrue(owner._tainted)
        before = list(self.adapters.events)
        with self.assertRaises(Refusal):
            owner.import_credential('probe', b'NONSECRET-probe')
        with self.assertRaises(Refusal):
            owner.run('NONSECRET-operation')
        self.assertEqual(self.adapters.events, before)

    def test_invalidation_at_each_memory_boundary_stops_new_io(self):
        for fault in ('expiry', 'taint'):
            for boundary in ('directory', 'lock', 'absent-read', 'seal', 'write', 'reload'):
                with self.subTest(fault=fault, boundary=boundary):
                    self.adapters.records.clear()
                    self.adapters.clock = 100.0
                    with self.opened() as owner:
                        cut = []
                        def invalidate():
                            self.invalidate(owner, fault)
                            cut.append(len(self.adapters.events))

                        original_check = self.adapters.directory_class.check
                        def check(directory):
                            original_check(directory)
                            invalidate()

                        @contextmanager
                        def lock(directory, name):
                            with self.adapters.lock(directory, name):
                                invalidate()
                                yield

                        reads = 0
                        def read(directory, name):
                            nonlocal reads
                            value = self.adapters.read(directory, name)
                            reads += 1
                            if (boundary == 'absent-read' and reads == 1
                                    or boundary == 'reload' and reads == 2):
                                invalidate()
                            return value

                        original_encrypt = self.adapters.cipher_class.encrypt
                        def encrypt(cipher, nonce, plain, aad):
                            raw = original_encrypt(cipher, nonce, plain, aad)
                            invalidate()
                            return raw

                        def write(directory, name, raw, *, expected):
                            value = self.adapters.write(directory, name, raw, expected=expected)
                            invalidate()
                            return value

                        targets = {
                            'directory': (self.adapters.directory_class, 'check', check),
                            'lock': (custody_fs, 'record_lock', lock),
                            'absent-read': (custody_fs, 'read_record', read),
                            'seal': (self.adapters.cipher_class, 'encrypt', encrypt),
                            'write': (custody_fs, 'write_record', write),
                            'reload': (custody_fs, 'read_record', read),
                        }
                        target, name, adapter = targets[boundary]
                        with patch.object(target, name, adapter), self.assertRaises(Refusal):
                            owner.import_credential('probe', b'NONSECRET-probe')
                        self.assertTrue(cut, 'fault must actually execute')
                        self.assertEqual(self.adapters.events[cut[0]:], [], self.adapters.events)
                        # Publication already completed at write/reload: no rollback claim.
                        expected_records = {(self.value['vault_root'], 'probe')} if boundary in ('write', 'reload') else set()
                        self.assertEqual(set(self.adapters.records), expected_records)
                        self.assert_no_credit_or_retry(owner)

    def test_replay_invalidation_after_read_stops_authentication(self):
        for fault in ('expiry', 'taint'):
            with self.subTest(fault=fault):
                self.adapters.records.clear()
                self.adapters.clock = 100.0
                with self.opened() as owner:
                    owner.import_credential('probe', b'NONSECRET-probe')
                saved = dict(self.adapters.records)
                with self.opened() as owner:
                    cut = []
                    def read(directory, name):
                        value = self.adapters.read(directory, name)
                        self.invalidate(owner, fault)
                        cut.append(len(self.adapters.events))
                        return value
                    with patch.object(custody_fs, 'read_record', read), self.assertRaises(Refusal):
                        owner.import_credential('probe', b'NONSECRET-probe')
                    self.assertEqual(self.adapters.events[cut[0]:], [])
                    self.assertEqual(self.adapters.records, saved)
                    self.assert_no_credit_or_retry(owner)

    def test_invalidation_after_authentication_keeps_completed_record_without_credit(self):
        for fault in ('expiry', 'taint'):
            with self.subTest(fault=fault):
                self.adapters.records.clear()
                self.adapters.clock = 100.0
                original = self.adapters.cipher_class.decrypt
                with self.opened() as owner:
                    cut = []
                    def decrypt(cipher, nonce, token, aad):
                        plain = original(cipher, nonce, token, aad)
                        self.invalidate(owner, fault)
                        cut.append(len(self.adapters.events))
                        return plain
                    with patch.object(self.adapters.cipher_class, 'decrypt', decrypt), self.assertRaises(Refusal):
                        owner.import_credential('probe', b'NONSECRET-probe')
                    self.assertEqual(self.adapters.events[cut[0]:], [])
                    self.assertEqual(set(self.adapters.records), {(self.value['vault_root'], 'probe')})
                    self.assert_no_credit_or_retry(owner)

    def private_reentry(self, purpose):
        original = self.adapters.write
        entered = False
        with self.opened() as owner:
            def write(directory, name, raw, *, expected):
                nonlocal entered
                if not entered:
                    entered = True
                    before = list(self.adapters.events)
                    saved = dict(self.adapters.records)
                    with self.assertRaises(Refusal):
                        owner._vault._import_role(owner._config.roles[purpose].secret_ref,
                                                purpose, b'NONSECRET-' + purpose.encode())
                    self.assertEqual(self.adapters.events, before)
                    self.assertEqual(self.adapters.records, saved)
                # This already-initiated outer IO may finish; no second import may begin.
                return original(directory, name, raw, expected=expected)
            with patch.object(custody_fs, 'write_record', write), self.assertRaises(Refusal):
                owner.import_credential('probe', b'NONSECRET-probe')
            self.assertTrue(entered)
            self.assertEqual(set(self.adapters.records), {(self.value['vault_root'], 'probe')})
            self.assert_no_credit_or_retry(owner)

    def test_same_role_private_recursion_refuses_before_io(self):
        self.private_reentry('probe')

    def test_other_role_private_recursion_refuses_before_io(self):
        self.private_reentry('migrator')
