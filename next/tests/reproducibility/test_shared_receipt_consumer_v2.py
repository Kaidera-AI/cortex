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


class OperationBoundaryV2(unittest.TestCase):
    def test_wrong_argv_refuses_before_affected_operation_record_decode(self):
        from unittest.mock import patch
        from cortex_core.release import receipts as owner

        value, data, receipt, outputs, evidence, acceptance = receipt_fixture()
        admitted = inputs.admit(canonical_bytes(value), data.__getitem__, acceptance)
        operation = receipt["builds"]["A"]["operations"][2]
        operation["argv"].append("foreign-argument")
        target = operation["evidence"]["path"]
        reads, decodes = [], []
        original = owner._json_record

        def reader(path):
            reads.append(path)
            return evidence[path]

        def decode(record, measured):
            decodes.append(record["path"])
            return original(record, measured)

        with patch.object(owner, "_json_record", side_effect=decode):
            with self.assertRaisesRegex(ValueError, "^operation_recipe_argv$"):
                receipts.verify_shared_receipt(
                    canonical_bytes(receipt),
                    admitted,
                    outputs.__getitem__,
                    reader,
                    acceptance,
                )
        self.assertNotIn(target, decodes)
        self.assertIn(target, reads)
        self.assertEqual(len(reads), 41)
