"""Admit a checked native producer's public outputs into the observation chain."""
from __future__ import annotations

from custody import actual_source_file, bound_file, bound_json, public_data, require
from observations import allowed_next, validate_payload


PRODUCERS = {"ALLOCATED": "launcher", "HOST_BOUND": "manager", "INSTALLED": "literal_recorder",
             "ARMED": "restoration_observer", "REBOOT_VERIFIED": "restoration_observer",
             "FAILURE": "literal_recorder", "RETIRED": "cleanup_verifier"}


def admit(authority, phase, receipt_row, *, store=None):
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
    require(store is not None, "native_observation_store_required")
    rows, records = store.read()
    require(records and invocation.get("from_phase") == rows[-1]["phase"]
            and invocation.get("observation_predecessor") == records[-1], "native_invocation_predecessor")
    require(phase in allowed_next(rows), "observation_phase_transition")
    spec = authority.B["commands"][rows[-1]["phase"]][role]
    require(invocation.get("input_digests") == spec["input_digests"], "native_invocation_inputs")
    raw = R.get("raw_evidence")
    require(isinstance(raw, dict) and raw, "native_raw_evidence_missing")
    require(set(raw) == {"facts"} and raw["facts"] == invocation.get("public_output"),
            "native_output_not_bound_to_invocation")
    for row in raw.values():
        bound_file(row)
    payload = R.get("payload")
    require(isinstance(payload, dict), "native_payload_shape")
    public_data(payload)
    facts = bound_json(raw["facts"])
    authority.validate_context(facts)
    require(facts.get("schema") == "cortex.gate-a.run8.native-facts.v2"
            and facts.get("execution_scope") == "native" and facts.get("phase") == phase
            and facts.get("from_phase") == invocation["from_phase"]
            and facts.get("observation_predecessor") == records[-1]
            and facts.get("input_digests") == invocation["input_digests"], "native_raw_contract")
    require(payload == facts.get("facts"), "native_payload_not_derived")
    validate_payload(phase, payload, rows)
    return payload
