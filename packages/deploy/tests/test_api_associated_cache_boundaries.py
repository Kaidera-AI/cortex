"""All associated caches are inspected, with fail-closed path/presence boundaries."""
import copy
from pathlib import Path
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import api_source_predicates as predicate
from test_api_source_predicates import fixture,caches,OLD,NEW
class AssociatedCacheBoundaries(unittest.TestCase):
 def check(self,f):return predicate.validate(*f,old_revision=OLD,new_revision=NEW,envelope={'old_source_revision':OLD,'new_source_revision':NEW},role='api')
 def test_cache_addition_and_deletion_refuse(self):
  for side in (0,1):
   for name in ('db_tls','main'):
    f=fixture();caches(f);path='app/__pycache__/'+name+'.cpython-314.pyc';f[side].pop(path);f[side+2].pop(path)
    with self.subTest(side=side,cache=name),self.assertRaises(ValueError):self.check(f)
 def test_unsupported_associated_tag_is_not_ignored(self):
  f=fixture();caches(f)
  for side in (0,1):
   path='app/__pycache__/main.cpython-314.pyc';alias='app/__pycache__/main.cpython-313.pyc';f[side][alias]=copy.deepcopy(f[side][path]);f[side+2][alias]=f[side+2][path]
  with self.assertRaisesRegex(ValueError,'unsupported associated cache'):self.check(f)
 def test_legacy_associated_caches_require_same_code_and_headers(self):
  f=fixture();caches(f)
  for side in (0,1):
   for name in ('db_tls','main'):
    path='app/__pycache__/'+name+'.cpython-314.pyc';alias='app/'+name+'.pyc';f[side][alias]=f[side].pop(path);f[side+2][alias]=f[side+2].pop(path)
  self.assertEqual(len(self.check(f)),4)
  f[3]['app/main.pyc']=f[2]['app/main.pyc'];f[1]['app/main.pyc']=copy.deepcopy(f[0]['app/main.pyc'])
  with self.assertRaises(ValueError):self.check(f)
if __name__=='__main__':unittest.main()
