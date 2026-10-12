"""Whole-output oracle and independent-build identities."""

import hashlib
import os
import stat
from pathlib import Path


def _measure(value):
    if isinstance(value, bytes):
        return len(value), hashlib.sha256(value).hexdigest()
    path = Path(value)
    if path.is_symlink() or not path.is_file():
        raise ValueError("physical complete artifact required")

    def identity(value):
        return (
            value.st_dev,
            value.st_ino,
            value.st_size,
            value.st_mtime_ns,
            value.st_ctime_ns,
        )

    before = path.stat(follow_symlinks=False)
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as stream:
        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode) or identity(before) != identity(opened):
            raise ValueError("artifact changed before hash")
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
        after = os.fstat(stream.fileno())
        current = path.stat(follow_symlinks=False)
        if identity(opened) != identity(after) or identity(after) != identity(current):
            raise ValueError("artifact changed during hash")
        return after.st_size, digest


def compare_archives(a, b):
    if _measure(a) != _measure(b):
        raise ValueError("whole archive bytes differ")
    return True


def require_independent(a, b):
    for field in ["build_id", "source", "store", "runroot", "output"]:
        if not a.get(field) or not b.get(field) or a[field] == b[field]:
            raise ValueError("independent build identity missing/shared: " + field)
    roots = [
        Path(row[field]).resolve()
        for row in (a, b)
        for field in ("source", "store", "runroot", "output")
    ]
    if len(set(roots)) != len(roots) or any(
        left.is_relative_to(right) for left in roots for right in roots if left != right
    ):
        raise ValueError("build roots alias, nest or share across roles")
    return True


def compare_closure(a, b):
    if not a or a != b:
        raise ValueError("OCI closure differs or empty")
    return True
