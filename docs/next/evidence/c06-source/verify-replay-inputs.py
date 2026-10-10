"""Fail before resources: every copied product byte and required test must be committed."""
import ast
import hashlib
import json
from pathlib import Path
import subprocess
import sys
wt=Path(sys.argv[1]); checks={}; expected={'outbox':32,'outbox_caller':2,'outbox_guards':10,'outbox_retention':3,'outbox_inventory':3}
for directory,count in expected.items():
 files=list((wt/'next/tests'/directory).glob('test_*.py'))
 actual=sum(sum(isinstance(n,ast.FunctionDef) and n.name.startswith('test_') for n in ast.walk(ast.parse(p.read_text()))) for p in files)
 checks[directory]={'actual':actual,'expected':count,'passed':actual==count}
status=subprocess.check_output(['git','status','--porcelain','--untracked-files=all','--','next'],cwd=wt,text=True)
checks['product_committed']={'passed':not status,'status':status}
mirror=wt/'docs/next/evidence/c06-source/run-core-pg.py'
controller=Path(__file__).with_name('run-core-pg.py')
checks['controller_committed']={'passed':mirror.read_bytes()==controller.read_bytes() and not subprocess.check_output(['git','diff','HEAD','--',str(mirror)],cwd=wt)}
passed=all(v['passed'] for v in checks.values());print(json.dumps({'passed':passed,'checks':checks}));raise SystemExit(0 if passed else 1)
