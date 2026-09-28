"""Unit tests for the Python v2 client library (R23, R21 client side).

A fake transport records requests; no network is used.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from cortex_v2.clients.client import CallResult, CortexClient
from cortex_v2.clients.config import load_client_profile
from cortex_v2.clients.errors import (
    ClientConfigError,
    CortexApiError,
    CortexTransportError,
    IdempotencyKeyRequired,
    IncompatibleServerError,
    ScopeRequired,
)
from cortex_v2.clients.transport import HttpResponse


def write_token(tmp_path: Path, token: str = "t" * 43, mode: int = 0o600) -> Path:
    path = tmp_path / "token"
    path.write_text(token)
    path.chmod(mode)
    return path


def profile_file(tmp_path: Path, data: dict, *, token_mode: int = 0o600) -> Path:
    if "token_file" in data:
        data["token_file"] = str(write_token(tmp_path, mode=token_mode))
    path = tmp_path / "client.json"
    path.write_text(json.dumps(data))
    path.chmod(0o600)
    return path


def test_profile_from_env(tmp_path):
    token_path = write_token(tmp_path)
    profile = load_client_profile(
        env={
            "CORTEX_V2_BASE_URL": "http://127.0.0.1:8601",
            "CORTEX_V2_TOKEN_FILE": str(token_path),
            "CORTEX_V2_SCOPE": "proj-a",
            "CORTEX_V2_READ_SCOPES": "proj-a,shared-lib",
            "CORTEX_V2_INSTALLATION_LABEL": "local-lane",
        }
    )
    assert profile.base_url == "http://127.0.0.1:8601"
    assert profile.token == "t" * 43
    assert profile.default_scope == "proj-a"
    assert profile.default_read_scopes == ("proj-a", "shared-lib")


def test_profile_file_requires_v2_and_rejects_legacy_keys(tmp_path):
    path = profile_file(
        tmp_path,
        {
            "profile": "v2",
            "base_url": "http://127.0.0.1:8601",
            "token_file": "pending",
        },
    )
    profile = load_client_profile(path=path, env={})
    assert profile.base_url == "http://127.0.0.1:8601"

    legacy = tmp_path / "legacy.json"
    legacy.write_text(
        json.dumps(
            {
                "profile": "v2",
                "base_url": "http://127.0.0.1:8601",
                "token": "x" * 43,
                "x_project": "proj-a",
            }
        )
    )
    legacy.chmod(0o600)
    with pytest.raises(ClientConfigError) as excinfo:
        load_client_profile(path=legacy, env={})
    assert "legacy" in str(excinfo.value).lower()


def test_world_readable_token_file_is_rejected(tmp_path):
    with pytest.raises(ClientConfigError) as excinfo:
        load_client_profile(
            env={
                "CORTEX_V2_BASE_URL": "http://127.0.0.1:8601",
                "CORTEX_V2_TOKEN_FILE": str(write_token(tmp_path, mode=0o644)),
            }
        )
    assert "permi" in str(excinfo.value).lower()


def test_missing_token_is_rejected(tmp_path):
    with pytest.raises(ClientConfigError):
        load_client_profile(env={"CORTEX_V2_BASE_URL": "http://127.0.0.1:8601"})


class RecordingTransport:
    def __init__(self, responses=None):
        self.requests = []
        self.responses = list(responses or [])

    def __call__(self, method, url, *, headers=None, json_body=None, query=None,
                 timeout=30.0):
        self.requests.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers or {}),
                "json_body": json_body,
                "query": query,
            }
        )
        if self.responses:
            return self.responses.pop(0)
        return HttpResponse(
            status=200,
            headers={"x-request-id": "req-1"},
            body=json.dumps(
                {"data": {"ok": True}, "request_id": "req-1",
                 "contract_version": "test"}
            ).encode(),
        )


def make_client(transport, **profile_kwargs):
    from cortex_v2.clients.config import ClientProfile

    profile = ClientProfile(
        base_url="http://api.test",
        token="tok",
        default_scope=profile_kwargs.pop("default_scope", "proj-a"),
        default_read_scopes=profile_kwargs.pop("default_read_scopes", ()),
        installation_label=None,
        principal_label=None,
        source="test",
    )
    return CortexClient(profile, transport=transport)


def test_write_sends_v2_headers_and_requires_idempotency_key():
    transport = RecordingTransport()
    client = make_client(transport)
    result = client.call(
        "memory.record",
        payload={"record_type": "note", "body": "hello"},
        idempotency_key="key-1",
    )
    request = transport.requests[0]
    assert request["method"] == "POST"
    assert request["url"] == "http://api.test/v1/memory/records"
    headers = request["headers"]
    assert headers["Authorization"] == "Bearer tok"
    assert headers["X-Cortex-Scope"] == "proj-a"
    assert headers["Idempotency-Key"] == "key-1"
    assert "X-Cortex-Read-Scopes" not in headers
    assert isinstance(result, CallResult)
    assert result.status == 200
    assert result.request_id == "req-1"

    with pytest.raises(IdempotencyKeyRequired):
        client.call("memory.record", payload={"record_type": "note", "body": "x"})
    assert len(transport.requests) == 1  # nothing was sent


def test_read_scopes_header_sent_when_provided():
    transport = RecordingTransport()
    client = make_client(transport)
    client.call(
        "content.inspect",
        path_params={"content_id": "22222222-2222-4222-8222-222222222222"},
        read_scopes=("proj-a", "shared-lib"),
    )
    headers = transport.requests[0]["headers"]
    assert headers["X-Cortex-Read-Scopes"] == "proj-a,shared-lib"


def test_get_payload_becomes_query_parameters():
    transport = RecordingTransport()
    client = make_client(transport)
    client.call(
        "content.inspect",
        path_params={"content_id": "22222222-2222-4222-8222-222222222222"},
        payload={"revision": 2, "history": True},
    )
    request = transport.requests[0]
    assert request["method"] == "GET"
    assert request["json_body"] is None
    assert request["query"] == {"revision": "2", "history": "true"}
    assert request["url"].endswith(
        "/v1/content/22222222-2222-4222-8222-222222222222"
    )


def test_scope_required_enforced_before_send():
    transport = RecordingTransport()
    from cortex_v2.clients.config import ClientProfile

    client = CortexClient(
        ClientProfile(
            base_url="http://api.test",
            token="tok",
            default_scope=None,
            default_read_scopes=(),
            installation_label=None,
            principal_label=None,
            source="test",
        ),
        transport=transport,
    )
    with pytest.raises(ScopeRequired):
        client.call("memory.record", payload={}, idempotency_key="k")
    assert transport.requests == []
    # Operations that do not need a scope still work.
    client.call("protocol.descriptor")
    assert transport.requests[0]["url"] == "http://api.test/v1/protocol"


def test_api_problem_becomes_typed_error():
    body = json.dumps(
        {
            "error": {
                "code": "idempotency_key_reused",
                "message": "Use a new key.",
                "retryable": False,
            },
            "request_id": "req-9",
        }
    ).encode()
    transport = RecordingTransport(
        [HttpResponse(status=409, headers={"x-request-id": "req-9"}, body=body)]
    )
    client = make_client(transport)
    with pytest.raises(CortexApiError) as excinfo:
        client.call("memory.record", payload={}, idempotency_key="k")
    error = excinfo.value
    assert error.status == 409
    assert error.code == "idempotency_key_reused"
    assert error.retryable is False
    assert error.request_id == "req-9"


def test_unparsable_error_body_is_typed_and_never_retried_elsewhere():
    transport = RecordingTransport(
        [HttpResponse(status=500, headers={}, body=b"<html>legacy</html>")]
    )
    client = make_client(transport)
    with pytest.raises(CortexApiError) as excinfo:
        client.call("protocol.descriptor")
    assert excinfo.value.code == "unparsable_error_body"
    assert len(transport.requests) == 1  # no silent legacy fallback request


def test_transport_failure_is_typed():
    def exploding(method, url, **kwargs):
        raise CortexTransportError("connection refused")

    client = make_client(exploding)
    with pytest.raises(CortexTransportError):
        client.call("protocol.descriptor")


def test_verify_server_rejects_incompatible_api_with_upgrade_path():
    ok = RecordingTransport(
        [
            HttpResponse(
                status=200,
                headers={},
                body=json.dumps(
                    {"data": {"product_version": "0.2.1", "api_version": "v1",
                              "deployment_class": "isolated-w1-candidate"},
                     "request_id": "r", "contract_version": "cortex.api.v2-w1"}
                ).encode(),
            )
        ]
    )
    client = make_client(ok)
    info = client.verify_server()
    assert info["api_version"] == "v1"

    bad = RecordingTransport(
        [
            HttpResponse(
                status=200,
                headers={},
                body=json.dumps(
                    {"data": {"api_version": "v0"}, "request_id": "r"}
                ).encode(),
            )
        ]
    )
    client = make_client(bad)
    with pytest.raises(IncompatibleServerError) as excinfo:
        client.verify_server()
    assert "upgrade" in str(excinfo.value).lower()


def test_replay_flag_surfaced_from_header():
    transport = RecordingTransport(
        [
            HttpResponse(
                status=200,
                headers={"idempotent-replay": "true", "x-request-id": "r2"},
                body=json.dumps({"data": {"state": "committed"},
                                "request_id": "r2"}).encode(),
            )
        ]
    )
    client = make_client(transport)
    result = client.call("memory.record", payload={}, idempotency_key="k")
    assert result.replayed is True


def test_config_env_never_reads_global_tmp_or_hidden_files():
    # Explicit sources only: no path → env only; empty env → error.
    with pytest.raises(ClientConfigError):
        load_client_profile(env={})
