"""Admit a checked native producer's public outputs into the observation chain."""
from __future__ import annotations

from custody import actual_source_file, bound_file, bound_json, public_data, require


PRODUCERS = {"ALLOCATED": "launcher", "HOST_BOUND": "manager", "INSTALLED": "literal_recorder",
             "ARMED": "restoration_observer", "REBOOT_VERIFIED": "restoration_observer",
             "FAILURE": "literal_recorder", "RETIRED": "cleanup_verifier"}


def admit(authority, phase, receipt_row):
    R = bound_json(receipt_row)
    authority.validate_context(R)
    require(R.get("schema") == "cortex.gate-a.run8.native-output.v2" and R.get("phase") == phase,
            "native_output_phase")
    require(R.get("execution_scope") == "native", "synthetic_is_not_native")
    role = PRODUCERS[phase]
    inventory = authority.validate_files()
    source_key = inventory["root_roles"][role]
    source = authority.B["sealed_files"][source_key]
    actual = actual_source_file(source)
    expected_sha = source["sha256"]
    invocation = bound_json(R["checked_invocation"])
    authority.validate_context(invocation)
    require(invocation.get("schema") == "cortex.gate-a.run8.invocation.v2" and
            invocation.get("role") == role and invocation.get("source_sha256") == expected_sha and
            invocation.get("executable_path") == str(actual) and invocation.get("exit") == 0,
            "native_checked_invocation")
    raw = R.get("raw_evidence")
    require(isinstance(raw, dict) and raw, "native_raw_evidence_missing")
    for row in raw.values():
        bound_file(row)
    payload = R.get("payload")
    require(isinstance(payload, dict), "native_payload_shape")
    public_data(payload)
    return payload
