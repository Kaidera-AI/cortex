"""Real OCI reader integration; synthetic source fixtures, not native image proof."""
import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import signed_reference_filesystem as guard
import verify_api_signed_reference as wrapper
from test_api_source_predicates import fixture, OLD, NEW

def archive(path,files,state,revision,mtime):
 layer=io.BytesIO()
 with tarfile.open(fileobj=layer,mode='w') as tar:
  for name,raw in files.items():
   row=state[name];item=tarfile.TarInfo(name);item.size=len(raw);item.mode=row['mode'];item.uid=row['uid'];item.gid=row['gid'];item.mtime=mtime;tar.addfile(item,io.BytesIO(raw))
 blobs={}
 def blob(raw):
  digest=hashlib.sha256(raw).hexdigest();blobs['blobs/sha256/'+digest]=raw;return {'digest':'sha256:'+digest,'size':len(raw)}
 config=blob(json.dumps({'config':{'Labels':{'org.opencontainers.image.revision':revision}}}).encode())
 desc=blob(layer.getvalue());manifest=blob(json.dumps({'config':config,'layers':[desc]}).encode());blobs['index.json']=json.dumps({'manifests':[manifest]}).encode()
 with tarfile.open(path,'w') as tar:
  for name,raw in blobs.items():
   item=tarfile.TarInfo(name);item.size=len(raw);tar.addfile(item,io.BytesIO(raw))
class APIWrapper(unittest.TestCase):
 def test_real_reader_strict_red_overlay_pass(self):
  f=fixture()
  for side,revision in ((0,OLD),(1,NEW)):
   raw=json.dumps({'source_revision':revision}).encode();f[side+2]['app/release_identity.json']=raw
   f[side]['app/release_identity.json']={'type':'0','mode':420,'uid':0,'gid':0,'xattrs':{},'size':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}
  with tempfile.TemporaryDirectory() as tmp:
   old=Path(tmp)/'old.tar';new=Path(tmp)/'new.tar';archive(old,f[2],f[0],OLD,100);archive(new,f[3],f[1],NEW,200)
   result=wrapper.verify(old,new,OLD,NEW,{'old_source_revision':OLD,'new_source_revision':NEW})
   self.assertEqual(result['original_strict_result'],'RED');self.assertEqual(result['result'],'PASS');self.assertFalse(result['qualification']);self.assertEqual(len(result['api_source_predicate_receipts']),2)
   with self.assertRaises(ValueError):wrapper.verify(old,new,OLD,'0'*40,{'old_source_revision':OLD,'new_source_revision':NEW})
  self.assertEqual(guard.OLD,OLD)
if __name__=='__main__':unittest.main()
