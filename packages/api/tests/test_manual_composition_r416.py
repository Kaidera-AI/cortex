"""r416 frozen behavioral composition, private enrollment and readiness controls."""
import hashlib
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
TOKEN = 'ctx1_' + '1'*32 + '.' + 'a'*43
INSTANCE = str(UUID(int=416))


def runtime(tmp_path):
    loader = importlib.machinery.SourceFileLoader('runtime_r416', str(ROOT/'packages/deploy/cortex-runtime'))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    obj = object.__new__(module.Runtime)
    obj.args = SimpleNamespace(project='cortex', credential_dir=str(tmp_path/'credentials'),
        credential_project='cortex-standalone-canary', owner_engine='local', schema_revision=None)
    obj.state = tmp_path/'state'; obj.state.mkdir(mode=0o700)
    obj.payload=ROOT
    obj.installation={'files':{'packages/deploy/owner_helper.py':hashlib.sha256((ROOT/'packages/deploy/owner_helper.py').read_bytes()).hexdigest()}}
    obj.port = 8501
    obj.identity = {'version':'0.1.003-manual.1', 'source_revision':'a'*40}
    return module, obj


def credential(obj):
    root = Path(obj.args.credential_dir); root.mkdir(mode=0o700)
    directory = root/obj.args.credential_project; directory.mkdir(mode=0o700)
    file = directory/'console.token'; file.write_text(TOKEN+'\n'); file.chmod(0o600)
    return file


def test_canonical_private_serviceauth_composition():
    cfg = yaml.safe_load((ROOT/'packages/deploy/docker-compose.yml').read_text())
    api = cfg['services']['cortex-api']
    assert api['environment']['CORTEX_SERVICE_AUTH_ENABLED'] == 'true'
    assert api['environment']['CORTEX_SERVICE_AUTH_OWNER_DIR'] == '/run/cortex-owner'
    assert api['read_only'] is True
    assert 'cortex-owner-control:/run/cortex-owner' in api['volumes']
    assert cfg['volumes']['cortex-owner-control']['labels']['org.opencontainers.image.source'] == 'https://github.com/Kaidera-AI/cortex'
    holders = {name for name, item in cfg['services'].items() if any('cortex-owner-control:' in value for value in item.get('volumes', []))}
    assert holders == {'cortex-api','cortex-tls-init'}
    assert cfg['services']['cortex-tls-init']['environment']['CORTEX_OWNER_CONTROL_DIR'] == '/run/cortex-owner'


@pytest.mark.parametrize('unsafe', ['missing','mode','symlink','hardlink','malformed'])
def test_readiness_refuses_credential_before_transport(tmp_path, monkeypatch, unsafe):
    module,obj = runtime(tmp_path)
    if unsafe != 'missing':
        file = credential(obj)
        if unsafe == 'mode': file.chmod(0o644)
        elif unsafe == 'symlink':
            target=file.with_name('target'); file.rename(target); file.symlink_to(target)
        elif unsafe == 'hardlink': os.link(file,file.with_name('alias'))
        elif unsafe == 'malformed': file.write_text('not-a-token\n')
    calls=[]
    monkeypatch.setattr(module.urllib.request,'urlopen',lambda *a,**k: calls.append(a))
    with pytest.raises((ValueError,RuntimeError)):
        obj.authenticated_get('/health',timeout=10)
    assert calls == []


def test_readiness_bearer_exact_descriptor_identity_and_no_proxy_redirect(tmp_path, monkeypatch):
    module,obj = runtime(tmp_path); credential(obj)
    calls=[]
    class Response:
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def read(self,n=-1): return b'{"status":"healthy"}'
    class Opener:
        def open(self,request,timeout):
            calls.append((request,timeout)); return Response()
    handlers=[]
    monkeypatch.setattr(module.urllib.request,'build_opener',lambda *h: (handlers.extend(h) or Opener()))
    assert obj.authenticated_get('/health',timeout=10)=={'status':'healthy'}
    request,timeout=calls[0]
    assert request.full_url=='http://127.0.0.1:8501/health'
    assert dict((k.lower(),v) for k,v in request.header_items()) == {
        'authorization':'Bearer '+TOKEN,'x-project':obj.args.credential_project,'x-agent-name':'console'}
    assert any(isinstance(h,module.urllib.request.ProxyHandler) and h.proxies=={} for h in handlers)
    redirect = next(h for h in handlers if isinstance(h,module.urllib.request.HTTPRedirectHandler))
    with pytest.raises(Exception): redirect.redirect_request(request,None,302,'move',{},'https://elsewhere.invalid')


def fake_owner(obj, calls, fail=False):
    def action(action,**fields):
        calls.append(action)
        if action=='owner-pair':
            binding={'instance_id':INSTANCE}
            (obj.state/'owner-binding.json').write_text(json.dumps(binding)); (obj.state/'owner-binding.json').chmod(0o600)
            return {'paired':True,'instance_id':INSTANCE}
        request=json.loads(Path(fields['request_file']).read_text())
        calls.append(request)
        if fail: raise RuntimeError('outcome unknown')
        result={'raw_token': 'ctxs1_PUBLIC'} if request['operation']=='setup-grant' else {'raw_token':TOKEN}
        output=Path(fields['response_file']); output.write_text(json.dumps({'protocol':'cortex.owner.v1','instance_id':INSTANCE,'result':result})); output.chmod(0o600)
        return {'owner_request':'complete'}
    obj.owner_action=action


def test_explicit_enrollment_yields_exact_console_file_without_token_receipt(tmp_path):
    module,obj=runtime(tmp_path); calls=[]; fake_owner(obj,calls)
    result=obj.enroll_console()
    file=Path(obj.args.credential_dir)/obj.args.credential_project/'console.token'
    assert file.read_text()==TOKEN+'\n' and file.stat().st_mode&0o777==0o600
    assert file.parent.stat().st_mode&0o777==0o700
    assert result['credential_file']==str(file) and result['credential_agent']=='console'
    assert result['credential_project']==obj.args.credential_project and result['credential_dir']==obj.args.credential_dir
    assert TOKEN not in json.dumps(result)
    requests=[c for c in calls if isinstance(c,dict)]
    assert [r['operation'] for r in requests]==['setup-grant','consume-setup']
    assert requests[1]['identity']=={'project_key':obj.args.credential_project,'agent_name':'console'}
    assert requests[1]['purpose']=='bootstrap' and requests[1]['revoke_existing'] is False
    assert not list(obj.state.glob('console-enrollment-*')), 'successful staging must leave no setup/raw-token copies'


def test_existing_credential_never_replaced_or_owner_called(tmp_path):
    module,obj=runtime(tmp_path); file=credential(obj); calls=[]; fake_owner(obj,calls)
    with pytest.raises((ValueError,RuntimeError)): obj.enroll_console()
    assert not calls and file.read_text()==TOKEN+'\n'


def test_unknown_enrollment_never_retries(tmp_path):
    module,obj=runtime(tmp_path); calls=[]; fake_owner(obj,calls,fail=True)
    with pytest.raises((ValueError,RuntimeError)): obj.enroll_console()
    count=len(calls)
    with pytest.raises((ValueError,RuntimeError)): obj.enroll_console()
    assert len(calls)==count


def test_readiness_uses_credential_on_each_private_route(tmp_path,monkeypatch):
    module,obj=runtime(tmp_path); credential(obj)
    obj.container=lambda s:s; obj.verify_running_image=lambda *a:None
    obj.migration_status=lambda:{'schema_revision':'b'*64}
    record={'Config':{'Labels':{'org.opencontainers.image.source':'https://github.com/Kaidera-AI/cortex',
        'org.opencontainers.image.version':obj.identity['version'],'org.opencontainers.image.revision':obj.identity['source_revision']}},
        'State':{'Status':'running','Health':{'Status':'healthy'}},'Image':'sha256:'+ 'c'*64}
    monkeypatch.setattr(module.subprocess,'check_output',lambda *a,**k:json.dumps([record]))
    calls=[]
    def authenticated(route,timeout):
        calls.append(route)
        return {'status':'healthy','postgres':'connected','surface_version':'cortex-standalone-'+obj.identity['version'],
            'installation_id':INSTANCE,'rls_enforced':True}
    obj.authenticated_get=authenticated
    monkeypatch.setattr(module.urllib.request,'urlopen',lambda *a,**k:pytest.fail('private readiness attempted unauthenticated transport'))
    assert obj.check()['healthy'] is True
    assert calls==['/health','/search?q=standalone-canary&limit=1','/handoffs?status=pending']

@pytest.mark.parametrize('case',['fresh','private-existing','unsafe-existing','symlink'])
def test_finite_initializer_private_control_contract(tmp_path,case):
    import subprocess
    directory=tmp_path/'control'; directory.mkdir(mode=0o755 if case=='fresh' else 0o700)
    if case!='fresh': (directory/'sentinel').write_text('preserve')
    if case=='unsafe-existing': directory.chmod(0o755)
    if case=='symlink':
        destination=tmp_path/'real'; directory.rename(destination); directory.symlink_to(destination,target_is_directory=True)
    env=dict(os.environ,CORTEX_OWNER_CONTROL_DIR=str(directory),CORTEX_TLS_CLIENT_UID=str(os.getuid()),CORTEX_TLS_CLIENT_GID=str(os.getgid()))
    result=subprocess.run(['/bin/sh',str(ROOT/'packages/deploy/tls/init.sh'),'owner-control'],env=env,capture_output=True,text=True,timeout=10)
    if case in {'fresh','private-existing'}:
        assert result.returncode==0, result.stderr
        assert directory.stat().st_uid==os.getuid() and directory.stat().st_mode&0o777==0o700
    else: assert result.returncode!=0
    if case!='fresh': assert (directory/'sentinel').read_text()=='preserve'

@pytest.mark.asyncio
async def test_finite_migration_registers_canary_console_after_project(tmp_path,monkeypatch):
    runtime(tmp_path)
    spec=importlib.util.spec_from_file_location('migrate_r416',ROOT/'packages/deploy/migrate.py')
    module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    calls=[]
    class Conn:
        async def execute(self,sql,*args): calls.append((sql,args))
    async def result(*args,**kwargs): return {'applied_count':0,'results':[]}
    async def plan(*args,**kwargs): return {'migrations':[]}
    async def empty(*args,**kwargs): return {}
    monkeypatch.setattr(module,'adopt_baseline',empty); monkeypatch.setattr(module,'converge_roles',empty)
    monkeypatch.setattr(module,'schema_status',empty)
    monkeypatch.setattr(module.cortex,'apply_schema_migrations',result)
    monkeypatch.setattr(module.cortex,'schema_migration_plan',plan)
    await module.migrate_connection(Conn())
    project=next(i for i,(sql,_) in enumerate(calls) if 'INSERT INTO cortex_projects' in sql)
    role=next(i for i,(sql,_) in enumerate(calls) if 'INSERT INTO public.roles' in sql)
    agent=next(i for i,(sql,_) in enumerate(calls) if 'INSERT INTO public.agents' in sql)
    assert project < role < agent
    for index in (role,agent): assert 'cortex-standalone-canary' in calls[index][0]
    assert "'console'" in calls[agent][0] and 'keep_visible' in calls[agent][0]
