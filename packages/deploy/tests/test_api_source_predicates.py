import copy, hashlib, importlib.util, json, marshal, struct, sys, unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
FIX=Path(__file__).parent/'fixtures/api-source'
OLD='15aff361c9835f24d1e5eedba4be9b5f5168a87f'
NEW='166a76a83ff8b6ddae23ace4731e42c580461a8d'
def fixture():
 states=json.loads((FIX/'metadata.json').read_text());files={}
 for side in ('before','after'):
  files[side]={n:(FIX/side/Path(n).name).read_bytes() for n in states[side]}
 return states['before'],states['after'],files['before'],files['after'],{n:100 for n in states['before']},{n:200 for n in states['after']}
def change(f,path,raw):
 f[3][path]=raw;f[1][path]=dict(f[1].get(path,f[1]['app/main.py']),sha256=hashlib.sha256(raw).hexdigest(),size=len(raw))
def caches(f):
 for side,mtime in ((0,100),(1,200)):
  for source in ('app/db_tls.py','app/main.py'):
   path='app/__pycache__/'+Path(source).stem+'.cpython-314.pyc';raw=f[side+2][source]
   body=marshal.dumps(compile(raw,'/'+source,'exec',dont_inherit=True,optimize=0))
   cache=importlib.util.MAGIC_NUMBER+struct.pack('<III',0,mtime,len(raw))+body
   f[side+2][path]=cache;f[side][path]=dict(f[side][source],size=len(cache),sha256=hashlib.sha256(cache).hexdigest())
class APISourcePredicates(unittest.TestCase):
 def check(self,f,old=OLD,new=NEW,role='api'):
  try: import api_source_predicates as p
  except ImportError as error: raise ValueError('API source predicate missing') from error
  return p.validate(*f,old_revision=old,new_revision=new,envelope={'old_source_revision':OLD,'new_source_revision':NEW},role=role)
 def test_exact_approved_source_pair(self):
  try: result=self.check(fixture())
  except ValueError as error:self.fail(str(error))
  self.assertEqual(set(result),{'app/db_tls.py','app/main.py'})
 def test_timestamp_caches(self):
  f=fixture();caches(f);self.assertEqual(len(self.check(f)),4)
 def test_required_refusals(self):
  for mutation in ('main_byte','revision','third_path'):
   f=fixture();new=NEW
   if mutation=='main_byte':change(f,'app/main.py',f[3]['app/main.py']+b'#changed')
   elif mutation=='revision':new='0'*40
   else:change(f,'app/third.py',b'x=1\n')
   with self.subTest(mutation=mutation),self.assertRaises(ValueError):self.check(f,new=new)
 def test_boundary_refusals(self):
  for mutation in ('mode','owner','type','xattrs','size','unbound','role','flags','magic','mtime','body','trailing','cache_delete'):
   f=fixture();caches(f);role='api';source='app/main.py';cache='app/__pycache__/main.cpython-314.pyc'
   if mutation=='mode':f[1][source]['mode']=384
   elif mutation=='owner':f[1][source]['uid']=1
   elif mutation=='type':f[1][source]['type']='2'
   elif mutation=='xattrs':f[1][source]['xattrs']={'x':'y'}
   elif mutation=='size':f[1][source]['size']+=1
   elif mutation=='unbound':f[3][source]+=b'x'
   elif mutation=='role':role='pdf-worker'
   elif mutation=='cache_delete':f[1].pop(cache);f[3].pop(cache)
   else:
    raw=f[3][cache]
    if mutation=='flags':raw=raw[:4]+struct.pack('<I',3)+raw[8:]
    elif mutation=='magic':raw=b'BAD!'+raw[4:]
    elif mutation=='mtime':raw=raw[:8]+struct.pack('<I',201)+raw[12:]
    elif mutation=='body':raw=raw[:16]+marshal.dumps(compile('x=1','/'+source,'exec'))
    elif mutation=='trailing':raw+=b'x'
    change(f,cache,raw)
   with self.subTest(mutation=mutation),self.assertRaises(ValueError):self.check(f,role=role)
if __name__=='__main__':unittest.main()
