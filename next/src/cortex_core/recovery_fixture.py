"""X01a external ACK ledger, reconciliation and inert native edition plans.

This module never starts PostgreSQL, Podman or a VM. X01b supplies an admitted
executor and stages authenticated O01b members before using these plans.
"""

import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from uuid import UUID, uuid5, NAMESPACE_URL


class RecoveryError(ValueError):
    """A recovery claim cannot be trusted."""


_SHA = re.compile(r"^[0-9a-f]{64}$")
_RESOURCE = re.compile(r"^kaidera-test-cortex-[a-z0-9-]+$")
_IMAGE = re.compile(r"^[a-zA-Z0-9./_-]+@sha256:[0-9a-f]{64}$")
_ZERO = "0" * 64
_FIELDS = ("request_id", "record_id", "revision", "payload_sha256", "tombstone")


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def _valid_request(request):
    if (not isinstance(request, dict) or set(request) != set(_FIELDS) | {"ack_ns"}
            or any(not isinstance(request.get(key), str) or not request[key]
                   or "\x00" in request[key] for key in ("request_id", "record_id"))
            or type(request.get("revision")) is not int or request["revision"] < 1
            or not _SHA.fullmatch(str(request.get("payload_sha256", "")))
            or type(request.get("tombstone")) is not bool
            or type(request.get("ack_ns")) is not int or request["ack_ns"] <= 0):
        raise RecoveryError("invalid synthetic write receipt")


def _event_rows(body):
    if not body:
        return []
    if not body.endswith(b"\n") or len(body) > 64 * 1024 * 1024:
        raise RecoveryError("ledger is incomplete or exceeds bound")
    events = []
    previous = _ZERO
    for number, line in enumerate(body.splitlines(), 1):
        try:
            record = json.loads(line)
            if (set(record) != {"seq", "previous", "event", "sha256"}
                    or record["seq"] != number or record["previous"] != previous
                    or line != _canonical(record)
                    or not _SHA.fullmatch(str(record["sha256"]))):
                raise RecoveryError("ledger chain differs")
            digest = hashlib.sha256(_canonical({key: record[key] for key in
                                                ("seq", "previous", "event")})).hexdigest()
            if digest != record["sha256"]:
                raise RecoveryError("ledger chain differs")
            events.append(record["event"])
            previous = digest
        except (UnicodeError, ValueError, TypeError, KeyError) as error:
            raise RecoveryError("ledger chain cannot be authenticated") from error
    return events


class AckLedger:
    """Fsynced hash-chained JSONL; ambiguous attempts never count as ACKs."""

    def __init__(self, directory, fault_roots):
        path = Path(directory)
        if not path.is_absolute() or not path.is_dir() or path.is_symlink():
            raise RecoveryError("ledger requires an existing owned directory")
        self.directory = path.resolve(strict=True)
        if not fault_roots:
            raise RecoveryError("faulted volumes must be named")
        for fault_root in fault_roots:
            fault = Path(fault_root).resolve(strict=True)
            if self.directory == fault or self.directory in fault.parents or fault in self.directory.parents:
                raise RecoveryError("ledger overlaps a faulted volume")

    def _path(self, kind):
        if kind not in ("acks", "ambiguous"):
            raise RecoveryError("unknown ledger kind")
        return self.directory / (kind + ".jsonl")

    def _open(self, kind, *, write):
        flags = os.O_NOFOLLOW | (os.O_RDWR | os.O_CREAT | os.O_APPEND if write else os.O_RDONLY)
        try:
            descriptor = os.open(self._path(kind), flags, 0o600)
        except FileNotFoundError:
            if not write:
                return None
            raise
        except OSError as error:
            raise RecoveryError("ledger file cannot be opened safely") from error
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            raise RecoveryError("ledger file is not regular")
        return descriptor

    def _read_locked(self, descriptor):
        os.lseek(descriptor, 0, os.SEEK_SET)
        chunks = []
        size = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > 64 * 1024 * 1024:
                raise RecoveryError("ledger exceeds bound")
            chunks.append(chunk)
        body = b"".join(chunks)
        return body, _event_rows(body)

    def _append(self, kind, event):
        descriptor = self._open(kind, write=True)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            body, events = self._read_locked(descriptor)
            if kind == "acks" and any(row["request_id"] == event["request_id"] for row in events):
                raise RecoveryError("duplicate acknowledged request")
            previous = json.loads(body.splitlines()[-1])["sha256"] if body else _ZERO
            record = {"seq": len(events) + 1, "previous": previous, "event": event}
            record["sha256"] = hashlib.sha256(_canonical(record)).hexdigest()
            data = _canonical(record) + b"\n"
            if os.write(descriptor, data) != len(data):
                raise RecoveryError("short ledger write")
            os.fsync(descriptor)
            directory_fd = os.open(self.directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            return record["sha256"]
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def append_ack(self, request, result):
        """Caller invokes this only after receiving the committed server result."""
        _valid_request(request)
        if (not isinstance(result, dict) or set(result) != set(_FIELDS) | {"state"}
                or result.get("state") != "committed"
                or any(result.get(key) != request[key] for key in _FIELDS)):
            raise RecoveryError("write lacks matching server-confirmed commit")
        return self._append("acks", dict(request))

    def mark_ambiguous(self, request, reason):
        _valid_request(request)
        if reason not in ("timeout_after_send", "connection_lost", "unknown_server_result"):
            raise RecoveryError("invalid ambiguous-write reason")
        return self._append("ambiguous", {**request, "reason": reason})

    def read(self, kind="acks"):
        descriptor = self._open(kind, write=False)
        if descriptor is None:
            return []
        try:
            fcntl.flock(descriptor, fcntl.LOCK_SH)
            return self._read_locked(descriptor)[1]
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def digest(self, kind="acks"):
        descriptor = self._open(kind, write=False)
        if descriptor is None:
            return hashlib.sha256(b"").hexdigest()
        try:
            fcntl.flock(descriptor, fcntl.LOCK_SH)
            body, _ = self._read_locked(descriptor)
            return hashlib.sha256(body).hexdigest()
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)


def capture_write(send, request, ledger, *, clock_ns):
    """Call the native edition client, then record only its confirmed result."""
    if not isinstance(request, dict) or set(request) != set(_FIELDS):
        raise RecoveryError("invalid pending synthetic write")
    _valid_request({**request, "ack_ns": 1})
    if not callable(send) or not callable(clock_ns) or not isinstance(ledger, AckLedger):
        raise RecoveryError("write controller is incomplete")
    try:
        result = send(dict(request))
    except (TimeoutError, ConnectionError) as error:
        reason = "timeout_after_send" if isinstance(error, TimeoutError) else "connection_lost"
        ledger.mark_ambiguous({**request, "ack_ns": clock_ns()}, reason)
        return "ambiguous"
    ledger.append_ack({**request, "ack_ns": clock_ns()}, result)
    return "committed"


def reconcile(acks, recovered, *, fault_ns, detect_mono_ns, read_mono_ns,
              write_mono_ns, rpo_limit_ns, rto_limit_ns):
    """Require an exact recovered ACK prefix, then score this individual trial."""
    if not isinstance(acks, list) or not isinstance(recovered, list) or not acks:
        raise RecoveryError("reconciliation needs acknowledged writes")
    if any(type(value) is not int or value < 0 for value in
           (fault_ns, detect_mono_ns, read_mono_ns, write_mono_ns,
            rpo_limit_ns, rto_limit_ns)):
        raise RecoveryError("invalid recovery timestamps")
    if read_mono_ns < detect_mono_ns or write_mono_ns < detect_mono_ns:
        raise RecoveryError("read or write precedes fault detection")
    seen_ack = set()
    previous_time = 0
    for ack in acks:
        _valid_request(ack)
        if ack["request_id"] in seen_ack or ack["ack_ns"] <= previous_time or ack["ack_ns"] > fault_ns:
            raise RecoveryError("ACK ledger is duplicate, unordered or after fault")
        seen_ack.add(ack["request_id"])
        previous_time = ack["ack_ns"]
    expected = {row["request_id"]: row for row in acks}
    restored = set()
    for row in recovered:
        if (not isinstance(row, dict) or set(row) != set(_FIELDS) | {"state"}
                or row.get("state") != "committed"):
            raise RecoveryError("invalid recovered receipt")
        request_id = row["request_id"]
        if request_id in restored or request_id not in expected:
            raise RecoveryError("duplicate or unexplained recovered receipt")
        if any(row[key] != expected[request_id][key] for key in _FIELDS):
            raise RecoveryError("recovered receipt differs from ACK")
        restored.add(request_id)
    prefix = len(restored)
    if not prefix or restored != {row["request_id"] for row in acks[:prefix]}:
        raise RecoveryError("recovered ACKs are not a contiguous prefix")
    rpo_ns = fault_ns - acks[prefix - 1]["ack_ns"]
    rto_ns = max(read_mono_ns, write_mono_ns) - detect_mono_ns
    return {"recovered_ack_count": prefix,
            "lost_ack_ids": [row["request_id"] for row in acks[prefix:]],
            "rpo_ns": rpo_ns, "rto_ns": rto_ns,
            "passed": rpo_ns <= rpo_limit_ns and rto_ns <= rto_limit_ns}


def native_restore_plan(edition, *, os_name, arch, image_ref, source_volume,
                        archive_volume,
                        target_volume, target_container, cpus, memory_gib):
    """Render commands only; authenticated staging and execution belong to X01b."""
    platforms = {"production-linux": ("Linux", "x86_64", 4),
                 "standalone-macos": ("Darwin", "arm64", 2)}
    if edition not in platforms:
        raise RecoveryError("unknown edition")
    expected_os, expected_arch, max_memory = platforms[edition]
    if (os_name != expected_os or arch != expected_arch
            or not isinstance(image_ref, str) or not _IMAGE.fullmatch(image_ref)
            or any(not isinstance(value, str) or not _RESOURCE.fullmatch(value)
                   for value in (source_volume, archive_volume, target_volume, target_container))
            or len({source_volume, archive_volume, target_volume, target_container}) != 4
            or type(cpus) is not int or not 1 <= cpus <= 2
            or type(memory_gib) is not int or not 1 <= memory_gib <= max_memory):
        raise RecoveryError("edition target or resource boundary is invalid")
    mount = f"type=volume,source={target_volume},destination=/pgdata"
    archive_mount = f"type=volume,source={archive_volume},destination=/wal,readonly"
    commands = [
        ["podman", "volume", "create", target_volume],
        ["podman", "run", "--rm", "--network", "none", "--cpus", str(cpus),
         "--memory", f"{memory_gib}g", "--mount", mount, "--mount", archive_mount,
         "--entrypoint", "pg_verifybackup", image_ref, "-q", "-w", "/wal", "/pgdata"],
        ["podman", "run", "--detach", "--name", target_container, "--network", "none",
         "--cpus", str(cpus), "--memory", f"{memory_gib}g", "--mount", mount,
         "--mount", archive_mount,
         "--entrypoint", "postgres", image_ref, "-D", "/pgdata"],
        ["podman", "exec", target_container, "pg_ctl", "-D", "/pgdata", "status"],
    ]
    return {"edition": edition, "os": os_name, "architecture": arch,
            "image_ref": image_ref, "source_volume": source_volume,
            "archive_volume": archive_volume,
            "restore_volume": target_volume, "target_container": target_container,
            "dry_run": True, "commands": commands,
            "precondition": "X01b authenticated base/WAL/blob staging and edition GO"}


def synthetic_fixture(seed):
    """Deterministic two-tenant data, with no source for customer bytes."""
    if not isinstance(seed, str) or not 1 <= len(seed) <= 128 or "\x00" in seed:
        raise RecoveryError("invalid synthetic fixture seed")
    def identity(label):
        return str(uuid5(NAMESPACE_URL, "cortex-x01a:" + seed + ":" + label))
    tenants, records, vectors, blobs, checkpoints = [], [], [], [], []
    model = identity("model")
    for number in (1, 2):
        tenant, project = identity(f"tenant:{number}"), identity(f"project:{number}")
        record = identity(f"record:{number}")
        body = f"X01 synthetic {seed} tenant {number} revision 1".encode()
        blob = f"X01 synthetic blob {seed} tenant {number}".encode()
        tenants.append({"tenant_id": tenant, "project_id": project})
        records.append({"tenant_id": tenant, "project_id": project,
                        "record_id": record, "revision": 1, "tombstone": False,
                        "payload_hex": body.hex(),
                        "payload_sha256": hashlib.sha256(body).hexdigest()})
        vectors.append({"tenant_id": tenant, "project_id": project,
                        "record_id": record, "revision": 1, "model_id": model,
                        "dimensions": 3, "values": [number, 0, 1]})
        blobs.append({"tenant_id": tenant, "project_id": project,
                      "object_key": f"synthetic/{record}", "bytes_hex": blob.hex(),
                      "sha256": hashlib.sha256(blob).hexdigest()})
        checkpoints.append({"tenant_id": tenant, "project_id": project,
                            "module_id": "synthetic-graph", "generation": 1,
                            "cursor": number})
    return {"installation_id": identity("installation"), "tenants": tenants,
            "records": records, "vectors": vectors, "blobs": blobs,
            "consumer_checkpoints": checkpoints,
            "schema_ledger": [{"id": "synthetic-0001",
                               "sha256": hashlib.sha256(b"synthetic-0001").hexdigest()}],
            "model_identities": [{"id": model, "provider": "fixture",
                                  "model": "synthetic-3d", "version": "1",
                                  "dimensions": 3}]}


def fault_plan(kind, *, edition, primary_container, primary_volume,
               archive_volume, record_id, fixture):
    """Describe a fault; never expose an executable destructive command."""
    if edition not in ("production-linux", "standalone-macos"):
        raise RecoveryError("unknown edition")
    names = (primary_container, primary_volume, archive_volume)
    if (any(not isinstance(name, str) or not _RESOURCE.fullmatch(name) for name in names)
            or len(set(names)) != 3):
        raise RecoveryError("fault resources are not distinct and owned")
    try:
        UUID(record_id)
    except (TypeError, ValueError, AttributeError) as error:
        raise RecoveryError("invalid synthetic record id") from error
    if (not isinstance(fixture, dict) or not isinstance(fixture.get("records"), list)
            or record_id not in {row.get("record_id") for row in fixture["records"]
                                 if isinstance(row, dict)}):
        raise RecoveryError("logical fault record is outside synthetic fixture")
    operations = {"primary": ("stop_owned_primary", primary_container),
                  "storage": ("remove_owned_primary_data", primary_volume),
                  "logical": ("commit_synthetic_corruption", primary_container)}
    if kind not in operations:
        raise RecoveryError("unknown recovery fault")
    operation, target = operations[kind]
    return {"edition": edition, "fault": kind, "operation": operation,
            "target": target, "archive_volume": archive_volume,
            "synthetic_record_id": record_id if kind == "logical" else None,
            "dry_run": True, "requires_go": True}
