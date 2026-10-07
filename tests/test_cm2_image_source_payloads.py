"""Actual own-source and tiny OCI bytes; native tools explicitly intercepted."""
import hashlib
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from test_cm2_build_catalog_assembly import image, SOURCE, VERSION
from test_linux_builder_contract import load


def inputs(tmp_path):
    root = tmp_path / 'source'
    (root / 'src').mkdir(parents=True)
    (root / 'migrations').mkdir()
    (root / 'deploy/release').mkdir(parents=True)
    (root / 'src/public.py').write_bytes(b'PUBLIC = 1\n')
    (root / 'migrations/0001_core.sql').write_bytes(b'SELECT 1;\n')
    (root / 'deploy/release/10-create-roles.sh').write_bytes(b'#!/bin/sh\n# PUBLIC init source\n')
    return root


def expected(root):
    digest = lambda raw: hashlib.sha256(raw).hexdigest()
    app = {'src/public.py': digest((root / 'src/public.py').read_bytes()),
           'migrations/0001_core.sql': digest((root / 'migrations/0001_core.sql').read_bytes())}
    db = {'deploy/release/10-create-roles.sh': digest((root / 'deploy/release/10-create-roles.sh').read_bytes())}
    return {role: {'files': dict(files), 'sha256': digest(json.dumps(files, sort_keys=True, separators=(',', ':')).encode())}
            for role, files in [('api', app), ('doc', app), ('embed', app), ('graph', app), ('db', db)]}


PURE = ['valid', 'change-app', 'change-db', 'missing-source', 'empty-source',
        'missing-migrations', 'empty-migrations', 'missing-db', 'linked-db',
        'hardlinked-db', 'fifo-db', 'oversized-db', 'linked-source', 'special-source', 'hardlinked-source']


@pytest.mark.parametrize('case', PURE)
def test_exact_canonical_role_source_inputs_and_unsafe_object_refusals(case, tmp_path, monkeypatch):
    module = load('linux_build_catalog.py', monkeypatch)
    assert callable(getattr(module, 'source_payloads', None)), 'all-role source payload materializer missing'
    root = inputs(tmp_path)
    original = expected(root)
    db = root / 'deploy/release/10-create-roles.sh'
    if case == 'change-app': (root / 'src/public.py').write_bytes(b'PUBLIC = 2\n')
    if case == 'change-db': db.write_bytes(b'#!/bin/sh\n# PUBLIC changed init\n')
    if case == 'missing-source': (root / 'src/public.py').unlink(); (root / 'src').rmdir()
    if case == 'empty-source': (root / 'src/public.py').unlink()
    if case == 'missing-migrations': (root / 'migrations/0001_core.sql').unlink(); (root / 'migrations').rmdir()
    if case == 'empty-migrations': (root / 'migrations/0001_core.sql').unlink()
    if case == 'missing-db': db.unlink()
    if case == 'linked-db': db.rename(root / 'PUBLIC-db'); db.symlink_to(root / 'PUBLIC-db')
    if case == 'hardlinked-db': os.link(db, root / 'PUBLIC-db')
    if case == 'fifo-db': db.unlink(); os.mkfifo(db, 0o600)
    if case == 'oversized-db': db.write_bytes(b'X' * (8 * 1024**2 + 1))
    if case == 'linked-source': (root / 'src').rename(root / 'PUBLIC-src'); (root / 'src').symlink_to(root / 'PUBLIC-src')
    if case == 'special-source': os.mkfifo(root / 'src/PUBLIC-fifo', 0o600)
    if case == 'hardlinked-source': os.link(root / 'src/public.py', root / 'PUBLIC-code')
    if case in ('valid', 'change-app', 'change-db'):
        result = module.source_payloads(root)
        assert result == expected(root)
        assert result['api'] == result['doc'] == result['embed'] == result['graph']
        if case == 'change-app': assert result['api'] != original['api'] and result['db'] == original['db']
        if case == 'change-db': assert result['db'] != original['db'] and result['api'] == original['api']
        result['doc']['files']['PUBLIC-mutated-result'] = '0' * 64
        assert 'PUBLIC-mutated-result' not in result['api']['files']
    else:
        with pytest.raises(RuntimeError): module.source_payloads(root)


BUILD = ['valid', 'build-drift', 'save-drift', 'sbom-drift', 'rehearsal-drift',
         'same-byte-replace', 'change-db', 'add-source', 'remove-source', 'missing-source', 'foreign-target']


@pytest.mark.parametrize('case', BUILD)
def test_complete_cm2_image_stage_materializes_actual_maps_and_refuses_drift(case, tmp_path, monkeypatch):
    module = load('build-candidate.py', monkeypatch)
    catalog = load('linux_build_catalog.py', monkeypatch)
    assert callable(getattr(catalog, 'source_payloads', None)), 'all-role source payload materializer missing'
    root = inputs(tmp_path)
    wanted = expected(root)
    out = tmp_path / 'out'
    monkeypatch.setattr(module, 'ROOT', root)
    monkeypatch.setattr(module.platform, 'system', lambda: 'Linux')
    monkeypatch.setattr(module.platform, 'machine', lambda: 'x86_64')
    monkeypatch.setattr(module, 'source_identity', lambda revision: None)
    import linux_recipe_parity
    monkeypatch.setattr(linux_recipe_parity, 'verify_recipe_parity', lambda source: {'public': True})
    brew = {'formulae': [{'name': 'podman', 'tap': 'homebrew/core', 'revision': 0,
            'versions': {'stable': '6.1.3'}, 'linked_keg': '6.1.3',
            'installed': [{'version': '6.1.3', 'poured_from_bottle': True}],
            'bottle': {'stable': {'files': {'x86_64_linux': {'sha256': '1' * 64,
                'url': 'https://ghcr.io/v2/homebrew/core/podman/blobs/sha256:' + '1' * 64}}}}}]}
    calls = []
    changed = False
    def drift():
        nonlocal changed
        if changed: return
        changed = True
        file = root / ('deploy/release/10-create-roles.sh' if case == 'change-db' else 'src/public.py')
        if case == 'same-byte-replace':
            value = file.read_bytes(); file.rename(root / 'PUBLIC-old-input'); file.write_bytes(value)
        elif case == 'add-source': (root / 'src/PUBLIC-new.py').write_bytes(b'PUBLIC_NEW = 1\n')
        elif case == 'remove-source': file.unlink()
        else: file.write_bytes(b'# PUBLIC changed build input\n')
    def run(args, *, read=False):
        calls.append(args)
        if args[0] == 'brew': return json.dumps(brew)
        if 'version' in args: return '6.1.3'
        if 'build' in args:
            if case in ('build-drift', 'same-byte-replace', 'change-db', 'add-source', 'remove-source'): drift()
            return ''
        assert 'save' in args
        target = Path(args[args.index('--output') + 1])
        image(target, role=target.name.split('.')[0])
        if case == 'save-drift': drift()
        return ''
    def sbom(*args, **kwargs):
        if case == 'sbom-drift': drift()
        return {'public': True}
    def rehearse(entries, revision, version, *, target):
        if case == 'rehearsal-drift': drift()
        return {'status': 'PUBLIC intercepted rehearsal'}
    monkeypatch.setattr(module, 'run', run)
    monkeypatch.setattr(module, 'sbom', sbom)
    monkeypatch.setitem(sys.modules, 'package_rehearsal', SimpleNamespace(rehearse=rehearse))
    if case == 'missing-source': (root / 'src/public.py').unlink()
    target = 'macos-arm64' if case == 'foreign-target' else 'linux-x86_64'
    if case == 'valid':
        module.images(out, SOURCE, VERSION, target=target, cm2=True)
        receipt = json.loads((out / 'image-inventory.json').read_text())
        for role, entry in receipt['images'].items():
            assert entry['source_payload_sha256'] == wanted[role]['sha256']
            assert entry['archive_sha256'] == hashlib.sha256((out / entry['archive']).read_bytes()).hexdigest()
            assert entry['source_payload_sha256'] != entry['archive_sha256']
        inventory = {'schema': 'cortex.image-source-payloads.v1', 'source_revision': SOURCE, 'roles': wanted}
        assert (out / 'source-payload-inventory.json').read_bytes() == json.dumps(inventory, sort_keys=True, separators=(',', ':')).encode() + b'\n'
    else:
        with pytest.raises(RuntimeError): module.images(out, SOURCE, VERSION, target=target, cm2=True)
        assert not (out / 'image-inventory.json').exists()
        assert not (out / 'source-payload-inventory.json').exists()
        if case in ('missing-source', 'foreign-target'): assert not calls and not out.exists()


@pytest.mark.parametrize('stage', ['cm2-images', 'images'])
def test_explicit_cm2_images_cli_route_preserves_foundation_route(stage, tmp_path, monkeypatch):
    module = load('build-candidate.py', monkeypatch)
    calls = []
    monkeypatch.setattr(module, 'images', lambda *args, **kwargs: calls.append((args, kwargs)))
    monkeypatch.setattr(sys, 'argv', ['build-candidate.py', '--target', 'linux-x86_64', stage,
        '--source-sha', SOURCE, '--version', VERSION, '--output', str(tmp_path / 'out')])
    module.main()
    assert len(calls) == 1 and calls[0][1].get('cm2', False) == (stage == 'cm2-images')
