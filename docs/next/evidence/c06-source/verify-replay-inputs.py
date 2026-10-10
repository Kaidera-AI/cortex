"""Fail before resources: every copied product byte and required test must be committed."""
import ast
import hashlib
import json
from pathlib import Path
import subprocess
import sys
wt=Path(sys.argv[1]);
for script,fixture in [('verify-fixture-contract.py','next/tests/outbox/test_outbox.py'),('verify-expiry-fixture.py','next/tests/outbox_retention/test_outbox_retention.py')]:
 result=subprocess.run([sys.executable,str(Path(__file__).with_name(script)),str(wt/fixture)],capture_output=True,text=True)
 assert result.returncode==0,script+': '+result.stdout
checks={}; expected={'outbox':32,'outbox_caller':2,'outbox_guards':19,'outbox_retention':4,'outbox_inventory':6,'outbox_late_publication':1}
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
status=subprocess.check_output(['git','status','--porcelain','--untracked-files=all','--','next'],cwd=wt,text=True)
checks['product_committed']={'passed':not status,'status':status}
mirror=wt/'docs/next/evidence/c06-source/run-core-pg.py'
controller=Path(__file__).with_name('run-core-pg.py')
checks['controller_committed']={'passed':mirror.read_bytes()==controller.read_bytes() and not subprocess.check_output(['git','diff','HEAD','--',str(mirror)],cwd=wt)}
passed=all(v['passed'] for v in checks.values());print(json.dumps({'passed':passed,'checks':checks}));raise SystemExit(0 if passed else 1)
