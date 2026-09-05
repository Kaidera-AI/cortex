from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


API_MAIN_PATH = Path(__file__).resolve().parents[1] / "main.py"


def _load_main(name: str):
    spec = importlib.util.spec_from_file_location(name, API_MAIN_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(API_MAIN_PATH.parent))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    return module


class _Response:
    def __init__(self, revision: str, *, status_code: int = 200) -> None:
        self.status_code = status_code
        self._payload = {"projection_revision": revision, "providers": []}
        self.content = json.dumps(self._payload).encode()
        self.headers = {"content-length": str(len(self.content))}

    def json(self):
        return self._payload


class _Client:
    def __init__(self, *responses: _Response, **_kwargs) -> None:
        self.responses = list(responses)
        assert self.responses
        self.headers = None
        self.calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def get(self, _url, *, headers):
        self.headers = headers
        response = self.responses[min(self.calls, len(self.responses) - 1)]
        self.calls += 1
        return response


@pytest.mark.asyncio
async def test_projection_keys_require_matching_live_masked_authority_revision(
    monkeypatch,
):
    module = _load_main("cortex_api_projection_match_test")
    revision = "a" * 64
    fake_reader = SimpleNamespace(
        load_provider_projection=lambda: ({"openrouter": "in-memory-secret"}, revision)
    )
    monkeypatch.setitem(__import__("sys").modules, "openkai_provider_env", fake_reader)
    client = _Client(_Response(revision))
    monkeypatch.setattr(module.httpx, "AsyncClient", lambda **kwargs: client)
    monkeypatch.setenv(
        "OPENKAI_PROVIDER_STATUS_URL",
        "http://host.containers.internal:8766/provider-config",
    )
    monkeypatch.setenv("HARNESS_SERVICE_TOKEN", "t" * 48)

    assert await module._load_provider_keys_from_openkai() == {
        "openrouter": "in-memory-secret"
    }
    assert client.headers == {"Authorization": f"Bearer {'t' * 48}"}
    assert client.calls == 2


@pytest.mark.asyncio
async def test_stale_projection_or_unavailable_host_fails_closed(monkeypatch):
    module = _load_main("cortex_api_projection_stale_test")
    fake_reader = SimpleNamespace(
        load_provider_projection=lambda: ({"openrouter": "stale-secret"}, "a" * 64)
    )
    monkeypatch.setitem(__import__("sys").modules, "openkai_provider_env", fake_reader)
    monkeypatch.setenv(
        "OPENKAI_PROVIDER_STATUS_URL",
        "http://host.containers.internal:8766/provider-config",
    )
    monkeypatch.setenv("HARNESS_SERVICE_TOKEN", "t" * 48)
    monkeypatch.setattr(
        module.httpx,
        "AsyncClient",
        lambda **kwargs: _Client(_Response("b" * 64)),
    )

    assert await module._load_provider_keys_from_openkai() == {}

    monkeypatch.delenv("HARNESS_SERVICE_TOKEN")
    assert await module._load_provider_keys_from_openkai() == {}


@pytest.mark.asyncio
async def test_auth_state_change_between_status_and_projection_read_fails_closed(
    monkeypatch,
):
    module = _load_main("cortex_api_projection_auth_interleave_test")
    before = "a" * 64
    after = "b" * 64
    fake_reader = SimpleNamespace(
        load_provider_projection=lambda: ({"openrouter": "formerly-valid"}, before)
    )
    monkeypatch.setitem(__import__("sys").modules, "openkai_provider_env", fake_reader)
    client = _Client(_Response(before), _Response(after))
    monkeypatch.setattr(module.httpx, "AsyncClient", lambda **kwargs: client)
    monkeypatch.setenv(
        "OPENKAI_PROVIDER_STATUS_URL",
        "http://host.containers.internal:8766/provider-config",
    )
    monkeypatch.setenv("HARNESS_SERVICE_TOKEN", "t" * 48)

    # Models an atomic auth.json transition after the first host reconciliation:
    # the mounted projection still carries the old eligibility revision, while
    # the second live status has moved to the held state.
    assert await module._load_provider_keys_from_openkai() == {}
    assert client.calls == 2


@pytest.mark.asyncio
async def test_secret_use_revalidates_cached_key_after_patch_and_delete(monkeypatch):
    module = _load_main("cortex_api_projection_cached_mutation_test")
    module._provider_key_cache = {
        "keys": {"openrouter": "superseded-secret"},
        "expires": float("inf"),
    }
    snapshots = iter(({"openrouter": "rotated-secret"}, {}))
    calls = 0

    async def load_current():
        nonlocal calls
        calls += 1
        return next(snapshots)

    monkeypatch.setattr(module, "_load_provider_keys_from_openkai", load_current)

    assert await module.resolve_provider_key("openrouter") == "rotated-secret"
    assert await module.resolve_provider_key("openrouter") == ""
    assert calls == 2
    assert module._provider_key_cache["keys"] == {}
