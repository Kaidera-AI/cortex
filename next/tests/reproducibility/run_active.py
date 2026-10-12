"""Explicit original-99 inventory plus versioned integration controls."""

import hashlib
import json
import sys
import unittest
from pathlib import Path

root = Path(__file__).resolve().parents[2]
release = root / "tests/release"
repro = root / "tests/reproducibility"
sys.path[:0] = [str(root / "src"), str(release), str(repro)]
retired = {
    "test_shared_receipt_consumer.DecidingSharedGuards.test_changed_actual_argv_is_rejected_at_raw_operation_evidence": "Kai 2026-10-12 07:53: stricter operation_recipe_argv runs first; versioned boundary control replaces historical phase expectation",
    "test_shared_receipt_consumer.DecidingSharedGuards.test_rehashed_external_layer_is_rejected_at_oci_inventory_body": "Kai 2026-10-12 07:53: historical A/layer.tar transport replaced by versioned layered fixture; original bytes retained",
}
loader = unittest.TestLoader()
suite = unittest.TestSuite()
for name in (
    "test_build_contract_roundtrip",
    "test_build_input",
    "test_build_receipt",
    "test_unit1_red",
):
    suite.addTests(loader.loadTestsFromName(name))
for path in sorted(repro.glob("test_*.py")):
    suite.addTests(loader.loadTestsFromName(path.stem))


def flatten(value):
    for item in value:
        if isinstance(item, unittest.TestSuite):
            yield from flatten(item)
        else:
            yield item


cases = list(flatten(suite))
ids = [case.id() for case in cases]
assert len(ids) == len(set(ids)), "duplicate active inventory"
assert set(retired) <= set(ids), "retired control absent from inventory"
for name, reason in retired.items():
    print(
        json.dumps(
            dict(
                retired=name,
                reason=reason,
                file_sha256=hashlib.sha256(
                    (repro / "test_shared_receipt_consumer.py").read_bytes()
                ).hexdigest(),
            )
        ),
        flush=True,
    )
active = [case for case in cases if case.id() not in retired]
print(json.dumps(dict(active_ids=[case.id() for case in active])), flush=True)
result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(active))
sys.exit(0 if result.wasSuccessful() and not result.skipped else 1)
