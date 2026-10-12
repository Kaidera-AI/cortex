"""Canonical U1 schema/vector binding, no engine or admission implementation."""
import hashlib
import json
from pathlib import Path
import unittest

from jsonschema import Draft202012Validator, FormatChecker

NEXT = Path(__file__).resolve().parents[2]
SCHEMA = NEXT / "contracts/deployment/runtime-policy.schema.json"
VECTORS = Path(__file__).with_name("runtime-policy-vectors.json")
SCHEMA_SHA = "efa7e63ff20dfbab85153bac102e96fc8fe7bf04153148fd1d8ca302f5088b36"
VECTORS_SHA = "de0b89deea4e2f71bf12dba2e5918051c3dc82bc32d931b88a8d723356446185"


class RuntimePolicyContract(unittest.TestCase):
    def test_frozen_canonical_schema_and_vectors(self):
        self.assertEqual(hashlib.sha256(SCHEMA.read_bytes()).hexdigest(), SCHEMA_SHA)
        self.assertEqual(hashlib.sha256(VECTORS.read_bytes()).hexdigest(), VECTORS_SHA)
        Draft202012Validator.check_schema(json.loads(SCHEMA.read_text()))

    def test_all_vector_shapes_and_required_refusal_boundary(self):
        document = json.loads(VECTORS.read_text())
        validator = Draft202012Validator(json.loads(SCHEMA.read_text()), format_checker=FormatChecker())
        held = []
        for case in document["cases"]:
            with self.subTest(case=case["id"]):
                raw = case["raw_utf8"].encode("utf8")
                self.assertEqual(hashlib.sha256(raw).hexdigest(), case["raw_sha256"])
                self.assertIs(validator.is_valid(json.loads(raw)), case["expected_schema_valid"])
                if case["required_phase"] != "shape":
                    held.append(case["id"])
                    self.assertTrue(case["expected_schema_valid"])
                    self.assertEqual(case["required_receiver_verdict"], "REFUSE")
        self.assertEqual(held, ["RF-S-BASELINE", "RF-S-ASOF", "RF-S-DENIAL-DATE",
                                "RF-S-DUPLICATE-VERSION", "RF-S-UTF8-BYTES", "RF-R-FLOAT-1.0"])
        self.assertEqual(document["semantic_raw_required_refusals"], 6)


if __name__ == "__main__":
    unittest.main()
