"""Physical safe evidence custody, with captured bytes and complete transport."""

import hashlib
from pathlib import Path
from .receipts import verify_checked_bytes


def capture(path, expected_sha256):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError("physical input required")
    before = path.stat()
    raw = path.read_bytes()
    after = path.stat()

    def identity(s):
        return (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)

    if identity(before) != identity(after):
        raise ValueError("input changed during capture")
    return verify_checked_bytes(raw, expected_sha256), identity(after)


def stage_captured(raw, expected, destination):
    raw = verify_checked_bytes(raw, expected)
    path = Path(destination)
    with path.open("xb") as f:
        f.write(raw)
    if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
        raise ValueError("staged input differs")
    return path


def complete_transport(chunks, expected_bytes, expected_sha256):
    if type(expected_bytes) is not int or expected_bytes < 0:
        raise ValueError("invalid transport size")
    parts = []
    measured = 0
    for chunk in chunks:
        if not isinstance(chunk, bytes):
            raise ValueError("binary transport chunks required")
        measured += len(chunk)
        if measured > expected_bytes:
            raise ValueError("transport exceeds declared complete size")
        parts.append(chunk)
    raw = b"".join(parts)
    if len(raw) != expected_bytes:
        raise ValueError("partial transport")
    return verify_checked_bytes(raw, expected_sha256)


def stage_snapshot(source, raw, captured_identity, expected, destination):
    """Stage the validated buffer only while its source identity remains unchanged."""
    source = Path(source)

    def check():
        current = source.stat(follow_symlinks=False)
        observed = (
            current.st_dev,
            current.st_ino,
            current.st_size,
            current.st_mtime_ns,
            current.st_ctime_ns,
        )
        if source.is_symlink() or observed != captured_identity:
            raise ValueError("source changed after validation")

    check()
    result = stage_captured(raw, expected, destination)
    try:
        check()
    except BaseException:
        result.unlink()
        raise
    return result
