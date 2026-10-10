"""Freeze the single measured pip --target RECORD relocation; no generic alias."""
import importlib.util
from pathlib import Path
import unittest
s=importlib.util.spec_from_file_location('frozen',Path(__file__).with_name('test_graph_class_predicates.py'));f=importlib.util.module_from_spec(s);s.loader.exec_module(f)
class TargetLayout(unittest.TestCase):
 def fixture(self):
  b,a,old,new=f.fixture();source='opt/bcrg/lib/python3.13/site-packages/bin/jp.py';cache='opt/bcrg/lib/python3.13/site-packages/bin/__pycache__/jp.cpython-313.pyc';rec='opt/bcrg/lib/python3.13/site-packages/jmespath-1.1.0.dist-info/RECORD'
  for state,files in [(b,old),(a,new)]:
   state[source]=state.pop(f.SOURCE);files[source]=files.pop(f.SOURCE)
   raw=files.pop(f.CACHE);body=f.marshal.dumps(compile(files[source],'/'+source,'exec',dont_inherit=True,optimize=0));raw=raw[:16]+body;files[cache]=raw;state[cache]=f.row(raw);state.pop(f.CACHE)
   files[rec]=files.pop(f.RECORD).replace(b'../../../../../app/__pycache__/sample.cpython-313.pyc',b'../../bin/__pycache__/jp.cpython-313.pyc');state.pop(f.RECORD);state[rec]=f.row(files[rec])
  return b,a,old,new
 def test_exact_measured_target_layout(self):
  try:result=f.p.validate(*self.fixture(),helper_bytes=f.HELPER)
  except ValueError as e:self.fail(str(e))
  self.assertEqual(len(result),2)
 def test_neighbor_alias_refused(self):
  b,a,old,new=self.fixture();rec=next(k for k in old if k.endswith('/RECORD'));old[rec]=old[rec].replace(b'../../bin/',b'../../../bin/');b[rec]=f.row(old[rec])
  with self.assertRaises(ValueError):f.p.validate(b,a,old,new,helper_bytes=f.HELPER)
if __name__=='__main__':unittest.main()
