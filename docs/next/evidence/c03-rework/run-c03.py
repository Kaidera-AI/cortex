from pathlib import Path
import subprocess,json,hashlib,sys,time
wt=Path.cwd();root=wt.parents[1];out=root/'docs/plans/cortex-v2-v0.1.020/lane-A/evidence/c03-rework'
phase=sys.argv[1];pod='kaidera-test-core-schema-1';db='kaidera-test-core-db-1';driver='kaidera-test-core-driver-1'
pg='sha256:db676a0ed906c00f55020fb8999e4fb30c598bf5c3b5c188630aef2812d3f11d';py='sha256:ce9a404c2c0138e747a43e6ea022d2f7e670ed868df35d627a663ba7fb940ea9';results=[];created=False

def run(cmd):
 r=subprocess.run(cmd,text=True,capture_output=True);results.append({'command':cmd,'exit_code':r.returncode,'stdout':r.stdout,'stderr':r.stderr});print(json.dumps({'command':cmd[:4],'exit_code':r.returncode,'output':(r.stdout+r.stderr)[-3500:]}),flush=True);return r

def checked(cmd):
 r=run(cmd);assert r.returncode==0;return r

try:
 for image in (pg,py):
  info=json.loads(subprocess.check_output(['podman','image','inspect',image],text=True))[0];assert info['Architecture']=='arm64'
 checked(['podman','pod','create','--name',pod,'--network','none','--cpus','2','--memory','1g','--label','owner=cox@helix','--label','slice=C03']);created=True
 checked(['podman','run','-d','--pod',pod,'--name',db,'--cpus','1','--memory','768m','--read-only','--user','70:70','--cap-drop=ALL','--security-opt=no-new-privileges','--image-volume=ignore','--tmpfs','/var/lib/postgresql:rw,size=536870912,mode=1777','--tmpfs','/var/run/postgresql:rw,size=16777216,mode=1777','--tmpfs','/tmp:rw,size=16777216,mode=1777','--env','PGDATA=/var/lib/postgresql/18/data','--env','POSTGRES_HOST_AUTH_METHOD=trust',pg,'-c','max_wal_size=64MB','-c','min_wal_size=32MB','-c','checkpoint_timeout=30s'])
 checked(['podman','run','-d','--pod',pod,'--name',driver,'--cpus','1','--memory','256m','--read-only','--user','10001:10001','--cap-drop=ALL','--security-opt=no-new-privileges','--tmpfs','/tmp:rw,size=268435456,mode=1777',py,'sleep','1800'])
 checked(['podman','cp',str(wt/'next'),driver+':/tmp/next'])
 checked(['podman','cp',str(wt/'tmp/wheels'),driver+':/tmp/wheels'])
 checked(['podman','exec',driver,'python','-m','pip','install','--no-index','--find-links=/tmp/wheels','--no-cache-dir','--target=/tmp/deps','-r','/tmp/next/requirements-db-test.txt'])
 for _ in range(30):
  ready=run(['podman','exec',db,'pg_isready','-U','postgres'])
  if ready.returncode==0:break
  time.sleep(1)
 else:raise RuntimeError('disposable PG failed readiness')
 checked(['podman','exec',db,'psql','-U','postgres','-At','-c','SELECT version();'])
 env=['podman','exec','--env','PYTHONPATH=/tmp/deps:/tmp/next/src','--env','PYTHONDONTWRITEBYTECODE=1','--env','TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:5432/postgres',driver]
 test=run(env+['python','-m','unittest','discover','-s','/tmp/next/tests/schema','-v'])
 if phase=='causal-red':assert test.returncode!=0 and "'killed' != 'inconclusive'" in test.stderr,'expected causal regression RED'
 elif phase=='signal-red':assert test.returncode!=0 and 'SystemExit not raised' in test.stderr,'expected signal receipt regression RED'
 elif phase=='integrity-red':assert test.returncode!=0 and 'AssertionError' in test.stderr,'expected integrity regression RED'
 elif phase=='red':assert test.returncode!=0 and 'cortex_core.migrations' in test.stderr,'unexpected RED'
 else:
  assert test.returncode==0
  checked(env+['python','/tmp/next/scripts/mutate_schema.py'])
finally:
 if created:
  run(['podman','logs',db])
  run(['podman','exec',db,'df','-h','/var/lib/postgresql'])
  checked(['podman','pod','rm','-f',pod])
 remaining=subprocess.run(['podman','pod','exists',pod],capture_output=True,text=True);assert remaining.returncode==1
 for name in (db,driver):assert subprocess.run(['podman','container','exists',name],capture_output=True).returncode==1
 receipt={'phase':phase,'tree':subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),'images':[pg,py],'native_arch':'arm64','network':'none; shared loopback only','published_ports':[],'bind_mounts':[],'limits':{'pod_cpus':2,'pod_memory':'1g','pg_memory':'768m','driver_memory':'256m'},'wheel_hashes':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (wt/'tmp/wheels').iterdir()},'results':results,'stack_removed':True}
 (out/(phase+'.json')).write_text(json.dumps(receipt,indent=2)+'\n');print(json.dumps({'receipt':str(out/(phase+'.json')),'removed':True}),flush=True)
