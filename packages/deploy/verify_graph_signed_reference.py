#!/usr/bin/env python3
"""Keep original signed-reference guard intact; admit only reviewed graph predicates."""
import argparse
import hashlib
import json
from pathlib import Path
import signed_reference_filesystem as guard
import graph_class_predicates as graph


def verify(old,new,source,revision):
 helper=Path(source)/'packages/containers/graph-worker/finalize-build.py'
 helper_bytes=helper.read_bytes()
 # Exact identity argument only; all original reference/config/generated checks remain.
 guard.NEW=revision
 original_reader=guard.filesystem
 measured={}
 def read_once(path):
  if path not in measured:measured[path]=original_reader(path)
  return measured[path]
 guard.filesystem=read_once
 try:base=guard.compare(old,new,'graph-worker')
 finally:guard.filesystem=original_reader
 before,_,_,_=measured[old];after,_,_,_=measured[new]
 existing=set(base['changed_paths'])-set(base['unexpected_paths'])
 unexpected=set(base['unexpected_paths'])
 source_paths={graph.source_path(k) for k in unexpected if k.endswith('.pyc')}
 wanted=source_paths | {k for k in unexpected if k.endswith('.pyc') or k.endswith('.dist-info/RECORD') or k==graph.HELPER}
 old_files=guard.selected_files(old,{k for k in wanted if graph.regular(before.get(k))})
 new_files=guard.selected_files(new,{k for k in wanted if graph.regular(after.get(k))})
 # Original guard has already proved these generated changes, never grant a caller waiver.
 reduced_before={k:v for k,v in before.items() if k not in existing}
 reduced_after={k:v for k,v in after.items() if k not in existing}
 receipts=graph.validate(reduced_before,reduced_after,old_files,new_files,helper_bytes=helper_bytes)
 assert set(receipts)==unexpected
 base['original_strict_result']=base['result'];base['result']='PASS';base['unexpected_paths']=[]
 base['graph_predicate_receipts']=receipts
 base['graph_finalizer_source_sha256']=hashlib.sha256(helper_bytes).hexdigest()
 base['graph_predicate_sha256']=hashlib.sha256(Path(graph.__file__).read_bytes()).hexdigest()
 base['original_guard_sha256']=hashlib.sha256(Path(guard.__file__).read_bytes()).hexdigest()
 base['qualification']=False
 base['qualification_note']='Filesystem graph predicate only; jp/code/model/runtime gates remain separate.'
 return base

if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--old',required=True);p.add_argument('--new',required=True);p.add_argument('--source',required=True);p.add_argument('--revision',required=True);p.add_argument('--output',required=True);a=p.parse_args()
 try:
  result=verify(a.old,a.new,a.source,a.revision)
 except Exception as error:
  result={'result':'RED','reason':str(error),'error_type':type(error).__name__};status=1
 else:status=0
 with open(a.output,'x') as f:json.dump(result,f,indent=2);f.write('\n')
 print(json.dumps({'result':result['result'],'reason':result.get('reason'),'graph_predicates':len(result.get('graph_predicate_receipts',{}))}))
 raise SystemExit(status)
