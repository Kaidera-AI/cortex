"""Guarded local transports; operational endpoints remain sealed native commands."""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
import uuid

from custody import (actual_source_file, exclusive_json, json_bytes, measured_file,
                     parse_json, private_directory, require, fsync_directory)
from keys import bind_console, scan_binding
from recorder import PersistentRecorder


def validate_protocol(authority, store, role, spec):
    protocol = spec.get("protocol", "command")
    require(protocol in {"command", "bounded-console-v2", "persistent-literals-v2"}, "command_protocol")
    if protocol == "bounded-console-v2":
        require(role == "manager", "console_protocol_role")
        # STOP must have a concrete, sealed retirement and verification route.
        for cleanup in ("retirer", "cleanup_verifier"):
            require(cleanup in authority.B.get("commands", {}).get("FAILURE", {}), "console_cleanup_slots_unbound")
        require(authority.B["commands"]["FAILURE"]["cleanup_verifier"].get("output_contract") ==
                "native-facts-json-v2", "console_cleanup_native_contract")
    if protocol == "persistent-literals-v2":
        require(role == "literal_recorder", "literal_protocol_role")
        rows = spec.get("literals")
        require(isinstance(rows, list) and rows and len({r.get("name") for r in rows}) == len(rows),
                "literal_protocol_membership")
        for row in rows:
            source = authority.B["sealed_files"].get(row["file"]["path"])
            require(source is not None and measured_file(actual_source_file(source)) == row["file"],
                    "literal_not_in_executable_closure")
            require(isinstance(row.get("public_contract"), dict) and isinstance(row.get("expected_facts"), dict),
                    "literal_observer_contract_unbound")
        private_directory(Path(spec["cwd"]))
    return protocol


def console_effect(authority, store, argv, spec, env, runner, directory, clock, sleep):
    latest = {}
    allocation = store.phase("ALLOCATED")
    def invoke():
        authority.validate_files()
        result = runner(argv, env=env, capture_output=True, timeout=spec["timeout_sec"])
        require(result.returncode == 0, "console_producer_failed")
        require(len(result.stdout) <= 4 * 1024 * 1024, "console_producer_output_bound")
        reply = parse_json(result.stdout)
        authority.validate_context(reply)
        require(reply.get("schema") == "cortex.gate-a.run8.console-attempt.v2"
                and reply.get("instance_id") == allocation["instance_id"]
                and reply.get("local_watchdog", {}).get("state") == "ARMED", "console_attempt_context_watchdog")
        require(set(reply) == set(authority.context) | {"schema", "instance_id", "local_watchdog",
                "console_output", "scan_host", "scan_text", "native_output"}, "console_attempt_fields")
        latest.clear(); latest.update(reply)
        return reply["console_output"]
    retired = []
    outcome = bind_console(invoke, lambda: retired.append(True), clock, sleep, watchdog_armed=True)
    if outcome["exit"]:
        return SimpleNamespace(returncode=outcome["exit"], stdout=b"", stderr=b"", retire_now=True)
    binding = scan_binding(latest["console_output"], latest["scan_text"], latest["scan_host"])
    # Write the unique matching public key once into private custody; later SSH
    # consumers bind this observed file, never a trust-on-first-use response.
    folder = directory / "known-hosts"
    private_directory(folder, create=not folder.exists())
    path = folder / (uuid.uuid4().hex + ".known_hosts")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(latest["scan_text"].encode()); stream.flush(); os.fsync(stream.fileno())
    fsync_directory(folder)
    native = latest["native_output"]
    require(isinstance(native, dict) and isinstance(native.get("facts"), dict), "console_native_output_missing")
    native["facts"]["host_key_binding"] = {**binding, "known_hosts_file": measured_file(path),
                                          "strict_host_key_checking": True}
    return SimpleNamespace(returncode=0, stdout=json_bytes(native), stderr=b"")


def literal_effect(authority, argv, spec, env, directory):
    folder = directory / "literal-observations"
    private_directory(folder, create=not folder.exists())
    nonce = uuid.uuid4().hex
    with_recorder = PersistentRecorder(argv, spec["cwd"], env)
    try:
        for index, row in enumerate(spec["literals"], 1):
            def observer(out, err):
                facts = parse_json(out) if out.strip() else {}
                require(facts == row["expected_facts"], "literal_observed_effect_mismatch")
                return facts
            receipt = with_recorder.run(row["name"], row["file"], timeout=spec["timeout_sec"],
                                        observer=observer, public_contract=row["public_contract"])
            exclusive_json(folder / (nonce + f"-{index:04d}.json"), {**authority.context, **receipt})
    finally:
        with_recorder.close()
    # A separately sealed final native observer may produce the install receipt;
    # scalar literal records alone never synthesize an INSTALLED observation.
    return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
