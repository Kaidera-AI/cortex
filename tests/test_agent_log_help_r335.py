"""Current help text and environment goal behavior, with no profile/key access."""
import contextlib
import io
import os
from pathlib import Path
import shutil
import subprocess

import pytest
from cortex_v2.cli import agent_request as bridge
from test_agent_log_command_r335 import legacy,SUMMARY,FILES,ID
from test_member_client_integration import FixtureReader,member_profile

ROOT=Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('arguments',[[],['--help'],['-h']])
def test_native_log_help_matches_exact_current_command_without_profile(arguments,tmp_path,monkeypatch):
    directory=tmp_path/'old';directory.mkdir();shutil.copyfile(ROOT/'tests/fixtures/r335-cortex-log',directory/'cortex-log');(directory/'_cortex_api.sh').write_text('# PUBLIC help fixture; no API\n')
    actual=subprocess.run(['bash',str(directory/'cortex-log'),*arguments],capture_output=True,text=True)
    def forbidden(*a):pytest.fail('help loaded profile or credentials')
    monkeypatch.setattr(bridge,'load_member_profile',forbidden)
    out,err=io.StringIO(),io.StringIO()
    with contextlib.redirect_stdout(out),contextlib.redirect_stderr(err):
        try:code=bridge.main(['log',*arguments],stdout=out,stderr=err)
        except SystemExit as exit:code=exit.code
    assert code==actual.returncode==1 and out.getvalue()==actual.stdout and err.getvalue()==actual.stderr==''


def test_native_log_environment_goal_and_existing_prefix_are_exact(monkeypatch):
    from cortex_v2.cli import agent_log
    from cortex_v2.clients.transport import HttpResponse
    import json
    reader=FixtureReader();profile=member_profile('http://127.0.0.1:1',reader);profile.principal_label='worker'
    monkeypatch.setattr(bridge,'load_member_profile',lambda *a:profile);monkeypatch.setenv('CORTEX_PARENT_GOAL_ID','PUBLIC-goal');calls=[]
    def transport(method,url,**kwargs):calls.append(kwargs['json_body']);return HttpResponse(200,{},json.dumps({'id':ID,'logged':True,'verified':True}).encode())
    monkeypatch.setattr(agent_log,'http_request',transport)
    for summary in [SUMMARY,'[GOAL:PUBLIC-original] '+SUMMARY]:
        out,err=io.StringIO(),io.StringIO();code=bridge.main(['log','--no-confirm','worker','commit',summary],stdout=out,stderr=err)
        wanted=summary if summary.startswith('[GOAL:') else '[GOAL:PUBLIC-goal] '+summary
        assert code==0 and err.getvalue()=='' and out.getvalue()=='\033[32mLogged: [commit] worker — '+wanted+'\033[0m\n'
        assert calls[-1]['summary']==wanted and calls[-1]['metadata']=={'parent_goal_id':'PUBLIC-goal','goal_ancestry':['PUBLIC-goal']}
    assert reader.reads==2


@pytest.mark.parametrize('arguments',[[],['--help'],['-h']])
def test_release_log_help_reaches_native_usage_without_connection_profile(arguments,tmp_path):
    directory=tmp_path/'bin';directory.mkdir();shutil.copyfile(ROOT/'scripts/agent-shims/cortex-log',directory/'cortex-log')
    agent=directory/'cortex-agent';agent.write_text('#!/usr/bin/env python3\nimport json,sys\nprint(json.dumps(sys.argv[1:]))\n');agent.chmod(0o755)
    environment=dict(os.environ);environment.pop('CORTEX_CONNECTION_PROFILE',None)
    result=subprocess.run(['bash',str(directory/'cortex-log'),*arguments],env=environment,capture_output=True,text=True)
    assert result.returncode==0 and result.stderr=='' and result.stdout.strip()=='["log", "--help"]'
