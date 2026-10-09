"""Host orchestration only; actual controller control-flow tests run contained."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from replay_lifecycle import Lifecycle

ROOT = Path('/Users/amadmalik/DevVault/helix')
OUT = Path(__file__).resolve().parent
OUT.mkdir(parents=True, exist_ok=True)
PHASE = sys.argv[1]
TARGET = OUT / (PHASE + '.json')
assert not TARGET.exists()
VARIANT = 'old' if PHASE.startswith('red') else 'safe'
NAME = 'kaidera-test-replay-lifecycle-1'
IMAGE = 'sha256:b4600cb7d697d46d382b9581a7c52fa76f32d19536309c5096994757e62f3700'
results = []
passed = False


def run(args):
    r = subprocess.run(args, capture_output=True, text=True, timeout=60)
    results.append({'command': args, 'exit_code': r.returncode, 'stdout': r.stdout, 'stderr': r.stderr})
    print(json.dumps({'command': args[:6], 'exit_code': r.returncode, 'output': (r.stdout+r.stderr)[-1600:]}), flush=True)
    return r


def checked(args):
    r = run(args)
    assert r.returncode == 0
    return r


life=Lifecycle(ROOT,run)
SOURCE={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in OUT.glob('*.py')}
try:
    life.acquire()
    life.create('container',NAME,['podman','run','-d','--name',NAME,*life.labels(),
             '--network','none','--cpus','2','--memory','1g','--read-only','--user','10001:10001',
             '--cap-drop=ALL','--security-opt=no-new-privileges','--tmpfs','/tmp:rw,size=268435456,mode=1777',
             IMAGE,'sleep','infinity'])
    checked(['podman','cp',str(OUT),NAME+':/tmp/proof'])
    checked(['podman','cp',str(ROOT/'output/Cortex/design55-r426/2026-10-10/graph-cleanup/test_receipts.py'),NAME+':/tmp/test_receipts.py'])
    r = run(['podman','exec','--env','REPLAY_VARIANT='+VARIANT,NAME,'python','/tmp/test_receipts.py','/tmp/proof'])
    marker = 'CORTEX_TEST_RESULT='
    records = [json.loads(line[len(marker):]) for line in r.stdout.splitlines() if line.startswith(marker)]
    assert len(records)==1
    value = records[0]
    assert value['tests_run']==(6 if VARIANT=='old' else 18) and not value['errors']
    if VARIANT=='old':
        expected = {'test_producer_lifecycle.ProducerLifecycle.'+name for name in (
            'test_c03_lost_ack_cleanup_reconciles_effect','test_c03_output_prepared_before_effect',
            'test_graph_same_owner_foreign_lifecycle_preserved','test_graph_removal_uses_reconciled_immutable_id')}
        assert r.returncode==1 and {row['id'] for row in value['failures']}==expected
        assert all(row['phase']=='test' and row['is_assertion'] for row in value['failures'])
    else:
        assert r.returncode==0 and not value['failures']
        suite = """import sys,json,unittest
sys.path.insert(0,'/tmp');sys.path.insert(0,'/tmp/proof')
from test_receipts import AssertionResult,MARKER
s=unittest.defaultTestLoader.discover('/tmp/proof',pattern='test_producer_lifecycle.py')
r=unittest.TextTestRunner(verbosity=2,resultclass=AssertionResult).run(s)
print(MARKER+json.dumps({'tests_run':r.testsRun,'failures':r.assertions,'errors':[{'id':t.id(),'traceback':tb} for t,tb in r.errors]}),flush=True)
raise SystemExit(0 if r.wasSuccessful() else 1)
"""
        recipes = [
            ('pending-after-ack', 'replay_lifecycle.py',
             '        self.pending.add(resource)  # BEFORE the external effect or acknowledgement.\n        self.checked(args)',
             '        self.checked(args)\n        self.pending.add(resource)',
             ['test_c03_lost_ack_cleanup_reconciles_effect','test_graph_lost_ack_cleanup_reconciles_effect','test_graph_removal_uses_reconciled_immutable_id']),
            ('foreign-nonce-ignored', 'replay_lifecycle.py',
             " or (labels or {}).get('kaidera.cox.lifecycle') != self.lifecycle", '',
             ['test_c03_foreign_lifecycle_preserved','test_graph_same_owner_foreign_lifecycle_preserved']),
            ('remove-by-name', 'replay_lifecycle.py',
             "args = ['podman', 'pod', 'rm', '-f', row['Id']] if kind == 'pod' else ['podman', 'rm', '-f', row['Id']]",
             "args = ['podman', 'pod', 'rm', '-f', name] if kind == 'pod' else ['podman', 'rm', '-f', name]",
             ['test_graph_removal_uses_reconciled_immutable_id']),
            ('output-not-prepared', 'safe-run-adoption.py',
             'out.mkdir(parents=True,exist_ok=True)', '# deliberate omitted output preparation',
             ['test_c03_output_prepared_before_effect'])]
        mutation_rows = []
        for name, filename, before, after, expected in recipes:
            original = (OUT/filename).read_text()
            assert original.count(before)==1
            changed = original.replace(before, after, 1)
            patch = "from pathlib import Path;p=Path('/tmp/proof/"+filename+"');assert p.read_text()=="+repr(original)+";p.write_text("+repr(changed)+")"
            checked(['podman','exec',NAME,'python','-c',patch])
            try:
                fault = run(['podman','exec','--env','REPLAY_VARIANT=safe','--env','PYTHONDONTWRITEBYTECODE=1',NAME,'python','-c',suite])
                rows=[json.loads(x.split('=',1)[1]) for x in fault.stdout.splitlines() if x.startswith('CORTEX_TEST_RESULT=')]
                assert len(rows)==1 and rows[0]['tests_run']==6 and not rows[0]['errors']
                ids={'test_producer_lifecycle.ProducerLifecycle.'+x for x in expected}
                assert fault.returncode==1 and {x['id'] for x in rows[0]['failures']}==ids
                assert all(x['phase']=='test' and x['is_assertion'] for x in rows[0]['failures'])
                mutation_rows.append({'name':name,'file':filename,'before':before,'after':after,
                                      'source_sha256':hashlib.sha256(original.encode()).hexdigest(),
                                      'mutated_sha256':hashlib.sha256(changed.encode()).hexdigest(),
                                      'expected_ids':sorted(ids),'status':'killed','raw':results[-1]})
            finally:
                checked(['podman','cp',str(OUT/filename),NAME+':/tmp/proof/'+filename])
        checked(['podman','exec','--env','REPLAY_VARIANT=safe','--env','PYTHONDONTWRITEBYTECODE=1',NAME,'python','/tmp/test_receipts.py','/tmp/proof'])
    passed=True
finally:
    error=life.finish()
    gone=subprocess.run(['podman','container','exists',NAME],capture_output=True,timeout=30).returncode==1
    TARGET.write_text(json.dumps({'phase':PHASE,'variant':VARIANT,'results':results,'mutations':globals().get('mutation_rows',[]),'image_id':IMAGE,'container_removed':gone,
                                 'passed':passed and life.cleanup_verified and gone,'cleanup':life.receipt(),
                                 'lifecycle':life.lifecycle,'limits':{'cpus':2,'memory':'1g'},'ports':[],'bind_mounts':[],
                                 'controller_safety_only':True,'application_tests_executed':False,
                                 'source_sha256':SOURCE},indent=2)+'\n')
    if error:raise error
    assert gone
