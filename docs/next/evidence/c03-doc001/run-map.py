from pathlib import Path
import subprocess,json,hashlib,sys
wt=Path.cwd();root=wt.parents[1];out=root/'docs/plans/cortex-v2-v0.1.020/lane-A/evidence/c03-doc001';phase=sys.argv[1]
name='kaidera-test-schema-map-1';image='sha256:ce9a404c2c0138e747a43e6ea022d2f7e670ed868df35d627a663ba7fb940ea9';results=[];created=False

def run(cmd):
 r=subprocess.run(cmd,capture_output=True,text=True);results.append({'command':cmd,'exit_code':r.returncode,'stdout':r.stdout,'stderr':r.stderr});print(json.dumps({'command':cmd[:4],'exit_code':r.returncode,'output':(r.stdout+r.stderr)[-1600:]}),flush=True);return r

def checked(cmd):
 r=run(cmd);assert r.returncode==0;return r

try:
 checked(['podman','run','-d','--name',name,'--network','none','--label','owner=cox@helix','--label','slice=C03-DOC-001','--cpus','1','--memory','256m','--read-only','--user','10001:10001','--cap-drop=ALL','--security-opt=no-new-privileges','--tmpfs','/tmp:rw,size=134217728,mode=1777',image,'sleep','1800']);created=True
 checked(['podman','cp',str(wt/'next'),name+':/tmp/next'])
 checked(['podman','cp',str(wt/'tmp/legacy-schema.sql'),name+':/tmp/legacy.sql'])
 checked(['podman','exec',name,'python','-c',"from pathlib import Path;Path('/tmp/map').mkdir()"])
 for f in ('legacy-schema-disposition.json','legacy-schema-inventory.json'):
  checked(['podman','cp',str(wt/'docs/next/evidence/c03-source'/f),name+':/tmp/map/'+f])
 env=['podman','exec','--env','PYTHONDONTWRITEBYTECODE=1','--env','LEGACY_SCHEMA_SOURCE=/tmp/legacy.sql','--env','LEGACY_MAP_DIRECTORY=/tmp/map',name]
 artifact=run(env+['python','/tmp/next/scripts/check_legacy_map.py','/tmp/legacy.sql','/tmp/map/legacy-schema-disposition.json','/tmp/map/legacy-schema-inventory.json'])
 tests=run(env+['python','/tmp/next/scripts/test_receipts.py','/tmp/next/tests/artifact'])
 if phase=='red':
  assert artifact.returncode==tests.returncode==1 and 'public.decisions' in artifact.stdout and 'public.messages' in artifact.stdout
 else:
  assert artifact.returncode==tests.returncode==0
  checked(['podman','cp',str(wt/'tmp/mutate-map.py'),name+':/tmp/mutate-map.py'])
  checked(env+['python','/tmp/mutate-map.py'])
finally:
 if created:checked(['podman','rm','-f',name])
 assert subprocess.run(['podman','container','exists',name],capture_output=True).returncode==1
 receipt={'phase':phase,'source_tree':subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),'legacy_source_ref':'04a26d0bf40c07ed3082328d3cc42c5a72ca8411','legacy_source_sha256':hashlib.sha256((wt/'tmp/legacy-schema.sql').read_bytes()).hexdigest(),'map_sha256':hashlib.sha256((wt/'docs/next/evidence/c03-source/legacy-schema-disposition.json').read_bytes()).hexdigest(),'image_id':image,'limits':{'cpus':1,'memory':'256m','tmpfs':'128m'},'network':'none','bind_mounts':[],'ports':[],'results':results,'container_removed':True}
 (out/(phase+'.json')).write_text(json.dumps(receipt,indent=2)+'\n')
