#!/usr/bin/env python3
"""Run the byte-identical original guard first; admit only reviewed PDF OS predicates."""
import argparse
import hashlib
import json
from pathlib import Path
import signed_reference_filesystem as guard
import pdf_os_file_predicates as pdf


def verify(old,new,revision):
 guard.NEW=revision
 reader=guard.filesystem;measured={}
 def once(path):
  if path not in measured:measured[path]=reader(path)
  return measured[path]
 guard.filesystem=once
 try:base=guard.compare(old,new,'pdf-worker')
 finally:guard.filesystem=reader
 before,_,_,_=measured[old];after,_,_,_=measured[new]
 existing=set(base['changed_paths'])-set(base['unexpected_paths']);unexpected=set(base['unexpected_paths'])
 wanted=unexpected&pdf.PATHS
 if pdf.AUX in wanted:wanted.add(pdf.AUTH)
 old_files=guard.selected_files(old,{n for n in wanted if before.get(n,{}).get('type') in ('0','\x00')})
 new_files=guard.selected_files(new,{n for n in wanted if after.get(n,{}).get('type') in ('0','\x00')})
 reduced_before={k:v for k,v in before.items() if k not in existing};reduced_after={k:v for k,v in after.items() if k not in existing}
 receipts=pdf.validate(reduced_before,reduced_after,old_files,new_files,role='pdf-worker')
 if set(receipts)!=unexpected:raise ValueError('PDF OS predicate does not cover exactly the original unexpected set')
 base.update(original_strict_result=base['result'],result='PASS',unexpected_paths=[],pdf_os_predicate_receipts=receipts,qualification=False,qualification_note='PDF filesystem predicate only; runtime/DBPG/MAIN remain separate.',original_guard_sha256=hashlib.sha256(Path(guard.__file__).read_bytes()).hexdigest(),pdf_predicate_sha256=hashlib.sha256(Path(pdf.__file__).read_bytes()).hexdigest())
 return base

if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--old',required=True);p.add_argument('--new',required=True);p.add_argument('--revision',required=True);p.add_argument('--output',required=True);a=p.parse_args()
 try:result=verify(a.old,a.new,a.revision)
 except Exception as e:result={'result':'RED','reason':str(e),'error_type':type(e).__name__};status=1
 else:status=0
 with open(a.output,'x') as f:json.dump(result,f,indent=2);f.write('\n')
 print(json.dumps({'result':result['result'],'reason':result.get('reason'),'PDF_OS_predicates':len(result.get('pdf_os_predicate_receipts',{}))}));raise SystemExit(status)
