from replay_lifecycle import Lifecycle
from pathlib import Path
import subprocess,json,hashlib,sys,time
wt=Path.cwd();root=wt.parents[1];out=root/'docs/plans/cortex-v2-v0.1.020/lane-A/evidence/c03-adoption'
out.mkdir(parents=True,exist_ok=True)
phase=sys.argv[1];assert not (out/(phase+'.json')).exists();pod='kaidera-test-core-schema-1';db='kaidera-test-core-db-1';driver='kaidera-test-core-driver-1'
pg='sha256:db676a0ed906c00f55020fb8999e4fb30c598bf5c3b5c188630aef2812d3f11d';py='sha256:ce9a404c2c0138e747a43e6ea022d2f7e670ed868df35d627a663ba7fb940ea9';results=[];passed=False

def run(cmd):
 r=subprocess.run(cmd,text=True,capture_output=True,timeout=180);results.append({'command':cmd,'exit_code':r.returncode,'stdout':r.stdout,'stderr':r.stderr});print(json.dumps({'command':cmd[:4],'exit_code':r.returncode,'output':(r.stdout+r.stderr)[-3500:]}),flush=True);return r

def checked(cmd):
 r=run(cmd);assert r.returncode==0;return r

life=Lifecycle(root,run)
TREE=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()
SOURCE={str(p.relative_to(wt)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted((wt/'next').rglob('*')) if p.is_file()}
try:
 life.acquire()
 for image in (pg,py):
  info=json.loads(subprocess.check_output(['podman','image','inspect',image],text=True))[0];assert info['Architecture']=='arm64'
 life.create('pod',pod,['podman','pod','create','--name',pod,'--network','none','--cpus','2','--memory','1g',*life.labels(),'--label','slice=C03'])
 life.create('container',db,['podman','run','-d',*life.labels(),'--pod',pod,'--name',db,'--cpus','1','--memory','768m','--read-only','--user','70:70','--cap-drop=ALL','--security-opt=no-new-privileges','--image-volume=ignore','--tmpfs','/var/lib/postgresql:rw,size=536870912,mode=1777','--tmpfs','/var/run/postgresql:rw,size=16777216,mode=1777','--tmpfs','/tmp:rw,size=16777216,mode=1777','--env','PGDATA=/var/lib/postgresql/18/data','--env','POSTGRES_HOST_AUTH_METHOD=trust',pg,'-c','max_wal_size=64MB','-c','min_wal_size=32MB','-c','checkpoint_timeout=30s'])
 life.create('container',driver,['podman','run','-d',*life.labels(),'--pod',pod,'--name',driver,'--cpus','1','--memory','256m','--read-only','--user','10001:10001','--cap-drop=ALL','--security-opt=no-new-privileges','--tmpfs','/tmp:rw,size=268435456,mode=1777',py,'sleep','1800'])
 checked(['podman','cp',str(wt/'next'),driver+':/tmp/next'])
 checked(['podman','cp',str(wt/'tmp/wheels'),driver+':/tmp/wheels'])
 checked(['podman','cp',str(wt/'tmp/legacy-schema.sql'),driver+':/tmp/legacy.sql'])
 checked(['podman','cp',str(wt/'docs/next/evidence/c03-source'),driver+':/tmp/map'])
 checked(['podman','exec',driver,'python','-m','pip','install','--no-index','--find-links=/tmp/wheels','--no-cache-dir','--target=/tmp/deps','-r','/tmp/next/requirements-db-test.txt'])
 for _ in range(30):
  ready=run(['podman','exec',db,'pg_isready','-U','postgres'])
  if ready.returncode==0:break
  time.sleep(1)
 else:raise RuntimeError('disposable PG failed readiness')
 checked(['podman','exec',db,'psql','-U','postgres','-At','-c','SELECT version();'])
 env=['podman','exec','--env','PYTHONPATH=/tmp/deps:/tmp/next/src','--env','PYTHONDONTWRITEBYTECODE=1','--env','TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:5432/postgres',driver]
 test=run(env+['python','-m','unittest','discover','-s','/tmp/next/tests/schema','-v'])
 assert test.returncode==0
 helper='/tmp/next/scripts/test_receipts.py' if phase=='red' else '/tmp/next/tests/test_receipts.py'
 artifact_env=['podman','exec','--env','PYTHONPATH=/tmp/deps:/tmp/next/src','--env','PYTHONDONTWRITEBYTECODE=1','--env','LEGACY_SCHEMA_SOURCE=/tmp/legacy.sql','--env','LEGACY_MAP_DIRECTORY=/tmp/map',driver]
 original_caller=None
 if phase=='location-fault-red':
  seed="from pathlib import Path;p=Path('/tmp/next/scripts/mutate_schema.py');s=p.read_text();assert s.count('str(NEXT / \"tests\")')==1;p.with_suffix('.original').write_text(s);p.write_text(s.replace('str(NEXT / \"tests\")','str(NEXT / \"scripts\")',1))"
  checked(env+['python','-c',seed]);original_caller=True
 artifact=run(artifact_env+['python',helper,'/tmp/next/tests/artifact'])
 if original_caller:
  restore="from pathlib import Path;p=Path('/tmp/next/scripts/mutate_schema.py');q=p.with_suffix('.original');p.write_bytes(q.read_bytes());q.unlink()"
  checked(env+['python','-c',restore])
 marker='CORTEX_TEST_RESULT='
 reports=[json.loads(line[len(marker):]) for line in artifact.stdout.splitlines() if line.startswith(marker)]
 assert len(reports)==1;value=reports[0]
 assert value['tests_run']==8 and not value['errors']
 if phase=='location-fault-red':
  assert artifact.returncode==1 and {r['id'] for r in value['failures']}=={'test_c03_receipts.C03ReceiptLocation.test_c03_caller_imports_shared_helper'}
  assert all(r['phase']=='test' and r['is_assertion'] is True for r in value['failures'])
 elif phase=='red':
  assert artifact.returncode==1
  assert {r['id'] for r in value['failures']}=={'test_c03_receipts.C03ReceiptLocation.test_c03_caller_imports_shared_helper','test_c03_receipts.C03ReceiptLocation.test_no_private_receipt_copy_remains'}
  assert all(r['phase']=='test' and r['is_assertion'] is True for r in value['failures'])
 else:
  assert artifact.returncode==0 and not value['failures']
  checked(env+['python','/tmp/next/scripts/mutate_schema.py'])
  checked(artifact_env+['python','/tmp/next/scripts/check_legacy_map.py','/tmp/legacy.sql','/tmp/map/legacy-schema-disposition.json','/tmp/map/legacy-schema-inventory.json'])
  checked(['podman','cp',str(wt/'tmp/mutate-map.py'),driver+':/tmp/mutate-map.py'])
  checked(artifact_env+['python','/tmp/mutate-map.py'])

 passed=True
finally:
 cleanup_error=life.finish()
 print('REPLAY_LIFECYCLE_RESULT='+json.dumps(life.receipt()),flush=True)
 remaining=subprocess.run(['podman','pod','exists',pod],capture_output=True,text=True,timeout=30)
 if cleanup_error:raise cleanup_error
 assert remaining.returncode==1
 for name in (db,driver):assert subprocess.run(['podman','container','exists',name],capture_output=True).returncode==1
 receipt={'phase':phase,'tree':TREE,'passed':passed,'cleanup':life.receipt(),'source_before_sha256':SOURCE,'images':[pg,py],'native_arch':'arm64','network':'none; shared loopback only','published_ports':[],'bind_mounts':[],'limits':{'pod_cpus':2,'pod_memory':'1g','pg_memory':'768m','driver_memory':'256m'},'wheel_hashes':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (wt/'tmp/wheels').iterdir()},'results':results,'stack_removed':True,'source_sha256':{str(p.relative_to(wt)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted((wt/'next').rglob('*')) if p.is_file()},'legacy_source_sha256':hashlib.sha256((wt/'tmp/legacy-schema.sql').read_bytes()).hexdigest()}
 assert subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()==TREE
 assert receipt['source_sha256']==SOURCE
 (out/(phase+'.json')).write_text(json.dumps(receipt,indent=2)+'\n');print(json.dumps({'receipt':str(out/(phase+'.json')),'removed':True}),flush=True)
