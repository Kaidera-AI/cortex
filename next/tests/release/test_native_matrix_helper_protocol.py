"""Helper protocol checks use the actual product result, not stdout or a label."""
import hashlib
from pathlib import Path
import sys
import unittest

import native_matrix_fixture as matrix
import receipt_fixture as legacy
import test_native_matrix_guard_companions_v2 as witness


class HelperProtocol(unittest.TestCase):
    def test_real_cli_positive_reads_bounded_canonical_decision(self):
        result=matrix.cli(legacy.receipt_fixture())
        self.assertEqual(result['decision'],'verified')
        self.assertFalse(result['native_qualified'])

    def test_real_cli_unsupported_profile_reason_is_not_json_decode(self):
        with self.assertRaisesRegex(ValueError,'^invalid_release_policy$'):
            matrix.cli(matrix.family())

    def test_effective_binding_requires_actual_execution_and_loaded_path(self):
        executed=set()
        def observe(frame,event,arg):
            if event=='call':executed.add(frame.f_code.co_filename)
        sys.setprofile(observe)
        try:matrix.api(legacy.receipt_fixture())
        finally:sys.setprofile(None)
        used=sys.modules.get('cortex_core.release.artifacts')
        self.assertIsNotNone(used)
        path=Path(used.__file__).resolve();digest=hashlib.sha256(path.read_bytes()).hexdigest()
        self.assertTrue(witness.effective_binding('cortex_core.release.artifacts',path,digest,executed)['bound'])
        self.assertFalse(witness.effective_binding('cortex_core.release.artifacts',path,digest,set())['bound'])
        self.assertFalse(witness.effective_binding('cortex_core.release.artifacts',path,'f'*64,executed)['bound'])
        self.assertFalse(witness.effective_binding('cortex_core.release.artifacts',path.with_name('unused.py'),digest,executed)['bound'])

    def test_effective_binding_does_not_import_an_unused_module(self):
        key='cortex_core.release.unused_witness_module'
        self.assertNotIn(key,sys.modules)
        result=witness.effective_binding(key,Path(__file__),'0'*64,set())
        self.assertFalse(result['bound'])
        self.assertNotIn(key,sys.modules)


if __name__=='__main__':unittest.main(verbosity=2)
