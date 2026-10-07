"""Malformed public response shapes remain safe one-attempt refusals."""
import io
import json

import pytest
from cortex_v2.cli import agent_request as bridge,agent_log
from cortex_v2.clients.transport import HttpResponse
from test_member_client_integration import FixtureReader,member_profile

ID='10000000-0000-4000-8000-000000000001'


@pytest.mark.parametrize('row',[None,[], 'PUBLIC malformed row'])
def test_confirmation_non_object_rows_refuse_without_traceback_or_body_echo(row,monkeypatch):
    reader=FixtureReader();profile=member_profile('http://127.0.0.1:1',reader);profile.principal_label='worker'
    monkeypatch.setattr(bridge,'load_member_profile',lambda *a:profile);calls=[]
    def transport(method,url,**kwargs):
        calls.append((method,url))
        body={'logged':True,'verified':True,'id':ID} if method=='POST' else {'verified':True,'id':ID,'kind':'team_event','row':row}
        return HttpResponse(200,{},json.dumps(body).encode())
    monkeypatch.setattr(agent_log,'http_request',transport)
    out,err=io.StringIO(),io.StringIO();code=bridge.main(['log','worker','commit','PUBLIC summary'],stdin=io.StringIO(),stdout=out,stderr=err)
    assert code!=0 and out.getvalue()=='' and 'Traceback' not in err.getvalue() and 'PUBLIC malformed row' not in err.getvalue()
    assert len(calls)==reader.reads==2 and [c[0] for c in calls]==['POST','GET']


def test_ambiguous_command_write_has_one_attempt_and_safe_error(monkeypatch):
    from cortex_v2.clients.errors import CortexTransportError
    reader=FixtureReader();profile=member_profile('http://127.0.0.1:1',reader);profile.principal_label='worker'
    monkeypatch.setattr(bridge,'load_member_profile',lambda *a:profile);calls=[]
    def transport(*args,**kwargs):calls.append(True);raise CortexTransportError('PUBLIC private diagnostics unissued marker')
    monkeypatch.setattr(agent_log,'http_request',transport)
    out,err=io.StringIO(),io.StringIO();code=bridge.main(['log','worker','commit','PUBLIC summary'],stdin=io.StringIO(),stdout=out,stderr=err)
    assert code==7 and out.getvalue()=='' and 'unissued marker' not in err.getvalue() and calls==[True] and reader.reads==1
