import unittest
import test_deciding_controls as deciding
from receipt_fixture import receipt_fixture


class SemanticOracle(unittest.TestCase):
    def verify(self, **options):
        return deciding.DecidingControls().verify(receipt_fixture(**options))

    def success(self, **options):
        try:result=self.verify(**options)
        except ValueError as error:result={'refusal':str(error)}
        self.assertTrue(hasattr(result,'native_qualified'),str(result))
        return result

    def test_observed_config_clock_matches_admitted_epoch(self):
        with self.assertRaisesRegex(ValueError,'^oci_clock_policy$'):
            self.verify(config_created='2035-01-01T00:00:00Z',config_history=[{'created':'2035-01-01T00:00:00Z','created_by':'/home/noncanonical-builder build'}])

    def test_checked_hash_python_and_final_filename_are_supported(self):
        verified=self.success(checked_pyc=True)
        self.assertFalse(verified.native_qualified)

    def test_reviewed_runtime_config_is_supported_and_unapproved_refuses(self):
        runtime={'Cmd':['python3','/app/module.py']}
        verified=self.success(runtime_config=runtime,approved_runtime_config=runtime)
        self.assertFalse(verified.native_qualified)
        with self.assertRaises(ValueError):self.verify(runtime_config=runtime)

    def test_reviewed_later_layer_replacement_is_supported(self):
        verified=self.success(replace_layer=True)
        self.assertFalse(verified.native_qualified)


if __name__=='__main__':unittest.main(verbosity=2)
