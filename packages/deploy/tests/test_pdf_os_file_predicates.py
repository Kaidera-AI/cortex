import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import unittest
ROOT=Path(__file__).resolve().parents[1]
s=importlib.util.spec_from_file_location('pdf_predicates',ROOT/'pdf_os_file_predicates.py');p=importlib.util.module_from_spec(s);s.loader.exec_module(p)
FIX=Path(__file__).parent/'fixtures/pdf-os'
NOISE={'var/cache/ldconfig/aux-cache','var/log/alternatives.log','var/log/apt/history.log','var/log/apt/term.log','var/log/dpkg.log'}
AUTH='etc/ld.so.cache'
def fixture():
 m=json.loads((FIX/'metadata.json').read_text());files={label:{n:(FIX/label/n).read_bytes() for n in m[label]} for label in ['before','after']};return m['before'],m['after'],files['before'],files['after']
def change(f,name,raw):f[3][name]=raw;f[1][name]['size']=len(raw);f[1][name]['sha256']=hashlib.sha256(raw).hexdigest()
class PDFOSPredicates(unittest.TestCase):
 def check(self,f,role='pdf-worker'):return p.validate(*f,role=role)
 def test_retained_exact_five_paths_are_conditionally_admissible(self):
  try:result=self.check(fixture())
  except ValueError as error:self.fail(str(error))
  self.assertEqual(set(result),NOISE)
 def test_non_clock_log_mutant_is_refused_in_body(self):
  f=fixture();name='var/log/apt/term.log';change(f,name,f[3][name].replace(b'poppler-utils',b'poppler-utilX',1));self.assertNotEqual(f[2][name],f[3][name])
  with self.assertRaises(ValueError):self.check(f)
 def test_authoritative_loader_cache_mutant_is_refused_in_body(self):
  f=fixture();change(f,AUTH,bytes([f[3][AUTH][0]^1])+f[3][AUTH][1:])
  with self.assertRaises(ValueError):self.check(f)
 def test_boundary_refusals(self):
  for mutant in ['role','clock','mode','owner','size','type','xattrs','missing_authority','extra_path','deleted_noise','unbound_bytes']:
   with self.subTest(mutant=mutant):
    f=fixture();n='var/log/dpkg.log';role='pdf-worker'
    if mutant=='role':role='api'
    elif mutant=='clock':change(f,n,f[3][n].replace(b'2026-10-10',b'2026-99-99',1))
    elif mutant=='mode':f[1][n]['mode']=384
    elif mutant=='owner':f[1][n]['uid']=123
    elif mutant=='size':change(f,n,f[3][n]+b'\n')
    elif mutant=='type':f[1][n]['type']='2'
    elif mutant=='xattrs':f[1][n]['xattrs']={'SCHILY.xattr.user.fixture':'changed'}
    elif mutant=='missing_authority':f[1].pop(AUTH);f[3].pop(AUTH)
    elif mutant=='extra_path':f[1]['var/log/other.log']={'type':'0','mode':420,'uid':0,'gid':0,'xattrs':{},'size':1,'sha256':hashlib.sha256(b'x').hexdigest()};f[3]['var/log/other.log']=b'x'
    elif mutant=='deleted_noise':f[1].pop(n);f[3].pop(n)
    elif mutant=='unbound_bytes':f[3][n]=b'wrong bytes'
    with self.assertRaises(ValueError):self.check(f,role)
if __name__=='__main__':unittest.main()
