import copy
import sys
import unittest
from pathlib import Path
from cortex_core.release.build import inputs, receipts
from cortex_core.release.codec import canonical_bytes
from cortex_core.release.contracts import VerifiedBuildReceipt

sys.path.insert(0, str(Path(__file__).parents[1] / "release"))
from receipt_fixture import receipt_fixture


class SharedReceipt(unittest.TestCase):
    def emit(self, receipt, verified, outputs, evidence, policy):
        return receipts.emit(
            verified,
            receipt["builds"],
            receipt["audit"],
            receipt["evidence"],
            receipt["custody"],
            receipt["cleanup"],
            outputs.__getitem__,
            evidence.__getitem__,
            policy,
        )

    def test_owner_fixture_roundtrips_through_product_emitter_without_translation(self):
        value, data, receipt, outputs, evidence, policy = receipt_fixture()
        verified = inputs.admit(canonical_bytes(value), data.__getitem__, policy)
        raw, admitted = self.emit(receipt, verified, outputs, evidence, policy)
        self.assertEqual(raw, canonical_bytes(receipt))
        self.assertIsInstance(admitted, VerifiedBuildReceipt)
        self.assertFalse(admitted.native_qualified)
        self.assertEqual(admitted.input_sha256, verified.input_sha256)

    def test_missing_cleanup_and_same_build_refuse_actual_owner_verification(self):
        value, data, receipt, outputs, evidence, policy = receipt_fixture()
        verified = inputs.admit(canonical_bytes(value), data.__getitem__, policy)
        for kind in ["cleanup", "same-build", "binding"]:
            bad = copy.deepcopy(receipt)
            if kind == "cleanup":
                bad["cleanup"]["complete"] = False
            elif kind == "same-build":
                bad["builds"]["B"]["observed_builder"]["store_id"] = bad["builds"]["A"][
                    "observed_builder"
                ]["store_id"]
            else:
                bad["builds"]["A"]["source_sha256"] = "f" * 64
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                self.emit(bad, verified, outputs, evidence, policy)

    def test_mutated_original_archive_or_evidence_refuses_before_handback(self):
        value, data, receipt, outputs, evidence, policy = receipt_fixture()
        verified = inputs.admit(canonical_bytes(value), data.__getitem__, policy)
        changed = dict(outputs)
        changed["A/image.tar"] += b"changed"
        with self.assertRaises(ValueError):
            self.emit(receipt, verified, changed, evidence, policy)
        changed = dict(evidence)
        changed["evidence/A/build.json"] += b"changed"
        with self.assertRaises(ValueError):
            self.emit(receipt, verified, outputs, changed, policy)


class DecidingSharedGuards(unittest.TestCase):
    def test_changed_input_cannot_adopt_previous_receipt(self):
        from cortex_core.release.codec import sha256

        value, data, receipt, outputs, evidence, policy = receipt_fixture()
        value["source"]["revision"] = "c" * 40
        raw = canonical_bytes(value)
        changed_policy = dict(policy, input_sha256=sha256(raw))
        other = inputs.admit(raw, data.__getitem__, changed_policy)
        with self.assertRaisesRegex(ValueError, "receipt_input_binding"):
            receipts.verify_shared_receipt(
                canonical_bytes(receipt),
                other,
                outputs.__getitem__,
                evidence.__getitem__,
                changed_policy,
            )

    def test_changed_actual_argv_is_rejected_at_raw_operation_evidence(self):
        value, data, receipt, outputs, evidence, policy = receipt_fixture()
        verified = inputs.admit(canonical_bytes(value), data.__getitem__, policy)
        receipt["builds"]["A"]["operations"][2]["argv"].append("foreign-argument")
        with self.assertRaisesRegex(ValueError, "operation_raw_evidence"):
            receipts.verify_shared_receipt(
                canonical_bytes(receipt),
                verified,
                outputs.__getitem__,
                evidence.__getitem__,
                policy,
            )

    def test_rehashed_external_layer_is_rejected_at_oci_inventory_body(self):
        from cortex_core.release.codec import sha256

        value, data, receipt, outputs, evidence, policy = receipt_fixture()
        verified = inputs.admit(canonical_bytes(value), data.__getitem__, policy)
        outputs["A/layer.tar"] += b"foreign-layer-data"
        row = next(
            row
            for row in receipt["builds"]["A"]["outputs"]
            if row["path"] == "A/layer.tar"
        )
        row.update(bytes=len(outputs[row["path"]]), sha256=sha256(outputs[row["path"]]))
        with self.assertRaisesRegex(ValueError, "oci_inventory_body"):
            receipts.verify_shared_receipt(
                canonical_bytes(receipt),
                verified,
                outputs.__getitem__,
                evidence.__getitem__,
                policy,
            )


class ClosedWireAndPhase(unittest.TestCase):
    def test_wire_alias_unknown_field_bool_and_digest_version_refuse(self):
        value, data, receipt, outputs, evidence, policy = receipt_fixture()
        verified = inputs.admit(canonical_bytes(value), data.__getitem__, policy)
        for kind in ["size-alias", "unknown", "bool", "version", "path"]:
            bad = copy.deepcopy(receipt)
            if kind == "size-alias":
                row = bad["builds"]["A"]["outputs"][0]
                row["size"] = row.pop("bytes")
            elif kind == "unknown":
                bad["owner_override"] = True
            elif kind == "bool":
                bad["format_version"] = True
            elif kind == "version":
                bad["schema"] = "cortex.build-receipt.v2"
            else:
                bad["evidence"]["index"]["path"] = "../outside.json"
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                receipts.verify_shared_receipt(
                    canonical_bytes(bad),
                    verified,
                    outputs.__getitem__,
                    evidence.__getitem__,
                    policy,
                )

    def test_deployment_lookalike_cannot_be_used_as_build_admission(self):
        value, data, receipt, outputs, evidence, policy = receipt_fixture()
        with self.assertRaises(ValueError):
            receipts.emit(
                {"schema": "cortex.deployment.v1", "verified": True},
                receipt["builds"],
                receipt["audit"],
                receipt["evidence"],
                receipt["custody"],
                receipt["cleanup"],
                outputs.__getitem__,
                evidence.__getitem__,
                policy,
            )
