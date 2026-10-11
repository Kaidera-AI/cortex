"""Checked commands. No shell, credentials, SDK or environment-file discovery."""
from __future__ import annotations

import os
import re
import subprocess
import time
import uuid
from types import SimpleNamespace

from custody import actual_source_file, exclusive_json, private_directory, require
from payload import validate_literals


PHASE_ROLES = {
    "PRE_ALLOC_CHECKED": {"launcher"},
    "ALLOCATED": {"manager", "local_watchdog"},
    "HOST_BOUND": {"admin_preparer", "guest_watchdog", "payload_extractor", "owner_setup",
                   "homebrew_installer", "cached_image_verifier", "runtime", "provider_prepare",
                   "provider_labels", "prerequisite_writer", "kos_installer", "openkai_installer",
                   "omp_binary", "denials", "literal_recorder", "kos_appdb_doctor"},
    "INSTALLED": {"restoration_observer", "ordinary_restart", "kos_appdb_doctor", "denials"},
    "ARMED": {"reboot", "restoration_observer", "kos_appdb_doctor", "denials"},
    "REBOOT_VERIFIED": {"receipt_exporter", "retirer", "cleanup_verifier"},
    "FAILURE": {"receipt_exporter", "retirer", "cleanup_verifier"},
}


def checked_command(authority, store, role, actual_literals=None):
    authority.validate_files()
    rows, _ = store.read()
    require(rows, "command_before_observed_admission")
    phase = rows[-1]["phase"]
    require(role in PHASE_ROLES.get(phase, set()), "command_phase_scope")
    specs = authority.B.get("commands")
    require(isinstance(specs, dict) and role in specs, "command_slot_unbound")
    spec = specs[role]
    require(spec.get("phase") == phase, "command_spec_phase")
    record_key = authority.B["command_inventory"]["root_roles"][role]
    executable = actual_source_file(authority.B["sealed_files"][record_key])
    argv = spec.get("argv")
    require(isinstance(argv, list) and argv and argv[0] == str(executable)
            and all(isinstance(arg, str) and not any(ord(c) < 32 for c in arg) for arg in argv),
            "command_argv_binding")
    require(spec.get("shell") is False and type(spec.get("timeout_sec")) is int
            and 0 < spec["timeout_sec"] <= 900, "command_deadline_shell")
    require(not any(arg in {"-S", "--sign", "--publish", "--all", "--latest", "--ignore"} for arg in argv),
            "command_forbidden_effect")
    if role in {"payload_extractor", "runtime", "provider_prepare", "provider_labels", "ordinary_restart"}:
        require(actual_literals is not None, "literal_inputs_required")
        validate_literals(authority, actual_literals, authority.B["delivery_binding"])
    require(isinstance(spec.get("input_digests"), dict) and spec["input_digests"] and
            spec["input_digests"] == rows[-1]["payload"].get("admitted_command_inputs", {}).get(role),
            "command_observed_inputs_missing")
    return argv, spec["timeout_sec"]


def execute(authority, store, role, actual_literals=None, *, runner=subprocess.run, invocation_directory=None):
    argv, deadline = checked_command(authority, store, role, actual_literals)
    clean = {k: os.environ[k] for k in ("PATH", "HOME", "XDG_RUNTIME_DIR") if k in os.environ}
    if actual_literals:
        clean.update(actual_literals)
    # Every operational endpoint is a sealed reviewed command. No fallback SDK,
    # .env reader, browser/SSO or arbitrary shell text exists in this dispatcher.
    require(invocation_directory is not None, "invocation_receipt_directory_required")
    directory = private_directory(invocation_directory)
    started = time.time()
    failure_kind = None
    try:
        result = runner(argv, env=clean, capture_output=True, timeout=deadline)
    except subprocess.TimeoutExpired:
        failure_kind = "timeout"
        result = SimpleNamespace(returncode=124, stdout=b"", stderr=b"")
    except OSError:
        failure_kind = "adapter_start_failed"
        result = SimpleNamespace(returncode=125, stdout=b"", stderr=b"")
    source_key = authority.B["command_inventory"]["root_roles"][role]
    row = authority.B["sealed_files"][source_key]
    receipt = {"schema": "cortex.gate-a.run8.invocation.v2", **authority.context,
               "role": role, "source_sha256": row["sha256"], "executable_path": argv[0],
               "started_at_unix": started, "finished_at_unix": time.time(), "exit": result.returncode,
               "timeout_sec": deadline, "captured_streams_exported": False}
    if failure_kind:
        receipt["failure_kind"] = failure_kind
    exclusive_json(directory / (role + "-" + uuid.uuid4().hex + ".json"), receipt)
    require(result.returncode == 0, "checked_command_failed")
    return result
