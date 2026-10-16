"""Separate PG-A BODY receipt; exact local nodes, same shared classifier."""
import hashlib,json,sys,unittest
from pathlib import Path
HERE=Path(__file__).resolve().parent
NEXT=HERE.parents[1]
sys.path[:0]=[str(HERE),str(NEXT/'src'),str(NEXT/'tests'),str(NEXT/'tests/runtime_grants')]
from test_receipts import AssertionResult,MARKER
suite=unittest.defaultTestLoader.discover(str(NEXT/'tests/runtime_grants_draft'),pattern='test_draft.py')
def flat(s):
 for x in s:
  if isinstance(x,unittest.TestSuite):yield from flat(x)
  else:yield x
nodes=[x.id() for x in flat(suite)]
print('DRAFT_LOADED='+json.dumps({'nodes':nodes,'file_hashes':{str(p.relative_to(NEXT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in [NEXT/'tests/runtime_grants_draft/test_draft.py',NEXT/'schema/auth/007-runtime-grants.sql'] if p.exists()}}),flush=True)
selected=sys.argv[1:]
if selected:
 if not set(selected)<=set(nodes):raise RuntimeError('unknown exact draft node')
 suite=unittest.defaultTestLoader.loadTestsFromNames(selected)
r=unittest.TextTestRunner(verbosity=2,resultclass=AssertionResult).run(suite)
print(MARKER+json.dumps({'tests_run':r.testsRun,'failures':r.assertions,'errors':[{'id':c.id(),'traceback':t} for c,t in r.errors],'skipped':r.skipped}),flush=True)
raise SystemExit(0 if r.wasSuccessful() else 1)
