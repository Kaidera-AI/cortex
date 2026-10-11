"""Checked commands. No shell, credentials, SDK or environment-file discovery."""
from __future__ import annotations

import os
import re
import subprocess
import time
import uuid
from types import SimpleNamespace

from custody import Refusal, actual_source_file, bound_json, exclusive_json, parse_json, private_directory, public_data, require
from payload import pre_load, validate_literals, validate_projects
from observations import allowed_next, validate_payload
from protocols import console_effect, literal_effect, validate_protocol


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
    require(isinstance(specs, dict) and isinstance(specs.get(phase), dict)
            and role in specs[phase], "command_slot_unbound")
    spec = specs[phase][role]
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
    if role in {"payload_extractor", "cached_image_verifier", "runtime", "provider_prepare", "provider_labels", "ordinary_restart"}:
        require(actual_literals is not None, "literal_inputs_required")
        validate_literals(authority, actual_literals, authority.B["delivery_binding"])
    if role in {"cached_image_verifier", "runtime", "provider_prepare", "provider_labels", "ordinary_restart"}:
        # These guards sit on the actual invocation path before any load/up effect.
        pre_load(authority, bound_json(authority.B["manifest"]), actual_literals, authority.B["delivery_binding"])
        validate_projects(authority, actual_literals, authority.B["delivery_binding"],
                          {"CORTEX_PROJECTS_ROOT": actual_literals.get("CORTEX_PROJECTS_ROOT")})
    require(isinstance(spec.get("input_digests"), dict) and spec["input_digests"] and
            spec["input_digests"] == rows[-1]["payload"].get("admitted_command_inputs", {}).get(role),
            "command_observed_inputs_missing")
    if role == "launcher":
        # Observing a fresh check does not extend its lifetime.
        authority.check_measured_receipts(rows[-1]["payload"]["checks"])
    validate_protocol(authority, store, role, spec)
    return argv, spec["timeout_sec"]


def execute(authority, store, role, actual_literals=None, *, runner=subprocess.run, invocation_directory=None,
            clock=time.monotonic, sleep=time.sleep):
    # A single checked effect and its invocation receipt share the writer lock.
    with store.locked():
        result, failure = _execute(authority, store, role, actual_literals, runner=runner,
                                   invocation_directory=invocation_directory, clock=clock, sleep=sleep)
        if failure is not None:
            # Commit STOP before releasing the effect's writer lock.
            rows, _ = store.read()
            if rows[-1]["phase"] != "FAILURE":
                retire_now = failure.pop("retire_now", False)
                store.append_locked("FAILURE", failure)
                if retire_now:
                    for cleanup in ("retirer", "cleanup_verifier"):
                        cleaned, cleanup_failure = _execute(authority, store, cleanup, None,
                            runner=runner, invocation_directory=invocation_directory, clock=clock, sleep=sleep)
                        require(cleanup_failure is None, "console_retirement_failed")
                    facts = parse_json(cleaned.stdout)
                    require(facts.get("phase") == "RETIRED", "console_retirement_proof_missing")
                    store.append_locked("RETIRED", {**facts["facts"],
                        "admitted_command_inputs": authority.B["phase_command_inputs"]["RETIRED"]})
    if failure is not None:
        require(False, "checked_command_failed")
    return result


def _execute(authority, store, role, actual_literals, *, runner, invocation_directory, clock, sleep):
    argv, deadline = checked_command(authority, store, role, actual_literals)
    rows, records = store.read()
    phase = rows[-1]["phase"]
    spec = authority.B["commands"][phase][role]
    clean = {k: os.environ[k] for k in ("PATH", "HOME", "XDG_RUNTIME_DIR") if k in os.environ}
    if actual_literals:
        clean.update({k: v for k, v in actual_literals.items() if isinstance(v, str)})
    # Every operational endpoint is a sealed reviewed command. No fallback SDK,
    # .env reader, browser/SSO or arbitrary shell text exists in this dispatcher.
    require(invocation_directory is not None, "invocation_receipt_directory_required")
    directory = private_directory(invocation_directory)
    started = time.time()
    failure_kind = None
    try:
        protocol = spec.get("protocol", "command")
        if protocol == "bounded-console-v2":
            result = console_effect(authority, store, argv, spec, clean, runner, directory, clock, sleep)
        elif protocol == "persistent-literals-v2":
            result = literal_effect(authority, argv, spec, clean, directory)
        else:
            result = runner(argv, env=clean, capture_output=True, timeout=deadline)
    except subprocess.TimeoutExpired:
        failure_kind = "timeout"
        result = SimpleNamespace(returncode=124, stdout=b"", stderr=b"")
    except OSError:
        failure_kind = "adapter_start_failed"
        result = SimpleNamespace(returncode=125, stdout=b"", stderr=b"")
    except (Refusal, ValueError, TypeError, KeyError):
        failure_kind = "protocol_refused"
        result = SimpleNamespace(returncode=126, stdout=b"", stderr=b"")
    source_key = authority.B["command_inventory"]["root_roles"][role]
    row = authority.B["sealed_files"][source_key]
    receipt = {"schema": "cortex.gate-a.run8.invocation.v2", **authority.context,
               "role": role, "source_sha256": row["sha256"], "executable_path": argv[0],
               "from_phase": phase, "observation_predecessor": records[-1],
               "input_digests": spec["input_digests"],
               "started_at_unix": started, "finished_at_unix": time.time(), "exit": result.returncode,
               "timeout_sec": deadline, "captured_streams_exported": False}
    if failure_kind:
        receipt["failure_kind"] = failure_kind
    nonce = role + "-" + uuid.uuid4().hex
    # Only an explicit public JSON contract is exported. Raw streams stay private.
    if result.returncode == 0 and spec.get("output_contract") == "native-facts-json-v2":
        try:
            require(result.stdout and len(result.stdout) <= 4 * 1024 * 1024, "command_public_output_missing")
            facts = parse_json(result.stdout)
            authority.validate_context(facts)
            require(facts.get("schema") == "cortex.gate-a.run8.native-facts.v2"
                    and facts.get("execution_scope") == "native"
                    and facts.get("from_phase") == phase
                    and facts.get("observation_predecessor") == records[-1]
                    and facts.get("input_digests") == spec["input_digests"], "command_public_output_binding")
            require(set(facts) == set(authority.context) | {"schema", "execution_scope", "phase",
                    "from_phase", "observation_predecessor", "input_digests", "facts"},
                    "command_public_output_fields")
            public_data(facts)
            require(facts.get("phase") in allowed_next(rows), "command_public_output_destination")
            expected_destination = {"launcher": "ALLOCATED", "manager": "HOST_BOUND",
                "literal_recorder": "INSTALLED", "cleanup_verifier": "RETIRED",
                "restoration_observer": "ARMED" if phase == "INSTALLED" else "REBOOT_VERIFIED"}.get(role)
            require(expected_destination is None or facts["phase"] == expected_destination,
                    "command_public_output_destination")
            validate_payload(facts["phase"], facts.get("facts"), rows)
            output_directory = directory / "public-output"
            private_directory(output_directory, create=not output_directory.exists())
            receipt["public_output"] = exclusive_json(output_directory / (nonce + ".json"), facts)
        except (Refusal, ValueError, TypeError, OSError):
            # The effect already happened: retain its exit and typed proof failure.
            receipt["failure_kind"] = "public_output_refused"
    invocation = exclusive_json(directory / (nonce + ".json"), receipt)
    if result.returncode != 0 or receipt.get("failure_kind"):
        allocated = next((r["payload"] for r in rows if r["phase"] == "ALLOCATED"), {})
        failure = {"failed_phase": phase, "refusal": "checked_command_failed",
                   "checked_invocation": invocation,
                   "admitted_command_inputs": authority.B["phase_command_inputs"]["FAILURE"]}
        if spec.get("protocol") == "bounded-console-v2":
            failure["retire_now"] = True
        for key in ("instance_id", "volume_id"):
            if key in allocated:
                failure[key] = allocated[key]
        return result, failure
    return result, None
