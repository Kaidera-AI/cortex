"""Additional boundary coverage; original frozen regression stays unchanged."""
import copy
import importlib.util
from pathlib import Path
import unittest

s=importlib.util.spec_from_file_location('frozen',Path(__file__).with_name('test_graph_class_predicates.py'));f=importlib.util.module_from_spec(s);s.loader.exec_module(f)
class Boundaries(unittest.TestCase):
 def test_exact_cleanup_and_helper_plus_cache_directory(self):
  b,a,old,new=f.fixture();path='var/log/apt/history.log';old[path]=b'log';b[path]=f.row(old[path])
  helper=f.p.HELPER;new[helper]=f.HELPER;a[helper]=f.row(f.HELPER)
  self.assertIn(path,f.p.validate(b,a,old,new,helper_bytes=f.HELPER))
 def test_directory_child_and_changed_common_RECORD_refuse(self):
  for kind in ['extra_child','changed_common','directory_mode','helper_metadata']:
   b,a,old,new=f.fixture()
   if kind.startswith('directory') or kind=='extra_child':
    a['app/__pycache__']={'type':'5','mode':493 if kind=='extra_child' else 448,'uid':0,'gid':0,'xattrs':{}}
    if kind=='extra_child':a['app/__pycache__/unvalidated']=f.row(b'wrong');new['app/__pycache__/unvalidated']=b'wrong'
   elif kind=='changed_common':new[f.RECORD]=new[f.RECORD].replace(b'abc',b'changed');a[f.RECORD]=f.row(new[f.RECORD])
   else:new[f.p.HELPER]=f.HELPER;a[f.p.HELPER]=f.row(f.HELPER);a[f.p.HELPER]['mode']=384
   with self.subTest(kind=kind),self.assertRaises(ValueError):f.p.validate(b,a,old,new,helper_bytes=f.HELPER)
if __name__=='__main__':unittest.main()
