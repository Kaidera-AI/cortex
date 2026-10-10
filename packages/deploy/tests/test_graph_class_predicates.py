import copy
import hashlib
import importlib.util
import marshal
from pathlib import Path
import struct
import unittest

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('predicates',ROOT/'graph_class_predicates.py')
p=importlib.util.module_from_spec(spec);spec.loader.exec_module(p)
SOURCE='app/sample.py'
CACHE='app/__pycache__/sample.cpython-313.pyc'
RECORD='usr/local/lib/python3.13/site-packages/example-1.dist-info/RECORD'
HELPER=b'# source reviewed finalizer\n'

def row(raw):
 return {'type':'0','mode':420,'uid':0,'gid':0,'xattrs':{},'size':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}

def fixture():
 raw=b'answer = 7\n';body=marshal.dumps(compile(raw,'/'+SOURCE,'exec',dont_inherit=True,optimize=0))
 old=importlib.util.MAGIC_NUMBER+struct.pack('<III',0,123,len(raw))+body
 new=importlib.util.MAGIC_NUMBER+struct.pack('<I',3)+importlib.util.source_hash(raw)+body
 rec=b'../../../../../app/__pycache__/sample.cpython-313.pyc,,\nexample.py,sha256=abc,9\n'
 a={SOURCE:raw,CACHE:old,RECORD:rec};b={SOURCE:raw,CACHE:new,RECORD:b'example.py,sha256=abc,9\n'}
 return {k:row(v) for k,v in a.items()},{k:row(v) for k,v in b.items()},a,b

class GraphPredicates(unittest.TestCase):
 def check(self,f):return p.validate(*f,helper_bytes=HELPER)
 def test_valid_source_bound_changed_cache_and_RECORD(self):
  try:result=self.check(fixture())
  except ValueError as error:self.fail(str(error))
  self.assertEqual(set(result),{CACHE,RECORD})
 def test_all_named_refusals(self):
  for mutant in ['missing_source','changed_source','source_metadata','flags','hash','body','duplicate_record','added_record','hashed_record','helper','cleanup','model_metadata']:
   with self.subTest(mutant=mutant):
    b,a,old,new=fixture()
    if mutant=='missing_source':a.pop(SOURCE);new.pop(SOURCE)
    elif mutant=='changed_source':new[SOURCE]=b'answer = 8\n';a[SOURCE]=row(new[SOURCE])
    elif mutant=='source_metadata':a[SOURCE]['mode']=384
    elif mutant in ('flags','hash','body'):
     data=bytearray(new[CACHE]);offset={'flags':4,'hash':8,'body':16}[mutant];data[offset]^=1;new[CACHE]=bytes(data);a[CACHE]=row(new[CACHE])
    elif mutant=='duplicate_record':new[RECORD]+=new[RECORD];a[RECORD]=row(new[RECORD])
    elif mutant=='added_record':new[RECORD]+=b'new.py,sha256=new,1\n';a[RECORD]=row(new[RECORD])
    elif mutant=='hashed_record':old[RECORD]=old[RECORD].replace(b'.pyc,,',b'.pyc,sha256=bad,1');b[RECORD]=row(old[RECORD])
    elif mutant=='helper':new['usr/local/libexec/kaidera/finalize-build.py']=b'wrong';a['usr/local/libexec/kaidera/finalize-build.py']=row(b'wrong')
    elif mutant=='cleanup':old['var/log/unexpected.log']=b'x';b['var/log/unexpected.log']=row(b'x')
    elif mutant=='model_metadata':b['opt/models/model.onnx']=row(b'model');a['opt/models/model.onnx']=row(b'model');a['opt/models/model.onnx']['mode']=384
    with self.assertRaises(ValueError):self.check((b,a,old,new))

if __name__=='__main__':unittest.main()
