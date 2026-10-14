"""L1-only, rootless, network-none disposable PostgreSQL source harness.

Invoke on Kai's admitted host with the copied public source directory and an
explicit venv. This does not control local Podman or any preexisting container.
"""
import hashlib
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
PG = 'docker.io/pgvector/pgvector@sha256:42e7f6b4e1eceb02ff14e3e6bc6108bbe259abbe83879dc1845d0da1ddeb555d'
ROLE = 'kaidera-test-rc1-probe'
SUITES = ('c11a', 'c11b', 'c11b2', 'auth', 'records', 'outbox', 'schema', 'module_consumer')

def run(args, check=True, timeout=300):
    r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if check and r.returncode: raise RuntimeError(f'public lab command failed: {args[:3]}: {r.stderr[-1000:]}')
    return r

def main():
    mode, interpreter, receipt_path = sys.argv[1:4]
    selectors = sys.argv[4:]
    regressions = selectors if mode == 'regression' else []
    if regressions and any(name not in SUITES for name in regressions): raise ValueError('unknown regression suite')
    if regressions: selectors = []
    if mode not in ('red', 'focus', 'green', 'donor', 'mutant', 'regression'): raise ValueError('unsupported mode')
    if os.uname().machine != 'x86_64' or Path.home() != Path('/home/labadmin'):
        raise RuntimeError('refuse outside the explicitly admitted L1 host account')
    token = uuid4().hex[:10]
    label = 'cox-rc1-' + token
    pod, db, volume = [f'kaidera-test-rc1-{kind}-{token}' for kind in ('pod', 'pg', 'data')]
    result = {'mode': mode, 'host': 'L1', 'machine': os.uname().machine, 'image_ref': PG, 'network': 'none', 'published_ports': [], 'pod': pod, 'container': db, 'volume': volume, 'label': label, 'source_sha256': hashlib.sha256((ROOT/'src/cortex_core/api/c11a.py').read_bytes()).hexdigest(), 'test_hashes': {str(f.relative_to(ROOT)): hashlib.sha256(f.read_bytes()).hexdigest() for f in sorted((ROOT/'tests/c11_availability').glob('test_*.py'))}, 'probe_role': ROLE, 'probe_role_authority': 'disposable cluster only; RC0 logical probe/no memberships/no tenant grants; production grant/resolver NOT_DELIVERED', 'selected_methods': selectors, 'suites': {}, 'control': [], 'cleanup': 'NOT_RUN'}
    exit_code = 1
    try:
        run(['podman', 'image', 'inspect', PG])
        run(['podman', 'pod', 'create', '--name', pod, '--network', 'none', '--label', 'cortex.test='+label])
        run(['podman', 'volume', 'create', '--label', 'cortex.test='+label, volume])
        run(['podman', 'run', '-d', '--pod', pod, '--name', db, '--label', 'cortex.test='+label, '--cpus', '1', '--memory', '768m', '--env', 'POSTGRES_USER=postgres', '--env', 'POSTGRES_DB=postgres', '--env', 'POSTGRES_HOST_AUTH_METHOD=trust', '--volume', volume+':/var/lib/postgresql', PG])
        for _ in range(100):
            if run(['podman', 'exec', db, 'pg_isready', '-h', '127.0.0.1', '-U', 'postgres', '-d', 'postgres'], check=False).returncode == 0: break
            time.sleep(0.1)
        else: raise RuntimeError('new owned PG failed readiness')
        result['image'] = json.loads(run(['podman', 'image', 'inspect', PG]).stdout)[0]
        result['postgres_version'] = run(['podman', 'exec', db, 'psql', '-h', '127.0.0.1', '-U', 'postgres', '-Atc', 'SHOW server_version']).stdout.strip()
        run(['podman', 'exec', db, 'psql', '-h', '127.0.0.1', '-U', 'postgres', '-v', 'ON_ERROR_STOP=1', '-c', f'CREATE ROLE "{ROLE}" LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS'])
        infra = json.loads(run(['podman', 'pod', 'inspect', pod]).stdout)[0]['InfraContainerID']
        pid = json.loads(run(['podman', 'inspect', infra]).stdout)[0]['State']['Pid']
        env = dict(os.environ, PYTHONPATH=str(ROOT/'src'), PYTHONDONTWRITEBYTECODE='1', TEST_DATABASE_URL='postgresql://postgres@127.0.0.1:5432/postgres', TEST_PROBE_DATABASE_URL=f'postgresql://{ROLE}@127.0.0.1:5432/postgres')
        prefix = ['podman', 'unshare', 'nsenter', '--target', str(pid), '--net', interpreter, '-u', str(ROOT/'tests/c11_availability/worker.py')]
        def suite(name):
            child = subprocess.Popen(prefix+[str(ROOT/'tests'/name)]+(selectors if name=='c11_availability' else []), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1, env=env)
            q = queue.Queue(); stdout=[]; stderr=[]
            def copy(stream, target, channel):
                for line in stream:
                    target.append(line)
                    if channel=='out': q.put(line)
                if channel=='out': q.put(None)
            a=threading.Thread(target=copy,args=(child.stdout,stdout,'out'),daemon=True); b=threading.Thread(target=copy,args=(child.stderr,stderr,'err'),daemon=True);a.start();b.start()
            deadline=time.monotonic()+600
            try:
                while True:
                    line=q.get(timeout=max(0.001,deadline-time.monotonic()))
                    if line is None:break
                    if line.startswith('CORTEX_CONTROL='):
                        action=line.strip().split('=',1)[1]
                        if action not in ('STOP','START'):raise RuntimeError('unknown owned control')
                        cmd=['podman','stop','--time','0',db] if action=='STOP' else ['podman','start',db]
                        event=run(cmd,check=False,timeout=30)
                        result['control'].append({'action':action,'exit_code':event.returncode,'container':db})
                        if event.returncode:raise RuntimeError('owned PG control failed')
                child.wait(timeout=max(1,deadline-time.monotonic()));a.join();b.join()
            except BaseException:
                child.kill();child.wait(timeout=10);raise
            out=''.join(stdout); skip=[line.split('=',1)[1] for line in out.splitlines() if line.startswith('CORTEX_SKIPS=')]; marker=[line.split('=',1)[1] for line in out.splitlines() if line.startswith('CORTEX_TEST_RESULT=')]
            return {'exit_code':child.returncode,'stdout':out,'stderr':''.join(stderr),'body_receipt':json.loads(marker[0]) if len(marker)==1 else None, 'skips':json.loads(skip[0]) if len(skip)==1 else None}
        if mode == 'regression':
            for name in regressions: result['suites'][name] = suite(name)
        else: result['suites']['c11_availability']=suite('c11_availability')
        if mode=='green' and result['suites']['c11_availability']['exit_code']==0:
            for name in SUITES:result['suites'][name]=suite(name)
        exit_code=0 if all(row['exit_code']==0 for row in result['suites'].values()) else 1
        return exit_code
    finally:
        run(['podman','pod','rm','-f',pod],check=False)
        run(['podman','volume','rm',volume],check=False)
        remaining=run(['podman','ps','-a','--filter','label=cortex.test='+label,'--format','{{.Names}}']).stdout.strip()
        pods=run(['podman','pod','exists',pod],check=False).returncode
        vol=run(['podman','volume','exists',volume],check=False).returncode
        result['cleanup']='PASS' if not remaining and pods==1 and vol==1 else 'FAILED'
        result['exit_code']=exit_code
        Path(receipt_path).write_text(json.dumps(result,indent=2)+'\n')
        print(json.dumps({'receipt':receipt_path,'mode':mode,'exit_code':exit_code,'cleanup':result['cleanup'],'suites':{name:{'exit_code':row['exit_code'],'tests_run':(row['body_receipt'] or {}).get('tests_run'),'failures':len((row['body_receipt'] or {}).get('failures',[])),'errors':len((row['body_receipt'] or {}).get('errors',[]))} for name,row in result['suites'].items()} }),flush=True)
        if result['cleanup']!='PASS':raise RuntimeError('owned resource cleanup failed')

if __name__=='__main__':raise SystemExit(main())
