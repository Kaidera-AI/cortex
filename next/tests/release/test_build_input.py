"""Deciding input-policy RED controls, not collection failures."""
import copy,sys,unittest
from pathlib import Path
from fixture import build_input,canonical,digest
from test_unit1_red import product
class BuildInput(unittest.TestCase):
 def api(self):
  module=product('inputs');self.assertIsNotNone(module,'product admission absent');return module
 def test_complete_known_input_admits_without_future_output(self):
  m=self.api();value,data=build_input();parsed=m.parse_build_input(canonical(value))
  policy={'input_sha256':digest(value),'policy_sha256':'e'*64,'qualification_scope':'fixture'}
  admitted=m.admit_build_input(parsed,lambda path:data[path],policy)
  self.assertEqual(admitted.input_sha256,digest(value))
  self.assertEqual(admitted.target['module_id'],'api')
  self.assertFalse(admitted.native_qualified)
 def test_noncanonical_encoding_duplicate_float_unicode_and_size_refuse(self):
  m=self.api();value,data=build_input()
  bad=[b'{"x":1,"x":2}\n',b'{"x":1.5}\n',b'\xef\xbb\xbf{}\n',b'{"x":"\\ud800"}\n',b'x'*(1024*1024+1),canonical(value)+b' ']
  for raw in bad:
   with self.subTest(raw_kind=len(raw)),self.assertRaises(ValueError):m.parse_build_input(raw)
 def test_future_outputs_unknown_fields_platform_and_bool_integer_refuse(self):
  m=self.api();value,data=build_input()
  for edit in ['future','field','platform','integer']:
   bad=copy.deepcopy(value)
   if edit=='future':bad['output_sha256']='f'*64
   elif edit=='field':bad['source']['unknown']='value'
   elif edit=='platform':bad['target']['oci_platform']='windows/amd64'
   else:bad['format_version']=True
   with self.subTest(edit=edit),self.assertRaises(ValueError):m.parse_build_input(canonical(bad))
 def test_changed_inventory_recipe_platform_or_policy_has_zero_builder_effects(self):
  m=self.api();value,data=build_input();policy={'input_sha256':digest(value),'policy_sha256':'e'*64,'qualification_scope':'fixture'};effects=[]
  for edit in ['inventory','recipe','platform','policy','bytes']:
   bad=copy.deepcopy(value);actual=dict(data)
   if edit=='inventory':bad['source']['inventory']=[]
   elif edit=='recipe':bad['recipe']['recipe_id']='foreign'
   elif edit=='platform':bad['target']['oci_platform']='linux/arm64'
   elif edit=='policy':bad['admission_policy']['policy_sha256']='f'*64
   else:actual['source/module.py']=b'foreign bytes'
   try:
    m.admit_build_input(m.parse_build_input(canonical(bad)),lambda path:actual[path],policy);effects.append(edit)
   except ValueError:pass
  self.assertEqual(effects,[])
if __name__=='__main__':unittest.main(verbosity=2)
