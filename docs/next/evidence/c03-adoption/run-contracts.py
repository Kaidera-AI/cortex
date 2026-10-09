import subprocess, pathlib, json, sys, time, hashlib
wt=pathlib.Path.cwd()
root=wt.parents[1]
out=root/'docs/plans/cortex-v2-v0.1.020/lane-A/evidence/c03-adoption';out.mkdir(parents=True,exist_ok=True)
phase=sys.argv[1];assert not (out/(phase+'.json')).exists();name='kaidera-test-contract-1';image='sha256:ce9a404c2c0138e747a43e6ea022d2f7e670ed868df35d627a663ba7fb940ea9';results=[]
def run(cmd):
 r=subprocess.run(cmd,capture_output=True,text=True);results.append({'command':cmd,'exit_code':r.returncode,'stdout':r.stdout,'stderr':r.stderr});print(json.dumps({'command':cmd[:3],'exit_code':r.returncode,'output':(r.stdout+r.stderr)[-2400:]}),flush=True);return r
created=False
try:
 info=json.loads(subprocess.check_output(['podman','image','inspect',image],text=True))[0]
 assert info['Architecture']=='arm64',info['Architecture']
 r=run(['podman','run','-d','--name',name,'--label','owner=cox@helix','--label','slice=shared-receipts-move','--cpus','2','--memory','1g','--read-only','--user','10001:10001','--cap-drop=ALL','--security-opt=no-new-privileges','--tmpfs','/tmp:rw,size=268435456,mode=1777',image,'sleep','1800']);assert r.returncode==0;created=True
 assert run(['podman','cp',str(wt/'next'),name+':/tmp/next']).returncode==0
 assert run(['podman','exec',name,'python','-m','pip','install','--disable-pip-version-check','--no-cache-dir','--target','/tmp/deps','-r','/tmp/next/requirements-test.txt']).returncode==0
 assert run(['podman','network','disconnect','podman',name]).returncode==0
 test=run(['podman','exec','--env','PYTHONPATH=/tmp/deps:/tmp/next/src','--env','PYTHONDONTWRITEBYTECODE=1',name,'python','-m','unittest','discover','-s','/tmp/next/tests/contract','-v'])
 if phase=='receipt-red':assert test.returncode!=0 and "'killed' != 'inconclusive'" in test.stderr,'expected receipt RED'
 elif phase=='fixture-red':assert test.returncode!=0 and "'killed' != 'inconclusive'" in test.stderr,'expected fixture RED'
 elif phase=='review-red':assert test.returncode!=0 and 'ContractError not raised' in test.stderr,'expected review regression RED'
 elif phase=='timestamp-red':assert test.returncode!=0 and 'ContractError not raised' in test.stderr,'not expected timestamp regression RED'
 elif phase=='red':assert test.returncode!=0 and 'cortex_core.contracts' in test.stderr,'not expected missing-seam RED'
 else:
  assert test.returncode==0
  mutation=run(['podman','exec','--env','PYTHONPATH=/tmp/deps:/tmp/next/src','--env','PYTHONDONTWRITEBYTECODE=1',name,'python','/tmp/next/scripts/mutate_contracts.py']);assert mutation.returncode==0
  shared=run(['podman','exec','--env','PYTHONDONTWRITEBYTECODE=1',name,'python','/tmp/next/tests/test_receipts.py','/tmp/next/tests/receipt']);assert shared.returncode==0
  assert run(['podman','cp',str(wt/'tmp/mutate-receipts.py'),name+':/tmp/mutate-receipts.py']).returncode==0
  shared_mutation=run(['podman','exec','--env','PYTHONDONTWRITEBYTECODE=1',name,'python','/tmp/mutate-receipts.py']);assert shared_mutation.returncode==0
finally:
 if created:removed=run(['podman','rm','-f',name]);assert removed.returncode==0
 remaining=subprocess.run(['podman','container','exists',name],capture_output=True,text=True);assert remaining.returncode==1
 receipt={'phase':phase,'tree':subprocess.check_output(['git','-C',str(wt),'rev-parse','HEAD'],text=True).strip(),'image_id':image,'native_arch':'arm64','limits':{'cpus':2,'memory':'1g'},'source_sha256':{str(p.relative_to(wt)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted((wt/'next').rglob('*')) if p.is_file()},'ssd_mounts':False,'published_ports':[],'results':results,'container_removed':True};(out/(phase+'.json')).write_text(json.dumps(receipt,indent=2)+'\n')
 print(json.dumps({'receipt':str(out/(phase+'.json')),'removed':True}),flush=True)
