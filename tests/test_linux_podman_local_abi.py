"""R198-F001: local Podman has Client only; no fictional remote Server."""
import copy
import json
import subprocess
import pytest
from test_linux_builder_contract import load

def provider(version='6.1.3',revision=0):
    keg=version+('_'+str(revision) if revision else '')
    return {'formulae':[{'name':'podman','tap':'homebrew/core','versions':{'stable':version},
            'revision':revision,'linked_keg':keg,'installed':[{'version':keg,'poured_from_bottle':True}],
            'bottle':{'stable':{'files':{'x86_64_linux':{'sha256':'a'*64,'url':'https://ghcr.io/v2/homebrew/core/podman/blobs/sha256:'+'a'*64}}}}}]}

@pytest.mark.parametrize('version',['6.0.2','6.1.3','7.0.0'])
def test_local_abi_receipt_names_reported_client_and_same_native_engine(monkeypatch,version):
    m=load('podman_policy.py',monkeypatch)
    assert callable(getattr(m,'validate_local_version',None)), 'local ABI version policy absent'
    result=m.validate_local_version(version)
    assert result['client_version']==version and result['engine_version']==version
    assert result['transport']=='native-local-abi' and 'server_version' not in result
    assert result['minimum_version']=='6.0.2'

@pytest.mark.parametrize('value',['6.0.1','6.1.3-rc1','','6.1.3 6.1.3'])
def test_local_abi_minimum_still_refuses_unsupported_versions(monkeypatch,value):
    m=load('podman_policy.py',monkeypatch)
    assert callable(getattr(m,'validate_local_version',None)), 'local ABI version policy absent'
    with pytest.raises(ValueError):m.validate_local_version(value)

@pytest.mark.parametrize('revision',[0,1])
def test_current_linuxbrew_stock_bottle_binds_to_local_engine(monkeypatch,revision):
    m=load('podman_policy.py',monkeypatch)
    assert callable(getattr(m,'validate_linuxbrew_provider',None)), 'provider version binding absent'
    result=m.validate_linuxbrew_provider(provider(revision=revision),'6.1.3')
    assert result['version']=='6.1.3' and result['bottle_sha256']=='a'*64

@pytest.mark.parametrize('bad',['wrong-name','wrong-tap','not-installed','old-current','old-linked','wrong-keg','source-build','bad-checksum','wrong-bottle-url'])
def test_provider_drift_refuses_before_any_native_build(monkeypatch,bad):
    m=load('podman_policy.py',monkeypatch)
    assert callable(getattr(m,'validate_linuxbrew_provider',None)), 'provider version binding absent'
    value=provider();f=value['formulae'][0]
    if bad=='wrong-name':f['name']='different'
    if bad=='wrong-tap':f['tap']='untrusted/tap'
    if bad=='not-installed':f['installed']=[]
    if bad=='old-current':f['versions']['stable']='6.1.4'
    if bad=='old-linked':f['linked_keg']='6.0.2'
    if bad=='wrong-keg':f['installed'][0]['version']='6.0.2'
    if bad=='source-build':f['installed'][0]['poured_from_bottle']=False
    if bad=='bad-checksum':f['bottle']['stable']['files']['x86_64_linux']['sha256']='invalid'
    if bad=='wrong-bottle-url':f['bottle']['stable']['files']['x86_64_linux']['url']='https://untrusted.invalid/file'
    with pytest.raises(ValueError):m.validate_linuxbrew_provider(value,'6.1.3')

def test_native_rehearsal_uses_local_abi_and_forces_local_mode(monkeypatch):
    m=load('package_rehearsal.py',monkeypatch)
    monkeypatch.setenv('GITHUB_ACTIONS','true');monkeypatch.setattr(m.platform,'system',lambda:'Linux');monkeypatch.setattr(m.platform,'machine',lambda:'x86_64');monkeypatch.setattr(m.shutil,'which',lambda name:'/public-fixture/podman')
    calls=[]
    def run(self,args,**kwargs):
        calls.append(args)
        if args[0]=='version':
            if args[-1]!='{{.Client.Version}}':raise m.Refusal('upstream local ABI has no Server')
            return '6.1.3'
        return 'amd64' if 'Arch' in args[-1] else 'true'
    monkeypatch.setattr(m.NativeBuilder,'run',run)
    engine=m.NativeBuilder('linux-x86_64')
    assert engine.prefix==['/public-fixture/podman','--remote=false']
    assert engine.version_receipt['engine_version']=='6.1.3'
    assert not any('Server' in str(c) for c in calls)

class BeforeBuild(RuntimeError):pass

@pytest.mark.parametrize('bad_provider',[False,True])
def test_image_stage_queries_local_version_and_checks_provider_before_build(tmp_path,monkeypatch,bad_provider):
    m=load('build-candidate.py',monkeypatch)
    monkeypatch.setattr(m.platform,'system',lambda:'Linux');monkeypatch.setattr(m.platform,'machine',lambda:'x86_64');monkeypatch.setattr(m,'source_identity',lambda sha:None)
    value=provider();value['formulae'][0]['name']='wrong' if bad_provider else 'podman';calls=[]
    def run(args,*,read=False):
        calls.append(args)
        if args[0]=='brew':return json.dumps(value)
        op=args[2] if args[1]=='--remote=false' else args[1]
        if op=='version':
            if args[-1]!='{{.Client.Version}}':raise subprocess.CalledProcessError(125,args)
            return '6.1.3'
        assert args[:2]==['podman','--remote=false']
        if op=='build':raise BeforeBuild('first native image mutation intercepted')
        pytest.fail('unexpected command '+repr(args))
    monkeypatch.setattr(m,'run',run)
    with pytest.raises(ValueError if bad_provider else BeforeBuild):m.images(tmp_path/'out','a'*40,'fixture',target='linux-x86_64')
    if bad_provider:
        assert not (tmp_path/'out').exists() and not any('build' in c for c in calls)
