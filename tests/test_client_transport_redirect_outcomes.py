"""R63 verifier follow-up: redirect Location is not parsed or followed."""
import pytest

from cortex_v2.clients.transport import http_request
from test_client_transport_origin import (
    isolated_transport_environment,
    member_headers,
    receiver,
)


@pytest.mark.parametrize('status', (301, 302, 303, 307, 308))
def test_malformed_redirect_preserves_original_outcome(receiver, status):
    body = b'{"error":{"code":"redirect_fixture"}}'
    origin, requests = receiver(status=status, location='http://[', body=body)
    response = http_request('GET', origin + '/projects',
                            headers=member_headers(), timeout=2)
    assert len(requests) == 1
    assert response.status == status
    assert response.headers['location'] == 'http://['
    assert response.body == body
