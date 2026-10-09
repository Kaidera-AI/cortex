"""Bounded semantic mutations for each C01 implementation/schema source."""
import json
from pathlib import Path
import subprocess
import sys

NEXT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(NEXT / "tests"))
from test_receipts import classify, report, suite as receipt_suite


def suite():
    return receipt_suite(NEXT / "tests/contract")


def changed_json(path, change):
    value = json.loads(path.read_text())
    change(value)
    return (json.dumps(value, indent=2) + "\n").encode()


def run():
    baseline = suite()
    if classify(baseline, set()) != "survived":
        print(baseline.stdout + baseline.stderr)
        raise SystemExit("Mutation baseline is RED")
    module = NEXT / "src/cortex_core/contracts.py"
    event = NEXT / "contracts/outbox-event.schema.json"
    api = NEXT / "contracts/openapi.json"
    manifest = NEXT / "contracts/provenance.json"
    source = module.read_text()
    replacements = [
        ("validation bypass", "    try:\n        Draft202012Validator", "    return value\n    try:\n        Draft202012Validator"),
        ("format checks removed", "format_checker=FORMAT_CHECKER", "format_checker=None"),
        ("duplicate IDs accepted", " or len(set(ids)) != len(ids)", ""),
        ("references unchecked", "    for node in _walk(document):", "    return len(ids)\n    for node in _walk(document):"),
        ("C02 gaps ignored", 'if "x-c02-freeze-gaps" not in operation:', "if False:"),
        ("unknown wire names admitted", 'raise ContractError("Unsupported wire schema")', "return value"),
        ("invalid timestamp offsets accepted", r"(?:[01]\d|2[0-3]):[0-5]\d", r"\d{2}:\d{2}"),
        ("schema index version ignored", 'if type(manifest.get("schema_index_version")) is not int or manifest["schema_index_version"] != 1:', "if False:"),
        ("event schema identity unchecked", 'if schema.get("$id") != entry["schema_id"]:', "if False:"),
    ]
    mutants = []
    for label, before, after in replacements:
        assert before in source, label
        mutants.append((module, label, source.replace(before, after, 1).encode()))
    event_changes = [
        ("event identity optional", lambda v: v["required"].remove("event_id")),
        ("scope optional", lambda v: v["required"].remove("tenant_id")),
        ("zero revision accepted", lambda v: v["properties"]["aggregate_revision"].update(minimum=0)),
        ("unknown event schema version admitted", lambda v: v["properties"]["schema_version"].pop("const")),
        ("unknown fields accepted", lambda v: v.update(additionalProperties=True)),
        ("delete rule removed", lambda v: v.pop("allOf")),
        ("payload digest unchecked", lambda v: v["properties"]["payload_sha256"].pop("pattern")),
        ("project identity unchecked", lambda v: v["properties"].update(project_id={"type": "string"})),
        ("occurrence time unchecked", lambda v: v["properties"]["occurred_at"].pop("format")),
        ("kind newline admitted", lambda v: v["properties"]["aggregate_kind"].update(pattern="^[a-z][a-z0-9_.-]{0,63}$")),
        ("digest newline admitted", lambda v: v["properties"]["payload_sha256"].update(pattern="^[0-9a-f]{64}$", maxLength=65)),
    ]
    for label, change in event_changes:
        mutants.append((event, label, changed_json(event, change)))
    api_changes = [
        ("queued ACK admitted", lambda v: v["components"]["schemas"]["ProposedWriteReceipt"]["properties"].update(durability={"enum": ["canonical_committed", "queued"]})),
        ("untyped error admitted", lambda v: v["components"]["schemas"]["ProposedError"]["properties"]["code"]["enum"].append("all good")),
        ("untyped readiness admitted", lambda v: v["components"]["schemas"]["ProposedCapabilityState"]["properties"]["state"]["enum"].append("green")),
    ]
    for label, change in api_changes:
        mutants.append((api, label, changed_json(api, change)))
    # Temporary valid alternate schemas isolate manifest dispatch semantics.
    event_probe = NEXT / "contracts/mutation-event.json"
    wire_probe = NEXT / "contracts/mutation-wire.json"
    event_probe.write_bytes(changed_json(event, lambda v: v["properties"]["payload_sha256"].pop("pattern")))
    wire_probe.write_bytes(changed_json(api, api_changes[0][1]))
    index_changes = [
        ("event index selects relaxed schema", lambda v: v["schemas"]["outbox_event"]["1"].update(file=event_probe.name)),
        ("wire index selects relaxed schema", lambda v: v["schemas"]["proposed_wire"].update(file=wire_probe.name)),
    ]
    for label, change in index_changes:
        mutants.append((manifest, label, changed_json(manifest, change)))
    helper = NEXT / "tests/test_receipts.py"
    original_helper = helper.read_text()
    mutants.append((helper, "unrelated errors counted as kills", original_helper.replace('def classify(result, expected):\n', 'def classify(result, expected):\n    return "killed" if result.returncode != 0 else "survived"\n', 1).encode()))
    expected = {
        "validation bypass": "test_contracts.EventContract.test_all_required_fields_refuse_omission",
        "format checks removed": "test_contracts.EventContract.test_payload_identity_kind_digest_and_timestamp_checked",
        "duplicate IDs accepted": "test_contracts.OpenAPIContract.test_duplicate_operation_refused",
        "references unchecked": "test_contracts.OpenAPIContract.test_broken_reference_refused",
        "C02 gaps ignored": "test_contracts.OpenAPIContract.test_c02_gaps_cannot_be_removed",
        "unknown wire names admitted": "test_contracts.WireContract.test_unknown_schema_refused",
        "invalid timestamp offsets accepted": "test_rfc3339.TimestampContract.test_invalid_offsets_are_refused",
        "schema index version ignored": "test_schema_index.SchemaIndexContract.test_unknown_index_version_refused",
        "event schema identity unchecked": "test_schema_index.SchemaIndexContract.test_event_schema_identity_mismatch_refused",
        "event identity optional": "test_contracts.EventContract.test_all_required_fields_refuse_omission",
        "scope optional": "test_contracts.EventContract.test_all_required_fields_refuse_omission",
        "zero revision accepted": "test_contracts.EventContract.test_revision_positive_and_schema_supported",
        "unknown event schema version admitted": "test_review_regressions.ReviewRegressions.test_schema_version_cannot_be_relabelled_by_index",
        "unknown fields accepted": "test_contracts.EventContract.test_unknown_fields_refused",
        "delete rule removed": "test_contracts.EventContract.test_delete_and_tombstone_agree_in_both_directions",
        "payload digest unchecked": "test_contracts.EventContract.test_payload_identity_kind_digest_and_timestamp_checked",
        "project identity unchecked": "test_contracts.EventContract.test_scope_and_object_identifiers_are_canonical_uuids",
        "occurrence time unchecked": "test_contracts.EventContract.test_payload_identity_kind_digest_and_timestamp_checked",
        "kind newline admitted": "test_review_regressions.ReviewRegressions.test_digest_and_kind_reject_final_newline",
        "digest newline admitted": "test_review_regressions.ReviewRegressions.test_digest_and_kind_reject_final_newline",
        "queued ACK admitted": "test_contracts.WireContract.test_queued_delivery_is_not_a_commit_receipt",
        "untyped error admitted": "test_contracts.WireContract.test_capability_state_and_error_codes_are_typed",
        "untyped readiness admitted": "test_contracts.WireContract.test_capability_state_and_error_codes_are_typed",
        "event index selects relaxed schema": "test_contracts.EventContract.test_payload_identity_kind_digest_and_timestamp_checked",
        "wire index selects relaxed schema": "test_contracts.WireContract.test_queued_delivery_is_not_a_commit_receipt",
        "unrelated errors counted as kills": "test_review_regressions.ReviewRegressions.test_setup_import_runner_and_wrong_test_are_inconclusive",
    }
    survivors, inconclusive = [], []
    for path, label, data in mutants:
        original = path.read_bytes()
        try:
            path.write_bytes(data)
            result = suite()
            status = classify(result, {expected[label]})
            print(json.dumps({"source": str(path.relative_to(NEXT)), "mutation": label, "expected_test": expected[label], "status": status, "exit_code": result.returncode, "receipt": report(result), "stdout": result.stdout, "stderr": result.stderr}), flush=True)
            if status == "survived":
                survivors.append(label)
            elif status == "inconclusive":
                inconclusive.append(label)
        finally:
            path.write_bytes(original)
    event_probe.unlink()
    wire_probe.unlink()
    final = suite()
    print(json.dumps({"mutants": len(mutants), "killed": len(mutants) - len(survivors) - len(inconclusive), "survivors": survivors, "inconclusive": inconclusive, "restored_baseline_exit": final.returncode, "stdout": final.stdout, "stderr": final.stderr}), flush=True)
    if survivors or inconclusive or classify(final, set()) != "survived":
        raise SystemExit(1)


if __name__ == "__main__":
    run()
