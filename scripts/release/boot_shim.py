"""Physical source-owned boot shim custody, independent of native ELF identities."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat
import subprocess

from cortex_v2.clients.native_prerequisite import physical_path

MESSAGE = 'source-owned boot shim missing or mismatched'


def boot_enabled(root: Path) -> bool:
    path = root / 'src/cortex_v2/cli/agent_boot.py'
    if not os.path.lexists(path):
        return False  # Historical/minimal source capsules carry no boot claim.
    physical_path(path)
    return True


def _read(path: Path):
    physical_path(path)
    before = path.lstat()
    def stamp(info):
        return (info.st_dev, info.st_ino, info.st_uid, info.st_mode, info.st_nlink,
                info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
            or before.st_nlink != 1 or before.st_mode & 0o7022
            or not before.st_mode & 0o100 or not 0 < before.st_size <= 65536):
        raise ValueError
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        if stamp(os.fstat(stream.fileno())) != stamp(before):
            raise ValueError
        raw = stream.read(65537)
        if stamp(os.fstat(stream.fileno())) != stamp(before):
            raise ValueError
    physical_path(path)
    if stamp(path.lstat()) != stamp(before) or len(raw) != before.st_size:
        raise ValueError
    return raw, stamp(before)


def freeze(root: Path, stage: Path) -> dict:
    source, target = root / 'scripts/agent-shims/cortex-boot', stage / 'bin/cortex-boot'
    created = None
    try:
        raw, before = _read(source)
        check = subprocess.run(['/bin/bash', '--noprofile', '--norc', '-n', str(source)],
                               env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'},
                               capture_output=True, timeout=5)
        if check.returncode != 0 or _read(source) != (raw, before):
            raise ValueError
        physical_path(target.parent)
        parent = target.parent.lstat()
        if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid()
                or parent.st_mode & 0o7022):
            raise ValueError
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            created = os.fstat(stream.fileno())
            if stream.write(raw) != len(raw):
                raise ValueError
            os.fchmod(stream.fileno(), 0o755)
            stream.flush(); os.fsync(stream.fileno())
        if _read(source) != (raw, before) or _read(target)[0] != raw:
            raise ValueError
        return {'sha256': hashlib.sha256(raw).hexdigest(), 'syntax': 'PASS'}
    except Exception:
        if created is not None:
            try:
                current = target.lstat()
                if (current.st_dev, current.st_ino) == (created.st_dev, created.st_ino):
                    target.unlink()
            except OSError:
                pass
        raise RuntimeError(MESSAGE) from None


def validate(root: Path, stage: Path, receipts) -> dict:
    try:
        if not isinstance(receipts, dict) or set(receipts) != {'cortex-boot'}:
            raise ValueError
        receipt = receipts['cortex-boot']
        if (not isinstance(receipt, dict) or set(receipt) != {'sha256', 'syntax', 'help'}
                or receipt['syntax'] != 'PASS' or receipt['help'] != 'PASS'):
            raise ValueError
        source = _read(root / 'scripts/agent-shims/cortex-boot')[0]
        target = _read(stage / 'bin/cortex-boot')[0]
        if source != target or hashlib.sha256(source).hexdigest() != receipt['sha256']:
            raise ValueError
        return receipt
    except Exception:
        raise RuntimeError(MESSAGE) from None
