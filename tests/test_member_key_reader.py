"""R65 native fixture; no installed credentials, token output or network."""
from __future__ import annotations

import json
import os
import secrets
import shutil
import sys
import tempfile
from pathlib import Path

from cortex_v2.clients.key_store import KeyStore, KeyStoreError


def refused(action):
    try:
        action()
    except KeyStoreError:
        return
    raise AssertionError('required refusal did not occur')


def fixture():
    from cortex_v2.clients.member_reader import MemberKeyReader
    if sys.platform == 'darwin':
        parent = Path.home() / '.cortex/test'
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    elif sys.platform == 'linux':
        parent = Path('/tmp')
    else:
        raise RuntimeError('fixture requires a supported native platform')
    project = Path(tempfile.mkdtemp(prefix='r65-member-', dir=parent))
    project.chmod(0o700)
    installation = 'r65-native-fixture'
    root = project / '.kaidera/secrets'
    reader = MemberKeyReader(installation=installation, project='fixture', name='console', project_root=project)
    checks = []
    store = None
    try:
        refused(reader.headers)
        if root.exists():
            raise AssertionError('member read created an unenrolled store')
        checks.append('missing refuses without creating state')
        for name in ('owner', 'lead', 'recovery'):
            refused(lambda: MemberKeyReader(installation=installation, project='fixture', name=name, project_root=project))
        checks.append('reserved privileged defaults refused')
        store = KeyStore(installation, root=root)
        password = secrets.token_urlsafe(40).encode()
        if sys.platform == 'darwin':
            store.initialize_keychain(password)
            if not store._native.excluded():
                raise AssertionError('fixture keychain not excluded')
            checks.append('dedicated temporary keychain excluded')
        token, replacement = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        store.put('fixture', 'console', token, managed_by='kos', expires_at='2027-10-04T00:00:00Z')
        value = reader.headers()
        if value.get('Authorization') != 'Bearer ' + token or value.get('X-Cortex-Scope') != 'fixture' or set(value) != {'Authorization', 'X-Cortex-Scope'}:
            raise AssertionError('member credential/scope headers differed')
        checks.append('native store read and exact member headers')
        store.put('fixture', 'console', replacement, managed_by='kos', expires_at='2027-10-04T00:00:00Z')
        if reader.headers().get('Authorization') != 'Bearer ' + replacement:
            raise AssertionError('next request did not observe rotation')
        checks.append('rotation observed at next request')
        other = MemberKeyReader(installation=installation, project='other', name='console', project_root=project)
        refused(other.headers)
        checks.append('other project has no credential fallback')
        if sys.platform == 'darwin':
            store.lock_keychain()
            refused(reader.headers)
            refused(lambda: store.unlock_keychain(b'wrong-fixture-passphrase'))
            store.unlock_keychain(password)
            if reader.headers().get('Authorization') != 'Bearer ' + replacement:
                raise AssertionError('explicit unlock did not restore read')
            checks.append('locked/headless refusal and explicit unlock')
        store.put('fixture', 'console', replacement, managed_by='kos', expires_at='2000-01-01T00:00:00Z')
        refused(reader.headers)
        checks.append('expired key refused locally')
        store.delete('fixture', 'console')
        refused(reader.headers)
        checks.append('deleted key refuses without fallback')
        print(json.dumps({'status': 'PASS', 'platform': sys.platform, 'backend': store.backend, 'checks': checks}))
    finally:
        # Only this freshly allocated fixture; never another keychain/root.
        if store is not None and sys.platform == 'darwin' and store._native.path.exists():
            with store._native._open(require_unlocked=False) as reference:
                store._native.security.check(store._native.security.api.SecKeychainDelete(reference))
        shutil.rmtree(project)


def test_native_member_fixture():
    fixture()


def test_native_backend_selection():
    if sys.platform == 'darwin':
        store = KeyStore('r65-selector', root=Path.home() / '.cortex/test/r65-selector/.kaidera/secrets')
        assert store.backend == 'keychain'
    elif sys.platform == 'linux':
        store = KeyStore('r65-selector', root=Path('/tmp/r65-selector'))
        assert store.backend == 'file'


if __name__ == '__main__':
    fixture()
