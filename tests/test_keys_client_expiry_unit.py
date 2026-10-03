"""S8: keep finite expiry metadata on actual client outcomes."""

from __future__ import annotations

import json

import pytest

from cortex_v2.clients.errors import CortexApiError
from cortex_v2.clients.transport import HttpResponse
from test_interface_client_unit import RecordingTransport, make_client

EXPIRY = "2027-10-03T05:20:00+00:00"


@pytest.mark.parametrize("header", ("cortex-key-expires", "Cortex-Key-Expires"))
@pytest.mark.parametrize("expiry", (None, EXPIRY))
def test_success_keeps_exact_expiry_or_honest_absence(header, expiry) -> None:
    response = HttpResponse(
        status=200,
        headers={} if expiry is None else {header: expiry},
        body=json.dumps({"data": {"principal_id": "synthetic"}}).encode(),
    )
    transport = RecordingTransport([response])
    result = make_client(transport).call("auth.principal")
    assert result.key_expires_at == expiry
    assert result.data == {"principal_id": "synthetic"}
    assert len(transport.requests) == 1


@pytest.mark.parametrize("header", ("cortex-key-expires", "Cortex-Key-Expires"))
@pytest.mark.parametrize("expiry", (None, EXPIRY))
@pytest.mark.parametrize("valid_problem", (True, False))
def test_denial_keeps_expiry_without_retry_or_authority_change(
    header, expiry, valid_problem,
) -> None:
    body = (
        json.dumps({"error": {
            "code": "project_key_manager_required",
            "message": "Ask this project's active lead.", "retryable": False,
        }}).encode()
        if valid_problem else b"unparsable denial"
    )
    response = HttpResponse(
        status=403, headers={} if expiry is None else {header: expiry}, body=body,
    )
    transport = RecordingTransport([response])
    with pytest.raises(CortexApiError) as rejected:
        make_client(transport).call("auth.principal")
    assert rejected.value.status == 403
    assert rejected.value.code == (
        "project_key_manager_required" if valid_problem else "unparsable_error_body"
    )
    assert rejected.value.key_expires_at == expiry
    assert rejected.value.as_dict()["key_expires_at"] == expiry
    assert len(transport.requests) == 1
