"""Additive R221 author findings: bounded and line-safe CI publication."""
import os
from pathlib import Path
import stat
import subprocess
import sys

import pytest

from test_linux_builder_contract import ROOT, load


def values(root):
    return {'CONTAINERS_STORAGE_CONF': str(root / 'storage.conf'),
            'XDG_DATA_HOME': str(root / 'data')}


def test_environment_fifo_without_reader_refuses_without_blocking(tmp_path):
    path = tmp_path / 'github-env'
    os.mkfifo(path, 0o600)
    before = path.lstat()
    program = '''import importlib.util, pathlib, sys
spec = importlib.util.spec_from_file_location('storage', sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
root = pathlib.Path(sys.argv[2])
try:
    module.publish_environment(root / 'github-env', {
        'CONTAINERS_STORAGE_CONF': str(root / 'storage.conf'),
        'XDG_DATA_HOME': str(root / 'data')})
except (OSError, RuntimeError):
    raise SystemExit(0)
raise SystemExit(3)
'''
    try:
        result = subprocess.run(
            [sys.executable, '-c', program,
             str(ROOT / 'scripts/release/linux_ci_storage.py'), str(tmp_path)],
            capture_output=True, timeout=3, check=False)
    except subprocess.TimeoutExpired:
        pytest.fail('unsafe FIFO blocks before the regular-file refusal')
    assert result.returncode == 0
    assert result.stdout == result.stderr == b''
    after = path.lstat()
    assert stat.S_ISFIFO(after.st_mode)
    assert (after.st_dev, after.st_ino, after.st_size) == (before.st_dev, before.st_ino, before.st_size)


def test_environment_unterminated_predecessor_refuses_without_mutation(tmp_path, monkeypatch):
    module = load('linux_ci_storage.py', monkeypatch)
    path = tmp_path / 'github-env'
    path.write_bytes(b'OLD=preserved')
    path.chmod(0o600)
    with pytest.raises((RuntimeError, OSError)):
        module.publish_environment(path, values(tmp_path))
    assert path.read_bytes() == b'OLD=preserved'


@pytest.mark.parametrize('previous', [b'', b'OLD=preserved\n'])
def test_environment_valid_empty_and_terminated_predecessor_publish_literal_fields(tmp_path, monkeypatch, previous):
    module = load('linux_ci_storage.py', monkeypatch)
    path = tmp_path / 'github-env'
    path.write_bytes(previous)
    path.chmod(0o600)
    environment = values(tmp_path)
    module.publish_environment(path, environment)
    assert path.read_bytes() == previous + ''.join(k + '=' + v + '\n' for k, v in environment.items()).encode()
