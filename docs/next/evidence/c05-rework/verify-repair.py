"""Stdlib source, fixture, BODY-fault and cleanup custody verification."""
import hashlib,json,subprocess,sys
from pathlib import Path
OUT=Path(__file__).resolve().parent
WT=Path(sys.argv[1]).resolve()
def sha(body):return hashlib.sha256(body).hexdigest()
p=OUT/'mutation-final-002.json';d=json.loads(p.read_text());c=d['cleanup'];assert d['passed'] and d['stack_removed'] and not d['mutation_errors'] and d['final_inventory_error'] is None
assert all(c[k] for k in ('cleanup_verified','password_discarded','lock_released')) and not c['pending'] and not c['owned']
created={(e['kind'],e['id']) for e in c['events'] if e['event']=='created'};removed={(e['kind'],e['id']) for e in c['events'] if e['event']=='removed'};assert len(created)==3 and created==removed
for name,digest in d['source_sha256'].items():
 assert sha((WT/name).read_bytes())==digest
 assert sha(subprocess.check_output(['git','-C',str(WT),'show',d['tree']+':'+name]))==digest
pre=json.loads((OUT/'pre-edit.json').read_text());changed={'next/src/cortex_core/records.py','next/src/cortex_core/coordination.py','next/schema/manifest.json'}
assert all(d['source_sha256'][name]==digest for name,digest in pre['source_sha256'].items() if name not in changed)
manifest=json.loads((WT/'next/schema/manifest.json').read_text());old=json.loads(subprocess.check_output(['git','-C',str(WT),'show',pre['head']+':next/schema/manifest.json']));assert manifest['migrations'][:-1]==old['migrations'] and manifest['migrations'][-1]['id']=='coordination-0002'
for m in manifest['migrations']:assert sha((WT/'next/schema'/m['file']).read_bytes())==m['sha256']
assert sha((WT/'next/tests/c05_review/test_c05_review.py').read_bytes())==pre['review_probe_sha256']
assert (WT/'next/tests/c05_private/test_c05_private.py').read_bytes()==(OUT/'test_c05_private-frozen.py').read_bytes()
wheels=json.loads((OUT/'offline-wheel-inputs.json').read_text())['wheels'];assert d['tool_input_sha256']['actual_wheels']==wheels==d['tool_input_sha256']['copied_wheels']
clean={};matrices=[];aux=[]
for r in d['results']:
 cmd=r['command']
 if '/tmp/next/tests/test_receipts.py' in cmd:
  report=json.loads(next(s.split('=',1)[1] for s in r['stdout'].splitlines() if s.startswith('CORTEX_TEST_RESULT=')));assert r['exit_code']==0 and not report['errors'] and not report['failures'];clean[cmd[-1].split('/')[-1]]=report['tests_run']
 if any('/tmp/next/scripts/mutate_' in s for s in cmd):
  rows=[json.loads(s) for s in r['stdout'].splitlines() if s.startswith('{')];summary=rows[-1];faults=[x for x in rows if 'status' in x]
  assert r['exit_code']==0 and summary['mutants']==summary['killed']==len(faults) and not summary['survivors'] and not summary['inconclusive']
  for f in faults:
   report=f['receipt'];assert f['status']=='killed' and not report['errors'] and report['failures']
   assert all(x['is_assertion'] and x['phase']=='test' for x in report['failures'])
   assert set(f.get('expected_tests',[f['expected_test']]))<={x['id'].split(' (')[0] for x in report['failures']}
   a=f.get('auxiliary_control')
   if a is not None:
    assert a['qualified'] and a['expected_boundary_unchanged'] and a['exit_code']==0 and not a['receipt']['failures'] and not a['receipt']['errors'];aux.append(f['mutation'])
  matrices.append(len(faults))
assert sum(clean.values())==186 and matrices==[30,11,26,8] and len(aux)==5
for filename,count in [('review-red-001.json',6),('private-red-001.json',4)]:
 red=json.loads((OUT/filename).read_text());assert red['passed'] and red['stack_removed'] and red['cleanup']['cleanup_verified'];reports=[json.loads(next(s.split('=',1)[1] for s in r['stdout'].splitlines() if s.startswith('CORTEX_TEST_RESULT='))) for r in red['results'] if r['exit_code']==1 and '/tmp/next/tests/test_receipts.py' in r['command']];assert len(reports[-1]['failures'])==count and not reports[-1]['errors']
print(json.dumps(dict(tested_commit=d['tree'],raw_sha256=sha(p.read_bytes()),baseline=clean,total_tests=186,body_faults=75,matrices=matrices,auxiliary_only_green=5,source_files=len(d['source_sha256']),all_original_173_and_mike8_unchanged=True,all_source_restored=True,append_only_manifest=True,wheels_host_and_copy_match=True,cleanup_verified=True,password_discarded=True,lock_released=True),indent=2))
