"""CI-only owned-start diagnosis with public controlled child outputs."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
from types import SimpleNamespace
import pytest
import yaml
ROOT = Path(__file__).resolve().parents[1]

def load(monkeypatch):
    path = ROOT/'scripts/release/ci_start_diagnostics.py'
    assert path.is_file(), 'owned CI start diagnostic capture missing'
    monkeypatch.syspath_prepend(str(path.parent));monkeypatch.syspath_prepend(str(ROOT/'scripts'))
    spec=importlib.util.spec_from_file_location('diagnosis_r428',path);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m

def fixture(tmp_path):
    config=tmp_path/'containers.conf'
    paths={'runtime_path':str(tmp_path/'opt/crun/bin/crun'),'conmon_path':str(tmp_path/'opt/conmon/bin/conmon'),'helper_paths':[str(tmp_path/'opt/podman/libexec/podman')]}
    config.write_text('[engine]\nhelper_binaries_dir = '+json.dumps(paths['helper_paths'])+'\n');config.chmod(0o600)
    runtime=tmp_path/'runtime-preflight.json';runtime.write_text(json.dumps({'schema':'cortex.native-runtime-preflight.v1','status':'PASS','runtime':{'name':'crun','path':paths['runtime_path'],'version':'PUBLIC'},'conmon':{'path':paths['conmon_path'],'version':'PUBLIC'},'config_sha256':hashlib.sha256(config.read_bytes()).hexdigest()}));runtime.chmod(0o600)
    record={'installation':'10000000-0000-4000-8000-000000000001','source_sha':'1'*40,'version':'0.2.001-test.20261008.1'}
    calls=[]
    class Engine:
        architecture='amd64';prefix=['podman','--remote=false']
        def run(self,args,**kwargs):calls.append((args,kwargs));return 'UNCHANGED'
    return Engine(),record,tmp_path/'diagnosis',runtime,config,paths,calls

CASES={'safe':b'Error: public network helper refused\n','credential':b'Error: password=PRIVATE_VALUE\n','url':b'Error: postgresql://PUBLIC_USER:PRIVATE_VALUE@db/x\n','late-credential':b'Error: '+b'x '*250+b'token=PRIVATE_VALUE\n','control':b'Error: \x00PRIVATE_VALUE\n','missing-error':b'private unclassified PRIVATE_VALUE\n','overlong-safe':b'Error: '+b'public '*100+b'\n','other-private-line':b'Error: public permission refused\npassword=PRIVATE_VALUE\n','oversized':b'x'*70000+b'Error: PRIVATE_VALUE\n','success':b'', 'timeout':b'Error: public start timeout\n'}
@pytest.mark.parametrize('case',list(CASES))
def test_capture_is_private_public_line_is_scanned_before_truncation(case,tmp_path,monkeypatch,capsys):
    m=load(monkeypatch);engine,record,root,runtime,config,paths,calls=fixture(tmp_path);child=[]
    def run(command,**kwargs):
        child.append(command);stream=kwargs['stderr'];stream.write(CASES[case]);stream.flush()
        if case=='timeout':raise subprocess.TimeoutExpired(command,90)
        return SimpleNamespace(returncode=0 if case=='success' else 1)
    monkeypatch.setattr(m.subprocess,'run',run)
    ctx=m.attach(engine,record,root,runtime_receipt=runtime,config=config)
    args=['start',ctx.container]
    if case=='success':assert engine.run(args)=='0'
    else:
        with pytest.raises(RuntimeError):engine.run(args)
    receipts=list(root.glob('start-diagnostic-*.json'));raw=list(root.glob('*.stderr'))
    assert len(receipts)==len(raw)==1
    assert stat.S_IMODE(root.stat().st_mode)==0o700 and stat.S_IMODE(raw[0].stat().st_mode)==0o600
    assert raw[0].read_bytes()==CASES[case]
    value=json.loads(receipts[0].read_text());assert value['verb']=='start' and value['container']==ctx.container
    assert value['runtime_path']==paths['runtime_path'] and value['conmon_path']==paths['conmon_path'] and value['helper_paths']==paths['helper_paths']
    assert value['exit']==(None if case=='timeout' else 0 if case=='success' else 1)
    if case in ['credential','url','late-credential','control','missing-error','oversized']:
        assert value['redacted'] is True and value['first_error'] is None
    elif case in ['safe','overlong-safe','other-private-line','timeout']:
        assert value['redacted'] is False and value['first_error'].startswith('Error:')
        assert len(value['first_error'])<=400
    assert 'PRIVATE_VALUE' not in receipts[0].read_text()
    assert capsys.readouterr().out=='' and capsys.readouterr().err==''
    assert child==[engine.prefix+args] and not calls

def test_other_commands_are_delegated_and_repeat_start_has_exclusive_files(tmp_path,monkeypatch):
    m=load(monkeypatch);engine,record,root,runtime,config,paths,calls=fixture(tmp_path)
    monkeypatch.setattr(m.subprocess,'run',lambda command,**kwargs:SimpleNamespace(returncode=0))
    ctx=m.attach(engine,record,root,runtime_receipt=runtime,config=config)
    for args in [['start','FOREIGN'],['stop',ctx.container],['logs',ctx.container]]:assert engine.run(args,read=True)=='UNCHANGED'
    assert len(calls)==3 and not list(root.glob('*.stderr'))
    assert engine.run(['start',ctx.container])=='0';assert engine.run(['start',ctx.container])=='0'
    assert len(list(root.glob('*.stderr')))==len(list(root.glob('start-diagnostic-*.json')))==2

@pytest.mark.parametrize('case',['success','failure'])
def test_cleanup_receipt_is_public_even_after_failed_start(case,tmp_path,monkeypatch):
    m=load(monkeypatch);engine,record,root,runtime,config,paths,calls=fixture(tmp_path);ctx=m.attach(engine,record,root,runtime_receipt=runtime,config=config)
    if case=='success':ctx.cleanup({'status':'erased','installation':record['installation'],'source_sha':record['source_sha'],'removed':[]})
    else:ctx.cleanup(error=RuntimeError('PRIVATE_VALUE postgresql://private'))
    value=json.loads((root/'cleanup-receipt.json').read_text());assert value['status']==('erased' if case=='success' else 'FAIL')
    assert value['installation']==record['installation']
    assert 'PRIVATE_VALUE' not in (root/'cleanup-receipt.json').read_text() and 'postgresql://' not in (root/'cleanup-receipt.json').read_text()

@pytest.mark.parametrize('case',['existing','linked','wrong-config-hash'])
def test_custody_refuses_existing_linked_or_unqualified_root(case,tmp_path,monkeypatch):
    m=load(monkeypatch);engine,record,root,runtime,config,paths,calls=fixture(tmp_path)
    if case=='existing':root.mkdir(mode=0o700)
    if case=='linked':root.symlink_to(tmp_path)
    if case=='wrong-config-hash':config.write_text('PUBLIC_DRIFT')
    original=engine.run
    with pytest.raises(RuntimeError):m.attach(engine,record,root,runtime_receipt=runtime,config=config)
    assert engine.run==original and not calls

def test_both_amd64_workflows_upload_only_public_diagnosis_and_cleanup_always():
    for filename,job in [('cortex-package-rehearsal.yml','rehearse_amd64'),('cortex-linux-candidate.yml','images')]:
        doc=yaml.load((ROOT/'.github/workflows'/filename).read_text(),Loader=yaml.BaseLoader);value=doc['jobs'][job]
        assert value['env']['CORTEX_CI_START_DIAGNOSTICS'].endswith('/cortex-ci-diagnosis')
        uploads=[s for s in value['steps'] if 'start-diagnostic-' in s.get('with',{}).get('path','')]
        assert len(uploads)==1 and 'always()' in uploads[0]['if']
        paths=uploads[0]['with']['path'];assert 'cleanup-receipt.json' in paths and '.stderr' not in paths
        checks=[s for s in value['steps'] if 'test -s' in s.get('run','') and 'cleanup-receipt.json' in s['run']]
        assert len(checks)==1 and 'always()' in checks[0]['if']
    source=(ROOT/'scripts/release/package_rehearsal.py').read_text()
    assert 'CORTEX_CI_START_DIAGNOSTICS' in source
    assert 'diagnostics.cleanup(' in source[source.index('    finally:'):]

@pytest.mark.parametrize('cleanup_case',['success','failure'])
def test_actual_rehearsal_finally_emits_cleanup_after_owned_start_failure(cleanup_case,tmp_path,monkeypatch):
    m=load(monkeypatch);engine,record,root,runtime,config,paths,calls=fixture(tmp_path)
    from test_cm2_build_catalog_rehearsal import load as product_load
    rehearsal=product_load('package_rehearsal.py',monkeypatch)
    monkeypatch.setattr(rehearsal,'NativeBuilder',lambda target:engine)
    monkeypatch.setattr(rehearsal,'ROOT',tmp_path)
    monkeypatch.setattr(rehearsal.Path,'home',classmethod(lambda cls:tmp_path))
    monkeypatch.setattr(rehearsal,'names',lambda record:[])
    monkeypatch.setattr(rehearsal,'private_root',lambda path,create:path.mkdir(parents=True,mode=0o700))
    monkeypatch.setattr(rehearsal,'write_record',lambda *args:None)
    monkeypatch.setattr(rehearsal,'prepare',lambda *args:None)
    monkeypatch.setattr(rehearsal,'provision',lambda *args,**kwargs:None)
    monkeypatch.setenv('GITHUB_ACTIONS','true');monkeypatch.setenv('RUNNER_TEMP',str(tmp_path));monkeypatch.setenv('CONTAINERS_CONF',str(config));monkeypatch.setenv('CORTEX_CI_START_DIAGNOSTICS',str(root))
    def child(command,**kwargs):
        kwargs['stderr'].write(b'Error: public network failure\n');return SimpleNamespace(returncode=1)
    monkeypatch.setattr(m.subprocess,'run',child)
    def start(engine,manifest,record,path):
        engine.run(['start',rehearsal.namespace(record)+'_db'])
        raise RuntimeError('public controlled start stop')
    monkeypatch.setattr(rehearsal,'start_stack',start)
    erased=[]
    def erase(engine,path,record,installation):
        erased.append(installation)
        if cleanup_case=='failure':raise RuntimeError('PRIVATE_VALUE')
        return {'status':'erased','installation':installation,'source_sha':record['source_sha'],'removed':[]}
    monkeypatch.setattr(rehearsal,'erase',erase)
    entries={role:{'config_id':'sha256:'+hashlib.sha256(role.encode()).hexdigest()} for role in rehearsal.ROLES}
    with pytest.raises(RuntimeError):rehearsal.rehearse(entries,record['source_sha'],record['version'],target='linux-x86_64')
    assert len(erased)==1
    assert (root/'cleanup-receipt.json').is_file()
    receipt=json.loads((root/'cleanup-receipt.json').read_text())
    assert receipt['status']==('erased' if cleanup_case=='success' else 'FAIL')
    assert 'PRIVATE_VALUE' not in (root/'cleanup-receipt.json').read_text()
