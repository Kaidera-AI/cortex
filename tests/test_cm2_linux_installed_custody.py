"""Actual public package files, intercepted signing, no native installed claim."""
import copy
import hashlib
import importlib
import json
import os
from pathlib import Path
import shutil
import sys
import time

import pytest


def fixture(tmp_path, monkeypatch):
    module = importlib.import_module('cortex_v2.clients.native_prerequisite')
    root = tmp_path / 'runtime'; root.mkdir(mode=0o700)
    package = root / 'package'; package.mkdir(mode=0o700)
    signed = root / 'signed'; signed.mkdir(mode=0o700)
    payload = {'bin/cortex': b'PUBLIC helper bytes\n', 'bin/cortex-agent': b'PUBLIC agent bytes\n'}
    payload.update({'images/' + role + '.oci.tar': ('PUBLIC ' + role + ' image fixture\n').encode()
                    for role in module.ROLES})
    for name, raw in payload.items():
        path = package / name; path.parent.mkdir(mode=0o700, exist_ok=True)
        path.write_bytes(raw); path.chmod(0o700 if name.startswith('bin/') else 0o600)
    files = {name: hashlib.sha256(raw).hexdigest() for name, raw in payload.items()}
    release = json.loads((Path(__file__).parent / 'fixtures/cm2-revision4/fixtures/release.linux.json').read_text())
    release['files'] = dict(files)
    for role in module.ROLES: release['images'][role]['archive_sha256'] = files['images/' + role + '.oci.tar']
    inner = {'schema': 'cortex.test-package.v1', 'deployment_class': 'TEST',
             'target': 'linux-x86_64', 'source_sha': release['source_revision'],
             'files': files, 'images': copy.deepcopy(release['images'])}
    raw = json.dumps(inner, sort_keys=True).encode() + b'\n'
    (package / 'release.json').write_bytes(raw); (package / 'release.json').chmod(0o600)
    release['payload_manifest_sha256'] = hashlib.sha256(raw).hexdigest()
    archive = signed / release['archive']['name']; archive.write_bytes(b'PUBLIC retained outer archive\n')
    archive.chmod(0o600)
    release['archive']['size_bytes'] = archive.stat().st_size
    release['archive']['sha256'] = hashlib.sha256(archive.read_bytes()).hexdigest()
    manifest = signed / 'release.json'; signature = signed / 'release.json.minisig'
    signature.write_bytes(b'SYNTHETIC signature verification is intercepted\n'); signature.chmod(0o600)
    module.validate_release_manifest(release, target='linux-x86_64')
    calls = []
    def verify(path, detached, *, trusted_public_key=module.RELEASE_PUBLIC_KEY, timeout=5):
        assert path == manifest and detached == signature and trusted_public_key == module.RELEASE_PUBLIC_KEY
        assert type(timeout) in (int, float) and 0 < timeout <= 5
        calls.append('signature')
    monkeypatch.setattr(module, 'verify_signature', verify)
    return module, root, package, release, manifest, archive, calls


CASES = ['valid', 'signature-refused', 'wrong-target', 'wrong-lineage', 'inner-digest',
         'archive-digest', 'archive-size', 'helper-digest', 'extra-file', 'missing-file',
         'linked-package', 'linked-archive', 'archive-hardlink', 'helper-writable',
         'helper-replaced', 'parent-replaced', 'deadline', 'outer-overflow', 'inner-overflow']


@pytest.mark.parametrize('case', CASES)
def test_actual_installed_release_binding_refuses_before_native_execution(case, tmp_path, monkeypatch):
    module, root, package, release, manifest, archive, calls = fixture(tmp_path, monkeypatch)
    assert hasattr(module, 'verify_installed_release'), 'accepted installed signed-release binding is absent'
    if case == 'signature-refused':
        def refused(*a, **kw): raise module.PrerequisiteRefusal('cortex_release_signature_invalid')
        monkeypatch.setattr(module, 'verify_signature', refused)
    elif case == 'wrong-target': release['target'] = 'macos-arm64'
    elif case == 'wrong-lineage': release['release_lineage'] = 'legacy'
    elif case == 'inner-digest': release['payload_manifest_sha256'] = '0' * 64
    elif case == 'archive-digest': release['archive']['sha256'] = '0' * 64
    elif case == 'archive-size': release['archive']['size_bytes'] += 1
    elif case == 'helper-digest': release['files']['bin/cortex'] = '0' * 64
    elif case == 'extra-file':
        path = package / 'unexpected'; path.write_bytes(b'PUBLIC undeclared bytes'); path.chmod(0o600)
    elif case == 'missing-file': (package / 'bin/cortex-agent').unlink()
    elif case == 'linked-package':
        original = root / 'original-package'; package.rename(original); package.symlink_to(original)
    elif case == 'linked-archive':
        original = archive.with_suffix('.original'); archive.rename(original); archive.symlink_to(original)
    elif case == 'archive-hardlink': os.link(archive, root / 'archive-link')
    elif case == 'helper-writable': (package / 'bin/cortex').chmod(0o722)
    elif case in ('helper-replaced', 'parent-replaced'):
        original_open = os.open; changed = []
        def replace(path, flags, *args, **kw):
            fd = original_open(path, flags, *args, **kw)
            if str(path) == str(package / 'bin/cortex') and not changed:
                changed.append(True)
                if case == 'helper-replaced':
                    replacement = package / 'bin/replacement'; replacement.write_bytes(b'PUBLIC foreign bytes')
                    replacement.chmod(0o700); replacement.replace(package / 'bin/cortex')
                else:
                    retired = root / 'retired-bin'; (package / 'bin').rename(retired)
                    shutil.copytree(retired, package / 'bin'); shutil.rmtree(retired)
            return fd
        monkeypatch.setattr(os, 'open', replace)
    elif case == 'deadline':
        original = module.verify_signature
        def slow(*a, **kw): original(*a, **kw); time.sleep(.08)
        monkeypatch.setattr(module, 'verify_signature', slow)
    elif case == 'outer-overflow': release['release_id'] = 'x' * 1048577
    elif case == 'inner-overflow': (package / 'release.json').write_bytes(b'x' * 1048577)
    manifest.write_text(json.dumps(release)); manifest.chmod(0o600)
    start = time.monotonic(); deadline = start + (.03 if case == 'deadline' else 5)
    if case == 'valid':
        value = module.verify_installed_release(root, deadline=deadline)
        assert value['release'] == release and value['package_root'] == str(package)
        assert value['helper_sha256'] == release['files']['bin/cortex']
        assert value['release_manifest_sha256'] == hashlib.sha256(manifest.read_bytes()).hexdigest()
        assert calls == ['signature']
    else:
        with pytest.raises(module.PrerequisiteRefusal): module.verify_installed_release(root, deadline=deadline)
    assert time.monotonic() - start < 1


def test_actual_signature_process_obeys_remaining_budget(tmp_path, monkeypatch):
    module = importlib.import_module('cortex_v2.clients.native_prerequisite')
    tmp_path.chmod(0o700); manifest = tmp_path / 'release.json'; signature = tmp_path / 'signature'
    manifest.write_bytes(b'PUBLIC manifest fixture'); manifest.chmod(0o600)
    signature.write_bytes(b'PUBLIC signature fixture'); signature.chmod(0o600)
    tool = tmp_path / 'finite-signature-producer'
    tool.write_text('#!' + sys.executable + '\nimport time\ntime.sleep(.6)\n'); tool.chmod(0o700)
    monkeypatch.setattr(module.shutil, 'which', lambda name: str(tool))
    start = time.monotonic()
    with pytest.raises(module.PrerequisiteRefusal) as error:
        module.verify_signature(manifest, signature, timeout=.05)
    assert time.monotonic() - start < .4
    assert error.value.code == 'cortex_release_signature_invalid'
