"""Bounded host orchestration; the frozen C04 consumer tests execute in Podman."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import uuid

WT = Path.cwd()
OUT = Path(__file__).resolve().parent
OUT.mkdir(parents=True, exist_ok=True)
PHASE = sys.argv[1]
TARGET = OUT / (PHASE + '.json')
assert not TARGET.exists()
TREE = subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()
SOURCE = {str(p.relative_to(WT)):hashlib.sha256(p.read_bytes()).hexdigest()
          for p in (WT/'next').rglob('*') if p.is_file()}
CONTRACT_SHARED = PHASE.startswith('contract-shared')
NAME = 'kaidera-test-auth-receipts-1'
IMAGE = 'sha256:ce9a404c2c0138e747a43e6ea022d2f7e670ed868df35d627a663ba7fb940ea9'
NONCE = uuid.uuid4().hex
results = []
pending = False
removed = False
passed = False
CHECK = '''import json,sys,unittest
sys.path.insert(0,'/tmp/next/tests')
from test_receipts import AssertionResult,MARKER
tests=unittest.defaultTestLoader.discover('/tmp/next/tests/auth',pattern='test_auth_mutation_receipts.py')
result=unittest.TextTestRunner(verbosity=2,resultclass=AssertionResult).run(tests)
print(MARKER+json.dumps({'tests_run':result.testsRun,'failures':result.assertions,'errors':[{'id':t.id(),'traceback':tb} for t,tb in result.errors]}),flush=True)
raise SystemExit(0 if result.wasSuccessful() else 1)
'''
FILES = ['next/scripts/mutate_auth.py', 'next/tests/auth/test_auth_mutation_receipts.py',
         'next/tests/auth/test_authorization.py', 'next/tests/test_receipts.py',
         'next/src/cortex_core/auth.py', 'next/schema/auth/002-isolation.sql',
         'next/schema/manifest.json', 'next/contracts/auth-conformance.json']


def run(args):
    value = subprocess.run(args, capture_output=True, text=True, timeout=120)
    results.append({'command': args, 'exit_code': value.returncode,
                    'stdout': value.stdout, 'stderr': value.stderr})
    print(json.dumps({'command':args[:6], 'exit_code':value.returncode,
                      'output':(value.stdout+value.stderr)[-1600:]}), flush=True)
    return value


def checked(args):
    value = run(args)
    assert value.returncode == 0
    return value


def check():
    value = run(['podman','exec','--env','PYTHONPATH=/tmp/deps',
                 '--env','PYTHONDONTWRITEBYTECODE=1',NAME,'python','-c',CHECK])
    reports = [json.loads(line.removeprefix('CORTEX_TEST_RESULT='))
               for line in value.stdout.splitlines() if line.startswith('CORTEX_TEST_RESULT=')]
    assert len(reports)==1 and reports[0]['tests_run']==2
    return value, reports[0]


try:
    assert subprocess.run(['podman','container','exists',NAME],capture_output=True,timeout=30).returncode==1
    image = json.loads(checked(['podman','image','inspect',IMAGE]).stdout)[0]
    assert image['Architecture']=='arm64'
    pending = True
    checked(['podman','run','-d','--name',NAME,'--label','owner=cox@helix',
             '--label','kaidera.cox.lifecycle='+NONCE,'--network','none','--cpus','1',
             '--memory','256m','--read-only','--user','10001:10001','--cap-drop=ALL',
             '--security-opt=no-new-privileges','--tmpfs','/tmp:rw,size=268435456,mode=1777',
             IMAGE,'sleep','900'])
    checked(['podman','cp',str(WT/'next'),NAME+':/tmp/next'])
    wheels=WT.parents[1]/'tmp/cox-contract-wheels-20261010' if CONTRACT_SHARED else WT/'tmp/wheels'
    checked(['podman','cp',str(wheels),NAME+':/tmp/wheels'])
    checked(['podman','exec',NAME,'python','-m','pip','install','--no-index',
             '--find-links=/tmp/wheels','--no-cache-dir','--target=/tmp/deps',
             '-r','/tmp/next/requirements-test.txt' if CONTRACT_SHARED else '/tmp/next/requirements-db-test.txt'])
    if CONTRACT_SHARED:
        env=['podman','exec','--env','PYTHONPATH=/tmp/deps:/tmp/next/src',
             '--env','PYTHONDONTWRITEBYTECODE=1',NAME]
        for directory,count in [('contract',33),('receipt',20)]:
            value=checked(env+['python','/tmp/next/tests/test_receipts.py','/tmp/next/tests/'+directory])
            reports=[json.loads(line.split('=',1)[1]) for line in value.stdout.splitlines() if line.startswith('CORTEX_TEST_RESULT=')]
            assert len(reports)==1 and reports[0]['tests_run']==count
            assert not reports[0]['failures'] and not reports[0]['errors']
        checked(env+['python','/tmp/next/scripts/mutate_contracts.py'])
        checked(env+['python','/tmp/next/scripts/mutate_test_receipts.py'])
        value=checked(env+['python','-c',"from pathlib import Path;import hashlib,json;root=Path('/tmp/next');print(json.dumps({'next/'+str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file()}))"])
        restored = json.loads(value.stdout)
        assert all(restored.get(k)==v for k,v in SOURCE.items())
        extras = set(restored)-set(SOURCE)
        allowed = {'next/scripts/__pycache__/mutate_contracts.cpython-312.pyc',
                   'next/tests/__pycache__/test_receipts.cpython-312.pyc'}
        assert extras <= allowed, 'unexpected generated file in copied source tree'
        # Isolated receipt probes create these two caches with their own environment.
        # Keep the before-removal hashes in raw output; remove only known generated files.
        if extras:
            cleanup = "from pathlib import Path;names="+repr(sorted(extras))+";[(Path('/tmp')/name).unlink() for name in names]"
            checked(env+['python','-c',cleanup])
        value=checked(env+['python','-c',"from pathlib import Path;import hashlib,json;root=Path('/tmp/next');print(json.dumps({'next/'+str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file()}))"])
        assert json.loads(value.stdout)==SOURCE
    else:
        value, report = check()
    if PHASE=='consumer-missing-red':
        assert value.returncode==1 and not report['failures'] and len(report['errors'])==5
        assert all("has no attribute 'mutation_status'" in row['traceback'] for row in report['errors'])
    elif not CONTRACT_SHARED:
        assert value.returncode==0 and not report['failures'] and not report['errors']
        before = 'def mutation_status(result, expected):\n    return classify(result, expected)'
        after = "def mutation_status(result, expected):\n    return 'killed' if result.returncode else 'survived'"
        mutation = "from pathlib import Path;p=Path('/tmp/next/scripts/mutate_auth.py');s=p.read_text();before="+repr(before)+";after="+repr(after)+";assert s.count(before)==1;p.write_text(s.replace(before,after,1))"
        checked(['podman','exec',NAME,'python','-c',mutation])
        try:
            value, report = check()
            expected={'test_auth_mutation_receipts.AuthMutationReceipts.'+method for method in (
                'test_operational_and_fixture_failures_are_inconclusive',
                'test_expected_body_and_wrong_assertion_remain_distinct')}
            assert value.returncode==1 and not report['errors']
            assert {row['id'].split(' (')[0] for row in report['failures']}==expected
            assert all(row['phase']=='test' and row['is_assertion'] for row in report['failures'])
        finally:
            checked(['podman','cp',str(WT/'next/scripts/mutate_auth.py'),NAME+':/tmp/next/scripts/mutate_auth.py'])
        value, report = check()
        assert value.returncode==0 and not report['failures'] and not report['errors']
    passed = True
    assert subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()==TREE
finally:
    if pending:
        exists = run(['podman','container','exists',NAME])
        if exists.returncode==0:
            info = json.loads(checked(['podman','container','inspect',NAME]).stdout)[0]
            labels = info['Config']['Labels']
            if labels.get('owner')=='cox@helix' and labels.get('kaidera.cox.lifecycle')==NONCE:
                assert info['Name'].lstrip('/')==NAME and len(info['Id'])==64
                checked(['podman','rm','-f',info['Id']])
        removed=run(['podman','container','exists',NAME]).returncode==1
    TARGET.write_text(json.dumps({'phase':PHASE,'tree':TREE,
        'passed':passed,'container_removed':removed,'results':results,'image_id':IMAGE,
        'lifecycle':NONCE,'limits':{'cpus':1,'memory':'256m'},'published_ports':[], 'bind_mounts':[],
        'database_tests_executed':False,'missing_adapter_red_is_operational_not_mutant_kill':PHASE=='consumer-missing-red',
        'source_sha256':SOURCE,
        'wheel_sha256':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in wheels.iterdir() if p.suffix=='.whl'} if 'wheels' in globals() else {},
        'controller_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()},indent=2)+'\n')
    assert removed, 'owned fixture cleanup is incomplete or a foreign name remains'
