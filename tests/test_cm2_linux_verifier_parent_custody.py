"""An explicit pinned group-parent exception still detects namespace changes."""
import hashlib
import importlib
import os
import sys
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize('case', ['valid', 'undeclared', 'changed-ancestor', 'world-writable'])
def test_explicit_pinned_parent_never_admits_unmeasured_or_changed_verifier(case, tmp_path, monkeypatch):
    module = importlib.import_module('cortex_v2.clients.native_prerequisite')
    assert hasattr(module, '_MINISIGN_GROUP_PARENT_CUSTODY'), 'CM2 pinned group-parent custody missing'
    tmp_path.chmod(0o700)
    tools = tmp_path / 'tools'; tools.mkdir(mode=0o775); tools.chmod(0o775)
    binary = tools / 'bin'; binary.mkdir(mode=0o755)
    tool = binary / 'minisign'; tool.write_bytes(b'PUBLIC pinned tool'); tool.chmod(0o500)
    digest = hashlib.sha256(tool.read_bytes()).hexdigest()
    manifest, signature = tmp_path / 'release.json', tmp_path / 'signature'
    for p in (manifest, signature): p.write_bytes(b'PUBLIC signature fixture'); p.chmod(0o600)
    monkeypatch.setattr(module, '_MINISIGN_VERIFIERS', {sys.platform: (str(tool), os.getuid(), digest, ())})
    monkeypatch.setattr(module, '_MINISIGN_GROUP_PARENT_CUSTODY', frozenset() if case == 'undeclared' else frozenset({tools}))
    monkeypatch.setattr(module.shutil, 'which', lambda *a: pytest.fail('ambient verifier selection'))
    if case == 'world-writable': tools.chmod(0o777)
    calls = []
    def run(argv, **kw):
        calls.append(True)
        assert os.read(kw['pass_fds'][0], 65536) == b'PUBLIC pinned tool'
        if case == 'changed-ancestor':
            transient = tools / 'transient'; transient.write_bytes(b'PUBLIC namespace change'); transient.unlink()
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(module.subprocess, 'run', run)
    if case == 'valid': module.verify_signature(manifest, signature)
    else:
        with pytest.raises(module.PrerequisiteRefusal) as error: module.verify_signature(manifest, signature)
        assert error.value.code == 'cortex_release_signature_invalid'
    assert len(calls) == (1 if case in ('valid', 'changed-ancestor') else 0)
