"""PDF-only predicates for five reviewed, non-authoritative build OS files."""
import datetime
import hashlib
import re

AUX='var/cache/ldconfig/aux-cache'
AUTH='etc/ld.so.cache'
LOGS={'var/log/alternatives.log','var/log/apt/history.log','var/log/apt/term.log','var/log/dpkg.log'}
PATHS=LOGS|{AUX}
DATE=rb'(?P<date>[0-9]{4}-[0-9]{2}-[0-9]{2})'
TIME=rb'(?P<time>[0-9]{2}:[0-9]{2}:[0-9]{2})'
GRAMMARS={
 'var/log/alternatives.log':re.compile(rb'^(?P<prefix>update-alternatives )'+DATE+rb' '+TIME+rb'(?P<suffix>:)',re.M),
 'var/log/dpkg.log':re.compile(rb'^(?P<prefix>)'+DATE+rb' '+TIME+rb'(?P<suffix> )',re.M),
 'var/log/apt/history.log':re.compile(rb'^(?P<prefix>(?:Start-Date|End-Date): )'+DATE+rb'  '+TIME+rb'(?P<suffix>\r?$)',re.M),
 'var/log/apt/term.log':re.compile(rb'^(?P<prefix>Log (?:started|ended): )'+DATE+rb'  '+TIME+rb'(?P<suffix>\r?$)',re.M),
}


def _log_view(path,raw):
 count=0
 def token(match):
  nonlocal count
  try:datetime.datetime.strptime((match['date']+b' '+match['time']).decode('ascii'),'%Y-%m-%d %H:%M:%S')
  except ValueError as error:raise ValueError('invalid OS log clock token: '+path) from error
  count+=1
  return match['prefix']+b'<BUILD-CLOCK>'+match['suffix']
 return GRAMMARS[path].sub(token,raw),count


def _bytes(path,row,files):
 raw=files.get(path)
 if (row is None or row.get('type') not in ('0','\x00') or not isinstance(raw,bytes)
     or row.get('size')!=len(raw) or row.get('sha256')!=hashlib.sha256(raw).hexdigest()
     or len(raw)>16*1024*1024 or 'link' in row or 'hardlink' in row):
  raise ValueError('unbound, missing or non-regular PDF OS file: '+path)
 return raw


def _attributes(path,left,right):
 if left is None or right is None or any(left.get(k)!=right.get(k) for k in ('type','mode','uid','gid','xattrs','size')):
  raise ValueError('PDF OS file attributes differ: '+path)


def validate(before,after,old_files,new_files,*,role):
 if role!='pdf-worker':raise ValueError('PDF OS predicate cannot apply to another image')
 changed={name for name in set(before)|set(after) if before.get(name)!=after.get(name)}
 if changed-PATHS:raise ValueError('unclassified PDF path: '+sorted(changed-PATHS)[0])
 receipts={}
 if AUX in changed:
  _attributes(AUTH,before.get(AUTH),after.get(AUTH))
  a=_bytes(AUTH,before.get(AUTH),old_files);b=_bytes(AUTH,after.get(AUTH),new_files)
  if a!=b:raise ValueError('authoritative ld.so.cache bytes differ')
 for name in sorted(changed):
  a,b=before.get(name),after.get(name);_attributes(name,a,b);left=_bytes(name,a,old_files);right=_bytes(name,b,new_files)
  if name in LOGS:
   old,count_a=_log_view(name,left);new,count_b=_log_view(name,right)
   if count_a==0 or count_a!=count_b or old!=new:raise ValueError('non-clock PDF OS log bytes differ: '+name)
   receipts[name]={'class':'exact_OS_log_clock_only','clock_tokens':count_a,'non_clock_sha256':hashlib.sha256(old).hexdigest(),'attributes_equal':True}
  else:
   receipts[name]={'class':'non_authoritative_ldconfig_auxiliary_cache','authoritative_cache':AUTH,'authoritative_sha256':hashlib.sha256(old_files[AUTH]).hexdigest(),'authoritative_bytes_equal':True,'attributes_equal':True,'size':a['size']}
 return receipts
