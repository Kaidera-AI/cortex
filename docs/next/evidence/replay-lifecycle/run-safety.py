"""Host orchestration only; actual controller control-flow tests run contained."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import uuid

ROOT = Path('/Users/amadmalik/DevVault/helix')
OUT = Path(__file__).resolve().parent
OUT.mkdir(parents=True, exist_ok=True)
PHASE = sys.argv[1]
TARGET = OUT / (PHASE + '.json')
assert not TARGET.exists()
VARIANT = 'old' if PHASE.startswith('red') else 'safe'
NAME = 'kaidera-test-replay-lifecycle-1'
IMAGE = 'sha256:b4600cb7d697d46d382b9581a7c52fa76f32d19536309c5096994757e62f3700'
NONCE = uuid.uuid4().hex
results = []
pending = False


def run(args):
    r = subprocess.run(args, capture_output=True, text=True, timeout=60)
    results.append({'command': args, 'exit_code': r.returncode, 'stdout': r.stdout, 'stderr': r.stderr})
    print(json.dumps({'command': args[:6], 'exit_code': r.returncode, 'output': (r.stdout+r.stderr)[-1600:]}), flush=True)
    return r


def checked(args):
    r = run(args)
    assert r.returncode == 0
    return r


try:
    assert subprocess.run(['podman','container','exists',NAME],capture_output=True).returncode == 1
    pending = True
    checked(['podman','run','-d','--name',NAME,'--label','owner=cox@helix','--label','kaidera.cox.lifecycle='+NONCE,
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
    assert value['tests_run']==(6 if VARIANT=='old' else 14) and not value['errors']
    if VARIANT=='old':
        expected = {'test_producer_lifecycle.ProducerLifecycle.'+name for name in (
            'test_c03_lost_ack_cleanup_reconciles_effect','test_c03_output_prepared_before_effect',
            'test_graph_same_owner_foreign_lifecycle_preserved','test_graph_removal_uses_reconciled_immutable_id')}
        assert r.returncode==1 and {row['id'] for row in value['failures']}==expected
        assert all(row['phase']=='test' and row['is_assertion'] for row in value['failures'])
    else:
        assert r.returncode==0 and not value['failures']
finally:
    if pending and subprocess.run(['podman','container','exists',NAME],capture_output=True).returncode==0:
        info=json.loads(checked(['podman','container','inspect',NAME]).stdout)[0]
        if info['Config']['Labels'].get('kaidera.cox.lifecycle')==NONCE:
            checked(['podman','rm','-f',info['Id']])
    gone=subprocess.run(['podman','container','exists',NAME],capture_output=True).returncode==1
    TARGET.write_text(json.dumps({'phase':PHASE,'variant':VARIANT,'results':results,'image_id':IMAGE,'container_removed':gone,
                                 'lifecycle':NONCE,'limits':{'cpus':2,'memory':'1g'},'ports':[],'bind_mounts':[],
                                 'controller_safety_only':True,'application_tests_executed':False,
                                 'source_sha256':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in OUT.iterdir() if p.is_file() and p.suffix=='.py'}},indent=2)+'\n')
