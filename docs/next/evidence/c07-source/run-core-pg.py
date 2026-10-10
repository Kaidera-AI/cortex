"""C07 copied real-PG gate: predecessor suites, frozen module controls and faults."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
SHARED=Path(__file__).resolve().parent.parent/'c06-source'
sys.path.insert(0,str(SHARED))
from replay_lifecycle import Lifecycle
ROOT=Path('/Users/amadmalik/DevVault/helix')

WT=Path.cwd()
OUT=Path(__file__).resolve().parent
OUT.mkdir(parents=True,exist_ok=True)
PHASE=sys.argv[1]
TARGET=OUT/(PHASE+'.json')
assert not TARGET.exists()
TREE=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()
SOURCE={str(p.relative_to(WT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in (WT/'next').rglob('*') if p.is_file()}
TOOLS={Path(__file__).name:hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
POD='kaidera-test-core-schema-1'
DB='kaidera-test-core-db-1'
DRIVER='kaidera-test-core-driver-1'
PG='sha256:db676a0ed906c00f55020fb8999e4fb30c598bf5c3b5c188630aef2812d3f11d'
PY='sha256:ce9a404c2c0138e747a43e6ea022d2f7e670ed868df35d627a663ba7fb940ea9'
SUITES=[('schema', 30), ('auth', 40), ('records', 14), ('coordination', 23), ('core_adapters', 6), ('acceptance_guards', 7), ('auth_identity', 26), ('auth_identity_guards', 6), ('identity_coordination', 8), ('identity_portability', 3), ('identity_receipts', 5), ('identity_transition', 1), ('identity_policy_faults', 2), ('identity_binding', 2), ('contract', 33), ('receipt', 20), ('c05_review',8), ('c05_private',5)]
results=[]
passed=False
mutation_errors=[]


def run(args,timeout=120):
    try:
        result=subprocess.run(args,text=True,capture_output=True,timeout=timeout)
    except subprocess.TimeoutExpired as error:
        results.append({'command':args,'timed_out':True,'stdout':str(error.stdout),'stderr':str(error.stderr)})
        raise
    results.append({'command':args,'exit_code':result.returncode,'stdout':result.stdout,'stderr':result.stderr})
    print(json.dumps({'command':args[:6],'exit_code':result.returncode,'output':(result.stdout+result.stderr)[-1800:]}),flush=True)
    return result


def checked(args,timeout=120):
    result=run(args,timeout)
    assert result.returncode==0
    return result


assert Path(__file__).read_bytes()==subprocess.check_output(['git','show','HEAD:docs/next/evidence/c07-source/run-core-pg.py']), 'c07_controller_not_git_bound'
preflight=run([sys.executable,str(OUT/'verify-replay-inputs.py'),str(WT)])
assert preflight.returncode==0, 'copied source/test/controller preflight failed before any resource creation'

expected_wheels=json.loads((SHARED/'offline-wheel-inputs.json').read_text())['wheels']
actual_wheels={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (WT/'tmp/wheels').iterdir() if p.is_file()}
assert actual_wheels==expected_wheels, 'actual wheel bytes differ before resource creation'
if PHASE=='--preflight-only':
    print(json.dumps({'passed':True,'resource_operations':0,'actual_wheels':actual_wheels}));raise SystemExit(0)


life=Lifecycle(ROOT,run)
LABELS=life.labels()+['--label','slice=C07']
TOOLS['shared/replay_lifecycle.py']=hashlib.sha256((SHARED/'replay_lifecycle.py').read_bytes()).hexdigest()

try:
    life.acquire()
    for kind,name in [('pod',POD),('container',DB),('container',DRIVER)]:
        assert run(['podman',kind,'exists',name]).returncode==1
    assert json.loads(checked(['podman','ps','-a','--filter','label=owner=cox@helix','--format','json']).stdout)==[]
    for image in (PG,PY):
        assert json.loads(checked(['podman','image','inspect',image]).stdout)[0]['Architecture']=='arm64'
    life.create('pod',POD,['podman','pod','create','--name',POD,'--network','none','--cpus','2','--memory','1g']+LABELS)
    life.create('container',DB,['podman','run','-d','--pod',POD,'--name',DB]+LABELS+[
        '--cpus','1','--memory','768m','--read-only','--user','70:70','--cap-drop=ALL',
        '--security-opt=no-new-privileges','--image-volume=ignore',
        '--tmpfs','/var/lib/postgresql:rw,size=536870912,mode=1777',
        '--tmpfs','/var/run/postgresql:rw,size=16777216,mode=1777',
        '--tmpfs','/tmp:rw,size=16777216,mode=1777','--env','PGDATA=/var/lib/postgresql/18/data',
        '--env','POSTGRES_HOST_AUTH_METHOD=trust',PG,'-c','max_wal_size=64MB','-c','min_wal_size=32MB','-c','checkpoint_timeout=30s'])
    life.create('container',DRIVER,['podman','run','-d','--pod',POD,'--name',DRIVER]+LABELS+[
        '--cpus','1','--memory','256m','--read-only','--user','10001:10001',
        '--cap-drop=ALL','--security-opt=no-new-privileges',
        '--tmpfs','/tmp:rw,size=268435456,mode=1777',PY,'sleep','1800'])
    checked(['podman','cp',str(WT/'next'),DRIVER+':/tmp/next'])
    checked(['podman','cp',str(WT/'tmp/wheels'),DRIVER+':/tmp/wheels'])

    copied_wheels=checked(['podman','exec',DRIVER,'python','-c',"from pathlib import Path;import hashlib,json;print(json.dumps({p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in Path('/tmp/wheels').iterdir() if p.is_file()}))"])
    assert json.loads(copied_wheels.stdout)==expected_wheels
    TOOLS['actual_wheels']=actual_wheels
    TOOLS['copied_wheels']=json.loads(copied_wheels.stdout)

    checked(['podman','exec',DRIVER,'python','-m','pip','install','--no-index',
             '--find-links=/tmp/wheels','--no-cache-dir','--target=/tmp/deps','-r','/tmp/next/requirements-db-test.txt'])
    checked(['podman','exec',DRIVER,'python','-m','pip','install','--no-index',
             '--find-links=/tmp/wheels','--no-cache-dir','--target=/tmp/deps','-r','/tmp/next/requirements-test.txt'])
    for attempt in range(30):
        if run(['podman','exec',DB,'pg_isready','-U','postgres']).returncode==0:
            break
        time.sleep(1)
    else:
        raise RuntimeError('C07 disposable PG did not become ready')
    checked(['podman','exec',DB,'psql','-U','postgres','-At','-c','SELECT version();'])
    env=['podman','exec','--env','PYTHONPATH=/tmp/deps:/tmp/next/src',
         '--env','PYTHONDONTWRITEBYTECODE=1',
         '--env','TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:5432/postgres',DRIVER]
    checked(env+['python','-c',"import os,psycopg;from cortex_core.migrations import apply_migrations;c=psycopg.connect(os.environ['TEST_DATABASE_URL'],autocommit=True);print('CORTEX_MIGRATION_CHECK='+str(apply_migrations(c)));c.close()"])
    for directory,count in SUITES:
        if PHASE.startswith('write-only-event-red') and directory in ('c05_review','c05_private'):continue
        value=checked(env+['python','/tmp/next/tests/test_receipts.py','/tmp/next/tests/'+directory],300)
        report=json.loads(next(line.split('=',1)[1] for line in value.stdout.splitlines() if line.startswith('CORTEX_TEST_RESULT=')))
        assert report['tests_run']==count and not report['failures'] and not report['errors']
    value=run(env+['python','/tmp/next/tests/test_receipts.py','/tmp/next/tests/outbox_write_only'],300)
    report=json.loads(next(line.split('=',1)[1] for line in value.stdout.splitlines() if line.startswith('CORTEX_TEST_RESULT=')))
    assert report['tests_run']==2 and not report['errors']
    if PHASE.startswith('write-only-event-red'):
        assert value.returncode==1 and len(report['failures'])==2
        assert all(r['phase']=='test' and r['is_assertion'] for r in report['failures'])
    else:
        assert value.returncode==0 and not report['failures']
    value=run(env+['python','/tmp/next/tests/test_receipts.py','/tmp/next/tests/outbox_private_namespace'],300)
    report=json.loads(next(line.split('=',1)[1] for line in value.stdout.splitlines() if line.startswith('CORTEX_TEST_RESULT=')))
    assert report['tests_run']==2 and not report['errors']
    if PHASE.startswith('private-namespace-red'):
        assert value.returncode==1 and len(report['failures'])==2
        assert all(r['phase']=='test' and r['is_assertion'] for r in report['failures'])
    else:
        assert value.returncode==0 and not report['failures']
    value=run(env+['python','/tmp/next/tests/test_receipts.py','/tmp/next/tests/outbox'],300)
    report=json.loads(next(line.split('=',1)[1] for line in value.stdout.splitlines() if line.startswith('CORTEX_TEST_RESULT=')))
    assert report['tests_run']==32 and not report['errors']
    if PHASE.startswith('outbox-red'):
        assert value.returncode==1 and len(report['failures'])==32
        assert all(row['phase']=='test' and row['is_assertion'] for row in report['failures'])
    else:
        assert value.returncode==0 and not report['failures']
    value=run(env+['python','/tmp/next/tests/test_receipts.py','/tmp/next/tests/outbox_caller'],300)
    report=json.loads(next(line.split('=',1)[1] for line in value.stdout.splitlines() if line.startswith('CORTEX_TEST_RESULT=')))
    assert report['tests_run']==2 and not report['errors']
    if PHASE.startswith('outbox-red-caller'):
        assert value.returncode==1 and len(report['failures'])==2
        assert all(row['phase']=='test' and row['is_assertion'] for row in report['failures'])
    else:
        assert value.returncode==0 and not report['failures']
    value=run(env+['python','/tmp/next/tests/test_receipts.py','/tmp/next/tests/outbox_guards'],300)
    report=json.loads(next(line.split('=',1)[1] for line in value.stdout.splitlines() if line.startswith('CORTEX_TEST_RESULT=')))
    assert report['tests_run']==23 and not report['errors']
    if PHASE.startswith('outbox-receipt-actor-red'):
        assert value.returncode==1 and len(report['failures'])==3
        assert all(r['phase']=='test' and r['is_assertion'] for r in report['failures'])
        assert {r['id'].rsplit('.',1)[-1] for r in report['failures']}=={'test_authentic_completed_fact_cannot_ack_a_foreign_workers_completion','test_authentic_reviewed_fact_cannot_ack_a_workers_self_acceptance','test_authentic_sixty_second_claim_cannot_ack_an_hour_lease'}
    elif PHASE.startswith('outbox-final-review-red'):
        assert value.returncode==1 and len(report['failures'])==9
        assert all(r['phase']=='test' and r['is_assertion'] for r in report['failures'])
        assert {r['id'].split(' (')[0].rsplit('.',1)[-1] for r in report['failures']}=={'test_reverse_history_insertion_refuses_revision_reordering','test_authentic_pending_event_cannot_forge_successful_completion_receipt','test_authentic_running_event_cannot_forge_claim_attempt_fence_or_holder','test_authentic_upsert_receipt_cannot_ack_a_future_delete','test_authentic_pending_receipt_cannot_ack_a_future_retry','test_job_authoritative_id_cannot_be_relocated_without_old_parent_event','test_attempt_cannot_be_reparented_without_old_job_event','test_job_fact_cannot_be_replayed_as_ordinary_memory_put','test_mixed_record_and_claim_receipt_cannot_bypass_job_contract'}
    elif PHASE.startswith('outbox-guards-red'):
        assert value.returncode==1 and len(report['failures'])==10
        assert all(row['phase']=='test' and row['is_assertion'] for row in report['failures'])
    else:
        assert value.returncode==0 and not report['failures']
    for directory,count in [('outbox_retention',5),('outbox_inventory',10),('outbox_late_publication',1)]:
        value=run(env+['python','/tmp/next/tests/test_receipts.py','/tmp/next/tests/'+directory],300)
        report=json.loads(next(line.split('=',1)[1] for line in value.stdout.splitlines() if line.startswith('CORTEX_TEST_RESULT=')))
        assert report['tests_run']==count and not report['errors']
        if PHASE.startswith('pr51-retention-red') and directory=='outbox_retention':
            assert value.returncode==1 and len(report['failures'])==1
            assert report['failures'][0]['id'].rsplit('.',1)[-1]=='test_own_expired_cursor_prunes_behind_foreign_quarantine'
            assert report['failures'][0]['phase']=='test' and report['failures'][0]['is_assertion']
        elif PHASE.startswith('outbox-final-review-red') and directory=='outbox_retention':
            assert value.returncode==1 and len(report['failures'])==1
            assert report['failures'][0]['id'].rsplit('.',1)[-1].startswith('test_expired_checkpoint_cannot_advance_or_reactivate_completeness')
            assert report['failures'][0]['phase']=='test' and report['failures'][0]['is_assertion']
        elif PHASE.startswith('outbox-final-review-red') and directory=='outbox_inventory':
            assert value.returncode==1 and len(report['failures'])==2
            assert all(r['phase']=='test' and r['is_assertion'] for r in report['failures'])
            assert {r['id'].split(' (')[0].rsplit('.',1)[-1] for r in report['failures']}=={'test_quoted_qualified_writer_is_rejected','test_unqualified_writer_with_search_path_is_rejected'}
        elif PHASE.startswith('outbox-protocol-red'):
            assert value.returncode==1 and len(report['failures'])==count
            assert all(row['phase']=='test' and row['is_assertion'] for row in report['failures'])
        else:
            assert value.returncode==0 and not report['failures']
    value=run(env+['python','/tmp/next/tests/test_receipts.py','/tmp/next/tests/module_consumer'],300)
    report=json.loads(next(line.split('=',1)[1] for line in value.stdout.splitlines() if line.startswith('CORTEX_TEST_RESULT=')))
    assert report['tests_run']==22 and not report['errors']
    if PHASE.startswith('c07-red'):
        assert value.returncode==1 and len(report['failures'])==22
        assert all(r['phase']=='test' and r['is_assertion'] and r['phase_attribution']=='traceback+active-unittest-v2' for r in report['failures'])
    else:
        assert value.returncode==0 and not report['failures']
    if PHASE.startswith('mutation-c07'):
        value=run(env+['python','/tmp/next/scripts/mutate_module_consumer.py'],1200)
        rows=[json.loads(line) for line in value.stdout.splitlines() if line.startswith('{')]
        summary=rows[-1] if rows else {}
        faults=[row for row in rows if 'status' in row]
        valid=value.returncode==0 and faults and summary.get('killed')==summary.get('mutants')==len(faults)
        valid=valid and not summary.get('survivors') and not summary.get('inconclusive')
        for row in faults:
            receipt=row.get('receipt')
            valid=valid and row.get('status')=='killed' and receipt and not receipt['errors']
            valid=valid and row['expected_test'] in {r['id'].split(' (')[0] for r in receipt['failures']}
            valid=valid and all(r['phase']=='test' and r['is_assertion'] and r['phase_attribution']=='traceback+active-unittest-v2' for r in receipt['failures'])
        if not valid:mutation_errors.append({'script':'mutate_module_consumer.py','exit_code':value.returncode,'summary':summary})
    hashes=checked(env+['python','-c',"from pathlib import Path;import hashlib,json;root=Path('/tmp/next');print(json.dumps({str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file()}))"])
    restored=json.loads(hashes.stdout)
    expected={str(p.relative_to(WT/'next')):hashlib.sha256(p.read_bytes()).hexdigest() for p in (WT/'next').rglob('*') if p.is_file()}
    assert all(restored.get(name)==digest for name,digest in expected.items()), 'copied input changed'
    extras=set(restored)-set(expected)
    allowed={'scripts/__pycache__/mutate_contracts.cpython-312.pyc','tests/__pycache__/test_receipts.cpython-312.pyc'}
    assert extras <= allowed, 'unknown copied output inventory'
    if extras:
        for name in sorted(extras):
            checked(env+['python','-c',"from pathlib import Path;Path('/tmp/next/'+"+repr(name)+").unlink()"])
        hashes=checked(env+['python','-c',"from pathlib import Path;import hashlib,json;root=Path('/tmp/next');print(json.dumps({str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file()}))"])
        restored=json.loads(hashes.stdout)
    assert restored==expected, 'all copied source bytes must be restored'
    assert SOURCE=={str(p.relative_to(WT)):digest for p,digest in ((WT/'next'/name,digest) for name,digest in expected.items())}
    assert subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()==TREE
    passed=not mutation_errors
finally:
    cleanup_error=life.finish()
    inventory_error=None
    gone=None
    try:
        gone=all(subprocess.run(['podman',kind,'exists',name],capture_output=True,timeout=30).returncode==1
                 for kind,name in [('container',DB),('container',DRIVER),('pod',POD)])
    except Exception as caught:inventory_error=type(caught).__name__+': '+str(caught)
    TARGET.write_text(json.dumps({'phase':PHASE,'tree':TREE,
        'passed':bool(passed and life.cleanup_verified and gone),'stack_removed':gone,'mutation_errors':mutation_errors,
        'final_inventory_error':inventory_error,'cleanup':life.receipt(),
        'lifecycle':life.lifecycle,'images':[PG,PY],'native_arch':'arm64','network':'none; private loopback',
        'published_ports':[],'bind_mounts':[],'limits':{'cpus':2,'memory':'1g','pg_memory':'768m','driver_memory':'256m'},
        'results':results,'source_sha256':SOURCE,'tool_input_sha256':TOOLS,
        'controller_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()},indent=2)+'\n')
    if cleanup_error:raise cleanup_error
    if inventory_error:raise RuntimeError('final inventory unverified: '+inventory_error)
    assert gone,'C07 resource absence is unverified'

assert passed, "C07 qualification failed; failed matrix and restoration retained"
