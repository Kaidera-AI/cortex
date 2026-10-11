"""Bounded public-file custody. No environment discovery or target operations."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tarfile


class Refusal(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def require(condition: bool, code: str) -> None:
    if not condition:
        raise Refusal(code)


def hex_value(value, length=64):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{%d}" % length, value) is not None


def json_bytes(value) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()


def parse_json(body: bytes | str):
    def unique(rows):
        result = {}
        for key, value in rows:
            require(key not in result, "duplicate_json_field")
            result[key] = value
        return result

    def constant(_):
        raise Refusal("nonfinite_json")

    try:
        return json.loads(body, object_pairs_hook=unique, parse_constant=constant)
    except (ValueError, UnicodeError, TypeError) as error:
        raise Refusal("invalid_json") from error


def physical(path) -> Path:
    require(isinstance(path, (str, Path)), "file_slot_unbound")
    p = Path(path)
    require(p.is_absolute() and ".." not in p.parts, "unsafe_file_path")
    try:
        require(p.resolve(strict=True) == p, "symlink_file_path")
    except (OSError, RuntimeError) as error:
        raise Refusal("file_missing") from error
    return p


def measured_file(path, *, limit=None, read=False):
    p = physical(path)
    fd = -1
    try:
        fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        before = os.fstat(fd)
        require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1,
                "unsafe_file_type")
        require(not before.st_mode & 0o022, "writable_public_file")
        require(limit is None or before.st_size <= limit, "file_too_large")
        digest = hashlib.sha256()
        chunks = []
        size = 0
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
            if read:
                chunks.append(chunk)
        after = os.fstat(fd)
        named = p.lstat()
        identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        require(identity(before) == identity(after) == identity(named) and size == before.st_size,
                "file_changed_during_read")
        row = {"path": str(p), "bytes": size, "sha256": digest.hexdigest()}
        return (row, b"".join(chunks)) if read else row
    except OSError as error:
        raise Refusal("file_unreadable") from error
    finally:
        if fd >= 0:
            os.close(fd)


def bound_file(row, *, read=False, limit=None):
    require(isinstance(row, dict), "file_slot_unbound")
    require(set(row) == {"path", "bytes", "sha256"}, "invalid_file_binding")
    require(type(row["bytes"]) is int and row["bytes"] >= 0 and hex_value(row["sha256"]),
            "file_slot_unbound")
    measured = measured_file(row["path"], read=read, limit=limit)
    actual = measured[0] if read else measured
    require(actual == row, "file_binding_mismatch")
    return measured[1] if read else Path(row["path"])


def bound_json(row):
    return parse_json(bound_file(row, read=True, limit=4 * 1024 * 1024))


def source_record(row):
    """Verify a staged file or a signed archive member before target extraction."""
    require(isinstance(row, dict), "source_record_unbound")
    if row.get("kind") != "archive_member":
        bound_file(row)
        return
    require(set(row) == {"kind", "archive", "member", "bytes", "sha256", "target_path"},
            "archive_source_record_shape")
    require(hex_value(row["sha256"]) and type(row["bytes"]) is int
            and 0 <= row["bytes"] <= 256 * 1024 * 1024, "archive_member_slot")
    name = row["member"]
    require(isinstance(name, str) and name and not name.startswith("/")
            and ".." not in Path(name).parts and "\\" not in name, "archive_member_path")
    archive = bound_file(row["archive"])
    with tarfile.open(archive, "r:*") as stream:
        members = [m for m in stream.getmembers() if m.name == name]
        require(len(members) == 1 and members[0].isfile() and members[0].size == row["bytes"],
                "archive_member_type_size")
        body = stream.extractfile(members[0])
        require(body is not None, "archive_member_unreadable")
        with body:
            actual = hashlib.file_digest(body, "sha256").hexdigest()
        require(actual == row["sha256"], "archive_member_digest")
    target = row["target_path"]
    require(isinstance(target, str) and Path(target).is_absolute() and ".." not in Path(target).parts,
            "archive_target_rule")


def actual_source_file(row):
    source_record(row)
    if row.get("kind") == "archive_member":
        return bound_file({"path": row["target_path"], "bytes": row["bytes"], "sha256": row["sha256"]})
    return Path(row["path"])


def private_directory(path, *, create=False):
    p = Path(path)
    require(p.is_absolute() and ".." not in p.parts, "unsafe_receipt_directory")
    if create:
        require(not p.exists() and not p.is_symlink(), "receipt_directory_exists")
        parent = physical(p.parent)
        require(parent.is_dir() and parent.stat().st_uid == os.getuid(), "receipt_parent_owner")
        p.mkdir(mode=0o700)
        fsync_directory(parent)
    p = physical(p)
    info = p.stat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
            and stat.S_IMODE(info.st_mode) == 0o700, "receipt_directory_custody")
    return p


def fsync_directory(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def exclusive_json(path, value):
    p = Path(path)
    parent = private_directory(p.parent)
    body = json_bytes(value)
    try:
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except OSError as error:
        raise Refusal("receipt_output_exists_or_unsafe") from error
    with os.fdopen(fd, "wb") as stream:
        stream.write(body)
        stream.flush()
        os.fsync(stream.fileno())
    fsync_directory(parent)
    return measured_file(p)


def public_data(value, *, credential_metadata=False):
    """Reject secret-bearing field shapes before an observation is persisted."""
    if isinstance(value, dict):
        if credential_metadata:
            require(set(value) <= {"path", "uid", "gid", "mode", "nlink", "size", "type"},
                    "credential_content_in_receipt")
        for key, item in value.items():
            require(isinstance(key, str) and not key.startswith("_"), "private_internal_receipt_field")
            lower = key.lower()
            require(not re.search(r"password|private_key|secret|bearer|dsn|token_value|token_sha|credential_hash|api_key|access_token|authorization", lower),
                    "secret_field_in_receipt")
            require(lower not in {"env", "environment_values", "authorization", "token"},
                    "secret_field_in_receipt")
            public_data(item, credential_metadata=lower == "credential_metadata")
    elif isinstance(value, list):
        for item in value:
            public_data(item, credential_metadata=credential_metadata)
    else:
        require(value is None or type(value) in {str, int, float, bool}, "invalid_public_receipt_value")
    # Canonical JSON additionally refuses NaN/Infinity.
    json_bytes(value)
