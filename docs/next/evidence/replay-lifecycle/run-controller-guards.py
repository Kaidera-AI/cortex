"""Stdlib orchestration; controller-tail guards execute only inside Podman."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from replay_lifecycle import Lifecycle

ROOT=Path('/Users/amadmalik/DevVault/helix')
OUT=Path(__file__).resolve().parent
OUT.mkdir(parents=True,exist_ok=True)
PHASE=sys.argv[1]; TARGET=OUT/(PHASE+'.json'); assert not TARGET.exists()
NAME='kaidera-test-replay-lifecycle-1'
IMAGE='sha256:b4600cb7d697d46d382b9581a7c52fa76f32d19536309c5096994757e62f3700'
SOURCE={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in OUT.glob('*.py')}
results=[]; passed=False
def run(args):
    value=subprocess.run(args,capture_output=True,text=True,timeout=60)
    results.append({'command':args,'exit_code':value.returncode,'stdout':value.stdout,'stderr':value.stderr})
    print(json.dumps({'command':args[:5],'exit_code':value.returncode,'output':(value.stdout+value.stderr)[-1200:]}),flush=True)
    return value
def checked(args):
    value=run(args); assert value.returncode==0; return value
life=Lifecycle(ROOT,run)
try:
    life.acquire()
    life.create('container',NAME,['podman','run','-d','--name',NAME,*life.labels(),
                '--network','none','--cpus','2','--memory','1g','--read-only','--user','10001:10001',
                '--cap-drop=ALL','--security-opt=no-new-privileges','--tmpfs','/tmp:rw,size=268435456,mode=1777',IMAGE,'sleep','900'])
    checked(['podman','cp',str(OUT),NAME+':/tmp/proof'])
    checked(['podman','cp',str(ROOT/'output/Cortex/design55-r426/2026-10-10/graph-cleanup/test_receipts.py'),NAME+':/tmp/test_receipts.py'])
    suite="""import sys,json,unittest
sys.path.insert(0,'/tmp');sys.path.insert(0,'/tmp/proof')
from test_receipts import AssertionResult,MARKER
s=unittest.defaultTestLoader.discover('/tmp/proof',pattern='test_controller_guards.py')
r=unittest.TextTestRunner(verbosity=2,resultclass=AssertionResult).run(s)
print(MARKER+json.dumps({'tests_run':r.testsRun,'failures':r.assertions,'errors':[{'id':t.id(),'traceback':tb} for t,tb in r.errors]}),flush=True)
raise SystemExit(0 if r.wasSuccessful() else 1)
"""
    value=run(['podman','exec','--env','PYTHONDONTWRITEBYTECODE=1',NAME,'python','-c',suite])
    reports=[json.loads(x.split('=',1)[1]) for x in value.stdout.splitlines() if x.startswith('CORTEX_TEST_RESULT=')]
    assert len(reports)==1 and reports[0]['tests_run']==4 and not reports[0]['errors']
    if PHASE.startswith('red'):
        assert value.returncode==1
        assert {x['id'].split(' (')[0] for x in reports[0]['failures']}=={'test_controller_guards.ControllerGuards.'+x for x in ('test_c03_cleanup_failure_cannot_return_green','test_graph_cleanup_failure_cannot_return_green','test_direct_podman_control_calls_are_bounded','test_tool_input_binding_precedes_resource_launch')}
        assert all(x['phase']=='test' and x['is_assertion'] for x in reports[0]['failures'])
    else:
        assert value.returncode==0 and not reports[0]['failures']
    passed=True
finally:
    error=life.finish()
    TARGET.write_text(json.dumps({'phase':PHASE,'passed':passed and life.cleanup_verified,'source_sha256':SOURCE,'results':results,'cleanup':life.receipt(),'scope':'actual controller finally blocks with declared pre-passed control flag, no application suite executed'},indent=2)+'\n')
    if error: raise error
