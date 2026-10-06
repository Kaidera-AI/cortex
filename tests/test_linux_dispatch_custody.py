"""Linux provider mutations bind both workflow files; all GitHub calls synthetic."""
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import pytest

SHA = 'a' * 40
MAC = 'cortex-candidate.yml'
LINUX = 'cortex-linux-candidate.yml'

@pytest.mark.parametrize('stage', ['unchanged', 'pre-list', 'wait', 'post-list', 'overlap', 'missing'])
def test_linux_cli_binds_unselected_workflow_before_each_post(tmp_path, monkeypatch, stage):
    source = Path(__file__).resolve().parents[1] / 'scripts/release/dispatch-once.py'
    spec = importlib.util.spec_from_file_location('cx198_custody', source)
    m = importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
    directory=tmp_path/'.github/workflows';directory.mkdir(parents=True)
    (directory/LINUX).write_text('on: workflow_dispatch\n')
    counterpart=directory/MAC
    if stage!='missing':counterpart.write_text('on:\n  push:\n    branches: ['+('ren-cx/*' if stage=='overlap' else 'ren-cx/package-build-admitted-*')+']\n')
    inbox=tmp_path/'inbox';inbox.write_text('**▶ DO NOW (r999):** Linux CI on '+SHA+'\n')
    review=tmp_path/'review';review.write_text(json.dumps({'verdict':'PASS','target_receipt':{'head_commit':SHA}}))
    evidence=tmp_path/'evidence';calls=[]
    def drift():counterpart.write_bytes(counterpart.read_bytes().replace(b'\n',b'\r\n'))
    def git(args,**kwargs):
        assert args[:3]==['git','-C',str(tmp_path)]
        return SHA if args[3:]==['rev-parse','HEAD'] else ''
    def provider(args,**kwargs):
        endpoint=args[2];calls.append(endpoint)
        if '/runs?' in endpoint:
            assert '/'+LINUX+'/runs?' in endpoint
            count=sum('/runs?' in c for c in calls)
            if (stage=='pre-list' and count==1) or (stage=='post-list' and count==2):drift()
            value={'total_count':0,'workflow_runs':[]}
        elif endpoint.endswith('/git/refs'):
            value={'ref':'refs/heads/ren-cx/linux-package-build-admitted-'+SHA,'object':{'sha':SHA}}
        else:
            assert endpoint.endswith('/'+LINUX+'/dispatches');value=None
        return SimpleNamespace(returncode=0,stdout=json.dumps(value) if value else '')
    def wait(seconds):
        assert seconds==30
        if stage=='wait':drift()
    m.ROOT=tmp_path;monkeypatch.setattr(m.subprocess,'check_output',git);monkeypatch.setattr(m.subprocess,'run',provider);monkeypatch.setattr(m.time,'sleep',wait)
    monkeypatch.setattr(sys,'argv',['dispatch-once.py','--target','linux-x86_64','--source-sha',SHA,'--inbox',str(inbox),'--review-receipt',str(review),'--evidence',str(evidence)])
    code=m.main();record=json.loads(evidence.read_text())
    if stage=='unchanged':
        assert code==0 and record['workflow']==LINUX
        assert sum(c.endswith('/dispatches') for c in calls)==1
    else:
        assert code==2 and not any(c.endswith('/dispatches') for c in calls)
        assert sum(c.endswith('/git/refs') for c in calls)==(stage in ['wait','post-list'])
        assert record['reason']==('workflow_route_overlap' if stage=='overlap' else 'operator_input_or_provider_failed' if stage=='missing' else 'source_or_workflow_changed')
