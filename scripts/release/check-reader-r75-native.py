"""Native Mac r75 fixture, executable only in an ephemeral public hosted runner."""
from __future__ import annotations

import ctypes as C
import json
import os
import secrets
import shutil
import sys
import tempfile
from pathlib import Path

if (sys.platform != 'darwin' or os.environ.get('GITHUB_ACTIONS') != 'true'
        or os.environ.get('RUNNER_ENVIRONMENT') != 'github-hosted'
        or os.environ.get('GITHUB_REPOSITORY') != 'Kaidera-AI/cortex'):
    raise SystemExit('REFUSED: r75 native Mac fixture requires the public hosted CI runner')

from cortex_v2.clients.key_store import KeyStore, KeyStoreError
from cortex_v2.clients.member_reader import MemberKeyReader
from cortex_v2.clients.mac_keychain import _Security


class Callbacks(C.Structure):
    _fields_ = [('version', C.c_long), ('retain', C.c_void_p), ('release', C.c_void_p),
                ('description', C.c_void_p), ('equal', C.c_void_p)]


def refuse(action):
    try:
        action()
    except KeyStoreError:
        return
    raise AssertionError('required native refusal missing')


def main():
    security = _Security()
    api, cf = security.api, security.cf
    pointer = C.POINTER(C.c_void_p)
    for name, args in {
        'SecKeychainCopyDefault': [pointer],
        'SecKeychainSetDefault': [C.c_void_p],
        'SecKeychainCopySearchList': [pointer],
        'SecKeychainSetSearchList': [C.c_void_p],
    }.items():
        function = getattr(api, name)
        function.argtypes, function.restype = args, C.c_int32
    cf.CFArrayCreateMutable.argtypes = [C.c_void_p, C.c_long, C.c_void_p]
    cf.CFArrayCreateMutable.restype = C.c_void_p
    cf.CFArrayAppendValue.argtypes = [C.c_void_p, C.c_void_p]
    cf.CFArrayAppendValue.restype = None
    original_default, original_search = C.c_void_p(), C.c_void_p()
    security.check(api.SecKeychainCopyDefault(C.byref(original_default)))
    security.check(api.SecKeychainCopySearchList(C.byref(original_search)))
    parent = Path.home() / '.cortex/r75-ci'
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    project = Path(tempfile.mkdtemp(prefix='synthetic-', dir=parent))
    project.chmod(0o700)
    handles, stores, checks = [], [], []
    search = None
    try:
        password = secrets.token_urlsafe(40).encode()
        for name in ('ambient-default.keychain-db', 'ambient-search.keychain-db'):
            ref = C.c_void_p()
            path = project / name
            security.check(api.SecKeychainCreate(os.fsencode(path), len(password), password, 0, None, C.byref(ref)))
            if not ref.value:
                raise AssertionError('synthetic ambient keychain reference missing')
            path.chmod(0o600)
            handles.append(ref)
        root = project / '.kaidera/secrets'
        normal = KeyStore('r75-normal', root=root)
        fault = KeyStore('r75-fault', root=root)
        for store in (normal, fault):
            store.initialize_keychain(password)
            stores.append(store)
        callbacks = Callbacks.in_dll(cf, 'kCFTypeArrayCallBacks')
        search = cf.CFArrayCreateMutable(None, 0, C.byref(callbacks))
        if not search:
            raise AssertionError('synthetic search list missing')
        for ref in handles:
            cf.CFArrayAppendValue(search, ref)
        security.check(api.SecKeychainSetDefault(handles[0]))
        security.check(api.SecKeychainSetSearchList(search))
        checks.append('only synthetic ambient stores selected for fixture')

        reader = MemberKeyReader(installation='r75-normal', project='fixture', name='console', project_root=project)
        refuse(reader.headers)
        token, replacement = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        normal.put('fixture', 'console', token, managed_by='kos', expires_at='2027-10-05T00:00:00Z')
        headers = reader.headers()
        if (headers.get('Authorization') != 'Bearer ' + token
                or headers.get('X-Cortex-Scope') != 'fixture'
                or set(headers) != {'Authorization', 'X-Cortex-Scope'}):
            raise AssertionError('native member headers differ')
        checks.append('frozen bytes explicit native create and member read')
        normal.put('fixture', 'console', replacement, managed_by='kos', expires_at='2027-10-05T00:00:00Z')
        if reader.headers().get('Authorization') != 'Bearer ' + replacement:
            raise AssertionError('native rotation not observed')
        checks.append('native rotation observed')
        normal.lock_keychain()
        refuse(reader.headers)
        refuse(lambda: normal.unlock_keychain(b'wrong-synthetic-password'))
        normal.unlock_keychain(password)
        if reader.headers().get('Authorization') != 'Bearer ' + replacement:
            raise AssertionError('explicit native unlock failed')
        checks.append('native headless lock refusal and explicit unlock')
        normal.delete('fixture', 'console')
        refuse(reader.headers)
        checks.append('native deletion observed')

        class InvalidateBeforeAdd:
            def __init__(self):
                self.injected = False
            def __getattr__(self, name):
                return getattr(api, name)
            def invoke(self, name, *args):
                if self.injected:
                    raise AssertionError('unexpected second add')
                security.check(api.SecKeychainDelete(args[0]))
                self.injected = True
                return getattr(api, name)(*args)
            def SecKeychainAddGenericPassword(self, *args):
                return self.invoke('SecKeychainAddGenericPassword', *args)
            def SecKeychainItemAddNoUI(self, *args):
                return self.invoke('SecKeychainItemAddNoUI', *args)
        injected = InvalidateBeforeAdd()
        fault._native.security.api = injected
        completed = False
        try:
            fault.put('fixture', 'console', secrets.token_urlsafe(32), managed_by='kos', expires_at='2027-10-05T00:00:00Z')
            completed = True
        except KeyStoreError:
            pass
        if not injected.injected:
            raise AssertionError('native invalidation hook not reached')
        ambient_writes = 0
        account = b'fixture/console'
        for ref in handles:
            item = C.c_void_p()
            result = api.SecKeychainFindGenericPassword(ref, len(fault._native.service), fault._native.service,
                                                       len(account), account, None, None, C.byref(item))
            if item.value:
                cf.CFRelease(item)
            if result == 0:
                ambient_writes += 1
            elif result != -25300:
                security.check(result)
        if ambient_writes or completed:
            raise AssertionError(f'invalidation: ambient_writes={ambient_writes}, completed={completed}')
        checks.append('native dedicated handle deleted immediately before add')
        checks.append('zero writes to synthetic default and search-list stores; no success')
        print(json.dumps({'status': 'PASS', 'platform': sys.platform, 'python': sys.version.split()[0],
                          'ambient_writes': ambient_writes, 'checks': checks}, sort_keys=True))
    finally:
        # Restore the ephemeral runner preferences before deleting ONLY fixture stores.
        security.check(api.SecKeychainSetDefault(original_default))
        security.check(api.SecKeychainSetSearchList(original_search))
        for store in stores:
            if store._native.path.exists():
                ref = C.c_void_p()
                security.check(api.SecKeychainOpen(os.fsencode(store._native.path), C.byref(ref)))
                try:
                    security.check(api.SecKeychainDelete(ref))
                finally:
                    cf.CFRelease(ref)
        for ref in handles:
            try:
                security.check(api.SecKeychainDelete(ref))
            finally:
                cf.CFRelease(ref)
        for ref in (search, original_default, original_search):
            if ref:
                cf.CFRelease(ref)
        shutil.rmtree(project)


if __name__ == '__main__':
    main()
