"""R417 actual verified owner_action/OwnerHelper/Engine selector path.

Only engine lookup/platform and post-selector inventory/socket effects are seams.
No Podman, native target, network, live owner or credential is used.
"""
import builtins
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from test_manual_composition_r416 import runtime,INSTANCE


def wire(tmp_path,monkeypatch):
    module,obj=runtime(tmp_path)
    obj.args.state_dir=str(obj.state)
    obj.payload=Path(__file__).resolve().parents[3]
    source=obj.payload/'packages/deploy/owner_helper.py'
    obj.installation={'files':{'packages/deploy/owner_helper.py':hashlib.sha256(source.read_bytes()).hexdigest()}}
    marker=obj.state/'runtime-owner.json';marker.write_text(json.dumps({'schema':'cortex.runtime-owner.v1','project':obj.args.project}));marker.chmod(0o600)
    calls=[]
    def forbidden(*a,**k):pytest.fail('selector test attempted engine execution')
    def target(self,engine,volume):
        calls.append((engine.identity.copy(),engine.prefix.copy(),volume))
        assert engine.prefix==['/PUBLIC/fixture/podman','--log-level=error','--remote=false']
        return {'engine':engine.identity,'fingerprint':{},'volume':{'Name':volume}}
    def verified_exec(code,namespace):
        assert namespace['__file__']==str(source)
        builtins.exec(code,namespace)
        # Real loaded Engine/OwnerHelper definitions remain in place. Isolate
        # only native lookup/platform and target effects after selector checks.
        namespace['sys']=SimpleNamespace(platform='linux')
        namespace['shutil']=SimpleNamespace(which=lambda name:'/PUBLIC/fixture/podman')
        namespace['OwnerHelper']._target=target
        namespace['OwnerHelper']._invoke=lambda self,engine,target,request:{'protocol':'cortex.owner.v1','instance_id':INSTANCE,'result':{'instance_id':INSTANCE}}
    monkeypatch.setattr(module.subprocess,'Popen',forbidden)
    monkeypatch.setattr(module,'exec',verified_exec,raising=False)
    real=None
    return module,obj,real,calls


def test_documented_local_selector_traverses_real_owner_action_and_pair(tmp_path,monkeypatch):
    module,obj,real,calls=wire(tmp_path,monkeypatch)
    result=obj.owner_action('owner-pair',selector='local',volume='cortex_cortex-owner-control')
    assert result=={'paired':True,'instance_id':INSTANCE}
    assert len(calls)==1 and calls[0][0]['selector']=='local'
    assert json.loads((obj.state/'owner-binding.json').read_text())['engine']['selector']=='local'


def test_native_selector_refuses_before_journal_or_owner_dispatch(tmp_path,monkeypatch):
    module,obj,real,calls=wire(tmp_path,monkeypatch)
    obj.args.owner_engine='native'
    dispatched=[]
    original=obj.owner_action
    def spy(*a,**k):dispatched.append((a,k));return original(*a,**k)
    obj.owner_action=spy
    with pytest.raises((ValueError,RuntimeError)):obj.enroll_console()
    assert dispatched==[] and calls==[]
    assert not (obj.state/'console-enrollment-pending.json').exists()
    assert not list(obj.state.glob('console-enrollment-*'))
    assert not (obj.state/'owner-binding.json').exists()


@pytest.mark.parametrize('selector',['','LOCAL','connection:','connection:bad name',None])
def test_other_invalid_selectors_never_create_recovery_barrier(tmp_path,monkeypatch,selector):
    module,obj,real,calls=wire(tmp_path,monkeypatch);obj.args.owner_engine=selector
    with pytest.raises((ValueError,RuntimeError)):obj.enroll_console()
    assert not (obj.state/'console-enrollment-pending.json').exists()
    assert not calls
