"""Dedicated file keychain via Security.framework; no shell or ambient search."""
from __future__ import annotations

import ctypes as C
import os
import plistlib
import stat
import subprocess
import threading
from contextlib import contextmanager
from pathlib import Path

from .key_store import KeyStoreError, _KeyRecord, _label, _private_directory, _record

_UI_LOCK = threading.RLock()
_NOT_FOUND = -25300


class _Security:
    def __init__(self):
        self.api = C.CDLL('/System/Library/Frameworks/Security.framework/Security')
        self.cf = C.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
        self.cf.CFRelease.argtypes = [C.c_void_p]
        self.cf.CFRelease.restype = None
        pointer = C.POINTER(C.c_void_p)
        uint = C.c_uint32
        declarations = {
            'SecKeychainOpen': [C.c_char_p, pointer],
            'SecKeychainCreate': [C.c_char_p, uint, C.c_void_p, C.c_ubyte, C.c_void_p, pointer],
            'SecKeychainGetStatus': [C.c_void_p, C.POINTER(uint)],
            'SecKeychainLock': [C.c_void_p],
            'SecKeychainDelete': [C.c_void_p],
            'SecKeychainUnlock': [C.c_void_p, uint, C.c_void_p, C.c_ubyte],
            'SecKeychainGetUserInteractionAllowed': [C.POINTER(C.c_ubyte)],
            'SecKeychainSetUserInteractionAllowed': [C.c_ubyte],
            'SecKeychainFindGenericPassword': [C.c_void_p, uint, C.c_char_p, uint, C.c_char_p, C.POINTER(uint), pointer, pointer],
            'SecKeychainAddGenericPassword': [C.c_void_p, uint, C.c_char_p, uint, C.c_char_p, uint, C.c_void_p, pointer],
            'SecKeychainItemModifyContent': [C.c_void_p, C.c_void_p, uint, C.c_void_p],
            'SecKeychainItemFreeContent': [C.c_void_p, C.c_void_p],
            'SecKeychainItemDelete': [C.c_void_p],
        }
        for name, args in declarations.items():
            function = getattr(self.api, name)
            function.argtypes, function.restype = args, C.c_int32

    @staticmethod
    def check(status: int):
        if status:
            # Numeric status only: never propagate a credential/native buffer.
            raise KeyStoreError(f'dedicated Cortex keychain unavailable (OSStatus {status})')

    @contextmanager
    def headless(self):
        with _UI_LOCK:
            previous = C.c_ubyte()
            self.check(self.api.SecKeychainGetUserInteractionAllowed(C.byref(previous)))
            self.check(self.api.SecKeychainSetUserInteractionAllowed(0))
            try:
                yield
            finally:
                self.check(self.api.SecKeychainSetUserInteractionAllowed(previous))


def _custody(root: Path, *, create=False):
    if not root.is_absolute() or '..' in root.parts:
        raise KeyStoreError('keychain root must be an absolute physical path')
    for path in (root, *root.parents):
        if path.is_symlink():
            raise KeyStoreError('keychain root or ancestor is linked')
    probe = root
    while not probe.exists():
        probe = probe.parent
    device = subprocess.run(['df', '-P', str(probe)], capture_output=True, text=True, timeout=20)
    lines = device.stdout.splitlines()
    if device.returncode or len(lines) != 2:
        raise KeyStoreError('cannot establish keychain filesystem custody')
    check = subprocess.run(['diskutil', 'info', '-plist', lines[-1].split()[0]], capture_output=True, timeout=20)
    try:
        volume = plistlib.loads(check.stdout)
    except Exception:
        raise KeyStoreError('cannot establish keychain volume custody') from None
    if check.returncode or volume.get('Internal') is not True or volume.get('GlobalPermissionsEnabled') is not True:
        raise KeyStoreError('keychain requires an internal ownership-enabled volume')
    if create:
        _private_directory(root)
    if root.exists():
        info = root.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise KeyStoreError('keychain directory must be user-owned mode 0700')


class MacKeychainStore:
    def __init__(self, installation: str, root: Path):
        self.root = root
        self.directory = root / _label(installation)
        self.path = self.directory / 'cortex.keychain-db'
        self.service = ('Cortex ' + installation).encode()
        self.security = _Security()

    def _check(self) -> bool:
        _custody(self.root)
        if self.directory.is_symlink() or self.path.is_symlink():
            raise KeyStoreError('dedicated keychain path is linked')
        if not self.path.exists():
            return False
        _custody(self.directory)
        info = self.path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1:
            raise KeyStoreError('dedicated keychain must be user-owned mode 0600')
        return True

    @contextmanager
    def _open(self, *, require_unlocked=True):
        if not self._check():
            raise KeyStoreError('dedicated Cortex keychain is missing; enroll first')
        reference = C.c_void_p()
        with self.security.headless():
            self.security.check(self.security.api.SecKeychainOpen(os.fsencode(self.path), C.byref(reference)))
            if not reference.value:
                raise KeyStoreError('explicit dedicated keychain reference missing')
            try:
                status = C.c_uint32()
                self.security.check(self.security.api.SecKeychainGetStatus(reference, C.byref(status)))
                if require_unlocked and not status.value & 1:
                    raise KeyStoreError('dedicated Cortex keychain is locked; unlock explicitly')
                if not self._check():
                    raise KeyStoreError('dedicated keychain disappeared')
                yield reference
                self._check()
            finally:
                self.security.cf.CFRelease(reference)

    def initialize(self, password: bytes):
        if not isinstance(password, bytes) or len(password) < 16:
            raise KeyStoreError('dedicated keychain requires an explicit strong passphrase')
        _custody(self.root, create=True)
        _private_directory(self.directory)
        if self.path.exists() or self.path.is_symlink():
            raise KeyStoreError('dedicated keychain already exists; retained')
        reference = C.c_void_p()
        with self.security.headless():
            self.security.check(self.security.api.SecKeychainCreate(os.fsencode(self.path), len(password), password, 0, None, C.byref(reference)))
            if not reference.value:
                raise KeyStoreError('explicit dedicated keychain reference missing')
            self.security.cf.CFRelease(reference)
        self.path.chmod(0o600)
        subprocess.run(['tmutil', 'addexclusion', str(self.root)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not self.excluded():
            raise KeyStoreError('dedicated keychain is not excluded from Time Machine')
        self._check()

    def excluded(self) -> bool:
        result = subprocess.run(['tmutil', 'isexcluded', str(self.root)], capture_output=True, text=True, timeout=20)
        return result.returncode == 0 and result.stdout.startswith('[Excluded]')

    def lock(self):
        with self._open(require_unlocked=False) as reference:
            self.security.check(self.security.api.SecKeychainLock(reference))

    def unlock(self, password: bytes):
        if not isinstance(password, bytes) or not password:
            raise KeyStoreError('explicit keychain passphrase required')
        with self._open(require_unlocked=False) as reference:
            self.security.check(self.security.api.SecKeychainUnlock(reference, len(password), password, 1))

    def _find(self, reference, project: str, name: str):
        account = (_label(project) + '/' + _label(name)).encode()
        length, data, item = C.c_uint32(), C.c_void_p(), C.c_void_p()
        result = self.security.api.SecKeychainFindGenericPassword(reference, len(self.service), self.service, len(account), account, C.byref(length), C.byref(data), C.byref(item))
        if result == _NOT_FOUND:
            return None, item
        self.security.check(result)
        try:
            if length.value > 4096:
                raise KeyStoreError('credential record exceeds the safe size limit')
            raw = C.string_at(data, length.value)
        except BaseException:
            if item.value:
                self.security.cf.CFRelease(item)
            raise
        finally:
            self.security.check(self.security.api.SecKeychainItemFreeContent(None, data))
        return raw, item

    def get(self, project: str, name: str) -> _KeyRecord | None:
        if not self._check():
            return None
        with self._open() as reference:
            raw, item = self._find(reference, project, name)
            try:
                return None if raw is None else _record(raw)
            finally:
                if item.value:
                    self.security.cf.CFRelease(item)

    def put(self, project: str, name: str, record: _KeyRecord):
        import json
        raw = json.dumps({'token': record.token, 'managed_by': record.metadata.managed_by, 'expires_at': record.metadata.expires_at}, separators=(',', ':')).encode()
        if len(raw) > 4096:
            raise KeyStoreError('credential record exceeds the safe size limit')
        account = (_label(project) + '/' + _label(name)).encode()
        with self._open() as reference:
            _, item = self._find(reference, project, name)
            try:
                if item.value:
                    self.security.check(self.security.api.SecKeychainItemModifyContent(item, None, len(raw), raw))
                else:
                    self.security.check(self.security.api.SecKeychainAddGenericPassword(reference, len(self.service), self.service, len(account), account, len(raw), raw, None))
            finally:
                if item.value:
                    self.security.cf.CFRelease(item)

    def delete(self, project: str, name: str):
        with self._open() as reference:
            _, item = self._find(reference, project, name)
            try:
                if item.value:
                    self.security.check(self.security.api.SecKeychainItemDelete(item))
            finally:
                if item.value:
                    self.security.cf.CFRelease(item)

    def metadata_entries(self):
        raise KeyStoreError('Mac key status requires explicit project/member selection')
