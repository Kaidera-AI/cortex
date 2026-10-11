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
