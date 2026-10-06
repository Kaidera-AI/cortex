"""R221: reproduce both native CI failures with portable bounded fixtures."""
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tarfile

import pytest
import yaml

from test_linux_builder_contract import ROOT, load


@pytest.mark.parametrize('component', ['bootstrap', 'candidate'])
@pytest.mark.parametrize('data', [b'', b'public fixture', b'x' * (2 * 1024 * 1024 + 7)])
def test_controller_hashes_without_python311_file_digest(component, data, tmp_path, monkeypatch):
    module = load('bootstrap-linux-runtime.py' if component == 'bootstrap' else 'build-candidate.py', monkeypatch)
    path = tmp_path / 'source.tar'; path.write_bytes(data)
    expected = hashlib.sha256(data).hexdigest()
    monkeypatch.delattr(hashlib, 'file_digest', raising=False)
    if component == 'bootstrap':
        assert module.verify_archive(path, expected) is None
    else:
        assert module.digest(path) == expected


def test_controller_archive_digest_mismatch_refuses_without_new_hashlib_api(tmp_path, monkeypatch):
    module = load('bootstrap-linux-runtime.py', monkeypatch)
    path = tmp_path / 'source.tar'; path.write_bytes(b'altered upstream fixture')
    monkeypatch.delattr(hashlib, 'file_digest', raising=False)
    with pytest.raises(RuntimeError, match='digest mismatch'):
        module.verify_archive(path, 'a' * 64)


def oci_archive(path, *, corrupt=False):
    config = json.dumps({'os': 'linux', 'architecture': 'amd64'}).encode()
    layer = b'literal synthetic layer' * 100003
    layer_id = hashlib.sha256(layer).hexdigest()
    config_id = hashlib.sha256(config).hexdigest()
    manifest = json.dumps({'config': {'digest': 'sha256:' + config_id},
                          'layers': [{'digest': 'sha256:' + layer_id, 'size': len(layer)}]}).encode()
    manifest_id = hashlib.sha256(manifest).hexdigest()
    values = {'index.json': json.dumps({'manifests': [{'digest': 'sha256:' + manifest_id}]}).encode(),
              'blobs/sha256/' + manifest_id: manifest,
              'blobs/sha256/' + config_id: config,
              'blobs/sha256/' + layer_id: b'y' * len(layer) if corrupt else layer}
    with tarfile.open(path, 'w') as archive:
        for name, value in values.items():
            member = tarfile.TarInfo(name); member.size = len(value)
            archive.addfile(member, io.BytesIO(value))
    return layer_id


@pytest.mark.parametrize('corrupt', [False, True])
def test_oci_layer_stream_verifies_literal_bytes_without_file_digest(tmp_path, monkeypatch, corrupt):
    module = load('build-candidate.py', monkeypatch)
    path = tmp_path / 'image.tar'; layer_id = oci_archive(path, corrupt=corrupt)
    monkeypatch.delattr(hashlib, 'file_digest', raising=False)
    if corrupt:
        with pytest.raises(RuntimeError, match='layer checksum differs'):
            module.oci_identity(path, architecture='amd64')
    else:
        value = module.oci_identity(path, architecture='amd64')
        assert value['architecture'] == 'amd64' and value['layers'] == ['sha256:' + layer_id]


def test_stream_digest_has_bounded_reads_and_hashes_all_chunks(monkeypatch):
    module = load('build-candidate.py', monkeypatch)
    value = b'public bytes' * 300000
    calls = []
    class BoundedStream(io.BytesIO):
        def read(self, count=-1):
            assert 0 < count <= 1024 * 1024
            calls.append(count)
            return super().read(count)
    assert callable(getattr(module, 'stream_digest', None)), 'bounded stream digest is absent'
    assert module.stream_digest(BoundedStream(value)) == hashlib.sha256(value).hexdigest()
    assert len(calls) >= 4


def storage(monkeypatch):
    return load('linux_ci_storage.py', monkeypatch)


def test_fresh_storage_binds_all_paths_and_preserves_inherited_boltdb(tmp_path, monkeypatch):
    module = storage(monkeypatch)
    runner = tmp_path / 'runner'; runner.mkdir()
    inherited = tmp_path / 'old-store'; inherited.mkdir()
    old = inherited / 'bolt_state.db'; old.write_bytes(b'old runner state')
    monkeypatch.setenv('XDG_DATA_HOME', str(inherited))
    monkeypatch.setenv('CONTAINERS_STORAGE_CONF', str(inherited / 'storage.conf'))
    environment = module.prepare_storage(runner)
    assert set(environment) == {'CONTAINERS_STORAGE_CONF', 'XDG_DATA_HOME'}
    root = runner / 'cortex-podman'
    assert Path(environment['CONTAINERS_STORAGE_CONF']) == root / 'storage.conf'
    assert Path(environment['XDG_DATA_HOME']) == root / 'data'
    config = (root / 'storage.conf').read_text()
    assert 'driver = "overlay"' in config
    assert 'graphroot = ' + json.dumps(str(root / 'graphroot')) in config
    assert 'runroot = ' + json.dumps(str(root / 'runroot')) in config
    for directory in [root, root / 'graphroot', root / 'runroot', root / 'data']:
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700 and directory.stat().st_uid == os.getuid()
    assert stat.S_IMODE((root / 'storage.conf').stat().st_mode) == 0o600
    assert old.read_bytes() == b'old runner state'
    assert sorted(p.name for p in inherited.iterdir()) == ['bolt_state.db']


@pytest.mark.parametrize('case', ['relative', 'traversal', 'newline', 'missing', 'file', 'linked-parent', 'linked-temp', 'wrong-owner', 'root-user', 'reuse', 'linked-root'])
def test_invalid_storage_custody_refuses_before_podman(tmp_path, monkeypatch, case):
    module = storage(monkeypatch)
    uid = os.getuid()
    runner = tmp_path / 'runner'; runner.mkdir()
    selected = runner
    if case == 'relative': selected = Path('relative-runner')
    elif case == 'traversal': selected = runner / '..' / 'runner'
    elif case == 'newline': selected = Path(str(runner) + '\nINJECTED=yes')
    elif case == 'missing': selected = tmp_path / 'missing'
    elif case == 'file': selected = tmp_path / 'file'; selected.write_bytes(b'not a directory')
    elif case == 'linked-parent':
        alias = tmp_path.parent / (tmp_path.name + '-alias'); alias.symlink_to(tmp_path)
        selected = alias / 'runner'
    elif case == 'linked-temp':
        alias = tmp_path / 'alias'; alias.symlink_to(runner); selected = alias
    elif case == 'wrong-owner': monkeypatch.setattr(module.os, 'getuid', lambda: uid + 1)
    elif case == 'root-user': monkeypatch.setattr(module.os, 'getuid', lambda: 0)
    elif case == 'reuse': (runner / 'cortex-podman').mkdir()
    elif case == 'linked-root': (runner / 'cortex-podman').symlink_to(tmp_path)
    with pytest.raises((RuntimeError, OSError)):
        module.prepare_storage(selected)
    if case not in ('reuse', 'linked-root'):
        assert not (runner / 'cortex-podman').exists()


@pytest.mark.parametrize('case', ['valid', 'linked', 'hardlinked', 'wrong-owner', 'newline-value', 'unknown-field'])
def test_environment_publication_is_safe_and_literal(tmp_path, monkeypatch, case):
    module = storage(monkeypatch)
    uid = os.getuid()
    path = tmp_path / 'github-env'; path.write_text('PREVIOUS=retained\n'); path.chmod(0o600)
    selected = path
    if case == 'linked': selected = tmp_path / 'link'; selected.symlink_to(path)
    if case == 'hardlinked': os.link(path, tmp_path / 'second')
    if case == 'wrong-owner': monkeypatch.setattr(module.os, 'getuid', lambda: uid + 1)
    values = {'CONTAINERS_STORAGE_CONF': str(tmp_path / 'storage.conf'), 'XDG_DATA_HOME': str(tmp_path / 'data')}
    if case == 'newline-value': values['XDG_DATA_HOME'] += '\nINJECTED=yes'
    if case == 'unknown-field': values['UNRELATED'] = 'unsafe'
    if case == 'valid':
        module.publish_environment(selected, values)
        assert path.read_text() == 'PREVIOUS=retained\n' + ''.join(k + '=' + v + '\n' for k, v in values.items())
    else:
        with pytest.raises((RuntimeError, OSError)):
            module.publish_environment(selected, values)
        assert path.read_text() == 'PREVIOUS=retained\n'


def test_workflow_prepares_one_storage_before_any_engine_command():
    workflow = yaml.load((ROOT / '.github/workflows/cortex-linux-candidate.yml').read_text(), Loader=yaml.BaseLoader)
    steps = workflow['jobs']['images']['steps']
    preparation = [i for i, step in enumerate(steps) if 'linux_ci_storage.py' in step.get('run', '')]
    assert len(preparation) == 1, 'Linux workflow lacks exactly one fresh storage preparation'
    body = steps[preparation[0]]['run']
    assert '--runner-temp "$RUNNER_TEMP"' in body and '--github-env "$GITHUB_ENV"' in body
    for i, step in enumerate(steps):
        if 'podman' in step.get('run', '') or 'build-candidate.py' in step.get('run', ''):
            assert i > preparation[0]
    assert not any('system migrate' in step.get('run', '') or 'bolt_state.db' in step.get('run', '') for step in steps)


def test_real_child_commands_observe_same_fresh_storage_without_touching_old_state(tmp_path, monkeypatch):
    module = storage(monkeypatch)
    runner = tmp_path / 'runner'; runner.mkdir()
    environment = module.prepare_storage(runner)
    old = tmp_path / 'bolt_state.db'; old.write_bytes(b'inherited immutable fixture')
    probe = tmp_path / 'podman'
    probe.write_text('#!' + sys.executable + '\n' + '''import json,os,sys
from pathlib import Path
config=Path(os.environ['CONTAINERS_STORAGE_CONF'])
data=Path(os.environ['XDG_DATA_HOME'])
assert config.parent == data.parent and config.is_file() and data.is_dir()
assert '--remote=false' in sys.argv
with (config.parent/'commands.jsonl').open('a') as out:
 out.write(json.dumps(dict(args=sys.argv[1:],config=str(config),data=str(data)))+'\\n')
print('6.1.3')
''')
    probe.chmod(0o700)
    candidate = load('build-candidate.py', monkeypatch)
    for key, value in environment.items(): monkeypatch.setenv(key, value)
    for command in ['version', 'build', 'save', 'run', 'info', 'container', 'network', 'volume', 'secret']:
        assert candidate.run([str(probe), '--remote=false', command], read=True) == '6.1.3'
    rows = [json.loads(line) for line in (runner / 'cortex-podman/commands.jsonl').read_text().splitlines()]
    assert len(rows) == 9 and len({row['config'] for row in rows}) == 1
    assert old.read_bytes() == b'inherited immutable fixture'
