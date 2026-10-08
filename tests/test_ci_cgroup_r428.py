"""Host-capability receipt and ephemeral delegation contract, Kai03:22."""
import importlib.util,json
from pathlib import Path
import pytest,yaml
ROOT=Path(__file__).resolve().parents[1]
def load(monkeypatch):
 p=ROOT/'scripts/release/linux_ci_cgroup.py';assert p.is_file(),'cgroup preflight missing'
 monkeypatch.syspath_prepend(str(p.parent));monkeypatch.syspath_prepend(str(ROOT/'scripts'));monkeypatch.syspath_prepend(str(ROOT/'src'))
 s=importlib.util.spec_from_file_location('ci_cgroup_r428',p);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m

def fixture(tmp_path,controllers='cpu cpuset io memory pids'):
 root=tmp_path/'cgroup';current=root/'job';manager=root/'user.slice/user-1001.slice/user@1001.service'
 for p in [root,current,manager,manager.parent]:p.mkdir(parents=True,exist_ok=True);(p/'cgroup.controllers').write_text(controllers);(p/'cgroup.subtree_control').write_text(controllers)
 proc=tmp_path/'proc';proc.write_text('0::/job\n');return root,proc,'/user.slice/user-1001.slice/user@1001.service'

@pytest.mark.parametrize('missing',['cpu','memory'])
def test_missing_required_controller_refuses_before_start(missing,tmp_path,monkeypatch):
 m=load(monkeypatch);root,proc,manager=fixture(tmp_path,' '.join(x for x in ['cpu','cpuset','io','memory','pids'] if x!=missing))
 r=m.observe(source_root=ROOT,cgroup_root=root,proc_cgroup=proc,manager_path=manager)
 assert r['status']=='FAIL' and r['category']=='cgroup_controller_not_delegated:'+missing
 with pytest.raises(RuntimeError,match='cgroup_controller_not_delegated:'+missing):m.require(r)

def test_actual_observations_and_requirements_come_from_spec(tmp_path,monkeypatch):
 m=load(monkeypatch);root,proc,manager=fixture(tmp_path);r=m.observe(source_root=ROOT,cgroup_root=root,proc_cgroup=proc,manager_path=manager)
 assert r['status']=='PASS' and r['current']['path']=='/job' and r['current']['controllers']==['cpu','cpuset','io','memory','pids']
 assert r['manager']['path']==manager and r['current']['parent_subtree_control']==['cpu','cpuset','io','memory','pids']
 from install_candidate import ROLES,container_args
 record={'installation':'10000000-0000-4000-8000-000000000001'}
 expected=[{'service':role,'flags':[flag for flag in container_args(role,record) if flag in ['--cpus','--memory','--cpuset-cpus','--blkio-weight']]} for role in (*ROLES,'migrate')]
 assert r['specification']['run_services']==expected
 assert r['specification']['compose_services'] and set(r['required'])=={'cpu','memory'}
 m.require(r)

@pytest.mark.parametrize('kind',['traversal','multiple','linked','invalid','missing'])
def test_unsafe_observation_fails_closed(kind,tmp_path,monkeypatch):
 m=load(monkeypatch);root,proc,manager=fixture(tmp_path)
 if kind=='traversal':proc.write_text('0::/../private\n')
 if kind=='multiple':proc.write_text('0::/job\n0::/other\n')
 if kind=='linked':(root/'job/cgroup.controllers').unlink();(root/'job/cgroup.controllers').symlink_to(root/'cgroup.controllers')
 if kind=='invalid':(root/'job/cgroup.controllers').write_text('cpu PRIVATE_VALUE://bad')
 if kind=='missing':(root/'job/cgroup.controllers').unlink()
 r=m.observe(source_root=ROOT,cgroup_root=root,proc_cgroup=proc,manager_path=manager)
 assert r['status']=='FAIL' and 'PRIVATE_VALUE' not in json.dumps(r)
 with pytest.raises(RuntimeError):m.require(r)

def test_workflow_native_red_then_ephemeral_remedy_and_scope():
 doc=yaml.load((ROOT/'.github/workflows/cortex-package-rehearsal.yml').read_text(),Loader=yaml.BaseLoader)
 for job in ['rehearse','rehearse_amd64']:
  j=doc['jobs'][job];assert 'CORTEX_CI_CGROUP_RECEIPT' in j['env']
  assert any('cgroup-' in s.get('with',{}).get('path','') and 'always()' in s.get('if','') for s in j['steps'])
 steps=doc['jobs']['rehearse_amd64']['steps'];runs=[s.get('run','') for s in steps]
 red=next(i for i,x in enumerate(runs) if '--expect-missing cpu' in x)
 remedy=next(i for i,x in enumerate(runs) if 'enable-linger' in x)
 build=next(i for i,x in enumerate(runs) if 'build-candidate.py images' in x)
 assert red<remedy<build
 assert 'Delegate=cpu cpuset io memory pids' in runs[remedy] and 'daemon-reload' in runs[remedy]
 assert 'systemd-run --user --scope -p Delegate=yes' in runs[build]
 assert any('delegate.conf' in x and 'rm ' in x and 'always()' in steps[i].get('if','') for i,x in enumerate(runs))
 source=(ROOT/'scripts/release/package_rehearsal.py').read_text();assert 'CORTEX_CI_CGROUP_RECEIPT' in source and source.index('cgroup_require(')<source.index('        start_stack(')

@pytest.mark.parametrize('flag,controller',[('--cpuset-cpus','cpuset'),('--blkio-weight','io')])
def test_added_limit_requires_actual_delegation(flag,controller,tmp_path,monkeypatch):
 m=load(monkeypatch);original=m.container_args
 monkeypatch.setattr(m,'container_args',lambda role,record:original(role,record)+[flag,'1'])
 root,proc,manager=fixture(tmp_path,'cpu memory pids')
 r=m.observe(source_root=ROOT,cgroup_root=root,proc_cgroup=proc,manager_path=manager)
 assert controller in r['required'] and r['category']=='cgroup_controller_not_delegated:'+controller
 with pytest.raises(RuntimeError,match=controller):m.require(r)

def test_current_job_cannot_hide_missing_manager_cpu(tmp_path,monkeypatch):
 m=load(monkeypatch);root,proc,manager=fixture(tmp_path)
 (root/manager.lstrip('/')/'cgroup.controllers').write_text('memory pids')
 r=m.observe(source_root=ROOT,cgroup_root=root,proc_cgroup=proc,manager_path=manager)
 assert 'cpu' in r['current']['controllers'] and r['category']=='cgroup_controller_not_delegated:cpu'

def test_receipt_private_exclusive_and_symlink_refused(tmp_path,monkeypatch):
 import stat
 m=load(monkeypatch);p=tmp_path/'receipt.json';m.write_receipt(p,{'status':'FAIL','category':'cgroup_controller_not_delegated:cpu'})
 assert stat.S_IMODE(p.stat().st_mode)==0o600
 with pytest.raises(Exception):m.write_receipt(p,{'status':'PASS'})
 linked=tmp_path/'linked.json';linked.symlink_to(p)
 with pytest.raises(Exception):m.write_receipt(linked,{'status':'PASS'})
 assert json.loads(p.read_text())['status']=='FAIL'

def test_native_proc_self_uses_physical_current_pid_path(tmp_path,monkeypatch):
 import os
 m=load(monkeypatch);root,proc,manager=fixture(tmp_path);original=m._read;reads=[]
 def read(path):
  path=Path(path);reads.append(path)
  if str(path).startswith('/proc/'):return proc.read_text()
  return original(path)
 monkeypatch.setattr(m,'_read',read)
 r=m.observe(source_root=ROOT,cgroup_root=root,manager_path=manager)
 assert r['status']=='PASS' and Path('/proc/'+str(os.getpid())+'/cgroup') in reads
 assert Path('/proc/self/cgroup') not in reads
