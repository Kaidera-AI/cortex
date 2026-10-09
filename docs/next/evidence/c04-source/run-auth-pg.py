"""C04-only disposable PG gate. Quarantined C03/graph producers are never imported."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
import uuid

WT=Path.cwd()
OUT=Path(__file__).resolve().parent
OUT.mkdir(parents=True,exist_ok=True)
PHASE=sys.argv[1]
TARGET=OUT/(PHASE+'.json')
assert not TARGET.exists()
TREE=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()
SOURCE={str(p.relative_to(WT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in (WT/'next').rglob('*') if p.is_file()}
POD='kaidera-test-core-schema-1'
DB='kaidera-test-core-db-1'
DRIVER='kaidera-test-core-driver-1'
PG='sha256:db676a0ed906c00f55020fb8999e4fb30c598bf5c3b5c188630aef2812d3f11d'
PY='sha256:ce9a404c2c0138e747a43e6ea022d2f7e670ed868df35d627a663ba7fb940ea9'
NONCE=uuid.uuid4().hex
LABELS=['--label','owner=cox@helix','--label','kaidera.cox.lifecycle='+NONCE,'--label','slice=C04']
pending=[]
results=[]
passed=False
cleanup_errors=[]


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


def owned(kind,name):
    exists=run(['podman',kind,'exists',name])
    if exists.returncode==1:
        return None
    assert exists.returncode==0, 'resource existence cannot be established'
    info=json.loads(checked(['podman',kind,'inspect',name]).stdout)[0]
    labels=info.get('Labels',{}) if kind=='pod' else info['Config'].get('Labels',{})
    identity=info.get('Id',info.get('ID',''))
    assert info['Name'].lstrip('/')==name and len(identity)==64
    if labels.get('owner')!='cox@helix' or labels.get('kaidera.cox.lifecycle')!=NONCE:
        raise RuntimeError('foreign lifecycle is preserved: '+name)
    return identity


def create(kind,name,args):
    pending.append((kind,name))
    checked(args)
    identity=owned(kind,name)
    assert identity, 'acknowledged creation has no owned effect'
    return identity


def cleanup():
    for kind,name in list(reversed(pending)):
        try:
            identity=owned(kind,name)
            if identity:
                if kind=='pod':
                    assert not any(resource_kind=='container' for resource_kind,_ in pending), 'unreconciled child blocks pod deletion'
                    info=json.loads(checked(['podman','pod','inspect',identity]).stdout)[0]
                    infrastructure=info.get('InfraContainerID')
                    children={row.get('Id',row.get('ID')) for row in info['Containers']}
                    assert children==({infrastructure} if infrastructure else set()), 'foreign child blocks pod deletion'
                checked(['podman',kind,'rm','-f',identity])
            assert run(['podman',kind,'exists',name]).returncode==1
            pending.remove((kind,name))
        except Exception as error:
            cleanup_errors.append({'kind':kind,'name':name,'error':repr(error)})


try:
    for kind,name in [('pod',POD),('container',DB),('container',DRIVER)]:
        assert run(['podman',kind,'exists',name]).returncode==1
    assert json.loads(checked(['podman','ps','-a','--filter','label=owner=cox@helix','--format','json']).stdout)==[]
    for image in (PG,PY):
        assert json.loads(checked(['podman','image','inspect',image]).stdout)[0]['Architecture']=='arm64'
    create('pod',POD,['podman','pod','create','--name',POD,'--network','none','--cpus','2','--memory','1g']+LABELS)
    create('container',DB,['podman','run','-d','--pod',POD,'--name',DB]+LABELS+[
        '--cpus','1','--memory','768m','--read-only','--user','70:70','--cap-drop=ALL',
        '--security-opt=no-new-privileges','--image-volume=ignore',
        '--tmpfs','/var/lib/postgresql:rw,size=536870912,mode=1777',
        '--tmpfs','/var/run/postgresql:rw,size=16777216,mode=1777',
        '--tmpfs','/tmp:rw,size=16777216,mode=1777','--env','PGDATA=/var/lib/postgresql/18/data',
        '--env','POSTGRES_HOST_AUTH_METHOD=trust',PG,'-c','max_wal_size=64MB','-c','min_wal_size=32MB','-c','checkpoint_timeout=30s'])
    create('container',DRIVER,['podman','run','-d','--pod',POD,'--name',DRIVER]+LABELS+[
        '--cpus','1','--memory','256m','--read-only','--user','10001:10001',
        '--cap-drop=ALL','--security-opt=no-new-privileges',
        '--tmpfs','/tmp:rw,size=268435456,mode=1777',PY,'sleep','1800'])
    checked(['podman','cp',str(WT/'next'),DRIVER+':/tmp/next'])
    checked(['podman','cp',str(WT/'tmp/wheels'),DRIVER+':/tmp/wheels'])
    checked(['podman','exec',DRIVER,'python','-m','pip','install','--no-index',
             '--find-links=/tmp/wheels','--no-cache-dir','--target=/tmp/deps','-r','/tmp/next/requirements-db-test.txt'])
    for attempt in range(30):
        if run(['podman','exec',DB,'pg_isready','-U','postgres']).returncode==0:
            break
        time.sleep(1)
    else:
        raise RuntimeError('C04 disposable PG did not become ready')
    checked(['podman','exec',DB,'psql','-U','postgres','-At','-c','SELECT version();'])
    env=['podman','exec','--env','PYTHONPATH=/tmp/deps:/tmp/next/src',
         '--env','PYTHONDONTWRITEBYTECODE=1',
         '--env','TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:5432/postgres',DRIVER]
    checked(env+['python','/tmp/next/tests/test_receipts.py','/tmp/next/tests/schema'],300)
    if PHASE.startswith('private-red'):
        value=run(env+['python','/tmp/next/tests/test_receipts.py','/tmp/next/tests/auth'],300)
        reports=[json.loads(line.split('=',1)[1]) for line in value.stdout.splitlines() if line.startswith('CORTEX_TEST_RESULT=')]
        assert len(reports)==1
        report=reports[0]
        assert value.returncode==1 and report['tests_run']==39 and not report['errors']
        assert len(report['failures'])==2
        assert all(row['id'].split(' (')[0]=='test_private_binding.PrivateBindingTests.test_null_policy_targets_and_need_are_refused'
                   and row['phase']=='test' and row['is_assertion'] for row in report['failures'])
    elif PHASE.startswith('binding-red'):
        value=run(env+['python','/tmp/next/tests/test_receipts.py','/tmp/next/tests/auth'],300)
        reports=[json.loads(line.split('=',1)[1]) for line in value.stdout.splitlines() if line.startswith('CORTEX_TEST_RESULT=')]
        assert len(reports)==1
        report=reports[0]
        expected={'test_transaction_binding.TransactionBindingTests.'+name for name in (
            'test_temporary_action_elevation_restored_before_exit_cannot_commit',
            'test_temporary_granted_project_restored_before_exit_cannot_read',
            'test_temporary_other_credential_restored_before_exit_cannot_read',
            'test_null_action_resolver_refuses_owner_scope','test_missing_action_direct_rls_refuses_owner_scope')}
        assert value.returncode==1 and report['tests_run']==32 and not report['errors']
        assert {row['id'] for row in report['failures']}==expected
        assert all(row['phase']=='test' and row['is_assertion'] for row in report['failures'])
    else:
        checked(env+['python','/tmp/next/tests/test_receipts.py','/tmp/next/tests/auth'],300)
        checked(env+['python','/tmp/next/scripts/mutate_auth.py'],1200)
    hashes=checked(env+['python','-c',"from pathlib import Path;import hashlib,json;root=Path('/tmp/next');print(json.dumps({str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file()}))"])
    restored=json.loads(hashes.stdout)
    expected={str(p.relative_to(WT/'next')):hashlib.sha256(p.read_bytes()).hexdigest() for p in (WT/'next').rglob('*') if p.is_file()}
    assert restored==expected, 'all copied source bytes must be restored'
    assert SOURCE=={str(p.relative_to(WT)):digest for p,digest in ((WT/'next'/name,digest) for name,digest in expected.items())}
    assert subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()==TREE
    passed=True
finally:
    if ('container',DB) in pending:
        try:
            if owned('container',DB):
                run(['podman','logs',DB])
                run(['podman','exec',DB,'df','-h','/var/lib/postgresql'])
        except Exception as error:
            cleanup_errors.append({'diagnostic_error':repr(error)})
    cleanup()
    if pending:
        cleanup()
    TARGET.write_text(json.dumps({'phase':PHASE,'tree':TREE,
        'passed':passed,'stack_removed':not pending,'pending':pending,'cleanup_errors':cleanup_errors,
        'lifecycle':NONCE,'images':[PG,PY],'native_arch':'arm64','network':'none; private loopback',
        'published_ports':[],'bind_mounts':[],'limits':{'cpus':2,'memory':'1g','pg_memory':'768m','driver_memory':'256m'},
        'results':results,'source_sha256':SOURCE,
        'controller_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()},indent=2)+'\n')
    assert not pending, 'C04 lifecycle cleanup could not be verified; resource retained for reconciliation'
