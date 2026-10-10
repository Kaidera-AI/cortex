"""O01a: bind a PostgreSQL base backup to continuous archived WAL."""

import hashlib
import json
from pathlib import Path
import re
import stat
from uuid import UUID


class BackupError(ValueError):
    """A backup set is incomplete or cannot be sealed."""


_LSN = re.compile(r"^[0-9A-Fa-f]{1,8}/[0-9A-Fa-f]{1,8}$")
_SHA = re.compile(r"^[0-9a-f]{64}$")
_REQUIRED = frozenset({"installation_id", "schema_ledger", "consumer_generations",
                       "model_identities", "blob_inventory"})


def _lsn(value):
    if not isinstance(value, str) or not _LSN.fullmatch(value):
        raise BackupError("invalid WAL LSN")
    high, low = (int(part, 16) for part in value.split("/"))
    return high << 32 | low


def _regular_file(path):
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError as error:
        raise BackupError("required backup file is missing") from error
    if not stat.S_ISREG(mode):
        raise BackupError("backup file is not a regular file")


def _digest(path):
    _regular_file(path)
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return {"sha256": digest.hexdigest(), "byte_length": path.stat().st_size}


def _validated_metadata(value):
    if not isinstance(value, dict) or set(value) != _REQUIRED:
        raise BackupError("backup metadata is incomplete")
    try:
        UUID(value["installation_id"])
    except (TypeError, ValueError, AttributeError) as error:
        raise BackupError("invalid installation identity") from error
    for key in _REQUIRED - {"installation_id"}:
        if not isinstance(value[key], list):
            raise BackupError("backup metadata list is invalid")
    for entry in value["schema_ledger"]:
        if (not isinstance(entry, dict) or not isinstance(entry.get("id"), str)
                or not _SHA.fullmatch(str(entry.get("sha256", "")))):
            raise BackupError("schema ledger identity is invalid")
    for entry in value["consumer_generations"]:
        if (not isinstance(entry, dict) or not isinstance(entry.get("module"), str)
                or type(entry.get("generation")) is not int or entry["generation"] < 1
                or type(entry.get("cursor")) is not int or entry["cursor"] < 0):
            raise BackupError("consumer generation identity is invalid")
    for entry in value["model_identities"]:
        if (not isinstance(entry, dict) or not all(entry.get(key) for key in
                ("id", "provider", "model", "version"))
                or type(entry.get("dimensions")) is not int or entry["dimensions"] < 1):
            raise BackupError("model identity is invalid")
    for entry in value["blob_inventory"]:
        if (not isinstance(entry, dict) or not isinstance(entry.get("object_key"), str)
                or not _SHA.fullmatch(str(entry.get("sha256", "")))
                or type(entry.get("byte_length")) is not int or entry["byte_length"] < 0):
            raise BackupError("blob identity is invalid")
    try:
        canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    except (TypeError, ValueError) as error:
        raise BackupError("metadata is not canonical JSON") from error
    return canonical


def _wal_filename(timeline, number, segment_bytes):
    per_log = (1 << 32) // segment_bytes
    return f"{timeline:08X}{number // per_log:08X}{number % per_log:08X}"


def build_manifest(base_dir, archive_dir, archive_end_lsn, metadata, *, segment_bytes):
    """Return a usable manifest only for a complete, bound WAL interval.

    archive_end_lsn is the first LSN after the promised archive interval. The
    segment containing its preceding byte must already be archived whole.
    """
    base = Path(base_dir)
    archive = Path(archive_dir)
    if (type(segment_bytes) is not int or segment_bytes < (1 << 20)
            or segment_bytes > (1 << 30) or segment_bytes & (segment_bytes - 1)):
        raise BackupError("invalid PostgreSQL WAL segment size")
    if not base.is_dir() or not archive.is_dir() or base.is_symlink() or archive.is_symlink():
        raise BackupError("backup staging directory is missing or linked")
    canonical_metadata = _validated_metadata(metadata)
    for name in ("backup_manifest", "backup_label", "PG_VERSION"):
        _regular_file(base / name)
    try:
        native = json.loads((base / "backup_manifest").read_text())
    except (UnicodeError, ValueError) as error:
        raise BackupError("PostgreSQL backup manifest is invalid") from error
    if native.get("PostgreSQL-Backup-Manifest-Version") != 2:
        raise BackupError("unsupported PostgreSQL backup manifest")
    system = native.get("System-Identifier")
    ranges = native.get("WAL-Ranges")
    if type(system) is not int or system <= 0 or not isinstance(ranges, list) or len(ranges) != 1:
        raise BackupError("ambiguous PostgreSQL backup WAL range")
    wal_range = ranges[0]
    if not isinstance(wal_range, dict):
        raise BackupError("invalid PostgreSQL backup WAL range")
    timeline = wal_range.get("Timeline")
    if type(timeline) is not int or not 1 <= timeline <= 0xFFFFFFFF:
        raise BackupError("invalid WAL timeline")
    start_text, end_text = wal_range.get("Start-LSN"), wal_range.get("End-LSN")
    start, backup_end, archive_end = _lsn(start_text), _lsn(end_text), _lsn(archive_end_lsn)
    if not 0 < start < backup_end < archive_end or archive_end > (1 << 64) - 1:
        raise BackupError("archived WAL interval is not beyond the base backup")
    label = (base / "backup_label").read_text()
    if f"START WAL LOCATION: {start_text} (file {_wal_filename(timeline, start // segment_bytes, segment_bytes)})" not in label:
        raise BackupError("backup label does not bind the native WAL range")
    if f"START TIMELINE: {timeline}" not in label:
        raise BackupError("backup label timeline differs")

    first, last = start // segment_bytes, (archive_end - 1) // segment_bytes
    if last - first > 4096:
        raise BackupError("WAL interval exceeds bounded manifest size")
    segments = []
    for number in range(first, last + 1):
        name = _wal_filename(timeline, number, segment_bytes)
        path = archive / name
        file_info = _digest(path)
        if file_info["byte_length"] != segment_bytes:
            raise BackupError("archived WAL segment is incomplete")
        segments.append({"name": name, **file_info})
    if not segments:
        raise BackupError("no archived WAL")

    base_files = {}
    for path in sorted(base.rglob("*")):
        if path.is_symlink():
            raise BackupError("linked base backup member is unsupported")
        if path.is_file():
            base_files[path.relative_to(base).as_posix()] = _digest(path)
        elif not path.is_dir():
            raise BackupError("unsupported base backup member")
    return {
        "format": "cortex-o01a-v1", "usable": True,
        "postgres_system_identifier": system, "timeline": timeline,
        "segment_bytes": segment_bytes,
        "backup_start_lsn": start_text, "backup_end_lsn": end_text,
        "archive_end_lsn": archive_end_lsn,
        "metadata": metadata,
        "metadata_sha256": hashlib.sha256(canonical_metadata).hexdigest(),
        "base_files": base_files, "wal_segments": segments,
    }


def seal_encrypted_bundle(base_dir, archive_dir, archive_end_lsn, metadata, *,
                          segment_bytes, recipient, destination, age_binary="age"):
    return None
