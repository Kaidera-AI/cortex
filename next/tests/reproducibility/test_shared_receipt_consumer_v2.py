"""Versioned stricter-owner controls; historical bodies remain unchanged."""

import sys
import unittest
from pathlib import Path
from cortex_core.release.build import inputs, receipts
from cortex_core.release.codec import canonical_bytes, sha256

sys.path.insert(0, str(Path(__file__).parents[1] / "release"))
from receipt_fixture import receipt_fixture


class LayerTransportV2(unittest.TestCase):
    def test_rehashed_external_layer_refuses_at_owner_inventory_body(self):
        value, data, receipt, outputs, evidence, acceptance = receipt_fixture(
            replace_layer=True
        )
        admitted = inputs.admit(canonical_bytes(value), data.__getitem__, acceptance)
        row = next(
            row
            for row in receipt["builds"]["A"]["outputs"]
            if row["path"].startswith("A/layer-")
        )
        outputs[row["path"]] += b"foreign-layer-data"
        row.update(bytes=len(outputs[row["path"]]), sha256=sha256(outputs[row["path"]]))
        with self.assertRaisesRegex(ValueError, "^oci_inventory_body$"):
            receipts.verify_shared_receipt(
                canonical_bytes(receipt),
                admitted,
                outputs.__getitem__,
                evidence.__getitem__,
                acceptance,
            )
