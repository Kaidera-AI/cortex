"""O01b: withhold recovery eligibility until an encrypted set is complete.

The snapshot argument must be read from a disposable PostgreSQL recovery at
the sealed archive endpoint, not sampled from the mutable source primary.
"""

import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import tarfile
import tempfile

from cortex_core.backup import (BackupError, _lsn, _validated_metadata,
                                _wal_filename, build_manifest,
                                seal_encrypted_bundle)


_SHA = re.compile(r"^[0-9a-f]{64}$")

# One PostgreSQL statement sees one MVCC snapshot. It is deliberately valid
# only on a paused isolated recovery, never on the mutable source primary.
SNAPSHOT_SQL = """
SELECT CASE WHEN pg_is_in_recovery() AND pg_is_wal_replay_paused() THEN
  jsonb_build_object(
    'installation_id', (SELECT id::text FROM core.installations),
    'schema_ledger', (SELECT coalesce(jsonb_agg(jsonb_build_object(
       'id',migration_id,'sha256',sha256) ORDER BY migration_id),'[]'::jsonb)
       FROM core.schema_migrations),
    'consumer_generations', (SELECT coalesce(jsonb_agg(jsonb_build_object(
       'module',module_id,'project',project_id::text,'generation',generation,
       'cursor',applied_cursor,'scan_cursor',scan_cursor,'tenant_id',tenant_id::text)
       ORDER BY installation_id,module_id,tenant_id,project_id,generation),'[]'::jsonb)
       FROM coordination.consumer_project_checkpoints),
    'model_identities', (SELECT coalesce(jsonb_agg(jsonb_build_object(
       'id',id::text,'provider',provider,'model',model,'version',model_version,
       'dimensions',dimensions,'tenant_id',tenant_id::text,'project_id',project_id::text)
       ORDER BY tenant_id,project_id,id),'[]'::jsonb) FROM retrieval.embedding_models),
    'blob_inventory', (SELECT coalesce(jsonb_agg(jsonb_build_object(
       'object_key',object_key,'sha256',sha256,'byte_length',byte_length,
       'tenant_id',tenant_id::text,'project_id',project_id::text)
       ORDER BY tenant_id,project_id,object_key),'[]'::jsonb) FROM core.blob_manifests),
    'record_heads', (SELECT coalesce(jsonb_agg(jsonb_build_object(
       'id',r.id::text,'revision',r.current_revision,'payload_sha256',p.sha256,
       'tenant_id',r.tenant_id::text,'project_id',r.project_id::text,'tombstone',r.tombstone)
       ORDER BY r.tenant_id,r.project_id,r.id),'[]'::jsonb)
       FROM core.records r JOIN core.record_revisions rr ON
         (rr.tenant_id,rr.project_id,rr.record_id,rr.revision)=
         (r.tenant_id,r.project_id,r.id,r.current_revision)
       JOIN core.payloads p ON (p.tenant_id,p.project_id,p.id)=
         (rr.tenant_id,rr.project_id,rr.payload_ref)),
    'vectors', (SELECT coalesce(jsonb_agg(jsonb_build_object(
       'record_id',e.record_id::text,'revision',e.source_revision,
       'model_id',e.model_id::text,
       'sha256',encode(sha256(convert_to(e.embedding::text,'UTF8')),'hex'),
       'tenant_id',e.tenant_id::text,'project_id',e.project_id::text)
       ORDER BY e.tenant_id,e.project_id,e.record_id,e.source_revision,e.model_id),'[]'::jsonb)
       FROM retrieval.embeddings e JOIN core.records r ON
         (e.tenant_id,e.project_id,e.record_id,e.source_revision)=
         (r.tenant_id,r.project_id,r.id,r.current_revision)))::text
  ELSE NULL END;
"""


def read_restored_snapshot(connection):
    """Read the source inventory from one paused, isolated recovery statement."""
    row = connection.execute(SNAPSHOT_SQL).fetchone()
    if row is None or row[0] is None:
        raise BackupError("snapshot requires a paused isolated PostgreSQL recovery")
    value = json.loads(row[0]) if isinstance(row[0], str) else row[0]
    _validated_metadata(value)
    return value


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def seal_complete_bundle(base_dir, archive_dir, archive_end_lsn, metadata, *,
                         segment_bytes, blob_root, recipient, destination):
    """Seal all authoritative blob bytes alongside the O01a PG/WAL members."""
    if not isinstance(metadata, dict) or not {"record_heads", "vectors"} <= set(metadata):
        raise BackupError("full recovered-snapshot inventory is required")
    if blob_root is None:
        raise BackupError("authoritative blob root is required")
    preliminary = build_manifest(base_dir, archive_dir, archive_end_lsn, metadata,
                                 segment_bytes=segment_bytes)
    _verify_native_contents((Path(base_dir)/"backup_manifest").read_bytes(),
                            preliminary["base_files"])
    _validate_native_pg(Path(base_dir), Path(archive_dir), preliminary)
    return seal_encrypted_bundle(base_dir, archive_dir, archive_end_lsn, metadata,
                                 segment_bytes=segment_bytes, blob_root=blob_root,
                                 recipient=recipient, destination=destination)


def _verify_native_contents(body, base_files):
    """Check PostgreSQL's manifest checksum and each SHA256-listed base file."""
    lines = body.splitlines(keepends=True)
    if len(lines) < 2 or not lines[-1].startswith(b'"Manifest-Checksum":'):
        raise BackupError("native PostgreSQL manifest checksum is absent")
    try:
        native = json.loads(body)
    except (UnicodeError, ValueError) as error:
        raise BackupError("native PostgreSQL manifest is invalid") from error
    if (native.get("PostgreSQL-Backup-Manifest-Version") != 2
            or hashlib.sha256(b"".join(lines[:-1])).hexdigest() != native.get("Manifest-Checksum")):
        raise BackupError("native PostgreSQL manifest checksum differs")
    files = native.get("Files")
    if not isinstance(files, list) or not files:
        raise BackupError("native PostgreSQL file inventory is empty")
    listed = set()
    for row in files:
        if not isinstance(row, dict) or not isinstance(row.get("Path"), str):
            raise BackupError("unsupported native PostgreSQL file path")
        path = PurePosixPath(row["Path"])
        if path.is_absolute() or ".." in path.parts or row["Path"] in listed:
            raise BackupError("duplicate or unsafe native PostgreSQL file path")
        listed.add(row["Path"])
        expected = base_files.get(row["Path"])
        if (row.get("Checksum-Algorithm") != "SHA256" or expected is None
                or row.get("Checksum") != expected["sha256"]
                or row.get("Size") != expected["byte_length"]):
            raise BackupError("native PostgreSQL file checksum differs")
    if listed != set(base_files) - {"backup_manifest"}:
        raise BackupError("native PostgreSQL file inventory is incomplete")


def _validate_native_pg(base, archive, manifest):
    """Use upstream PostgreSQL tools for physical files and full WAL records."""
    commands = [
        ["pg_verifybackup", "-q", "-w", str(archive), str(base)],
        ["pg_waldump", "-q", "-p", str(archive), "-t", str(manifest["timeline"]),
         "-s", manifest["backup_start_lsn"], "-e", manifest["archive_end_lsn"]],
    ]
    for command in commands:
        try:
            result = subprocess.run(command, capture_output=True, timeout=300)
        except (OSError, subprocess.SubprocessError) as error:
            raise BackupError("native PostgreSQL backup verification unavailable") from error
        if result.returncode:
            raise BackupError("native PostgreSQL backup or WAL verification failed")


def _expected_members(manifest):
    if not isinstance(manifest, dict) or manifest.get("format") != "cortex-o01a-v1" or manifest.get("usable") is not True:
        raise BackupError("unsupported backup manifest")
    metadata = manifest.get("metadata")
    canonical = _validated_metadata(metadata)
    if not {"record_heads", "vectors"} <= set(metadata):
        raise BackupError("full recovered-snapshot inventory is absent")
    if hashlib.sha256(canonical).hexdigest() != manifest.get("metadata_sha256"):
        raise BackupError("backup metadata digest differs")
    heads = {(row["id"], row["revision"]) for row in metadata["record_heads"]}
    if len(heads) != len(metadata["record_heads"]):
        raise BackupError("duplicate record head")
    if any((row["record_id"], row["revision"]) not in heads for row in metadata["vectors"]):
        raise BackupError("vector revision lacks a matching record head")
    if len({row["object_key"] for row in metadata["blob_inventory"]}) != len(metadata["blob_inventory"]):
        raise BackupError("duplicate blob key")
    segment_bytes = manifest.get("segment_bytes")
    timeline = manifest.get("timeline")
    if (type(segment_bytes) is not int or segment_bytes < (1 << 20)
            or segment_bytes > (1 << 30) or segment_bytes & (segment_bytes - 1)
            or type(timeline) is not int or not 1 <= timeline <= 0xFFFFFFFF):
        raise BackupError("invalid WAL geometry")
    start = _lsn(manifest.get("backup_start_lsn"))
    base_end = _lsn(manifest.get("backup_end_lsn"))
    archive_end = _lsn(manifest.get("archive_end_lsn"))
    if not 0 < start < base_end < archive_end:
        raise BackupError("unbound WAL interval")
    first, last = start // segment_bytes, (archive_end - 1) // segment_bytes
    if last - first > 4096:
        raise BackupError("WAL interval exceeds bound")
    wal = manifest.get("wal_segments")
    names = [_wal_filename(timeline, number, segment_bytes)
             for number in range(first, last + 1)]
    if (not isinstance(wal, list) or [row.get("name") for row in wal] != names
            or any(row.get("byte_length") != segment_bytes for row in wal)):
        raise BackupError("WAL member list is not contiguous")
    base = manifest.get("base_files")
    if not isinstance(base, dict) or not {"PG_VERSION", "backup_label", "backup_manifest"} <= set(base):
        raise BackupError("base backup member list is incomplete")
    expected = {}
    for name, details in base.items():
        relative = PurePosixPath(name)
        if relative.is_absolute() or ".." in relative.parts or name.startswith("/"):
            raise BackupError("invalid base member path")
        expected["base/" + name] = details
    for row in wal:
        expected["wal/" + row["name"]] = row
    for row in metadata["blob_inventory"]:
        key = PurePosixPath(row["object_key"])
        if key.is_absolute() or ".." in key.parts:
            raise BackupError("invalid blob member path")
        expected["blobs/" + key.as_posix()] = row
    for details in expected.values():
        if (not isinstance(details, dict) or not _SHA.fullmatch(str(details.get("sha256", "")))
                or type(details.get("byte_length")) is not int or details["byte_length"] < 0):
            raise BackupError("invalid member digest or length")
    return expected


def verify_complete_bundle(path, identity, expected_manifest_sha256, snapshot):
    """Authenticate, stream-check every member, then compare restored PG state."""
    if not isinstance(expected_manifest_sha256, str) or not _SHA.fullmatch(expected_manifest_sha256):
        raise BackupError("independent manifest digest is required")
    path, identity = Path(path), Path(identity)
    for required in (path, identity):
        try:
            if not stat.S_ISREG(required.lstat().st_mode):
                raise BackupError("encryption input is not a regular file")
        except FileNotFoundError as error:
            raise BackupError("encryption input is missing") from error
    manifest = None
    native_body = None
    seen = set()
    expected = None
    scratch = tempfile.TemporaryDirectory(prefix="cortex-o01b-verify-")
    try:
        with path.open("rb") as encrypted:
            process = subprocess.Popen(["age", "-d", "-i", str(identity)],
                                       stdin=encrypted, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE)
            try:
                with tarfile.open(fileobj=process.stdout, mode="r|") as archive:
                    for member in archive:
                        if not member.isfile() or member.name in seen:
                            raise BackupError("duplicate or non-file backup member")
                        seen.add(member.name)
                        source = archive.extractfile(member)
                        if member.name == "manifest.json":
                            if manifest is not None or member.size > 8 * 1024 * 1024:
                                raise BackupError("backup manifest is duplicated or oversized")
                            body = source.read()
                            if hashlib.sha256(body).hexdigest() != expected_manifest_sha256:
                                raise BackupError("independent backup manifest digest differs")
                            manifest = json.loads(body)
                            if body != _canonical(manifest):
                                raise BackupError("backup manifest is not canonical")
                            expected = _expected_members(manifest)
                            continue
                        if expected is None or member.name not in expected:
                            raise BackupError("unexpected backup member")
                        details = expected[member.name]
                        if member.size != details["byte_length"]:
                            raise BackupError("backup member length differs")
                        digest = hashlib.sha256()
                        captured = bytearray() if member.name == "base/backup_manifest" else None
                        staged = None
                        if member.name.startswith(("base/", "wal/")):
                            destination = Path(scratch.name)/member.name
                            destination.parent.mkdir(parents=True, exist_ok=True)
                            staged = destination.open("xb")
                        try:
                            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                                digest.update(chunk)
                                if staged is not None:
                                    staged.write(chunk)
                                if captured is not None:
                                    captured.extend(chunk)
                                    if len(captured) > 8 * 1024 * 1024:
                                        raise BackupError("native PostgreSQL manifest is oversized")
                        finally:
                            if staged is not None:
                                staged.close()
                        if digest.hexdigest() != details["sha256"]:
                            raise BackupError("backup member digest differs")
                        if captured is not None:
                            native_body = bytes(captured)
                process.stdout.read()
                process.stderr.read()
                if process.wait() != 0:
                    raise BackupError("age authentication failed")
            except Exception:
                if process.poll() is None:
                    process.kill()
                process.wait()
                raise
    except (OSError, ValueError, tarfile.TarError, subprocess.SubprocessError) as error:
        scratch.cleanup()
        if isinstance(error, BackupError):
            raise
        raise BackupError("encrypted backup cannot be authenticated or parsed") from error
    try:
        if expected is None or seen != set(expected) | {"manifest.json"}:
            raise BackupError("backup member set is incomplete")
        if native_body is None:
            raise BackupError("native PostgreSQL manifest member is missing")
        _verify_native_contents(native_body, manifest["base_files"])
        if not isinstance(snapshot, dict) or snapshot != manifest["metadata"]:
            raise BackupError("restored PostgreSQL snapshot differs from bound metadata")
        _validate_native_pg(Path(scratch.name)/"base", Path(scratch.name)/"wal", manifest)
        return {"recoverable": True, "manifest_sha256": expected_manifest_sha256,
                "member_count": len(expected) + 1}
    finally:
        scratch.cleanup()
