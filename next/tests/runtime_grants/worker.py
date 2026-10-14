"""Exact-node gate over the shared BODY/fixture assertion receipt runner."""
import hashlib
import importlib.metadata
import json
from pathlib import Path
import sys
import unittest

HERE=Path(__file__).resolve().parent
NEXT=HERE.parents[1]
sys.path[:0]=[str(HERE),str(NEXT/'src'),str(NEXT/'tests')]
from test_receipts import AssertionResult,MARKER
from support import OBSERVATIONS

def flatten(suite):
    for item in suite:
        if isinstance(item,unittest.TestSuite):yield from flatten(item)
        else:yield item

suite=unittest.defaultTestLoader.discover(str(HERE))
nodes=[case.id() for case in flatten(suite)]
frozen=json.loads((HERE/'FROZEN_NODES.json').read_text())
if nodes!=frozen:raise RuntimeError('exact-node gate differs: no test receipt/RED credit')
selected=sys.argv[1:]
if selected:
    if not set(selected)<=set(nodes):raise RuntimeError('unknown exact selector')
    suite=unittest.defaultTestLoader.loadTestsFromNames(selected)
files=sorted(HERE.glob('*.py'))+[NEXT/'tests/test_receipts.py',NEXT/'tests/core_db_fixture.py',
    NEXT/'tests/auth/test_authorization.py',NEXT/'tests/schema/test_schema.py',
    NEXT/'src/cortex_core/migrations.py',NEXT/'src/cortex_core/upgrade.py']
print('CORTEX_LOADED='+json.dumps({'python':sys.version,'psycopg':importlib.metadata.version('psycopg'),
    'frozen_nodes':len(nodes),'selected_nodes':selected or nodes,
    'files':{str(p.relative_to(NEXT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in files}}),flush=True)
executed=set()
def trace(frame,event,arg):
    if event=='call' and frame.f_code.co_filename.startswith(str(NEXT)):
        executed.add((str(Path(frame.f_code.co_filename).relative_to(NEXT)),frame.f_code.co_qualname))
    return trace
sys.settrace(trace)
try:result=unittest.TextTestRunner(verbosity=2,resultclass=AssertionResult).run(suite)
finally:sys.settrace(None)
value={'tests_run':result.testsRun,'failures':result.assertions,
       'errors':[{'id':case.id(),'traceback':tb} for case,tb in result.errors],
       'skipped':result.skipped}
for row in value['failures']:
    row['pgo_category']=('surface_absence' if row['exception_type']=='support.SurfaceAbsent' else 'semantic') if row['phase']=='test' and row['is_assertion'] else 'inconclusive'
print(MARKER+json.dumps(value),flush=True)
print('CORTEX_EXECUTED='+json.dumps(sorted(executed)),flush=True)
print('CORTEX_ACL_OBSERVATIONS='+json.dumps(OBSERVATIONS),flush=True)
raise SystemExit(0 if result.wasSuccessful() else 1)
