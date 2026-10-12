"""R02a native emission is held before any candidate output read."""

import sys
import unittest
from pathlib import Path
from cortex_core.release import inputs as owner_inputs
from cortex_core.release.build import receipts
from cortex_core.release.codec import canonical_bytes

sys.path.insert(0, str(Path(__file__).parents[1] / "release"))
import test_native_policy_unit2_trust_red as trust


class NativeEmissionBoundary(unittest.TestCase):
    def test_native_emit_refuses_before_output_or_evidence_reader(self):
        value, data, receipt, outputs, evidence, acceptance = trust.native_case()
        admitted = owner_inputs.admit_build_input(
            owner_inputs.parse_build_input(canonical_bytes(value)),
            trust.ObservedInputs(value, data),
            acceptance,
        )
        reads = []

        def output_reader(path):
            reads.append(path)
            return outputs[path]

        def evidence_reader(path):
            reads.append(path)
            return evidence[path]

        with self.assertRaisesRegex(ValueError, "^r02_native_emission_unqualified$"):
            receipts.emit(
                admitted,
                receipt["builds"],
                receipt["audit"],
                receipt["evidence"],
                receipt["custody"],
                receipt["cleanup"],
                output_reader,
                evidence_reader,
                acceptance,
            )
        self.assertEqual(reads, [])
