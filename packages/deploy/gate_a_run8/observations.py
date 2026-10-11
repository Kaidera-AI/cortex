"""Exclusive, durable observation prefixes; static authority is never rewritten."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import re
import time

from custody import Refusal, bound_json, exclusive_json, hex_value, measured_file, private_directory, public_data, require


SUCCESS = ("PRE_ALLOC_CHECKED", "ALLOCATED", "HOST_BOUND", "INSTALLED", "ARMED",
           "REBOOT_VERIFIED", "RETIRED")
REQUIRED = {
    "PRE_ALLOC_CHECKED": {"checks"},
    "ALLOCATED": {"instance_id", "volume_id"},
    "HOST_BOUND": {"instance_id", "owner_uid", "boot_id", "engine", "host_key_binding",
                   "local_watchdog", "guest_watchdog"},
    "INSTALLED": {"instance_id", "kos_install_receipt", "initial_appdb_doctor", "cortex_check",
                  "container_identity_map", "cm1_sha256", "credential_metadata"},
    "ARMED": {"instance_id", "armed_receipt", "boot_id_before"},
    "REBOOT_VERIFIED": {"instance_id", "boot_id_after", "recovery_receipt"},
    "FAILURE": {"failed_phase", "refusal"},
    "RETIRED": {"retirement_receipt", "custody_complete"},
}


def allowed_next(rows):
    if not rows:
        return {"PRE_ALLOC_CHECKED"}
    last = rows[-1]["phase"]
    if last == "RETIRED":
        return set()
    if last == "FAILURE":
        return {"RETIRED"}
    return {SUCCESS[SUCCESS.index(last) + 1], "FAILURE"}


def validate_payload(phase, payload, rows):
    require(isinstance(payload, dict) and REQUIRED[phase] <= set(payload), "observation_required_facts")
    public_data(payload)
    validate_native_tree(payload)
    optional = {"admitted_command_inputs"}
    if phase == "FAILURE":
        optional |= {"instance_id", "volume_id", "checked_invocation"}
    require(set(payload) <= REQUIRED[phase] | optional, "observation_public_fields")
    if phase == "ALLOCATED":
        require(isinstance(payload["instance_id"], str) and
                re.fullmatch(r"i-[0-9a-f]{8,17}", payload["instance_id"]), "allocated_instance_id")
        require(isinstance(payload["volume_id"], str) and
                re.fullmatch(r"vol-[0-9a-f]{8,17}", payload["volume_id"]), "allocated_volume_id")
    allocated = next((r["payload"] for r in rows if r["phase"] == "ALLOCATED"), None)
    if allocated and phase not in {"FAILURE", "RETIRED"}:
        require(payload.get("instance_id") == allocated["instance_id"], "observation_foreign_instance")
    if phase == "HOST_BOUND":
        require(type(payload["owner_uid"]) is int and payload["owner_uid"] > 0, "root_owner_refused")
        require(payload["local_watchdog"].get("state") == "ARMED" and
                payload["guest_watchdog"].get("state") == "ARMED", "watchdog_not_armed")
    if phase == "INSTALLED":
        require(hex_value(payload["cm1_sha256"]), "cm1_identity")
        require(payload["cortex_check"].get("healthy") is True and
                payload["initial_appdb_doctor"].get("status") == "ready", "installed_effects_missing")
    if phase == "ARMED":
        require(any(r["phase"] == "INSTALLED" for r in rows), "armed_before_install")
        require(isinstance(payload["armed_receipt"], dict), "armed_receipt_missing")
    if phase == "REBOOT_VERIFIED":
        armed = next((r["payload"] for r in rows if r["phase"] == "ARMED"), None)
        require(armed and payload["boot_id_after"] != armed["boot_id_before"], "same_boot_recovery_refused")
    if phase == "RETIRED":
        require(type(payload["custody_complete"]) is bool, "retirement_custody_state")


# Native receipts have explicit nested public fields. File/credential records
# contain metadata only; arbitrary diagnostic/capture fields have no schema.
NESTED_FIELDS = frozenset({
    "path", "bytes", "sha256", "schema", "state", "fixture", "role", "version", "platform",
    "source_revision", "owner_uid", "boot_id", "instance_id", "volume_id", "run_id", "owner_tag",
    "client_token", "operation", "static_binding_sha256", "go_sha256", "sequence", "phase",
    "previous_receipt_sha256", "observed_at_unix", "from_phase", "observation_predecessor",
    "execution_scope", "source_sha256", "executable_path", "started_at_unix", "finished_at_unix",
    "exit", "timeout_sec", "captured_streams_exported", "failure_kind", "public_output",
    "executable", "rootless", "store", "driver", "healthy", "running", "container_id", "image",
    "image_sha256", "image_manifest_digest", "schema_revision", "uid", "gid", "mode", "nlink",
    "size", "type", "port", "database", "user", "volume", "network", "instance", "status",
    "runtime_identity_sha256", "distinct_console_keys", "scan_matches_console", "known_hosts_file",
    "strict_host_key_checking_required", "scan_host", "unit", "enabled", "active", "deadline_unix", "started_unix",
    "timer", "service", "properties", "deadline_minutes", "token_sweep", "shell_path", "template",
    "guard", "fresh_price", "fresh_ami", "operator_ip",
    "cortex-pg", "cortex-provider", "cortex-api", "cortex-embed-worker", "cortex-graph-worker",
    "cortex-pdf-worker", "kos-appdb",
})


def validate_native_tree(value, top=True):
    if isinstance(value, dict):
        if not top:
            require(set(value) <= NESTED_FIELDS, "native_nested_public_fields")
        for key, item in value.items():
            if key == "admitted_command_inputs" or key == "input_digests":
                # These are independently bound to the sealed command contract.
                continue
            validate_native_tree(item, False)
    elif isinstance(value, list):
        for item in value:
            validate_native_tree(item, False)


class ObservationStore:
    def __init__(self, directory, authority, *, create=False):
        self.directory = private_directory(directory, create=create)
        self.authority = authority
        if create:
            exclusive_json(self.directory / "0000-prefix.json", {
                "schema": "cortex.gate-a.run8.prefix.v2", **authority.context, "events": []})

    @contextmanager
    def locked(self):
        path = self.directory / ".append.lock"
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            require(info.st_uid == os.getuid() and info.st_nlink == 1 and info.st_mode & 0o777 == 0o600,
                    "observation_lock_custody")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise Refusal("observation_writer_busy") from error
            yield
        finally:
            os.close(fd)

    def read(self):
        prefixes = sorted(self.directory.glob("[0-9][0-9][0-9][0-9]-prefix.json"))
        require(prefixes, "observation_prefix_missing")
        require([p.name for p in prefixes] == [f"{i:04d}-prefix.json" for i in range(len(prefixes))],
                "observation_prefix_gap")
        previous_events = []
        for index, path in enumerate(prefixes):
            prefix = bound_json(measured_file(path))
            self.authority.validate_context(prefix)
            require(prefix.get("schema") == "cortex.gate-a.run8.prefix.v2", "prefix_schema")
            events = prefix.get("events")
            require(isinstance(events, list) and len(events) == index
                    and events[:index - 1 if index else 0] == previous_events,
                    "prefix_rewrite")
            previous_events = events
        rows = []
        previous_sha = None
        for index, row in enumerate(previous_events, 1):
            expected_path = self.directory / f"{index:04d}-event.json"
            require(row["path"] == str(expected_path), "event_path_escape")
            event = bound_json(row)
            self.authority.validate_context(event)
            require(event.get("schema") == "cortex.gate-a.run8.observation.v2" and
                    event.get("sequence") == index and event.get("previous_receipt_sha256") == previous_sha,
                    "observation_chain_mismatch")
            phase = event.get("phase")
            require(phase in allowed_next(rows), "observation_phase_transition")
            validate_payload(phase, event.get("payload"), rows)
            rows.append(event)
            previous_sha = row["sha256"]
        actual_events = set(self.directory.glob("[0-9][0-9][0-9][0-9]-event.json"))
        require(actual_events == {self.directory / f"{i:04d}-event.json" for i in range(1, len(rows) + 1)},
                "uncommitted_observation_retained")
        return rows, previous_events

    def append(self, phase, payload):
        with self.locked():
            return self.append_locked(phase, payload)

    def append_locked(self, phase, payload):
        """Caller must hold locked() across admission, effect and commit."""
        rows, records = self.read()
        require(phase in allowed_next(rows), "observation_phase_transition")
        validate_payload(phase, payload, rows)
        sequence = len(rows) + 1
        event = {"schema": "cortex.gate-a.run8.observation.v2", **self.authority.context,
                 "sequence": sequence, "phase": phase,
                 "previous_receipt_sha256": records[-1]["sha256"] if records else None,
                 "observed_at_unix": time.time(), "payload": payload}
        record = exclusive_json(self.directory / f"{sequence:04d}-event.json", event)
        exclusive_json(self.directory / f"{sequence:04d}-prefix.json", {
            "schema": "cortex.gate-a.run8.prefix.v2", **self.authority.context,
            "events": [*records, record]})
        return record

    def current(self, expected):
        rows, _ = self.read()
        require(rows and rows[-1]["phase"] == expected, "workflow_current_phase")
        return rows[-1]["payload"]

    def phase(self, name):
        rows, _ = self.read()
        selected = [r["payload"] for r in rows if r["phase"] == name]
        require(len(selected) == 1, "required_observed_phase_missing")
        return selected[0]
