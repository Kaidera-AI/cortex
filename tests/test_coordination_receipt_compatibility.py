"""Author follow-up: preserve native no-policy receipts and safe uncertainty."""
import json

import pytest

from cortex_v2.clients.client import CortexClient
from cortex_v2.clients.errors import CortexApiError
from cortex_v2.clients.transport import HttpResponse
from test_agent_coordination_write_contract import arguments, receipt
from test_member_client_integration import FixtureReader, member_profile, EXPIRY


def response_client(body):
    reader, calls = FixtureReader(), []
    def transport(*args, **kwargs):
        calls.append((args, kwargs))
        return HttpResponse(200, {'x-request-id': 'fixture-request', 'idempotent-replay': 'true',
                                 'cortex-key-expires': EXPIRY}, body)
    return CortexClient(member_profile('http://127.0.0.1:1', reader), transport=transport), reader, calls


def test_native_no_writer_policy_revision_zero_is_a_valid_committed_receipt():
    # repository.declare_policy_revision returns 0 when there is no writer policy.
    data = dict(receipt(), policy_revision=0)
    client, reader, calls = response_client(json.dumps({'data': data}).encode())
    result = client.call('coordination.handoff.return', **arguments('return'))
    assert result.data == data and result.replayed is True
    assert result.key_expires_at == EXPIRY and result.request_id == 'fixture-request'
    assert reader.reads == 1 and len(calls) == 1


@pytest.mark.parametrize('body', [b'[]', b'null', b'1', b'"unissued-response-marker"',
                                 b'<unissued-response-marker>', b'\xff'])
def test_non_object_success_is_safe_uncertainty_with_metadata_and_no_retry(body):
    client, reader, calls = response_client(body)
    with pytest.raises(CortexApiError) as refused:
        client.call('coordination.handoff.return', **arguments('return'))
    error = refused.value
    assert error.code == 'invalid_write_receipt' and error.retryable is False
    assert error.request_id == 'fixture-request' and error.key_expires_at == EXPIRY
    assert 'uncertain' in error.message and 'unissued-response-marker' not in str(error)
    assert reader.reads == 1 and len(calls) == 1
