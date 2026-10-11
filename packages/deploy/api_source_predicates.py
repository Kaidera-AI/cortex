"""Exact reviewed API source delta; never grants runtime qualification."""
import hashlib
import importlib.util
import io
import marshal
import re
import struct
import sys
import types
from graph_class_predicates import code_equal

APPROVED = {
 'app/db_tls.py': (('6a58a2389d902be67cf9ffb1a57c4e932b56108a0d0ac082dd97a8cc68cd34c8',3364),('d519309009ec2479316853250ae8c77c8d582e275a1688eaa5f1089f5fff9079',3350)),
 'app/main.py': (('11c23c39c31c650d1a015fd8419e601baa74fbadf680d6d7f28adfcdc101100f',855185),('b634ed969d3da3f871d0828c03993dd1e6e11e06a7fe5810bb7d78bd2f79dc8b',858274)),
}
def require(ok,reason):
 if not ok:raise ValueError(reason)
def bind_revisions(old_revision,new_revision,envelope):
 require(isinstance(envelope,dict),'qualification envelope missing')
 require(all(isinstance(v,str) and re.fullmatch('[0-9a-f]{40}',v) for v in (old_revision,new_revision)) and old_revision!=new_revision,'invalid revision arguments')
 require(envelope.get('old_source_revision')==old_revision and envelope.get('new_source_revision')==new_revision,'revision arguments differ from qualification envelope')
def attrs(row):return {k:v for k,v in row.items() if k not in ('sha256','size')}
def content(path,state,files):
 row=state.get(path)
 require(row is not None and row.get('type') in ('0','\x00') and path in files,'required regular content missing: '+path)
 raw=files[path]
 require(len(raw)==row.get('size') and hashlib.sha256(raw).hexdigest()==row.get('sha256'),'content/inventory binding differs: '+path)
 return raw

def cache_source(path):
 if path in ('app/db_tls.pyc','app/main.pyc'):return path[:-1]
 match=re.fullmatch(r'app/__pycache__/(db_tls|main)\.cpython-[^/]+\.pyc',path)
 return 'app/'+match[1]+'.py' if match else None

def check_cache(raw,source,source_raw,mtime):
 require(sys.version_info[:2]==(3,14),'matching Python 3.14 required')
 require(len(raw)>=16 and raw[:4]==importlib.util.MAGIC_NUMBER,'timestamp cache magic differs')
 _,flags,clock,size=struct.unpack('<4sIII',raw[:16])
 require(flags==0 and clock==int(float(mtime)) and size==len(source_raw),'timestamp cache header differs from actual source stats')
 try:
  stream=io.BytesIO(raw[16:]);actual=marshal.load(stream)
  require(stream.read()==b'','trailing timestamp cache bytes')
  expected=compile(source_raw,'/'+source,'exec',dont_inherit=True,optimize=0)
  require(isinstance(actual,types.CodeType) and code_equal(actual,expected),'recursive cache code differs from approved source')
 except (EOFError,TypeError,SyntaxError) as error:raise ValueError('invalid timestamp cache body') from error

def validate(before,after,old_files,new_files,before_times,after_times,*,old_revision,new_revision,envelope,role):
 bind_revisions(old_revision,new_revision,envelope);require(role=='api','API source predicate role mismatch')
 changed={n for n in set(before)|set(after) if before.get(n)!=after.get(n)}
 receipts={}
 for source,pair in APPROVED.items():
  a,b=before.get(source),after.get(source)
  left=content(source,before,old_files);right=content(source,after,new_files)
  require(attrs(a)==attrs(b),'approved source attributes differ: '+source)
  require((hashlib.sha256(left).hexdigest(),len(left))==pair[0] and (hashlib.sha256(right).hexdigest(),len(right))==pair[1],'source differs from exact approved pair: '+source)
  if source in changed:receipts[source]={'class':'exact_approved_API_source_delta','old_sha256':pair[0][0],'new_sha256':pair[1][0],'attributes_equal':True}
 associated={path for path in set(before)|set(after) if cache_source(path) in APPROVED}
 require(changed <= set(APPROVED)|associated,'unclassified API source change')
 for path in sorted(associated):
  source=cache_source(path)
  require(path in ('app/db_tls.pyc','app/main.pyc','app/__pycache__/db_tls.cpython-314.pyc','app/__pycache__/main.cpython-314.pyc'),'unsupported associated cache tag/optimization: '+path)
  a,b=before.get(path),after.get(path)
  left=content(path,before,old_files);right=content(path,after,new_files)
  require(attrs(a)==attrs(b),'API cache attributes differ: '+path)
  require(left[:8]==right[:8],'API cache magic/flags differ')
  require(source in before_times and source in after_times,'API cache source times missing')
  check_cache(left,source,old_files[source],before_times[source]);check_cache(right,source,new_files[source],after_times[source])
  receipt={'class':'timestamp_cache_exact_approved_source_code','source':source,'old_source_sha256':APPROVED[source][0][0],'new_source_sha256':APPROVED[source][1][0],'header_stat_and_recursive_code_match':True}
  receipts[source].setdefault('associated_caches_checked',[]).append(dict(receipt,path=path))
  if path in changed:receipts[path]=receipt
 require(set(receipts)==changed,'API source predicate coverage incomplete')
 return receipts
