"""Bounded semantic mutations for each C01 implementation/schema source."""
import json
from pathlib import Path
import subprocess
import sys

NEXT = Path(__file__).resolve().parents[1]


def classify(result, expected):
    # Existing predicate extracted unchanged for the review regression probe.
    return "killed" if result.returncode != 0 else "survived"


def suite():
    return subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", str(NEXT / "tests/contract")],
        capture_output=True, text=True,
    )


def changed_json(path, change):
    value = json.loads(path.read_text())
    change(value)
    return (json.dumps(value, indent=2) + "\n").encode()


def run():
    baseline = suite()
    if baseline.returncode:
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
        ("unknown wire error normalization removed", 'if not name.startswith("Proposed") or name not in schemas:', "if False:"),
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
        ("event schema version changed", lambda v: v["properties"]["schema_version"].update(const=2)),
        ("unknown fields accepted", lambda v: v.update(additionalProperties=True)),
        ("delete rule removed", lambda v: v.pop("allOf")),
        ("payload digest unchecked", lambda v: v["properties"]["payload_sha256"].pop("pattern")),
        ("project identity unchecked", lambda v: v["properties"].update(project_id={"type": "string"})),
        ("occurrence time unchecked", lambda v: v["properties"]["occurred_at"].pop("format")),
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
    index_changes = [
        ("unsupported index version", lambda v: v.update(schema_index_version=2)),
        ("event index identity changed", lambda v: v["schemas"]["outbox_event"]["1"].update(schema_id="urn:other:1")),
        ("wire index redirected", lambda v: v["schemas"]["proposed_wire"].update(file="outbox-event.schema.json")),
    ]
    for label, change in index_changes:
        mutants.append((manifest, label, changed_json(manifest, change)))
    survivors = []
    for path, label, data in mutants:
        original = path.read_bytes()
        try:
            path.write_bytes(data)
            result = suite()
            killed = result.returncode != 0
            print(json.dumps({"source": str(path.relative_to(NEXT)), "mutation": label, "killed": killed, "exit_code": result.returncode}), flush=True)
            if not killed:
                survivors.append(label)
        finally:
            path.write_bytes(original)
    print(json.dumps({"mutants": len(mutants), "killed": len(mutants) - len(survivors), "survivors": survivors}), flush=True)
    if survivors or suite().returncode:
        raise SystemExit(1)


if __name__ == "__main__":
    run()
