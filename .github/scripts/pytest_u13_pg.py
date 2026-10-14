"""Reuse cached legacy CI images; isolated current-v2 PG, no pulls or baseline."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tarfile
import time

from pytest_pg import Fixture, PYTHON, POSTGRES
import podman_stage0

class U13Fixture(Fixture):
    def labels(self):
        return ['--label','cortex.pytest-pg='+self.run_id,'--label','cortex.worker=mike']

    def create_u13(self):
        self.admission()
        self.pod = self.prefix+'-u13-pod'
        self.command(['pod','create','--name',self.pod,'--infra=false','--share=',
                      '--memory=1g','--memory-swap=1g','--cpus=2',*self.labels()])
        self.coordinator = None
        self.coordinator = self.run('u13-coordinator',podman_stage0.IMAGE,
            ['python','-c','import signal,time;signal.signal(signal.SIGTERM,lambda *args:exit(0));time.sleep(900)'],
            user='65535:65535',memory='32m',cpus='0.05')
        self.volume('u13-data',999)
        data = self.volumes[-1]
        pg = self.run('u13-db',POSTGRES,
            ['postgres','-p','55432','-c','shared_buffers=16MB','-c','max_connections=24',
             '-c','listen_addresses=127.0.0.1'],user='999:999',memory='256m',cpus='0.5',
            mounts=[(data,'/var/lib/postgresql/data',False)],
            env={'POSTGRES_DB':'kaidera-test-upgrade-ci','POSTGRES_HOST_AUTH_METHOD':'trust',
                 'PGDATA':'/var/lib/postgresql/data/pgdata'},
            extra=['--tmpfs=/var/run/postgresql:rw,size=8m,mode=1777'])
        deadline=time.monotonic()+30
        while time.monotonic()<deadline:
            ready=self.command(['exec',pg,'pg_isready','-h','127.0.0.1','-p','55432','-U','postgres'],check=False)
            if ready.returncode==0: return pg
            time.sleep(.3)
        raise RuntimeError('U13 database readiness failed')

    def execute_u13(self, control):
        self.results=self.output/'worker'
        self.results.mkdir()
        self.command(['unshare','chown','10001:10001',str(self.results)])
        self.command(['unshare','chmod','0755',str(self.results)])
        self.create_u13()
        env={'PYTHONDONTWRITEBYTECODE':'1','PYTEST_DISABLE_PLUGIN_AUTOLOAD':'1',
             'PYTHONPATH':'/deps:/source/next/src','PSYCOPG_IMPL':'python',
             'TEST_DATABASE_URL':'postgresql://postgres@127.0.0.1:55432/kaidera-test-upgrade-ci'}
        driver=self.run('u13-driver',PYTHON,['sleep','900'],
            mounts=[(self.source,'/source',True),(self.site,'/deps',True),(self.results,'/results',False)],env=env)
        result=self.command(['exec',driver,'python','/source/.github/scripts/pytest_u13_worker.py',
            '--output','/results/outcomes.json','--control',control],timeout=600,check=False)
        (self.output/'worker.txt').write_text(result.stdout+result.stderr)
        print(result.stdout,result.stderr,flush=True)
        self.receipt['worker_exit']=result.returncode
        self.receipt['control']=control
        self.receipt['database']='isolated current-v2 kaidera-test-upgrade-ci, no legacy bootstrap, loopback-only/no host port'
        return result.returncode

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--site',type=Path,required=True)
    parser.add_argument('--control',choices=('normal','missing','skip','mutant'),default='normal')
    args=parser.parse_args()
    output=args.output.resolve()
    output.mkdir(parents=True)
    source=output.parent/(output.name+'-source')
    fixture=U13Fixture(output,args.site.resolve(),source)
    exit_code=1
    try:
        source.mkdir()
        archive=subprocess.check_output(['git','archive','HEAD'])
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar: tar.extractall(source,filter='data')
        fixture.receipt['source_sha']=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()
        fixture.receipt['worker_sha256']=hashlib.sha256((source/'.github/scripts/pytest_u13_worker.py').read_bytes()).hexdigest()
        (output/'fixture-hosts.conf').write_text('127.0.0.1 localhost cortex-pg\n::1 localhost\n')
        for image in (PYTHON,POSTGRES,podman_stage0.IMAGE):
            fixture.command(['image','exists',image])  # reuse only; never fetch an image
        exit_code=fixture.execute_u13(args.control)
        fixture.receipt['status']='PASS' if exit_code==0 else 'FAIL'
    except BaseException as error:
        exit_code=1
        fixture.receipt.update(status='FAIL',reason=type(error).__name__+': '+str(error))
        print(fixture.receipt['reason'],flush=True)
    finally:
        if not fixture.cleanup():
            fixture.receipt['status']='FAIL'
            exit_code=1
        fixture.command(['unshare','chown','-R','0:0',str(output)])
        (output/'provisioner.json').write_text(json.dumps(fixture.receipt,indent=2)+'\n')
        shutil.rmtree(source)
    return exit_code

if __name__=='__main__':
    signal.signal(signal.SIGTERM,lambda signum,frame:sys.exit('terminated'))
    raise SystemExit(main())
