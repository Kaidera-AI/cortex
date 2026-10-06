"""Actual private-file counterexamples found in the author's verifier review.

The child and signature are public fixture models, not a native signature claim.
"""
import hashlib
import importlib
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


def fixture(tmp_path, monkeypatch, *, library=False):
    module = importlib.import_module('cortex_v2.clients.native_prerequisite')
    tmp_path.chmod(0o700)
    tools = tmp_path / 'tools'; tools.mkdir(mode=0o700)
    tool = tools / 'minisign'; tool.write_bytes(b'PUBLIC trusted tool'); tool.chmod(0o500)
    inputs = tmp_path / 'inputs'; inputs.mkdir(mode=0o700)
    manifest, signature = inputs / 'release.json', inputs / 'release.minisig'
    for p, data in ((manifest, b'PUBLIC unsigned manifest A'), (signature, b'PUBLIC invalid signature A')):
        p.write_bytes(data); p.chmod(0o600)
    dependencies = ()
    alias_root = alias = dependency = None
    if library:
        lib = tools / 'lib'; lib.mkdir(mode=0o700)
        dependency = lib / 'sodium'; dependency.write_bytes(b'PUBLIC pinned dependency'); dependency.chmod(0o400)
        alias_root = tmp_path / 'loader'; alias_root.mkdir(mode=0o700)
        alias = alias_root / 'sodium'; alias.symlink_to(dependency)
        dependencies = ((str(dependency), hashlib.sha256(dependency.read_bytes()).hexdigest(), str(alias)),)
    monkeypatch.setattr(module, '_MINISIGN_VERIFIERS', {sys.platform:
        (str(tool), os.getuid(), hashlib.sha256(tool.read_bytes()).hexdigest(), dependencies)})
    monkeypatch.setattr(module.shutil, 'which', lambda *a: pytest.fail('ambient verifier'))
    return module, manifest, signature, alias_root, alias, dependency


@pytest.mark.parametrize('case', ['valid', 'replace-pair-restore', 'rewrite-manifest-restore',
    'rewrite-signature-restore', 'input-directory-restore', 'replace-manifest-restore',
    'replace-signature-restore', 'deadline-during-child'])
def test_verifier_never_qualifies_restored_unsigned_inputs(case, tmp_path, monkeypatch):
    module, manifest, signature, _, _, _ = fixture(tmp_path, monkeypatch)
    originals = {p: p.read_bytes() for p in (manifest, signature)}
    clock = [0.0]; calls = []
    monkeypatch.setattr(module.time, 'monotonic', lambda: clock[0])
    def run(argv, **kw):
        calls.append(True)
        assert argv[4:8] == ['-m', str(manifest), '-x', str(signature)]
        assert len(kw['pass_fds']) == 1 and 0 < kw['timeout'] <= 5
        if case.startswith('rewrite-'):
            path = manifest if case == 'rewrite-manifest-restore' else signature
            before = path.stat(); path.write_bytes(b'PUBLIC valid signed input B')
            assert path.read_bytes() != originals[path]
            path.write_bytes(originals[path]); os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        if case.startswith('replace-'):
            selected = (manifest, signature) if case == 'replace-pair-restore' else (
                manifest if case == 'replace-manifest-restore' else signature,)
            saved = []
            for path in selected:
                old = path.with_name(path.name + '.held'); path.rename(old); saved.append((path, old))
                path.write_bytes(b'PUBLIC valid signed input B'); path.chmod(0o600)
            assert all(path.read_bytes() != originals[path] for path in selected)
            for path, old in saved: path.unlink(); old.rename(path)
        if case == 'input-directory-restore':
            old = manifest.parent.with_name('held-inputs'); manifest.parent.rename(old)
            manifest.parent.mkdir(mode=0o700)
            for path in (manifest, signature): path.write_bytes(b'PUBLIC valid signed input B'); path.chmod(0o600)
            for path in (manifest, signature): path.unlink()
            manifest.parent.rmdir(); old.rename(manifest.parent)
        if case == 'deadline-during-child': clock[0] = 6.0
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(module.subprocess, 'run', run)
    if case == 'valid': module.verify_signature(manifest, signature)
    else:
        with pytest.raises(module.PrerequisiteRefusal) as error: module.verify_signature(manifest, signature)
        assert error.value.code == 'cortex_release_signature_invalid'
    assert len(calls) == 1 and all(p.read_bytes() == raw for p, raw in originals.items())


@pytest.mark.parametrize('case', ['valid', 'writable-loader', 'foreign-loader-owner',
    'loader-ancestor-restore', 'alias-retarget-restore', 'foreign-alias-owner',
    'dependency-rewrite-restore', 'loader-mode-restore'])
def test_every_dependency_loader_namespace_is_protected_and_live(case, tmp_path, monkeypatch):
    module, manifest, signature, loader, alias, dependency = fixture(tmp_path, monkeypatch, library=True)
    calls = []
    if case == 'writable-loader': loader.chmod(0o777)
    if case in ('foreign-loader-owner', 'foreign-alias-owner'):
        selected = loader if case == 'foreign-loader-owner' else alias
        lstat, stat_path = Path.lstat, Path.stat
        def foreign(info):
            return SimpleNamespace(**{n: getattr(info, n) for n in dir(info) if n.startswith('st_')},
                                   **{})
        def changed(info):
            value = foreign(info); value.st_uid = os.getuid() + 1; return value
        monkeypatch.setattr(Path, 'lstat', lambda p, *a, **kw: changed(lstat(p, *a, **kw)) if p == selected else lstat(p, *a, **kw))
        monkeypatch.setattr(Path, 'stat', lambda p, *a, **kw: changed(stat_path(p, *a, **kw)) if p == selected else stat_path(p, *a, **kw))
    def run(argv, **kw):
        calls.append(True)
        if case == 'loader-ancestor-restore':
            old = loader.with_name('held-loader'); loader.rename(old); loader.mkdir(mode=0o700)
            (loader / alias.name).symlink_to(dependency)
            (loader / alias.name).unlink(); loader.rmdir(); old.rename(loader)
        if case == 'alias-retarget-restore':
            alternate = dependency.with_name('alternate'); alternate.write_bytes(b'PUBLIC untrusted dependency')
            alias.unlink(); alias.symlink_to(alternate)
            assert alias.resolve() != dependency
            alias.unlink(); alias.symlink_to(dependency); alternate.unlink()
        if case == 'dependency-rewrite-restore':
            raw = dependency.read_bytes(); before = dependency.stat(); dependency.chmod(0o600)
            dependency.write_bytes(b'PUBLIC untrusted dependency'); dependency.write_bytes(raw)
            dependency.chmod(0o400); os.utime(dependency, ns=(before.st_atime_ns, before.st_mtime_ns))
        if case == 'loader-mode-restore': loader.chmod(0o777); loader.chmod(0o700)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(module.subprocess, 'run', run)
    if case == 'valid': module.verify_signature(manifest, signature)
    else:
        with pytest.raises(module.PrerequisiteRefusal) as error: module.verify_signature(manifest, signature)
        assert error.value.code == 'cortex_release_signature_invalid'
    assert len(calls) == (0 if case in ('writable-loader', 'foreign-loader-owner', 'foreign-alias-owner') else 1)
