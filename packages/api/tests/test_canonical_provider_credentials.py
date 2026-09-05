"""The provider-key precondition must resolve from the authority, not a cold cache.

Provider credentials live ONLY in the OpenKai projection seam: cortex-api resolves
keys from the host-derived, read-only projection after bracketing the read with live
masked-revision checks. The KOS app_settings store and process env are deliberately
NOT fallbacks — retaining either recreates the split credential plane whose two
disagreeing stores let an operator watch a key Test succeed while every embedding
failed (marlow, before the OpenKai cutover).

The gate/resolver agreement the old dual-plane tests defended is STILL required, and
is exactly what happened on marlow 2026-08-18: after a stack restart every embedding
backfill was refused with "embedding provider key is not configured for cortex-api",
on an appliance whose key was configured and working. It persisted across retries
spanning more than the 60s cache TTL, so it does not self-heal. Those three defenses
are pinned below against the projection seam; the projection's own security
properties (revision match, fail-closed, quote round-trip) are pinned by
test_openkai_projection_authority.py and test_openkai_provider_env.py.
"""

import importlib.util
import json
from pathlib import Path

import pytest


API_MAIN_PATH = Path(__file__).resolve().parents[1] / "main.py"


def load_module():
    spec = importlib.util.spec_from_file_location(
        "cortex_api_canonical_credentials_test", API_MAIN_PATH
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
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


def _arm_projection(module, monkeypatch, keys: dict[str, str], revision: str = "a" * 64):
    """Wire a coherent live projection so resolve_provider_key can return a key."""
    from types import SimpleNamespace

    monkeypatch.setitem(
        __import__("sys").modules,
        "openkai_provider_env",
        SimpleNamespace(load_provider_projection=lambda: (keys, revision)),
    )
    monkeypatch.setattr(
        module.httpx, "AsyncClient", lambda **kwargs: _Client(_Response(revision))
    )
    monkeypatch.setenv(
        "OPENKAI_PROVIDER_STATUS_URL",
        "http://host.containers.internal:8766/provider-config",
    )
    monkeypatch.setenv("HARNESS_SERVICE_TOKEN", "t" * 48)


@pytest.mark.asyncio
async def test_provider_configured_resolves_from_a_COLD_cache(monkeypatch):
    """The precondition must FILL the key cache, not merely read it."""
    module = load_module()
    module._provider_key_cache["keys"] = {}
    module._provider_key_cache["expires"] = 0.0
    _arm_projection(module, monkeypatch, {"openrouter": "sk-from-the-projection"})

    assert (
        await module.provider_configured({"embedding_provider": "openrouter"}, "embedding")
        is True
    )
    assert module._provider_key_cache["keys"], "the cold cache was never populated"


@pytest.mark.asyncio
async def test_provider_configured_is_false_when_the_authority_has_no_key(monkeypatch):
    """The fix must not turn the gate into an unconditional yes."""
    module = load_module()
    module._provider_key_cache["keys"] = {}
    module._provider_key_cache["expires"] = 0.0
    _arm_projection(module, monkeypatch, {})

    assert (
        await module.provider_configured({"embedding_provider": "openrouter"}, "embedding")
        is False
    )


def test_no_synchronous_cache_only_provider_predicate_returns():
    """A sync predicate over this cache is the defect; it must not come back.

    Every caller is async -- including the doctor's config_sanity check, which is why it
    previously had to warm the cache by hand to avoid emitting a false critical.
    """
    module = load_module()
    assert not hasattr(module, "_provider_configured"), (
        "the cache-only predicate is back; callers can all await provider_configured"
    )


@pytest.mark.asyncio
async def test_process_env_and_settings_store_are_not_credential_planes(monkeypatch):
    """Only the projection resolves. Env/settings leftovers must not shadow it.

    The split plane this forbids is the exact marlow failure mode: two stores
    disagreeing about what "configured" means. So neither an exported env var nor a
    fake legacy loader may produce a key while the projection is empty.
    """
    module = load_module()
    module._provider_key_cache["keys"] = {}
    module._provider_key_cache["expires"] = 0.0
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-stale-process-env")
    _arm_projection(module, monkeypatch, {})

    assert await module.resolve_provider_key("openrouter") == ""
    assert not hasattr(module, "_load_provider_keys_from_settings"), (
        "the settings-store credential plane is back; the projection is the sole authority"
    )


@pytest.mark.asyncio
async def test_a_warm_projection_satisfies_readiness_without_a_live_status_round_trip():
    """Readiness gates may trust a warm cache; secret use still revalidates.

    provider_configured answers from a warm projection (fast readiness check), while
    every actual secret retrieval goes through resolve_provider_key(force=True), which
    re-checks the live masked revision before returning anything.
    """
    module = load_module()
    module._provider_key_cache["keys"] = {"openrouter": "sk-warm"}
    module._provider_key_cache["expires"] = float("inf")

    assert (
        await module.provider_configured({"embedding_provider": "openrouter"}, "embedding")
        is True
    )
