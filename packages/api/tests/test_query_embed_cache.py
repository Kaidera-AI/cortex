"""Query-embedding cache — the search path's provider round-trip, memoised.

Measured on marlow-aws before this existed: a /search matching ZERO rows still cost
1.0-1.7s, an uncontended real search 2.2-4.3s, and the SQL stages 19-114ms. The
latency was the external embedding call, not Postgres and not the 161.8MB of unused
indexes an earlier theory blamed. Nothing cached it, so three identical queries in a
row measured 3.18s, 2.05s and 2.45s.

These tests pin the properties that make the cache safe rather than merely fast:
it must not be unbounded (a cache keyed on caller text is otherwise a memory
exhaustion path), it must not serve a vector from a superseded model, and it must not
cache failures.
"""

import asyncio
import importlib.util
from pathlib import Path

import pytest

API_MAIN_PATH = Path(__file__).resolve().parents[1] / "main.py"


def _load():
    spec = importlib.util.spec_from_file_location("cortex_api_embedcache_test", API_MAIN_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    module._QUERY_EMBED_CACHE.clear()
    module._QUERY_EMBED_INFLIGHT.clear()
    return module


def _stub_provider(api, config=None, vector=(0.1, 0.2, 0.3)):
    """Count provider calls at the config-pinned provider boundary."""
    calls = []

    async def fake_config(force: bool = False):
        return config or {"embedding_provider": "p1", "embedding_model": "m1", "embedding_dims": 3}

    async def fake_embed(text, _config):
        calls.append(text)
        return list(vector) if vector else None

    api.load_cortex_platform_config_cached = fake_config
    api._embed_text_with_config = fake_embed
    return calls


@pytest.mark.asyncio
async def test_identical_queries_hit_the_provider_once():
    api = _load()
    calls = _stub_provider(api)
    a = await api.embed_query_cached("the agent inherits the runbook")
    b = await api.embed_query_cached("the agent inherits the runbook")
    assert a == b == [0.1, 0.2, 0.3]
    assert len(calls) == 1, f"expected one provider call, got {len(calls)}"


@pytest.mark.asyncio
async def test_concurrent_identical_queries_are_single_flight():
    """A burst must share the first miss instead of amplifying provider traffic."""
    api = _load()
    calls = []
    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_config(force: bool = False):
        return {"embedding_provider": "p1", "embedding_model": "m1", "embedding_dims": 3}

    async def slow_embed(text, _config):
        calls.append(text)
        started.set()
        await release.wait()
        return [0.1, 0.2, 0.3]

    api.load_cortex_platform_config_cached = fake_config
    api._embed_text_with_config = slow_embed
    tasks = [
        asyncio.create_task(api.embed_query_cached("the same burst query", "project-a"))
        for _ in range(20)
    ]
    await asyncio.wait_for(started.wait(), timeout=1)
    assert len(calls) == 1
    release.set()
    assert await asyncio.gather(*tasks) == [[0.1, 0.2, 0.3]] * 20


@pytest.mark.asyncio
async def test_cache_is_project_scoped():
    api = _load()
    calls = _stub_provider(api)
    query = "the same confidential project query"
    await api.embed_query_cached(query, "project-a")
    await api.embed_query_cached(query, "project-b")
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_different_queries_are_not_conflated():
    api = _load()
    calls = _stub_provider(api)
    await api.embed_query_cached("first distinct query text")
    await api.embed_query_cached("second distinct query text")
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_a_model_change_never_serves_the_old_vector():
    """The key carries provider/model/dims, so re-pointing the model centrally must
    miss rather than hand back a vector from a different embedding space."""
    api = _load()
    cfg = {"embedding_provider": "p1", "embedding_model": "m1", "embedding_dims": 3}
    calls = _stub_provider(api, config=cfg)
    await api.embed_query_cached("a stable query string")
    assert len(calls) == 1

    cfg["embedding_model"] = "m2"  # operator repoints the model
    await api.embed_query_cached("a stable query string")
    assert len(calls) == 2, "a model change must not be served from cache"


@pytest.mark.asyncio
async def test_failures_are_not_cached():
    """A provider blip must not pin a null for every later caller."""
    api = _load()
    calls = _stub_provider(api, vector=None)
    assert await api.embed_query_cached("query during an outage") is None
    assert await api.embed_query_cached("query during an outage") is None
    assert len(calls) == 2, "a None result was cached, so recovery would be invisible"


@pytest.mark.asyncio
async def test_the_cache_is_bounded():
    """The bound is the point. Keyed on caller-supplied text, an unbounded cache is a
    memory-exhaustion path — the same class of mistake as capping CSV rows but not
    columns."""
    api = _load()
    _stub_provider(api)
    limit = api._QUERY_EMBED_CACHE_MAX
    for i in range(limit + 50):
        await api.embed_query_cached(f"distinct query number {i}")
    assert len(api._QUERY_EMBED_CACHE) <= limit


@pytest.mark.asyncio
async def test_eviction_is_least_recently_used():
    api = _load()
    calls = _stub_provider(api)
    limit = api._QUERY_EMBED_CACHE_MAX
    first = "the very first query text"
    await api.embed_query_cached(first)
    # Keep `first` warm while filling the cache past its limit.
    for i in range(limit - 1):
        await api.embed_query_cached(f"filler query number {i}")
        await api.embed_query_cached(first)
    before = len(calls)
    await api.embed_query_cached(first)
    assert len(calls) == before, "the most recently used entry was evicted"


@pytest.mark.asyncio
async def test_short_queries_never_reach_the_provider():
    """Mirrors embed_text's own 10-character floor, so the cache cannot disagree with
    the function it fronts."""
    api = _load()
    calls = _stub_provider(api)
    assert await api.embed_query_cached("hi") is None
    assert await api.embed_query_cached("   ") is None
    assert calls == []
