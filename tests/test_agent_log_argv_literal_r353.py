"""Exact current argv boundary, literal flag-like file names and actual wire."""
import io
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest
from cortex_v2.cli import agent_request as bridge
from test_agent_log_command_r335 import wire,ROOT,ID,SUMMARY  # noqa: F401

CASES=[['--no-confirm'],['--goal','PUBLIC-goal'],['--goal-parent','PUBLIC-parent'],['--confirm'],['-h'],['--help'],['--query'],['--','--goal','PUBLIC-literal'],['']]


@pytest.mark.parametrize('files',CASES,ids=['no-confirm','goal','goal-parent','confirm','short-help','help','query','separator','empty-file'])
def test_every_argument_after_message_is_literal_exact_legacy_body_output_and_confirmation(files,tmp_path,monkeypatch,wire):
    directory=tmp_path/'old';directory.mkdir();shutil.copyfile(ROOT/'tests/fixtures/r335-cortex-log',directory/'cortex-log')
    (directory/'response.json').write_text(json.dumps({'id':ID,'logged':True,'verified':True}))
    (directory/'confirmation.json').write_text(json.dumps({'row':{'summary':SUMMARY,'event_type':'commit','files':files}}))
    (directory/'_cortex_api.sh').write_text('cortex_agent_base_name() { printf "%s" "${1%%@*}"; }\ncortex_api_urlencode() { printf "%s" "$1"; }\ncortex_api_call() { if [ "$1" = POST ]; then cat "$PUBLIC_POST"; else cat "$PUBLIC_GET"; fi; }\n')
    arguments=['worker','commit',SUMMARY,*files]
    expected=subprocess.run(['bash',str(directory/'cortex-log'),*arguments],env=dict(os.environ,PUBLIC_POST=str(directory/'response.json'),PUBLIC_GET=str(directory/'confirmation.json')),capture_output=True,text=True)
    assert expected.returncode==0 and expected.stderr==''
    profile,reader,records=wire('commit',SUMMARY,files)
    monkeypatch.setattr(bridge,'load_member_profile',lambda *a:profile)
    out,err=io.StringIO(),io.StringIO();code=bridge.main(['log',*arguments],stdout=out,stderr=err)
    assert code==expected.returncode and out.getvalue()==expected.stdout and err.getvalue()==expected.stderr
    assert len(records)==reader.reads==2 and [r['method'] for r in records]==['POST','GET']
    assert records[0]['body']=={'event_type':'commit','summary':SUMMARY,'files_affected':files}
