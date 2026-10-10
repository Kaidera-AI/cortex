"""Bounded graph generated-file predicates; all unclassified changes refuse."""
import csv
import hashlib
import importlib.util
import io
import marshal
import posixpath
import re
import struct
import sys
import types

HELPER='usr/local/libexec/kaidera/finalize-build.py'
DIRECTORIES={'app/__pycache__','usr/local/libexec/kaidera/__pycache__',
 'usr/local/lib/python3.13/config-3.13-x86_64-linux-gnu/__pycache__'}
CLEANUP={'home/kaidera/.cache/Microsoft/DeveloperTools/.onnxruntime/deviceid',
 'home/kaidera/.cache/Microsoft/DeveloperTools/.onnxruntime/onnxruntime.db','tmp/.ses',
 'var/log/apt/history.log','var/log/apt/term.log','var/log/dpkg.log','var/cache/ldconfig/aux-cache'}
ROOTS=('opt/bcrg/','usr/local/lib/python3.13/','app/','usr/local/libexec/kaidera/')

def require(condition, reason):
 if not condition:raise ValueError(reason)

def source_path(path):
 m=re.fullmatch(r'(.+)/__pycache__/([^/]+)\.cpython-313\.pyc',path)
 require(m is not None and path.startswith(ROOTS),'cache path outside exact compile roots/tag')
 return m[1]+'/'+m[2]+'.py'

def attrs(row):return {k:v for k,v in row.items() if k not in ('sha256','size')}

def regular(row):return row is not None and row.get('type') in ('0','\x00') and 'sha256' in row

def content(path, state, files):
 require(regular(state.get(path)) and path in files,'required regular content missing: '+path)
 raw=files[path];require(hashlib.sha256(raw).hexdigest()==state[path]['sha256'] and len(raw)==state[path]['size'],'content/inventory binding differs: '+path)
 return raw

def code_equal(a,b):
 if isinstance(a,types.CodeType) or isinstance(b,types.CodeType):
  if not isinstance(a,types.CodeType) or not isinstance(b,types.CodeType):return False
  fields=('co_argcount','co_posonlyargcount','co_kwonlyargcount','co_nlocals','co_stacksize','co_flags','co_code','co_consts','co_names','co_varnames','co_filename','co_name','co_qualname','co_firstlineno','co_linetable','co_exceptiontable','co_freevars','co_cellvars')
  return all(code_equal(getattr(a,k),getattr(b,k)) for k in fields)
 if isinstance(a,tuple) and isinstance(b,tuple):return len(a)==len(b) and all(code_equal(x,y) for x,y in zip(a,b))
 return type(a) is type(b) and a==b

def cache_check(path, raw, source, source_raw):
 require(sys.version_info[:2]==(3,13),'matching Python 3.13 interpreter required')
 require(len(raw)>=16 and raw[:4]==importlib.util.MAGIC_NUMBER,'cache magic differs')
 require(struct.unpack('<I',raw[4:8])[0]==3,'cache flags must be checked-hash 3')
 require(raw[8:16]==importlib.util.source_hash(source_raw),'cache hash does not bind actual source')
 try:
  stream=io.BytesIO(raw[16:]);actual=marshal.load(stream)
  require(stream.read()==b'','trailing cache body bytes')
  expected=compile(source_raw,'/'+source,'exec',dont_inherit=True,optimize=0)
  require(isinstance(actual,types.CodeType) and code_equal(actual,expected),'cache code body differs from exact source/final filename')
 except (EOFError,TypeError,SyntaxError) as e:raise ValueError('invalid cache body') from e

def parse_record(raw):
 try:rows=list(csv.reader(io.StringIO(raw.decode('utf-8')),strict=True))
 except (UnicodeError,csv.Error) as e:raise ValueError('invalid RECORD CSV') from e
 require(all(len(r)==3 for r in rows),'RECORD row arity')
 result={r[0]:r[1:] for r in rows};require(len(result)==len(rows),'duplicate RECORD row');return result

def validate(before, after, old_files, new_files, *, helper_bytes):
 changed={k for k in set(before)|set(after) if before.get(k)!=after.get(k)}
 caches={k for k in changed if k.endswith('.pyc')};permitted={};helper_added=HELPER in changed
 for path in sorted(caches):
  source=source_path(path);a,b=before.get(path),after.get(path)
  require(regular(after.get(source)),'cache source missing')
  source_raw=content(source,after,new_files)
  if source==HELPER:
   require(before.get(source) is None and source_raw==helper_bytes,'helper source not exact reviewed bytes')
  else:
   require(before.get(source)==after.get(source) and regular(before.get(source)),'cache source bytes or metadata changed')
   require(content(source,before,old_files)==source_raw,'cache source byte inequality')
  if b is None:
   require(regular(a),'removed cache not regular');permitted[path]={'class':'removed_cache_equal_source','source':source};continue
  require(regular(b),'new cache not regular')
  require(attrs(b)=={'type':'0','mode':420,'uid':0,'gid':0,'xattrs':{}},'cache attributes not reviewed root-owned 0644')
  if a is not None:require(attrs(a)==attrs(b),'cache metadata changed')
  cache_check(path,content(path,after,new_files),source,source_raw)
  permitted[path]={'class':'checked_hash_source_code','source':source,'source_sha256':after[source]['sha256']}
 for path in sorted(changed-caches):
  a,b=before.get(path),after.get(path)
  if path.endswith('.dist-info/RECORD'):
   require(regular(a) and regular(b) and attrs(a)==attrs(b),'RECORD attributes differ')
   old=parse_record(content(path,before,old_files));new=parse_record(content(path,after,new_files))
   require(not(set(new)-set(old)),'added RECORD row')
   require(all(old[k]==new[k] for k in set(old)&set(new)),'changed common RECORD row')
   removed=set(old)-set(new);require(bool(removed),'RECORD semantic delta missing')
   for name in removed:
    cache=posixpath.normpath(posixpath.join(posixpath.dirname(posixpath.dirname(path)),name))
    # One source-measured pip --target artifact: RECORD keeps its staging
    # scheme relative script path while the script is moved under target/bin.
    if (path=='opt/bcrg/lib/python3.13/site-packages/jmespath-1.1.0.dist-info/RECORD'
        and name=='../../bin/__pycache__/jp.cpython-313.pyc'):
     require(cache not in before and cache not in after,'ambiguous jmespath RECORD alias')
     cache='opt/bcrg/lib/python3.13/site-packages/bin/__pycache__/jp.cpython-313.pyc'
    require(old[name]==['',''] and cache in permitted and cache.endswith('.pyc'),'removed RECORD row not validated unhashed cache: '+path+':'+name)
   permitted[path]={'class':'only_removed_unhashed_cache_RECORD_rows','removed_rows':len(removed)}
  elif path==HELPER:
   require(a is None and regular(b) and attrs(b)=={'type':'0','mode':420,'uid':0,'gid':0,'xattrs':{}},'helper path/metadata unexpected')
   require(content(path,after,new_files)==helper_bytes,'helper bytes differ from reviewed source');permitted[path]={'class':'reviewed_build_finalizer'}
  elif path in DIRECTORIES:
   require(a is None and b=={'type':'5','mode':493,'uid':0,'gid':0,'xattrs':{}},'cache directory metadata unexpected')
   descendants={k for k in after if k.startswith(path+'/')}
   require(descendants and descendants <= set(permitted),'cache directory has unvalidated descendants');permitted[path]={'class':'named_cache_directory'}
  elif path in CLEANUP:
   require(regular(a) and b is None,'cleanup is not exact regular-file deletion');permitted[path]={'class':'named_build_byproduct'}
  else:raise ValueError('unclassified graph change: '+path)
 require(set(permitted)==changed,'predicate coverage incomplete')
 return permitted
