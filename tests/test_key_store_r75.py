"""Frozen r75 regressions. Mac unit checks never load Security.framework."""
from __future__ import annotations

import ctypes as C
import io
import os
import secrets
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cortex_v2.clients import key_store, mac_keychain
from cortex_v2.clients.key_store import KeyStore, KeyStoreError, KeyMetadata, _KeyRecord
from cortex_v2.cli.keys import human_main


class LinuxMissingStore(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve())
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'keys'
        self.platform = patch.object(key_store, '_is_linux', return_value=True)
        self.platform.start()
        self.addCleanup(self.platform.stop)
        self.store = KeyStore('installation', root=self.root)

    def layout(self, depth):
        paths = [self.root, self.root / 'installation', self.root / 'installation/project']
        for path in paths[:depth]:
            path.mkdir(mode=0o700)

    def test_absent_root_status_empty_noncreating(self):
        self.assertEqual(self.store.due(), [])
        self.assertFalse(self.root.exists())

    def test_absent_installation_status_empty_noncreating(self):
        self.layout(1)
        self.assertEqual(self.store.due(), [])
        self.assertEqual(list(self.root.iterdir()), [])

    def test_empty_installation_status_empty(self):
        self.layout(2)
        self.assertEqual(self.store.due(), [])
        self.assertEqual(list((self.root / 'installation').iterdir()), [])

    def test_cli_empty_exit_zero_without_creating_state(self):
        out, err = io.StringIO(), io.StringIO()
        self.assertEqual(human_main(['status'], store=self.store, out=out, err=err), 0)
        self.assertEqual((out.getvalue(), err.getvalue()), ('', ''))
        self.assertFalse(self.root.exists())

    def test_delete_missing_root_installation_project_and_file(self):
        for depth in range(4):
            with self.subTest(depth=depth):
                self.root = Path(self.temp.name) / f'keys-{depth}'
                self.store = KeyStore('installation', root=self.root)
                self.layout(depth)
                self.store.delete('project', 'member')
                self.assertFalse((self.root / 'installation/project/member.key').exists())

    def test_unsafe_root_missing_installation_refused(self):
        self.layout(1)
        self.root.chmod(0o755)
        for action in (self.store.due, lambda: self.store.delete('project', 'member')):
            with self.assertRaises(KeyStoreError):
                action()

    def test_unsafe_installation_missing_project_refused(self):
        self.layout(2)
        (self.root / 'installation').chmod(0o755)
        for action in (self.store.due, lambda: self.store.delete('project', 'member')):
            with self.assertRaises(KeyStoreError):
                action()

    def test_dangling_root_and_installation_symlink_refused(self):
        self.root.symlink_to(self.root.parent / 'absent')
        for action in (self.store.due, lambda: self.store.delete('project', 'member')):
            with self.assertRaises(KeyStoreError):
                action()
        self.root.unlink()
        self.layout(1)
        (self.root / 'installation').symlink_to(self.root / 'absent')
        for action in (self.store.due, lambda: self.store.delete('project', 'member')):
            with self.assertRaises(KeyStoreError):
                action()

    def test_project_disappearance_during_enumeration_is_error(self):
        self.layout(3)
        original = os.listdir
        removed = False
        def disappear(fd):
            nonlocal removed
            entries = original(fd)
            if not removed:
                (self.root / 'installation/project').rmdir()
                removed = True
            return entries
        with patch.object(os, 'listdir', side_effect=disappear):
            with self.assertRaises(KeyStoreError):
                self.store.due()

    def test_record_disappearance_during_enumeration_is_error(self):
        self.layout(3)
        record = self.root / 'installation/project/member.key'
        record.touch(mode=0o600)
        original = os.listdir
        def disappear(fd):
            entries = original(fd)
            if 'member.key' in entries:
                record.unlink()
            return entries
        with patch.object(os, 'listdir', side_effect=disappear):
            with self.assertRaises(KeyStoreError):
                self.store.due()

    def test_empty_installation_disappearance_after_list_is_error(self):
        self.layout(2)
        original = os.listdir
        def disappear(fd):
            entries = original(fd)
            (self.root / 'installation').rmdir()
            return entries
        with patch.object(os, 'listdir', side_effect=disappear):
            with self.assertRaises(KeyStoreError):
                self.store.due()


def value(pointer):
    return pointer.value if hasattr(pointer, 'value') else pointer


class FakeSecurity:
    """Models legacy fallback and explicit-only attachment; no native calls."""
    def __init__(self, *, invalidated=False, custody=7, null_item=False):
        self.api = self
        self.cf = self
        self.invalidated, self.custody, self.null_item = invalidated, custody, null_item
        self.ambient_writes = self.explicit_writes = self.modifications = 0
        self.released = []
        self.path = b'/synthetic/private/cortex.keychain-db'

    check = staticmethod(mac_keychain._Security.check)

    def headless(self):
        from contextlib import nullcontext
        return nullcontext()

    def CFRelease(self, ref):
        self.released.append(value(ref))

    def CFEqual(self, left, right):
        return value(left) == value(right)

    def SecKeychainOpen(self, path, out):
        out._obj.value = 7
        return 0

    def SecKeychainGetStatus(self, ref, out):
        out._obj.value = 1
        return 0

    def SecKeychainFindGenericPassword(self, *args):
        return -25300

    def SecKeychainAddGenericPassword(self, *args):
        if self.invalidated:
            self.ambient_writes += 1
        else:
            self.explicit_writes += 1
        if args[-1] is not None:
            args[-1]._obj.value = 21
        return 0

    def SecKeychainItemCreateNew(self, item_class, creator, length, data, out):
        out._obj.value = None if self.null_item else 21
        return 0

    def SecKeychainItemSetAttribute(self, *args):
        return 0

    def SecKeychainItemAddNoUI(self, ref, item):
        if not value(ref) or self.invalidated:
            return -25294
        self.explicit_writes += 1
        return 0

    def SecKeychainItemCopyKeychain(self, item, out):
        out._obj.value = self.custody
        return 0

    def SecKeychainGetPath(self, ref, size, data):
        path = self.path if value(ref) == 7 else b'/synthetic/default.keychain-db'
        C.memmove(data, path + b'\0', len(path) + 1)
        size._obj.value = len(path)
        return 0

    def SecKeychainItemModifyContent(self, *args):
        self.modifications += 1
        return 0


class MacExplicitCustody(unittest.TestCase):
    def store(self, **kwargs):
        store = mac_keychain.MacKeychainStore.__new__(mac_keychain.MacKeychainStore)
        store.security = FakeSecurity(**kwargs)
        store.path = Path(os.fsdecode(store.security.path))
        store.service = b'Cortex installation'
        store._check = lambda: True
        return store

    def put(self, store):
        store.put('project', 'member', _KeyRecord(
            secrets.token_urlsafe(32), KeyMetadata('kos', '2027-10-05T00:00:00Z')
        ))

    def test_invalidated_explicit_store_zero_ambient_writes_and_no_success(self):
        store = self.store(invalidated=True)
        completed = False
        try:
            self.put(store)
            completed = True
        except KeyStoreError:
            pass
        self.assertEqual(store.security.ambient_writes, 0)
        self.assertFalse(completed)
        self.assertEqual(store.security.explicit_writes, 0)

    def test_created_item_keychain_is_verified(self):
        store = self.store(custody=8)
        with self.assertRaises(KeyStoreError):
            self.put(store)
        self.assertIn(8, store.security.released)
        self.assertIn(21, store.security.released)

    def test_created_item_path_is_verified_even_if_cf_equal(self):
        store = self.store()
        store.security.path = b'/synthetic/other.keychain-db'
        with self.assertRaises(KeyStoreError):
            self.put(store)

    def test_null_created_item_refuses_before_add(self):
        store = self.store(null_item=True)
        with self.assertRaises(KeyStoreError):
            self.put(store)
        self.assertEqual(store.security.explicit_writes, 0)

    def test_false_postcheck_is_error(self):
        store = self.store()
        checks = iter((True, True, False))
        store._check = lambda: next(checks)
        with self.assertRaises(KeyStoreError):
            self.put(store)
        self.assertIn(7, store.security.released)

    def test_valid_explicit_attachment_balances_item_and_keychain_refs(self):
        store = self.store()
        self.put(store)
        self.assertEqual(store.security.ambient_writes, 0)
        self.assertEqual(store.security.explicit_writes, 1)
        self.assertIn(21, store.security.released)
        self.assertEqual(store.security.released.count(7), 2)

    def test_native_content_release_failure_also_releases_item(self):
        store = self.store()
        buffer = C.create_string_buffer(b'non-secret-unit-buffer')
        def found(*args):
            args[5]._obj.value = len(buffer) - 1
            args[6]._obj.value = C.addressof(buffer)
            args[7]._obj.value = 21
            return 0
        store.security.SecKeychainFindGenericPassword = found
        store.security.SecKeychainItemFreeContent = lambda *_: -1
        with self.assertRaises(KeyStoreError):
            store._find(C.c_void_p(7), 'project', 'member')
        self.assertIn(21, store.security.released)


if __name__ == '__main__':
    unittest.main(verbosity=2)
