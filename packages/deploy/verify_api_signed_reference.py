"""Original strict guard first, then exact envelope-bound API source predicate."""
import hashlib
from pathlib import Path
import signed_reference_filesystem as guard
import api_source_predicates as api

def verify(old,new,old_revision,new_revision,envelope):
 api.bind_revisions(old_revision,new_revision,envelope)
 previous_old,previous_new=guard.OLD,guard.NEW
 reader=guard.filesystem;measured={}
 def once(path):
  if path not in measured:measured[path]=reader(path)
  return measured[path]
 guard.OLD,guard.NEW=old_revision,new_revision;guard.filesystem=once
 try:base=guard.compare(old,new,'api')
 finally:guard.filesystem=reader;guard.OLD,guard.NEW=previous_old,previous_new
 before,bt,_,_=measured[old];after,at,_,_=measured[new]
 unexpected=set(base['unexpected_paths']);existing=set(base['changed_paths'])-unexpected
 wanted=set(api.APPROVED)|{p for p in unexpected if api.cache_source(p)}
 old_files=guard.selected_files(old,wanted);new_files=guard.selected_files(new,wanted)
 reduced_before={k:v for k,v in before.items() if k not in existing};reduced_after={k:v for k,v in after.items() if k not in existing}
 receipts=api.validate(reduced_before,reduced_after,old_files,new_files,bt,at,old_revision=old_revision,new_revision=new_revision,envelope=envelope,role='api')
 if set(receipts)!=unexpected:raise ValueError('API source predicate does not cover exact unexpected set')
 base.update(original_strict_result=base['result'],result='PASS',unexpected_paths=[],api_source_predicate_receipts=receipts,qualification=False,qualification_note='API filesystem source predicate only; runtime, DB/PG and MAIN remain separate.',original_guard_sha256=hashlib.sha256(Path(guard.__file__).read_bytes()).hexdigest(),api_predicate_sha256=hashlib.sha256(Path(api.__file__).read_bytes()).hexdigest())
 return base
