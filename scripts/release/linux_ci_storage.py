"""Fresh rootless CI storage; never migrate or delete inherited Podman state."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import stat
import sys


def physical_path(path: Path) -> None:
    if (not path.is_absolute() or '..' in path.parts
            or any(ord(char) < 32 for char in str(path))
            or any(parent.is_symlink() for parent in (path, *path.parents))):
        raise RuntimeError('absolute physical CI path required')


def prepare_storage(runner_temp: Path) -> dict[str, str]:
    physical_path(runner_temp)
    uid = os.getuid()
    info = runner_temp.lstat()
    if (uid == 0 or os.geteuid() != uid or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != uid):
        raise RuntimeError('owned non-root CI temporary directory required')
    root = runner_temp / 'cortex-podman'
    # mkdir is exclusive: a reused, linked or interrupted job root refuses.
    root.mkdir(mode=0o700)
    for name in ('graphroot', 'runroot', 'data'):
        (root / name).mkdir(mode=0o700)
    configuration = root / 'storage.conf'
    value = ('[storage]\ndriver = "overlay"\n'
             'graphroot = ' + json.dumps(str(root / 'graphroot')) + '\n'
             'rootless_storage_path = ' + json.dumps(str(root / 'graphroot')) + '\n'
             'runroot = ' + json.dumps(str(root / 'runroot')) + '\n')
    fd = os.open(configuration, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as output:
        output.write(value)
    return {'CONTAINERS_STORAGE_CONF': str(configuration), 'XDG_DATA_HOME': str(root / 'data')}


def publish_environment(path: Path, values: dict[str, str]) -> None:
    if set(values) != {'CONTAINERS_STORAGE_CONF', 'XDG_DATA_HOME'}:
        raise RuntimeError('closed CI storage environment required')
    for value in values.values():
        if not isinstance(value, str):
            raise RuntimeError('literal CI storage paths required')
        physical_path(Path(value))
    physical_path(path)
    # Nonblocking open refuses special files without waiting for a FIFO peer.
    fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'w', encoding='utf-8') as output:
        info = os.fstat(output.fileno())
        selected = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
                or (info.st_dev, info.st_ino) != (selected.st_dev, selected.st_ino)):
            raise RuntimeError('owned single-link CI environment file required')
        if info.st_size and os.pread(output.fileno(), 1, info.st_size - 1) != b'\n':
            raise RuntimeError('terminated CI environment entries required')
        output.write(''.join(key + '=' + value + '\n' for key, value in values.items()))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runner-temp', required=True, type=Path)
    parser.add_argument('--github-env', required=True, type=Path)
    args = parser.parse_args()
    try:
        if (os.environ.get('GITHUB_ACTIONS') != 'true' or platform.system() != 'Linux'
                or platform.machine() != 'x86_64'
                or str(args.runner_temp) != os.environ.get('RUNNER_TEMP')
                or str(args.github_env) != os.environ.get('GITHUB_ENV')):
            raise RuntimeError('explicit native Linux CI job required')
        values = prepare_storage(args.runner_temp)
        publish_environment(args.github_env, values)
        print(json.dumps({'scope': 'fresh job-owned rootless storage', **values}, sort_keys=True))
        return 0
    except (OSError, RuntimeError, ValueError):
        print('fresh CI storage refused; inherited storage was not touched', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
