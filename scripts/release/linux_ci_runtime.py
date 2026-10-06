"""Bind fresh Linux CI Podman and Buildah to the installed stock Linuxbrew crun."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import re
import stat
import subprocess

from linux_ci_storage import physical_path

BREW_CRUN = Path('/home/linuxbrew/.linuxbrew/bin/crun')


def _version(path: Path, run) -> str:
    value = run([str(path), '--version'], check=True, text=True, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, timeout=5).stdout
    if (not isinstance(value, str) or len(value.encode()) > 8192 or not value.splitlines()
            or not re.fullmatch(r'crun version [0-9][A-Za-z0-9.+_-]*', value.splitlines()[0])):
        raise RuntimeError('recognized bounded crun version required')
    return value.splitlines()[0]


def prepare_runtime(runner_temp: Path, *, brew_crun: Path = BREW_CRUN,
                    run=subprocess.run) -> dict:
    if os.environ.get('CONTAINERS_CONF_OVERRIDE'):
        raise RuntimeError('inherited runtime override refused')
    physical_path(runner_temp)
    uid = os.getuid()
    root = runner_temp / 'cortex-podman'
    physical_path(root)
    info = root.lstat()
    storage = (root / 'storage.conf').lstat()
    if (uid == 0 or os.geteuid() != uid or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != uid or stat.S_IMODE(info.st_mode) != 0o700
            or not stat.S_ISREG(storage.st_mode) or storage.st_uid != uid
            or storage.st_nlink != 1 or stat.S_IMODE(storage.st_mode) != 0o600):
        raise RuntimeError('existing owned fresh CI storage required')
    if (not brew_crun.is_absolute() or '..' in brew_crun.parts
            or any(ord(c) < 32 for c in str(brew_crun)) or brew_crun.name != 'crun'):
        raise RuntimeError('literal Linuxbrew runtime path required')
    resolved = brew_crun.resolve(strict=True)
    cellar = brew_crun.parent.parent / 'Cellar/crun'
    try:
        relative = resolved.relative_to(cellar)
    except ValueError:
        raise RuntimeError('stock Linuxbrew Cellar runtime required') from None
    if len(relative.parts) != 3 or relative.parts[1:] != ('bin', 'crun'):
        raise RuntimeError('stock Linuxbrew crun layout required')
    selected = resolved.lstat()
    if (not stat.S_ISREG(selected.st_mode) or selected.st_uid not in (0, uid)
            or selected.st_mode & 0o022 or not os.access(resolved, os.X_OK)):
        raise RuntimeError('stock executable runtime required')
    current = _version(brew_crun, run)
    observed = resolved.lstat()
    fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_uid', 'st_mode', 'st_nlink')
    if (brew_crun.resolve(strict=True) != resolved
            or any(getattr(observed, name) != getattr(selected, name) for name in fields)):
        raise RuntimeError('runtime changed during observation')
    inherited = {'path': '/usr/bin/crun'}
    try:
        inherited['version'] = _version(Path('/usr/bin/crun'), run)
    except FileNotFoundError:
        inherited['status'] = 'not-installed'
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, RuntimeError):
        inherited['status'] = 'version-unavailable'
    configuration = root / 'containers.conf'
    fd = os.open(configuration, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as output:
        output.write('[engine]\nruntime = "crun"\n[engine.runtimes]\ncrun = ['
                     + json.dumps(str(brew_crun)) + ']\n')
        output.flush(); os.fsync(output.fileno())
    return {'environment': {'CONTAINERS_CONF': str(configuration), 'BUILDAH_RUNTIME': str(brew_crun)},
            'versions': {'selected': {'path': str(brew_crun), 'resolved_path': str(resolved), 'version': current},
                         'inherited': inherited}}


def publish_runtime(path: Path, values: dict[str, str]) -> None:
    if set(values) != {'CONTAINERS_CONF', 'BUILDAH_RUNTIME'}:
        raise RuntimeError('closed runtime environment required')
    for value in values.values():
        if (not isinstance(value, str) or len(value) > 4096 or not Path(value).is_absolute()
                or '..' in Path(value).parts or any(ord(c) < 32 for c in value)):
            raise RuntimeError('literal runtime paths required')
    physical_path(path)
    fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'w') as output:
        info = os.fstat(output.fileno()); selected = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
                or (info.st_dev, info.st_ino) != (selected.st_dev, selected.st_ino)):
            raise RuntimeError('owned single-link CI environment required')
        if info.st_size and os.pread(output.fileno(), 1, info.st_size - 1) != b'\n':
            raise RuntimeError('terminated CI environment entries required')
        output.write(''.join(k + '=' + v + '\n' for k, v in values.items()))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runner-temp', type=Path, required=True)
    parser.add_argument('--github-env', type=Path, required=True)
    args = parser.parse_args()
    if platform.system() != 'Linux' or platform.machine() != 'x86_64':
        raise RuntimeError('Linux x86_64 runtime binding required')
    result = prepare_runtime(args.runner_temp)
    publish_runtime(args.github_env, result['environment'])
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
