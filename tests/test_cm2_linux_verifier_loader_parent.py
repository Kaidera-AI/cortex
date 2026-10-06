"""The measured Mac opt exception admits only pinned unchanged loader custody."""
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_cm2_linux_verifier_self_review import fixture


@pytest.mark.parametrize('case', ['valid', 'undeclared', 'changed-ancestor', 'world-writable'])
def test_explicit_loader_parent_exception_requires_pinned_protected_custody(case, tmp_path, monkeypatch):
    module, manifest, signature, loader, alias, dependency = fixture(tmp_path, monkeypatch, library=True)
    assert Path('/opt/homebrew/opt') in module._MINISIGN_GROUP_PARENT_CUSTODY, 'measured pinned Mac loader-parent binding missing'
    loader.chmod(0o775)
    monkeypatch.setattr(module, '_MINISIGN_GROUP_PARENT_CUSTODY',
        frozenset() if case == 'undeclared' else frozenset({loader}))
    if case == 'world-writable': loader.chmod(0o777)
    calls = []
    def run(argv, **kw):
        calls.append(True)
        if case == 'changed-ancestor':
            path = loader / 'transient'; path.write_bytes(b'PUBLIC namespace change'); path.unlink()
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(module.subprocess, 'run', run)
    if case == 'valid': module.verify_signature(manifest, signature)
    else:
        with pytest.raises(module.PrerequisiteRefusal) as error: module.verify_signature(manifest, signature)
        assert error.value.code == 'cortex_release_signature_invalid'
    assert len(calls) == (1 if case in ('valid', 'changed-ancestor') else 0)
