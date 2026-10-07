"""Untrusted JSON parser recursion must not escape the boot facade."""
import io
from types import SimpleNamespace
from cortex_v2.cli import agent_request as bridge,agent_boot
from cortex_v2.clients.transport import HttpResponse


def test_deep_json_refuses_without_traceback_or_partial_output(monkeypatch):
    calls=[]
    def headers(): calls.append('key');return {}
    profile=SimpleNamespace(member_reader=SimpleNamespace(project='helix',headers=headers),default_scope='helix',principal_label='kai',default_read_scopes=(),base_url='http://127.0.0.1:8501')
    monkeypatch.setattr(bridge,'load_member_profile',lambda *args:profile)
    def request(*args,**kwargs):calls.append('get');return HttpResponse(200,{},b'['*2000+b'0'+b']'*2000)
    monkeypatch.setattr(agent_boot,'http_request',request)
    out,err=io.StringIO(),io.StringIO()
    code=bridge.main(['boot','kai'],stdin=io.StringIO(),stdout=out,stderr=err)
    assert code != 0 and out.getvalue()=='' and 'Traceback' not in err.getvalue()
    assert calls==['key','get']
