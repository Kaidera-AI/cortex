"""Verifier follow-up: uncertain write outcomes retain the native correlation ID."""
import json

import pytest

from cortex_v2.clients.client import CortexClient
from cortex_v2.clients.errors import CortexApiError
from cortex_v2.clients.transport import HttpResponse
from test_agent_coordination_write_contract import arguments
from test_member_client_integration import FixtureReader, member_profile, EXPIRY


@pytest.mark.parametrize('header', [None, 'fixture-header-request'])
def test_uncertain_receipt_prefers_available_body_request_id_without_retry(header):
    reader, calls = FixtureReader(), []
    def transport(*args, **kwargs):
        calls.append((args, kwargs))
        headers = {'cortex-key-expires': EXPIRY}
        if header is not None:
            headers['x-request-id'] = header
        return HttpResponse(200, headers, json.dumps({
            'request_id': 'fixture-body-request', 'data': None}).encode())
    client = CortexClient(member_profile('http://127.0.0.1:1', reader), transport=transport)
    with pytest.raises(CortexApiError) as refused:
        client.call('coordination.handoff.return', **arguments('return'))
    assert refused.value.request_id == 'fixture-body-request'
    assert refused.value.key_expires_at == EXPIRY
    assert refused.value.code == 'invalid_write_receipt' and refused.value.retryable is False
    assert reader.reads == 1 and len(calls) == 1
