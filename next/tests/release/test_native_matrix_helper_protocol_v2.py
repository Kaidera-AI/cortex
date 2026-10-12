"""Kai05:01b phase-neutral transport contract; old checkpoint remains history."""
import unittest
import native_matrix_fixture as matrix
import test_native_policy_unit2_trust_red as trust


class HelperProtocolV2(unittest.TestCase):
    def test_verified_real_decision_file_transport(self):
        # RuntimeMatrix remains the usability oracle; this checks actual transport.
        result=matrix.cli(matrix.family())
        self.assertEqual(result['decision'],'verified')
        self.assertFalse(result['native_qualified'])

    def test_refused_real_decision_file_transport(self):
        with self.assertRaisesRegex(ValueError,'^accepted_source_origin$'):
            matrix.cli(trust.coherent_replacement())


if __name__=='__main__':unittest.main(verbosity=2)
