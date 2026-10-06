"""Closed workflow/prefix selection and exclusivity without real Github mutations."""
import importlib.util
from pathlib import Path
import pytest

ROOT=Path(__file__).resolve().parents[1];SHA='a'*40
MAC='cortex-candidate.yml';LINUX='cortex-linux-candidate.yml'
def load():
    spec=importlib.util.spec_from_file_location('cx198_dispatch',ROOT/'scripts/release/dispatch-once.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m
class Fake:
    def __init__(self,delayed=False):self.calls=[];self.delayed=delayed
    def list_runs(self,sha):
        self.calls.append(('list',sha))
        return [{'head_sha':sha}] if self.delayed and len(self.calls)>1 else []
    def create_ref(self,branch,sha):self.calls.append(('ref',branch))
    def dispatch(self,branch):self.calls.append(('dispatch',branch))

def test_known_target_has_one_fixed_workflow_prefix_pair():
    m=load();assert callable(getattr(m,'route',None)),'closed Linux route absent'
    assert m.route('macos-arm64')==(MAC,'ren-cx/package-build-admitted-')
    assert m.route('linux-x86_64')==(LINUX,'ren-cx/linux-package-build-admitted-')

@pytest.mark.parametrize('target',['linux-arm64','linux-amd64','other','',None])
def test_unknown_target_refuses_before_any_github_operation(target):
    m=load();assert callable(getattr(m,'route',None)),'closed Linux route absent';g=Fake()
    with pytest.raises(m.DispatchRefused):m.dispatch_once(SHA,'on: workflow_dispatch',g,lambda n:None,target=target)
    assert g.calls==[]

@pytest.mark.parametrize('delayed',[False,True])
def test_linux_push_route_creates_one_ref_and_never_manual_dispatches(delayed):
    m=load();assert callable(getattr(m,'route',None)),'closed Linux route absent';g=Fake(delayed)
    text='on:\n  push:\n    branches: [ren-cx/linux-package-build-admitted-*]\n  workflow_dispatch:\n'
    with pytest.raises(m.DispatchRefused):m.dispatch_once(SHA,text,g,lambda n:None,target='linux-x86_64')
    assert ('ref','ren-cx/linux-package-build-admitted-'+SHA) in g.calls
    assert len([c for c in g.calls if c[0]=='ref'])==1 and not any(c[0]=='dispatch' for c in g.calls)

def test_linux_manual_route_submits_exactly_once_when_push_cannot_trigger():
    m=load();assert callable(getattr(m,'route',None)),'closed Linux route absent';g=Fake()
    result=m.dispatch_once(SHA,'on: workflow_dispatch',g,lambda n:None,target='linux-x86_64')
    assert g.calls[-1]==('dispatch','ren-cx/linux-package-build-admitted-'+SHA)
    assert sum(c[0]=='dispatch' for c in g.calls)==1 and result['workflow']==LINUX

@pytest.mark.parametrize('overlap',[False,True])
def test_unselected_workflow_cannot_trigger_selected_linux_ref(overlap):
    m=load();assert callable(getattr(m,'check_routes',None)),'workflow exclusivity check absent'
    texts={LINUX:'on:\n  push:\n    branches: [ren-cx/linux-package-build-admitted-*]\n',MAC:'on:\n  push:\n    branches: ['+('ren-cx/*' if overlap else 'ren-cx/package-build-admitted-*')+']\n'}
    if overlap:
        with pytest.raises(m.DispatchRefused,match='workflow_route_overlap'):m.check_routes(texts,'linux-x86_64',SHA)
    else:m.check_routes(texts,'linux-x86_64',SHA)

def test_github_provider_queries_only_selected_native_workflow(monkeypatch):
    m=load();assert callable(getattr(m,'route',None)),'closed Linux route absent';g=m.Github([],target='linux-x86_64');calls=[]
    def request(endpoint,body=None):calls.append((endpoint,body));return {'total_count':0,'workflow_runs':[]}
    monkeypatch.setattr(g,'request',request);assert g.list_runs(SHA)==[]
    assert calls[0][0].startswith('actions/workflows/'+LINUX+'/runs?')
