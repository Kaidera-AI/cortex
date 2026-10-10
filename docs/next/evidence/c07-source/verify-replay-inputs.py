"""Fail before resources: every copied product byte and required test must be committed."""
import ast
import hashlib
import json
from pathlib import Path
import subprocess
import sys
wt=Path(sys.argv[1]);
required_inputs=('run-core-pg.py','replay_lifecycle.py','verify-replay-inputs.py',
                 'verify-fixture-contract.py','verify-expiry-fixture.py',
                 'verify-fixture-shapes.py','verify-publication.py','offline-wheel-inputs.json')
input_sha256={}
for name in required_inputs:
 relative='docs/next/evidence/c06-source/'+name
 local=(Path(__file__).parent.parent/'c06-source'/name).read_bytes()
 mirror=(wt/relative).read_bytes()
 committed=subprocess.check_output(['git','-C',str(wt),'show','HEAD:'+relative])
 assert local==mirror==committed,('controller_input_not_git_bound',name)
 input_sha256[name]=hashlib.sha256(local).hexdigest()
for script,fixture in [('verify-expiry-fixture.py','next/tests/outbox_retention/test_outbox_retention.py')]:
 result=subprocess.run([sys.executable,str(Path(__file__).parent.parent/'c06-source'/script),str(wt/fixture)],capture_output=True,text=True)
 assert result.returncode==0,script+': '+result.stdout
guard_result=subprocess.run([sys.executable,str(Path(__file__).with_name('verify-table-guards.py')),str(wt)],capture_output=True,text=True)
assert guard_result.returncode==0,'C07 table guard custody: '+guard_result.stdout+guard_result.stderr
checks={}; expected={'outbox':32,'outbox_caller':2,'outbox_guards':23,'outbox_retention':5,'outbox_inventory':12,'outbox_late_publication':1,'c05_review':8,'c05_private':5,'outbox_write_only':2,'outbox_private_namespace':2}
for directory,count in expected.items():
 files=list((wt/'next/tests'/directory).glob('test_*.py'))
 actual=sum(sum(isinstance(n,ast.FunctionDef) and n.name.startswith('test_') for n in ast.walk(ast.parse(p.read_text()))) for p in files)
 for file in files:
  for node in ast.walk(ast.parse(file.read_text())):
   if isinstance(node,ast.ImportFrom) and node.module and node.module.startswith('test_'):
    matches=list((wt/'next/tests').rglob(node.module+'.py'))
    if len(matches)==1:
     definitions={n.name:n for n in ast.walk(ast.parse(matches[0].read_text())) if isinstance(n,ast.ClassDef)}
     for alias in node.names:
      if alias.name in definitions:
       actual+=sum(isinstance(n,ast.FunctionDef) and n.name.startswith('test_') for n in definitions[alias.name].body)
 checks[directory]={'actual':actual,'expected':count,'passed':actual==count}
recipe_count=0
for filename in ('outbox-fault-recipes.json','outbox-identity-fault-recipes.json','outbox-adapters-fault-recipes.json','outbox-c05-fault-recipes.json','outbox-write-only-fault-recipes.json','outbox-private-namespace-fault-recipes.json'):
 for recipe in json.loads((wt/'next/contracts'/filename).read_text())['mutants']:
  recipe_count+=1
  sources={e['path']:(wt/'next'/e['path']).read_text() for e in recipe['changes']}
  for e in recipe['changes']:
   assert sources[e['path']].count(e['before'])==e['count'],('fault_preflight',recipe['label'],e['path'])
   sources[e['path']]=sources[e['path']].replace(e['before'],e['after'])
checks['all_fault_anchors']={'passed':recipe_count==120,'recipes':recipe_count}
status=subprocess.check_output(['git','status','--porcelain','--untracked-files=all','--','next'],cwd=wt,text=True)
tracked=set(subprocess.check_output(['git','-C',str(wt),'ls-tree','-r','--name-only','HEAD','next'],text=True).splitlines())
actual=set(str(p.relative_to(wt)) for p in (wt/'next').rglob('*') if p.is_file())
source_sha256={name:hashlib.sha256((wt/name).read_bytes()).hexdigest() for name in sorted(actual)}
checks['product_committed']={'passed':not status and tracked==actual,'status':status,
                              'tracked':len(tracked),'actual':len(actual)}
mirror=wt/'docs/next/evidence/c06-source/run-core-pg.py'
controller=Path(__file__).parent.parent/'c06-source'/'run-core-pg.py'
checks['controller_committed']={'passed':mirror.read_bytes()==controller.read_bytes() and not subprocess.check_output(['git','diff','HEAD','--',str(mirror)],cwd=wt)}
passed=all(v['passed'] for v in checks.values());print(json.dumps({'passed':passed,'checks':checks,
    'controller_input_sha256':input_sha256,'source_sha256':source_sha256}));raise SystemExit(0 if passed else 1)
