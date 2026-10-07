"""Real urllib redirect handling with a synthetic transport; no network/keys."""
from email.message import Message
import io
from pathlib import Path
import runpy
import sys
from urllib import request as transport
from urllib.response import addinfourl

import pytest


CLI = Path(__file__).resolve().parents[2] / "cli"
TOKEN = "PUBLIC-SYNTHETIC-CREDENTIAL-R409"


class Wire(transport.BaseHandler):
    handler_order = 100

    def __init__(self, code=200, target=None):
        self.code, self.target, self.calls = code, target, []

    def https_open(self, request):
        self.calls.append((request.full_url, request.get_header("Authorization")))
        first = len(self.calls) == 1
        code = self.code if first else 200
        headers = Message()
        if first and self.target:
            headers["Location"] = self.target
        body = b'{"projects":[{"project_key":"notes","status":"active"}]}'
        response = addinfourl(io.BytesIO(body), headers, request.full_url, code)
        response.msg = "PUBLIC synthetic response"
        return response

    http_open = https_open


@pytest.fixture
def manager(monkeypatch):
    monkeypatch.syspath_prepend(str(CLI))
    namespace = runpy.run_path(str(CLI / "cortex-auth-credential"), run_name="r409_fixture")
    return namespace["_projects"].__globals__


def install_wire(manager, monkeypatch, wire):
    actual = transport.build_opener

    def build(*handlers):
        return actual(*handlers, transport.ProxyHandler({}), wire)

    monkeypatch.setitem(manager, "build_opener", build)
    monkeypatch.setitem(manager, "urlopen", build().open)


def call(manager, operation, origin="https://trusted.invalid"):
    if operation == "proof":
        return manager["_prove_consumer_use"]("notes", "console", TOKEN, origin)
    return manager["_projects"](origin, TOKEN)


@pytest.mark.parametrize("operation", ("proof", "projects"))
@pytest.mark.parametrize("code", (301, 302, 303, 307, 308))
@pytest.mark.parametrize("target", ("https://other.invalid/target", "http://trusted.invalid/target",
                                    "https://trusted.invalid/target"))
def test_credentialed_redirect_is_refused_before_any_hop(manager, monkeypatch, operation, code, target):
    wire = Wire(code, target)
    install_wire(manager, monkeypatch, wire)
    error = None
    try:
        call(manager, operation)
    except manager["CredentialOperationError"] as failure:
        error = failure
    assert len(wire.calls) == 1, "R409 credential crossed a redirect hop"
    assert error is not None, "R409 every credentialed 3xx must refuse"
    assert wire.calls[0][1] == "Bearer " + TOKEN


@pytest.mark.parametrize("operation", ("proof", "projects"))
def test_remote_initial_http_is_refused_before_transport(manager, monkeypatch, operation):
    wire = Wire()
    install_wire(manager, monkeypatch, wire)
    with pytest.raises(manager["CredentialOperationError"]):
        call(manager, operation, "http://other.invalid")
    assert wire.calls == []


@pytest.mark.parametrize("operation", ("proof", "projects"))
@pytest.mark.parametrize("origin", ("https://trusted.invalid", "http://127.0.0.1:8501"))
def test_direct_tls_and_intentional_loopback_keep_the_exact_client_effect(manager, monkeypatch, operation, origin):
    wire = Wire()
    install_wire(manager, monkeypatch, wire)
    result = call(manager, operation, origin)
    assert result == (None if operation == "proof" else ["notes"])
    assert len(wire.calls) == 1 and wire.calls[0][1] == "Bearer " + TOKEN
