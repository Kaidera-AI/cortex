"""Malformed actual HTTP JSON is a safe typed refusal before auth/storage."""
import importlib
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient


@pytest.mark.parametrize('body',[b'PUBLIC malformed JSON',b'{"event_type":"commit","event_type":"lesson","summary":"PUBLIC"}',b'[]',b' '*1048577])
def test_log_invalid_json_is_typed_and_refuses_before_auth_or_storage(body):
    module=importlib.import_module('cortex_v2.app');app=module.create_app();calls=[]
    def forbidden(*args,**kwargs):calls.append(True);pytest.fail('malformed request used auth/storage')
    app.state.pool=SimpleNamespace(acquire=forbidden)
    client=TestClient(app,raise_server_exceptions=False)
    result=client.post('/log',content=body,headers={'Content-Type':'application/json'})
    assert result.status_code==422 and result.json()['error']['code']=='invalid_log_request'
    assert calls==[] and 'PUBLIC malformed JSON' not in result.text and 'Traceback' not in result.text
