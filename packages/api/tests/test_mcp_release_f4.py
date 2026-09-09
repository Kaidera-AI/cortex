"""F4 actual main dispatch: HTTP unavailable, generic SDK2 surface preserved."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


TOKEN = "f4-synthetic-token-not-a-credential"


@pytest.fixture
def dispatch(monkeypatch):
    for name in (
        "CORTEX_MCP_TRANSPORT", "CORTEX_MCP_HOST", "CORTEX_MCP_PORT",
        "CORTEX_MCP_BEARER_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    source = Path(__file__).resolve().parents[1] / "mcp_server.py"
    spec = importlib.util.spec_from_file_location("cortex_mcp_f4", source)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    calls = []

    def record(name, result=None):
        def called(*args, **kwargs):
            calls.append((name, args, kwargs))
            return result
        return called

    def unsafe(*_args, **_kwargs):
        pytest.fail("real process context must not be used by this test")

    monkeypatch.setattr(module.os, "setpgid", unsafe)
    monkeypatch.setattr(module.os, "killpg", unsafe)
    monkeypatch.setattr(module.os, "_exit", unsafe)
    monkeypatch.setattr(module, "_setup_pgroup", record("pgroup"))
    monkeypatch.setattr(module.mcp, "run", record("run"))
    monkeypatch.setattr(module.mcp, "streamable_http_app", record("app", object()))
    monkeypatch.setattr(module, "BearerAuthMiddleware", record("middleware", object()))
    monkeypatch.setitem(sys.modules, "uvicorn", SimpleNamespace(run=record("uvicorn")))
    return module, calls


@pytest.mark.parametrize("token", [None, "", TOKEN], ids=["absent", "empty", "present"])
@pytest.mark.parametrize("host,port", [
    (None, None), ("127.0.0.1", "8502"), ("0.0.0.0", "8502"),
    ("::1", "8502"), ("::", "8502"), ("example.invalid", "8502"),
    ("0.0.0.0", "not-a-port"),
], ids=["defaults", "loopback", "wildcard", "ipv6-loopback", "ipv6-any", "hostname", "bad-port"])
def test_http_main_refuses_before_serve(dispatch, monkeypatch, capsys, token, host, port):
    module, calls = dispatch
    monkeypatch.setenv("CORTEX_MCP_TRANSPORT", "streamable-http")
    if token is not None:
        monkeypatch.setenv("CORTEX_MCP_BEARER_TOKEN", token)
    # Production captures this setting at import; model both representations.
    monkeypatch.setattr(module, "_BEARER_TOKEN", token or "")
    if host is not None:
        monkeypatch.setenv("CORTEX_MCP_HOST", host)
    if port is not None:
        monkeypatch.setenv("CORTEX_MCP_PORT", port)
    with pytest.raises(SystemExit) as exc:
        module.main()
    assert isinstance(exc.value.code, int) and exc.value.code != 0
    captured = capsys.readouterr()
    assert "SEC-06" in captured.err and "v0.1.002" in captured.err
    assert TOKEN not in captured.out + captured.err
    assert calls == [("pgroup", (), {})]


@pytest.mark.parametrize("transport", [None, "stdio"], ids=["default", "explicit"])
def test_stdio_dispatch_stays_unchanged(dispatch, monkeypatch, transport):
    module, calls = dispatch
    if transport is not None:
        monkeypatch.setenv("CORTEX_MCP_TRANSPORT", transport)
    assert module.main() is None
    assert calls == [("pgroup", (), {}), ("run", (), {"transport": "stdio"})]


def test_unknown_transport_still_refuses(dispatch, monkeypatch, capsys):
    module, calls = dispatch
    monkeypatch.setenv("CORTEX_MCP_TRANSPORT", "unsupported-f4")
    with pytest.raises(SystemExit) as exc:
        module.main()
    assert exc.value.code == 2
    assert "unsupported CORTEX_MCP_TRANSPORT" in capsys.readouterr().err
    assert calls == [("pgroup", (), {})]
