"""`CORTEX_AUTH_REQUIRE_JWT` enforcement — the flag that had never been executed.

Ren's whole-product review raised this as a P0 and it stands: the Cortex API's
audit trail rests on a caller-supplied `X-Agent-Name` with no credential, because
`CORTEX_AUTH_REQUIRE_JWT` defaults false (`main.py:248`) and is set true NOWHERE in
the repository. Enforcement was therefore dead code in every deployment — and,
before this file, code no test had ever run.

These tests deliberately do NOT change the default. Flipping it is a Gate 0 design
decision, not a cleanup: the shipped CLI sends `X-Agent-Name` with no bearer, so a
default flip would break every agent workflow at once. What they do is prove the
path WORKS when a deployment opts in, and pin the exemptions it relies on, so the
decision to enable it is a configuration choice rather than a leap of faith.

Note the flag is read at IMPORT time into a module-level constant, so a test must
patch the module attribute; setting the environment variable afterwards has no
effect. That is also true in production — enabling it requires a restart.
"""

import importlib.util
from pathlib import Path

import pytest
from starlette.requests import Request

API_MAIN_PATH = Path(__file__).resolve().parents[1] / "main.py"


def _load():
    spec = importlib.util.spec_from_file_location("cortex_api_requirejwt_test", API_MAIN_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _request(path: str, headers: dict[str, str] | None = None) -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return Request({
        "type": "http",
        "method": "GET",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": raw,
        "client": ("127.0.0.1", 1234),
        "server": ("testserver", 80),
        "scheme": "http",
        "root_path": "",
        "app": None,
    })


async def _passthrough(_request):
    """Stands in for the rest of the stack; reaching it means auth let the request by."""
    return "REACHED_HANDLER"


@pytest.mark.asyncio
async def test_default_is_off_so_an_unauthenticated_call_passes():
    """Documents today's real trust boundary rather than the one we would like."""
    api = _load()
    assert api.CORTEX_AUTH_REQUIRE_JWT is False, "default must stay OFF until Gate 0 decides"
    result = await api.local_jwt_middleware(_request("/handoffs"), _passthrough)
    assert result == "REACHED_HANDLER"


@pytest.mark.asyncio
async def test_when_enabled_a_bearerless_request_is_rejected():
    """The enforcement branch at main.py:1251, executed for the first time."""
    api = _load()
    api.CORTEX_AUTH_REQUIRE_JWT = True
    api.CORTEX_JWT_SECRET = "unit-test-secret"
    result = await api.local_jwt_middleware(_request("/handoffs"), _passthrough)
    assert result != "REACHED_HANDLER", "enforcement did not stop a credential-free request"
    assert result.status_code == 401
    assert b"Bearer token required" in result.body


@pytest.mark.asyncio
async def test_when_enabled_a_caller_supplied_agent_name_is_not_a_credential():
    """The actual P0: `X-Agent-Name` is caller-controlled and must not authenticate."""
    api = _load()
    api.CORTEX_AUTH_REQUIRE_JWT = True
    api.CORTEX_JWT_SECRET = "unit-test-secret"
    result = await api.local_jwt_middleware(
        _request("/handoffs", {"X-Agent-Name": "kai", "X-Project": "kaidera-os"}),
        _passthrough,
    )
    assert result != "REACHED_HANDLER"
    assert result.status_code == 401



@pytest.mark.asyncio
async def test_enforcement_with_an_empty_secret_refuses_loudly_not_openly():
    """SEC-03 (audit 2026-09-01): flipping enforcement on while the signing secret
    is empty must refuse every request with a loud 503 — a 401-with-forgery or a
    pass-through would both be universal token forgery under the empty key."""
    api = _load()
    api.CORTEX_AUTH_REQUIRE_JWT = True
    api.CORTEX_JWT_SECRET = ""
    result = await api.local_jwt_middleware(_request("/handoffs"), _passthrough)
    assert result != "REACHED_HANDLER"
    assert result.status_code == 503
    assert b"CORTEX_JWT_SECRET" in result.body

@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/health", "/metrics"])
async def test_health_and_metrics_stay_reachable_when_enabled(path):
    """Both are exempt by design. If enabling auth broke them, every container
    healthcheck and scrape would fail the moment a deployment opted in — which is
    exactly the kind of thing you want proven BEFORE flipping the flag."""
    api = _load()
    api.CORTEX_AUTH_REQUIRE_JWT = True
    result = await api.local_jwt_middleware(_request(path), _passthrough)
    assert result == "REACHED_HANDLER"


@pytest.mark.asyncio
async def test_a_valid_bearer_is_accepted_and_its_claims_are_attached():
    """The positive path: enforcement on, a properly signed token gets through and
    the claims land on request.state for downstream actor checks to use."""
    api = _load()
    api.CORTEX_AUTH_REQUIRE_JWT = True
    api.CORTEX_JWT_SECRET = "unit-test-secret"

    import base64
    import hashlib
    import hmac
    import json
    import time

    def b64(raw: bytes) -> bytes:
        return base64.urlsafe_b64encode(raw).rstrip(b"=")

    header = b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = b64(json.dumps({
        "agent": "kai",
        "project": "kaidera-os",
        "exp": int(time.time()) + 300,
    }).encode())
    signing_input = header + b"." + payload
    sig = b64(hmac.new(b"unit-test-secret", signing_input, hashlib.sha256).digest())
    token = (signing_input + b"." + sig).decode()

    request = _request("/handoffs", {"Authorization": f"Bearer {token}"})
    result = await api.local_jwt_middleware(request, _passthrough)
    assert result == "REACHED_HANDLER"
    assert request.state.jwt_claims["agent"] == "kai"


@pytest.mark.asyncio
async def test_a_bearer_project_may_not_contradict_the_project_header():
    """Cross-project isolation: a token scoped to one project cannot drive another."""
    api = _load()
    api.CORTEX_AUTH_REQUIRE_JWT = True
    api.CORTEX_JWT_SECRET = "unit-test-secret"

    import base64
    import hashlib
    import hmac
    import json
    import time

    def b64(raw: bytes) -> bytes:
        return base64.urlsafe_b64encode(raw).rstrip(b"=")

    header = b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = b64(json.dumps({
        "agent": "kai",
        "project": "kaidera-os",
        "exp": int(time.time()) + 300,
    }).encode())
    signing_input = header + b"." + payload
    sig = b64(hmac.new(b"unit-test-secret", signing_input, hashlib.sha256).digest())
    token = (signing_input + b"." + sig).decode()

    result = await api.local_jwt_middleware(
        _request("/handoffs", {"Authorization": f"Bearer {token}", "X-Project": "some-other-project"}),
        _passthrough,
    )
    assert result != "REACHED_HANDLER"
    assert result.status_code == 403
