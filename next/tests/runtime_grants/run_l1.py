"""Reuse RC1 rootless network-none PG fixture; owned resources only, no build."""
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

NEXT=Path(__file__).resolve().parents[2]
PG='docker.io/pgvector/pgvector@sha256:42e7f6b4e1eceb02ff14e3e6bc6108bbe259abbe83879dc1845d0da1ddeb555d'

def run(args,*,check=True,timeout=120):
    result=subprocess.run(args,capture_output=True,text=True,timeout=timeout)
    if check and result.returncode:raise RuntimeError(f'owned command failed {args[:3]}: {result.stderr[-1500:]}')
    return result

def main():
    interpreter,receipt_path=sys.argv[1:3]
    if os.uname().machine!='x86_64' or Path.home()!=Path('/home/labadmin'):raise RuntimeError('not admitted L1 host')
    mem={r.split(':')[0]:int(r.split()[1]) for r in Path('/proc/meminfo').read_text().splitlines() if r.startswith(('MemTotal:','MemAvailable:'))}
    if mem['MemAvailable']/mem['MemTotal']<.35:raise RuntimeError('memory preflight below35%')
    info=json.loads(run(['podman','info','--format','json']).stdout)
    if info['host']['security']['rootless'] is not True:raise RuntimeError('Podman not rootless')
    nonce=str(time.time_ns());token='nemo-pgo-'+nonce;label='cortex.test='+token
    pod,db,volume,database=[f'kaidera-test-grant-{kind}-{nonce}' for kind in ('pod','pg','data','db')]
    control=Path(receipt_path).parent/('control-'+nonce);control.mkdir()
    result={'started_utc':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),'image_ref':PG,
        'network':'none','published_ports':[],'pod':pod,'container':db,'volume':volume,
        'database':database,'run_id':token,'meminfo_kib':mem,'control':[],
        'cleanup':'NOT_RUN','exception':None}
    created_pod=False;created_volume=False;child=None
    try:
        image=json.loads(run(['podman','image','inspect',PG]).stdout)[0]
        result['image']={k:image.get(k) for k in ('Id','Digest','Architecture','Os','RepoDigests')}
        run(['podman','pod','create','--name',pod,'--network','none','--label',label]);created_pod=True
        run(['podman','volume','create','--label',label,volume]);created_volume=True
        run(['podman','run','-d','--pull=never','--pod',pod,'--name',db,'--label',label,
             '--cpus','1','--memory','768m','--env','POSTGRES_USER=postgres','--env','POSTGRES_DB=postgres',
             '--env','POSTGRES_HOST_AUTH_METHOD=trust','--volume',volume+':/var/lib/postgresql',PG])
        for _ in range(150):
            if run(['podman','exec',db,'pg_isready','-h','127.0.0.1','-U','postgres','-d','postgres'],check=False).returncode==0:break
            time.sleep(.1)
        else:raise RuntimeError('owned PG TCP readiness failed')
        def psql(database_name,statement):
            return run(['podman','exec',db,'psql','-h','127.0.0.1','-U','postgres','-d',database_name,'-v','ON_ERROR_STOP=1','-Atc',statement]).stdout
        result['postgres_version']=psql('postgres','SHOW server_version').strip()
        result['pgvector_version']=psql('postgres',"SELECT default_version FROM pg_available_extensions WHERE name='vector'").strip()
        psql('postgres','CREATE DATABASE "'+database+'"')
        psql(database,"CREATE TABLE public.pgo_owned_fixture(run_id text PRIMARY KEY); INSERT INTO public.pgo_owned_fixture VALUES ('"+token+"')")
        infra=json.loads(run(['podman','pod','inspect',pod]).stdout)[0]['InfraContainerID']
        pid=json.loads(run(['podman','inspect',infra]).stdout)[0]['State']['Pid']
        env=dict(os.environ,PYTHONPATH=str(NEXT/'src'),PYTHONDONTWRITEBYTECODE='1',
            TEST_DATABASE_URL='postgresql://postgres@127.0.0.1:5432/'+database,
            PGO_OWNED_RUN_ID=token,PGO_CONTROL_DIR=str(control))
        args=['podman','unshare','nsenter','--target',str(pid),'--net',interpreter,'-u',str(NEXT/'tests/runtime_grants/worker.py')]+sys.argv[3:]
        child=subprocess.Popen(args,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,bufsize=1,env=env)
        q=queue.Queue();stdout=[];stderr=[]
        def copy(stream,target,channel):
            for line in stream:
                target.append(line)
                if channel=='out':q.put(line)
            if channel=='out':q.put(None)
        threads=[threading.Thread(target=copy,args=(child.stdout,stdout,'out'),daemon=True),
                 threading.Thread(target=copy,args=(child.stderr,stderr,'err'),daemon=True)]
        for thread in threads:thread.start()
        deadline=time.monotonic()+1200;dump='/tmp/'+token+'.dump'
        while True:
            line=q.get(timeout=max(.001,deadline-time.monotonic()))
            if line is None:break
            if line.startswith('CORTEX_CONTROL='):
                action=line.strip().split('=',1)[1]
                if action=='DUMP':
                    event=run(['podman','exec',db,'pg_dump','-h','127.0.0.1','-U','postgres','-Fc','--create','-f',dump,database],check=False)
                    size=run(['podman','exec',db,'stat','-c','%s',dump],check=False)
                    receipt={'action':action,'exit_code':event.returncode,'bytes':int(size.stdout.strip()) if size.returncode==0 else 0}
                elif action=='RESTORE':
                    # Only our explicitly named disposable DB; no cluster roles.
                    event=run(['podman','exec',db,'pg_restore','-h','127.0.0.1','-U','postgres','--clean','--if-exists','--create','-d','postgres',dump],check=False)
                    receipt={'action':action,'exit_code':event.returncode,'stderr':event.stderr}
                else:raise RuntimeError('unknown owned control')
                result['control'].append(receipt)
                target=control/(action+'.tmp');target.write_text(json.dumps(receipt));target.replace(control/(action+'.json'))
        child.wait(timeout=max(1,deadline-time.monotonic()))
        for thread in threads:thread.join(3)
        result.update(exit_code=child.returncode,stdout=''.join(stdout),stderr=''.join(stderr))
        # Supplemental upstream capability evidence, outside the105-node oracle.
        # Transaction-local ACL changes roll back when the expected error exits
        # psql; fixture operator never supplies a product function or grant.
        probe='BEGIN; REVOKE CONNECT ON DATABASE "'+database+'" FROM PUBLIC,"kaidera-runtime-core-verifier"; SET LOCAL ROLE "kaidera-runtime-core-verifier"; SELECT session_user,current_user,has_database_privilege(current_user,current_database(),\'CONNECT\'); SELECT pg_database_size(current_database()); ROLLBACK;'
        event=run(['podman','exec',db,'psql','-h','127.0.0.1','-U','postgres','-d',database,
            '-v','ON_ERROR_STOP=1','-v','VERBOSITY=verbose','-Atc',probe],check=False)
        result['supplemental_size_privilege_probe']={'exit_code':event.returncode,'stdout':event.stdout,'stderr':event.stderr,
            'authority':'fixture controller SET LOCAL ROLE only; not a service-session or production grant receipt',
            'oracle_nodes':0,'product_mutations':0}
    except BaseException as error:
        result['exception']=repr(error)
        if child and child.poll() is None:child.kill();child.wait(timeout=10)
        raise
    finally:
        if created_pod:
            owned=json.loads(run(['podman','pod','inspect',pod]).stdout)[0]
            if owned['Labels'].get('cortex.test')!=token:raise RuntimeError('pod ownership differs')
            run(['podman','pod','rm','-f',pod])
        if created_volume:
            owned=json.loads(run(['podman','volume','inspect',volume]).stdout)[0]
            if owned['Labels'].get('cortex.test')!=token:raise RuntimeError('volume ownership differs')
            run(['podman','volume','rm',volume])
        remain=run(['podman','ps','-a','--filter','label='+label,'--format','{{.Names}}']).stdout.strip()
        result['cleanup']='PASS' if not remain and run(['podman','pod','exists',pod],check=False).returncode==1 and run(['podman','volume','exists',volume],check=False).returncode==1 else 'FAILED'
        result['ended_utc']=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())
        Path(receipt_path).write_text(json.dumps(result,indent=2)+'\n')
        print(json.dumps({k:result.get(k) for k in ('started_utc','ended_utc','postgres_version','exit_code','cleanup','exception')}),flush=True)
    return 0 if result['cleanup']=='PASS' and result.get('exit_code') in (0,1) else 2

if __name__=='__main__':raise SystemExit(main())
