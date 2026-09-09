"""Real search/cache/provider behavior with synthetic SQL and HTTP transport.

Initial bounded matrix for SEARCH_DEGRADATION_PLAN.md at 3810fe3689: paired
fault classes, not a claim of every provider/fault permutation or native proof.
No provider helper is replaced. MockTransport prevents actual DNS/IP requests;
run with the separate cleared-environment/no-network/no-.env audit wrapper.
"""

import asyncio
from collections import Counter
import importlib.util
import json
from pathlib import Path

import httpx
import pytest
import pytest_asyncio


API_PATH = Path(__file__).resolve().parents[1] / "main.py"
REAL_CLIENT = httpx.AsyncClient
QUERY = "synthetic provider outage query"
SECRET = "synthetic-provider-error-and-key-secret"
ROWS = [
    {"id": "11111111-1111-4111-8111-111111111111", "text": "First lexical document",
     "source": "knowledge", "score": 0.9},
    {"id": "22222222-2222-4222-8222-222222222222", "text": "Second distinct lexical document",
     "source": "knowledge", "score": 0.8},
]


class LexicalConnection:
    def __init__(self, rig):
        self.rig = rig

    async def fetch(self, sql, *_args):
        assert sql.lstrip().startswith(("SELECT", "WITH"))
        return [dict(row) for row in ROWS[:self.rig.row_count]] if "ts_rank_cd" in sql else []

    async def fetchrow(self, *_args):
        raise AssertionError("non-ID shared-hall search unexpectedly used fetchrow")

    async def execute(self, sql, *_args):
        # Actual search's selection telemetry stays a synthetic no-op.
        assert sql.startswith(("SET statement_timeout", "UPDATE knowledge SET times_selected"))
        return "UPDATE 0"


class SearchRig:
    def __init__(self, api):
        self.api = api
        self.provider = "local"
        self.row_count = 2
        self.config = {}
        self.key = SECRET
        self.faults = {}
        self.calls = Counter()
        self.block_stage = None
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.active_http_tasks = set()
        self.requests = []

    def select(self, provider):
        self.provider = provider
        self.config = {
            "embedding_provider": provider,
            "embedding_model": self.api.LOCAL_EMBED_MODEL if provider == "local" else "synthetic/embed",
            "embedding_dims": self.api.LOCAL_EMBED_DIMS if provider == "local" else 3,
            "rerank_provider": provider,
            "rerank_model": self.api.LOCAL_RERANK_MODEL if provider == "local" else "synthetic/rerank",
            "rerank_enabled": True, "embed_timeout_ms": 100, "rerank_timeout_ms": 100,
        }

    async def load_config(self, force=False):
        return dict(self.config)

    async def provider_key(self, provider):
        assert self.provider != "local", "local transport attempted external key resolution"
        assert provider == self.provider
        return self.key

    def good_payload(self, stage):
        if stage == "embedding":
            vector = [0.1] * self.config["embedding_dims"]
            return ({"model": self.config["embedding_model"], "dim": len(vector), "vectors": [vector]}
                    if self.provider == "local" else {"data": [{"embedding": vector}]})
        rows = [{"index": 1, "relevance_score": 0.9}, {"index": 0, "relevance_score": 0.8}]
        return {"model": self.config["rerank_model"], "results": rows}

    async def handle(self, request):
        assert request.method == "POST"
        stage = "embedding" if request.url.path.endswith(("/embed", "/embeddings")) else "rerank"
        expected = (f"http://worker.invalid/{'embed' if stage == 'embedding' else 'rerank'}"
                    if self.provider == "local" else
                    f"https://openrouter.ai/api/v1/{'embeddings' if stage == 'embedding' else 'rerank'}")
        assert str(request.url) == expected
        assert request.headers.get("authorization") == (None if self.provider == "local" else f"Bearer {SECRET}")
        body = json.loads(request.content)
        if stage == "rerank":
            assert body["documents"] == [row["text"] for row in ROWS]
        self.calls[stage] += 1
        if self.block_stage == stage:
            task = asyncio.current_task()
            self.active_http_tasks.add(task)
            self.entered.set()
            try:
                await asyncio.wait_for(self.release.wait(), timeout=0.75)
            finally:
                self.active_http_tasks.discard(task)
        fault = self.faults.get(stage)
        if fault == "timeout":
            raise httpx.ReadTimeout(SECRET, request=request)
        if fault == "connection":
            raise httpx.ConnectError(SECRET, request=request)
        if fault == "invalid-json":
            return httpx.Response(200, content=("{" + SECRET).encode(), request=request)
        payload = self.good_payload(stage)
        if fault == "invalid-payload":
            payload = {"error": SECRET}
        elif fault == "http":
            payload = {"error": SECRET}
        return httpx.Response(503 if fault in {"http", "http-valid"} else 200,
                              json=payload, request=request)

    async def search(self, *, query=QUERY, rerank=True):
        async with asyncio.timeout(1):
            return await self.api.execute_search(
                LexicalConnection(self), "synthetic-search-project", query,
                search_type="all", hall="shared", graph=False, rerank=rerank, limit=8,
            )

    def launch(self, **kwargs):
        task = asyncio.create_task(self.search(**kwargs))
        self.requests.append(task)
        return task


@pytest_asyncio.fixture
async def rig(monkeypatch):
    spec = importlib.util.spec_from_file_location("cortex_search_degradation_test", API_PATH)
    assert spec and spec.loader
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    state = SearchRig(api)
    state.select("local")
    transport = httpx.MockTransport(state.handle)
    monkeypatch.setattr(api, "load_cortex_platform_config_cached", state.load_config)
    monkeypatch.setattr(api, "resolve_provider_key", state.provider_key)
    monkeypatch.setattr(api, "EMBED_WORKER_URL", "http://worker.invalid")
    monkeypatch.setattr(api, "SEARCH_TOTAL_BUDGET_MS", 500)
    monkeypatch.setattr(api.httpx, "AsyncClient", lambda **kw: REAL_CLIENT(
        **{**kw, "transport": transport, "trust_env": False}))
    try:
        yield state
    finally:
        state.release.set()
        pending = set(state.requests) | state.active_http_tasks
        async with asyncio.timeout(1):
            await asyncio.gather(*pending, return_exceptions=True)


def assert_search(result, degraded, *, count=2, reranked=False):
    assert sorted(result["degraded"]) == sorted(degraded)
    assert SECRET not in json.dumps(result["degraded"])
    assert result["reranked"] is reranked
    expected = ROWS[:count][::-1] if reranked else ROWS[:count]
    assert [row["id"] for row in result["results"]] == [row["id"] for row in expected]
    assert [row["text"] for row in result["results"]] == [row["text"] for row in expected]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider,stage,fault", [
    ("local", "embedding", "timeout"), ("openrouter", "embedding", "timeout"),
    ("local", "rerank", "timeout"), ("openrouter", "rerank", "timeout"),
    ("local", "embedding", "connection"), ("openrouter", "rerank", "connection"),
    ("local", "rerank", "http"), ("openrouter", "embedding", "http-valid"),
    ("local", "embedding", "invalid-json"), ("openrouter", "rerank", "invalid-json"),
    ("local", "rerank", "invalid-payload"), ("openrouter", "embedding", "invalid-payload"),
])
async def test_attempted_provider_failure_is_labelled_and_keeps_lexical_rows(rig, provider, stage, fault):
    rig.select(provider)
    rig.faults[stage] = fault
    result = await rig.search(rerank=stage == "rerank")
    assert rig.calls[stage] == 1, "the actual selected provider transport was not reached"
    assert_search(result, [stage])


@pytest.mark.asyncio
@pytest.mark.parametrize("provider,control", [
    ("local", "healthy"), ("openrouter", "healthy"),
    ("local", "disabled"), ("openrouter", "disabled"),
    ("local", "short"), ("openrouter", "short"),
    ("openrouter", "unconfigured"), ("local", "one-document"),
])
async def test_deliberate_nonattempt_and_healthy_search_are_not_outages(rig, provider, control):
    rig.select(provider)
    if control == "disabled":
        rig.config["rerank_enabled"] = False
    if control == "unconfigured":
        rig.key = None
    if control == "one-document":
        rig.row_count = 1
    result = await rig.search(query="short" if control == "short" else QUERY)
    do_rerank = control in {"healthy", "short"}
    assert_search(result, [], count=rig.row_count, reranked=do_rerank)
    assert rig.calls["embedding"] == (0 if control in {"short", "unconfigured"} else 1)
    assert rig.calls["rerank"] == int(do_rerank)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["local", "openrouter"])
@pytest.mark.parametrize("stage", ["embedding", "rerank"])
async def test_same_query_retries_failure_and_recovers_without_stale_warning(rig, provider, stage):
    rig.select(provider)
    rig.faults[stage] = "timeout"
    for attempt in (1, 2):
        result = await rig.search(rerank=stage == "rerank")
        assert rig.calls[stage] == attempt, "a failed provider outcome was cached"
        assert_search(result, [stage])
    rig.faults.clear()
    result = await rig.search(rerank=stage == "rerank")
    assert rig.calls[stage] == 3
    assert_search(result, [], reranked=stage == "rerank")
    if stage == "embedding":
        assert_search(await rig.search(rerank=False), [])
        assert rig.calls[stage] == 3, "successful query embedding no longer caches"


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["local", "openrouter"])
async def test_shared_failed_embedding_reaches_surviving_waiters_after_first_cancels(rig, provider):
    rig.select(provider)
    rig.block_stage = "embedding"
    rig.faults["embedding"] = "timeout"
    # Queue all callers before the first provider gate; the cache itself must
    # establish single-flight ownership. No provider/cache helper is replaced.
    tasks = [rig.launch(rerank=False) for _ in range(3)]
    await asyncio.wait_for(rig.entered.wait(), timeout=0.5)
    assert rig.calls["embedding"] == 1
    tasks[0].cancel()
    with pytest.raises(asyncio.CancelledError):
        await tasks[0]
    rig.release.set()
    survivors = await asyncio.wait_for(asyncio.gather(*tasks[1:]), timeout=0.5)
    assert rig.calls["embedding"] == 1
    for result in survivors:
        assert_search(result, ["embedding"])
    rig.faults.clear()
    assert_search(await rig.search(rerank=False), [])
    assert rig.calls["embedding"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["local", "openrouter"])
async def test_legacy_optional_helpers_still_return_none_on_transport_failure(rig, provider):
    rig.select(provider)
    rig.faults = {"embedding": "timeout", "rerank": "timeout"}
    assert await rig.api._embed_text_with_config(QUERY, dict(rig.config)) is None
    assert await rig.api.embed_text(QUERY) is None
    assert await rig.api.rerank_results(QUERY, [row["text"] for row in ROWS]) is None
    assert rig.calls == {"embedding": 2, "rerank": 1}


@pytest.mark.asyncio
@pytest.mark.parametrize("provider,stage", [("local", "embedding"), ("openrouter", "rerank")])
async def test_outer_search_deadline_preserves_lexical_results(rig, monkeypatch, provider, stage):
    rig.select(provider)
    rig.block_stage = stage
    monkeypatch.setattr(rig.api, "SEARCH_TOTAL_BUDGET_MS", 30)
    result = await rig.search(rerank=stage == "rerank")
    assert rig.entered.is_set(), "deadline test never entered its provider gate"
    assert rig.calls[stage] == 1
    assert stage in result["degraded"]
    assert SECRET not in json.dumps(result["degraded"])
    assert [row["id"] for row in result["results"]] == [row["id"] for row in ROWS]
    rig.release.set()
