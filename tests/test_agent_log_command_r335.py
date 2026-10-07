"""Frozen current command output and actual loopback wire; unissued in-memory reader."""
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from types import SimpleNamespace
import uuid

import pytest
from cortex_v2.cli import agent_request as bridge
from test_member_client_integration import FixtureReader,member_profile
from test_client_transport_origin import isolated_transport_environment  # noqa: F401

ROOT=Path(__file__).resolve().parents[1]
TYPES=['commit','decision','lesson','started','stopped','blocked','unblocked','bug','handoff','question']
ID='10000000-0000-4000-8000-000000000001';EVENT='20000000-0000-4000-8000-000000000002'
SUMMARY="PUBLIC résumé ☃\n"+'long original '*400
FILES=['/PUBLIC/a b.py','/PUBLIC/é.md']


def required():
    assert importlib.util.find_spec('cortex_v2.cli.agent_log') is not None,'native log command missing'
    from cortex_v2.cli import agent_log
    assert callable(getattr(agent_log,'run',None)),'native log command missing'
    return agent_log


@pytest.fixture
def wire():
    servers=[]
    def start(event_type='commit',summary=SUMMARY,files=FILES,*,defect=None):
        records=[];reader=FixtureReader()
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def respond(self):
                raw=self.rfile.read(int(self.headers.get('Content-Length',0)))
                key=self.headers.get('Authorization');version=1 if key=='Bearer '+'A'*43 else 2 if key=='Bearer '+'B'*43 else 0
                records.append({'method':self.command,'path':self.path,'body':json.loads(raw) if raw else None,'scope':self.headers.get('X-Cortex-Scope'),'agent':self.headers.get('X-Agent-Name'),'idempotency':self.headers.get('Idempotency-Key'),'version':version})
                if self.command=='POST':
                    data={'id':ID,'verified':True,'logged':True}
                    if event_type in ['decision','lesson']:data.update(embedded=False,team_event_id=EVENT)
                    if defect=='unverified':data['verified']=False
                    if defect=='bad-id':data['id']='../../foreign'
                    reader.version=2
                else:
                    kind=event_type if event_type in ['decision','lesson'] and 'kind=team_event' not in self.path else 'team_event'
                    rid=EVENT if 'id='+EVENT in self.path else ID
                    row={'summary':summary,'event_type':event_type,'files':files}
                    if defect=='summary':row['summary']='PUBLIC changed'
                    if defect=='files':row['files']=list(reversed(files))
                    if defect=='kind':kind='foreign'
                    if defect=='confirm-unverified':data={'verified':False,'kind':kind,'id':rid,'row':row}
                    else:data={'verified':True,'kind':kind,'id':rid,'row':row}
                encoded=json.dumps(data,ensure_ascii=False).encode();self.send_response(403 if defect=='denied' else 200);self.send_header('Content-Length',str(len(encoded)));self.end_headers();self.wfile.write(encoded)
            do_GET=respond;do_POST=respond
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();servers.append((server,thread))
        profile=member_profile(f'http://127.0.0.1:{server.server_port}',reader);profile.principal_label='worker'
        return profile,reader,records
    yield start
    for server,thread in servers:server.shutdown();server.server_close();thread.join(2);assert not thread.is_alive()


def legacy(tmp_path,event_type,confirm,goal,summary=SUMMARY):
    directory=tmp_path/'legacy';directory.mkdir();shutil.copyfile(ROOT/'tests/fixtures/r335-cortex-log',directory/'cortex-log')
    body={'id':ID,'verified':True,'logged':True}
    if event_type in ['decision','lesson']:body.update(embedded=False,team_event_id=EVENT)
    expected='[GOAL:'+goal+'] '+summary if goal else summary
    response=directory/'response.json';response.write_text(json.dumps(body))
    readback=directory/'readback.json';readback.write_text(json.dumps({'row':{'summary':expected,'event_type':event_type,'files':FILES}}))
    (directory/'_cortex_api.sh').write_text('cortex_agent_base_name() { local base="${1%%@*}"; printf "%s" "${base%%:*}"; }\ncortex_api_urlencode() { printf "%s" "$1"; }\ncortex_api_call() { if [ "$1" = POST ]; then cat "$PUBLIC_RESPONSE"; else cat "$PUBLIC_READBACK"; fi; }\n')
    args=['bash',str(directory/'cortex-log'),'--confirm' if confirm else '--no-confirm']
    if goal:args+=['--goal',goal]
    args+=['WORKER@fixture-project:test',event_type,summary,*FILES]
    result=subprocess.run(args,env=dict(os.environ,PUBLIC_RESPONSE=str(response),PUBLIC_READBACK=str(readback)),capture_output=True,text=True)
    assert result.returncode==0 and result.stderr==''
    return result.stdout


@pytest.mark.parametrize('event_type',TYPES)
@pytest.mark.parametrize('confirm,goal',[(True,''),(False,'PUBLIC-goal')])
def test_exact_current_command_output_and_real_v2_member_wire(event_type,confirm,goal,tmp_path,monkeypatch,wire):
    required();summary='[GOAL:'+goal+'] '+SUMMARY if goal else SUMMARY
    profile,reader,records=wire(event_type,summary)
    monkeypatch.setattr(bridge,'load_member_profile',lambda *a:profile)
    args=['log','--confirm' if confirm else '--no-confirm']
    if goal:args+=['--goal',goal]
    args+=['WORKER@fixture-project:test',event_type,SUMMARY,*FILES]
    out,err=io.StringIO(),io.StringIO();code=bridge.main(args,stdin=io.StringIO(),stdout=out,stderr=err)
    assert code==0 and err.getvalue()=='' and out.getvalue()==legacy(tmp_path,event_type,confirm,goal)
    wanted={'event_type':event_type,'summary':summary,'files_affected':FILES}
    if goal:wanted['metadata']={'parent_goal_id':goal,'goal_ancestry':[goal]}
    assert records[0]['method']=='POST' and records[0]['path']=='/log' and records[0]['body']==wanted
    assert str(uuid.UUID(records[0]['idempotency']))==records[0]['idempotency']
    expected=1+(2 if event_type in ['decision','lesson'] else 1) if confirm else 1
    assert len(records)==reader.reads==expected and all(r['scope']=='fixture-project' and r['agent']=='worker' for r in records)
    assert records[0]['version']==1 and all(r['version']==2 and r['method']=='GET' and r['body'] is None and r['idempotency'] is None for r in records[1:])


@pytest.mark.parametrize('defect',['unverified','bad-id','summary','files','kind','confirm-unverified','denied'])
def test_log_success_and_confirmation_failures_never_print_success_or_retry(defect,monkeypatch,wire):
    required();profile,reader,records=wire(defect=defect);monkeypatch.setattr(bridge,'load_member_profile',lambda *a:profile)
    out,err=io.StringIO(),io.StringIO();code=bridge.main(['log','worker','commit',SUMMARY,*FILES],stdin=io.StringIO(),stdout=out,stderr=err)
    assert code!=0 and out.getvalue()=='' and 'Bearer' not in err.getvalue() and SUMMARY not in err.getvalue() and 'Traceback' not in err.getvalue()
    assert len([r for r in records if r['method']=='POST'])==1 and reader.reads==len(records)


@pytest.mark.parametrize('arguments',[['worker','foreign','PUBLIC'],['owner','commit','PUBLIC'],['worker','commit',''],['worker','commit','x'*65537],['worker','commit','PUBLIC\x00'],['--goal','a','--goal','b','worker','commit','PUBLIC']])
def test_invalid_log_inputs_refuse_before_profile_or_credentials(arguments,monkeypatch):
    required();calls=[]
    # Name mismatch is decided against the non-secret selected profile, before keys.
    reader=FixtureReader();profile=member_profile('http://127.0.0.1:1',reader);profile.principal_label='worker'
    monkeypatch.setattr(bridge,'load_member_profile',lambda *a:(calls.append(True),profile)[1])
    out,err=io.StringIO(),io.StringIO();code=bridge.main(['log',*arguments],stdin=io.StringIO(),stdout=out,stderr=err)
    assert code!=0 and out.getvalue()=='' and reader.reads==0 and 'Bearer' not in err.getvalue()
    if arguments[0]!='owner':assert calls==[]


def test_release_log_wrapper_forwards_only_original_arguments_and_selected_profile(tmp_path):
    wrapper=ROOT/'scripts/agent-shims/cortex-log';assert wrapper.is_file(),'released log wrapper missing'
    directory=tmp_path/'bin';directory.mkdir();shutil.copyfile(wrapper,directory/'cortex-log')
    (directory/'cortex-agent').write_text('#!/usr/bin/env python3\nimport json,sys\nprint(json.dumps(sys.argv[1:]))\n');(directory/'cortex-agent').chmod(0o755)
    result=subprocess.run(['bash',str(directory/'cortex-log'),'--no-confirm','worker','commit','PUBLIC quoted \u2603'],env=dict(os.environ,CORTEX_CONNECTION_PROFILE='/PUBLIC/non-secret-profile.json'),capture_output=True,text=True)
    assert result.returncode==0 and result.stderr==''
    assert json.loads(result.stdout)==['--config','/PUBLIC/non-secret-profile.json','log','--no-confirm','worker','commit','PUBLIC quoted \u2603']
