"""C06-only copied real-PG gates with hash-bound stdlib lifecycle custody."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
SHARED=Path(__file__).resolve().parent.parent/'replay-lifecycle'
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
SUITES=[('schema', 30), ('auth', 40), ('records', 14), ('coordination', 23), ('core_adapters', 6), ('acceptance_guards', 7), ('auth_identity', 26), ('auth_identity_guards', 6), ('identity_coordination', 8), ('identity_portability', 3), ('identity_receipts', 5), ('identity_transition', 1), ('identity_policy_faults', 2), ('identity_binding', 2), ('contract', 33), ('receipt', 20)]
results=[]
passed=False


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


life=Lifecycle(ROOT,run)
LABELS=life.labels()+['--label','slice=C06']
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
    checked(['podman','exec',DRIVER,'python','-m','pip','install','--no-index',
             '--find-links=/tmp/wheels','--no-cache-dir','--target=/tmp/deps','-r','/tmp/next/requirements-db-test.txt'])
    checked(['podman','exec',DRIVER,'python','-m','pip','install','--no-index',
             '--find-links=/tmp/wheels','--no-cache-dir','--target=/tmp/deps','-r','/tmp/next/requirements-test.txt'])
    for attempt in range(30):
        if run(['podman','exec',DB,'pg_isready','-U','postgres']).returncode==0:
            break
        time.sleep(1)
    else:
        raise RuntimeError('C05 disposable PG did not become ready')
    checked(['podman','exec',DB,'psql','-U','postgres','-At','-c','SELECT version();'])
    env=['podman','exec','--env','PYTHONPATH=/tmp/deps:/tmp/next/src',
         '--env','PYTHONDONTWRITEBYTECODE=1',
         '--env','TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:5432/postgres',DRIVER]
    for directory,count in SUITES:
        value=checked(env+['python','/tmp/next/tests/test_receipts.py','/tmp/next/tests/'+directory],300)
        report=json.loads(next(line.split('=',1)[1] for line in value.stdout.splitlines() if line.startswith('CORTEX_TEST_RESULT=')))
        assert report['tests_run']==count and not report['failures'] and not report['errors']
    value=run(env+['python','/tmp/next/tests/test_receipts.py','/tmp/next/tests/outbox'],300)
    report=json.loads(next(line.split('=',1)[1] for line in value.stdout.splitlines() if line.startswith('CORTEX_TEST_RESULT=')))
    assert report['tests_run']==32 and not report['errors']
    if PHASE.startswith('outbox-red'):
        assert value.returncode==1 and len(report['failures'])==32
        assert all(row['phase']=='test' and row['is_assertion'] for row in report['failures'])
    else:
        assert value.returncode==0 and not report['failures']
    if PHASE.startswith('mutation'):
        for script,count in [('mutate_outbox.py',32),('mutate_identity.py',32),('mutate_core_adapters.py',30),('mutate_contracts.py',26),('mutate_test_receipts.py',8)]:
            value=checked(env+['python','/tmp/next/scripts/'+script],900)
            rows=[json.loads(line) for line in value.stdout.splitlines() if line.startswith('{')]
            faults=[row for row in rows if 'status' in row]
            assert len(faults)==count and all(row['status']=='killed' for row in faults)
            for row in faults:
                receipt=row['receipt']
                assert not receipt['errors'] and all(r['phase']=='test' and r['is_assertion'] for r in receipt['failures'])
                assert row['expected_test'] in {r['id'].split(' (')[0] for r in receipt['failures']}
            summary=rows[-1]
            assert summary['mutants']==count and summary['killed']==count and not summary['survivors'] and not summary['inconclusive']
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
    passed=True
finally:
    cleanup_error=life.finish()
    inventory_error=None
    gone=None
    try:
        gone=all(subprocess.run(['podman',kind,'exists',name],capture_output=True,timeout=30).returncode==1
                 for kind,name in [('container',DB),('container',DRIVER),('pod',POD)])
    except Exception as caught:inventory_error=type(caught).__name__+': '+str(caught)
    TARGET.write_text(json.dumps({'phase':PHASE,'tree':TREE,
        'passed':bool(passed and life.cleanup_verified and gone),'stack_removed':gone,
        'final_inventory_error':inventory_error,'cleanup':life.receipt(),
        'lifecycle':life.lifecycle,'images':[PG,PY],'native_arch':'arm64','network':'none; private loopback',
        'published_ports':[],'bind_mounts':[],'limits':{'cpus':2,'memory':'1g','pg_memory':'768m','driver_memory':'256m'},
        'results':results,'source_sha256':SOURCE,'tool_input_sha256':TOOLS,
        'controller_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()},indent=2)+'\n')
    if cleanup_error:raise cleanup_error
    if inventory_error:raise RuntimeError('final inventory unverified: '+inventory_error)
    assert gone,'C05 resource absence is unverified'
