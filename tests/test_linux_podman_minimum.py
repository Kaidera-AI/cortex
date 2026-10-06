"""R197/R198 minimum and visible dated-denylist behaviour; no engine changes."""
import importlib.util
from pathlib import Path
import pytest

ROOT=Path(__file__).resolve().parents[1]
def load(name,monkeypatch):
    path=ROOT/'scripts/release'/name
    assert path.is_file(),'Cortex minimum policy component absent: '+name
    monkeypatch.syspath_prepend(str(path.parent));monkeypatch.syspath_prepend(str(ROOT/'scripts'))
    spec=importlib.util.spec_from_file_location('cx198_policy_'+name.replace('.','_'),path);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m

@pytest.mark.parametrize('client,server',[('6.0.2','6.0.2'),('6.1.3','6.1.3'),('7.0.0','7.1.0'),('6.0.12','6.1.0')])
def test_minimum_accepts_newer_stable_versions_without_a_family_ceiling(monkeypatch,client,server):
    module=load('podman_policy.py',monkeypatch)
    receipt=module.validate_versions(client+' '+server)
    assert receipt['client_version']==client and receipt['server_version']==server
    assert receipt['minimum_version']=='6.0.2'

@pytest.mark.parametrize('value',['6.0.1 6.0.2','6.0.2 6.0.1','5.9.9 7.0.0','6.1.0-rc1 6.1.0','nonsense','6.1.0 6.1.0 extra'])
def test_bad_or_below_minimum_versions_refuse(monkeypatch,value):
    module=load('podman_policy.py',monkeypatch)
    with pytest.raises(ValueError,match='cortex_podman_unsupported'):module.validate_versions(value)

def test_dated_denylist_names_the_observed_bad_version(monkeypatch):
    module=load('podman_policy.py',monkeypatch)
    policy={'minimum_version':'6.0.2','minimum_reason':'lifecycle qualified there','denylist':{'as_of':'2026-10-06','entries':[{'version':'6.1.99','date':'2026-10-06','reason':'synthetic regression fixture'}]}}
    with pytest.raises(ValueError,match='cortex_podman_denied.*6.1.99'):module.validate_versions('6.1.99 6.1.3',policy=policy)
    assert module.validate_versions('6.1.3 7.0.0',policy=policy)['server_version']=='7.0.0'

@pytest.mark.parametrize('entry',[{'version':'6.1.99','reason':'missing date'},{'version':'6.1.99','date':'tomorrow','reason':'bad date'},{'version':'6.1.99','date':'2026-10-06','reason':''}])
def test_untyped_or_undated_denial_policy_fails_closed(monkeypatch,entry):
    module=load('podman_policy.py',monkeypatch)
    policy={'minimum_version':'6.0.2','minimum_reason':'lifecycle qualified there','denylist':{'as_of':'2026-10-06','entries':[entry]}}
    with pytest.raises(ValueError):module.validate_versions('6.1.3 6.1.3',policy=policy)

@pytest.mark.parametrize('value,admit',[('6.1.3 7.0.0',True),('6.0.1 6.0.2',False)])
def test_real_product_preflight_consumes_minimum_not_a_dead_metadata_field(monkeypatch,value,admit):
    module=load('install_candidate.py',monkeypatch);engine=object.__new__(module.Podman)
    calls=[]
    def run(args,**kwargs):
        calls.append(args)
        if args[0]=='version':return value
        if 'Arch' in args[-1]:return 'arm64'
        if 'Rootless' in args[-1]:return 'true'
        pytest.fail('unexpected preflight read '+repr(args))
    engine.run=run
    if admit:engine.preflight()
    else:
        with pytest.raises(module.Refusal):engine.preflight()
        assert len(calls)==1
