"""Bounded supplemental candidate worker; frozen bodies remain separate credit."""
import hashlib,json,sys,unittest,faulthandler,os
from pathlib import Path
HERE=Path(__file__).resolve().parent;NEXT=HERE.parents[1]
sys.path[:0]=[str(NEXT/'tests/pg_b2_draft'),str(NEXT/'tests/runtime_grants'),str(NEXT/'tests/install'),str(NEXT/'tests/release'),str(NEXT/'tests/runtime'),str(NEXT/'tests'),str(NEXT/'src')]
from test_receipts import AssertionResult,MARKER
import test_pg_b2_native
suite=unittest.defaultTestLoader.loadTestsFromModule(test_pg_b2_native)
def flat(s):
 for x in s:
  if isinstance(x,unittest.TestSuite):yield from flat(x)
  else:yield x
nodes=[x.id() for x in flat(suite)]
if sys.argv[1:]:
 if not set(sys.argv[1:])<=set(nodes):raise RuntimeError('unknown exact candidate selector')
 suite=unittest.defaultTestLoader.loadTestsFromNames(sys.argv[1:])
print('PGB2_LOADED='+json.dumps({'nodes':nodes,'selected':sys.argv[1:] or nodes,'files':{str(x.relative_to(NEXT)):hashlib.sha256(x.read_bytes()).hexdigest() for x in [*sorted((NEXT/'tests/runtime_grants').glob('*.py')),*sorted((NEXT/'tests/pg_b2_draft').glob('*.py')),NEXT/'src/cortex_core/runtime/role_grants.py',NEXT/'src/cortex_core/install/lifecycle.py',NEXT/'schema/auth/007-runtime-grants.sql']}}),flush=True)
stackfile=(Path(os.environ['PGO_CONTROL_DIR'])/'finite-stacks.txt').open('w')
faulthandler.dump_traceback_later(30,repeat=True,file=stackfile)
executed=set();seen_codes=set();held_codes=[];prefix=str(NEXT)+'/'
def profile(frame,event,arg):
 if event!='call':return
 code=frame.f_code;key=id(code)
 if key in seen_codes:return
 seen_codes.add(key);held_codes.append(code)
 if code.co_filename.startswith(prefix):
  executed.add((code.co_filename[len(prefix):],code.co_qualname))
sys.setprofile(profile)
try:r=unittest.TextTestRunner(verbosity=2,resultclass=AssertionResult).run(suite)
finally:
 sys.setprofile(None);faulthandler.cancel_dump_traceback_later();stackfile.close()
print('PGB2_EXECUTED='+json.dumps(sorted(executed)),flush=True)
print(MARKER+json.dumps({'tests_run':r.testsRun,'failures':r.assertions,'errors':[{'id':c.id(),'traceback':v} for c,v in r.errors],'skipped':[(c.id(),reason) for c,reason in r.skipped]}),flush=True)
raise SystemExit(0 if r.wasSuccessful() else 1)
