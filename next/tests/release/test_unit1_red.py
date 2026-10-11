"""Frozen deciding R01 unit1 tests; missing product API is an inert RED seam."""
import copy,hashlib,importlib,json,sys,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'src'))
def product(name):
 try:return importlib.import_module('cortex_core.release.'+name)
 except ModuleNotFoundError as e:
  if e.name not in {'cortex_core.release','cortex_core.release.'+name}:raise
  return None
class Unit1(unittest.TestCase):
 def test_schemas_are_product_versioned_closed_contracts(self):
  for name,identity in [('build-input','cortex.build-input.v1'),('build-receipt','cortex.build-receipt.v1')]:
   with self.subTest(name=name):
    path=ROOT/'contracts/release'/ (name+'.schema.json')
    self.assertTrue(path.is_file(),'product schema absent')
    schema=json.loads(path.read_text())
    self.assertEqual(schema['properties']['schema']['const'],identity)
    self.assertFalse(schema['additionalProperties'])
 def test_input_parser_exists_and_refuses_future_output_before_builder(self):
  module=product('inputs');self.assertIsNotNone(module,'R01 input parser absent')
  self.assertTrue(callable(module.parse_build_input))
  self.assertTrue(callable(module.admit_build_input))
 def test_receipt_parser_and_verification_are_one_product_api(self):
  module=product('receipts');self.assertIsNotNone(module,'R01 receipt verifier absent')
  self.assertTrue(callable(module.parse_build_receipt))
  self.assertTrue(callable(module.verify_build_receipt))
if __name__=='__main__':unittest.main(verbosity=2)
