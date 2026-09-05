"""Cortex's appliance-local embedding/rerank transport contract.

The selected ``local`` provider is deliberately keyless and internal. Existing
external providers retain their canonical OpenKai credential path.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

import pytest


API_MAIN_PATH = Path(__file__).resolve().parents[1] / "main.py"


def load_module(name: str):
    spec = importlib.util.spec_from_file_location(name, API_MAIN_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    api_dir = str(API_MAIN_PATH.parent)
    sys.path.insert(0, api_dir)
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(api_dir)
    return module


class FakeResponse:
    def __init__(self, payload, *, content: bytes | None = None):
        self.payload = payload
        self.content = content if content is not None else json.dumps(payload).encode()

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def aiter_bytes(self):
        yield self.content


class FakeClient:
    def __init__(self, response: FakeResponse, calls: list[dict], **kwargs):
        self.response = response
        self.calls = calls
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def post(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs, "client": self.kwargs})
        return self.response

    def stream(self, method, url, **kwargs):
        assert method == "POST"
        self.calls.append({"url": url, **kwargs, "client": self.kwargs})
        return self.response


def arm_client(module, monkeypatch, response: FakeResponse):
    calls: list[dict] = []
    monkeypatch.setattr(
        module.httpx,
        "AsyncClient",
        lambda **kwargs: FakeClient(response, calls, **kwargs),
    )
    return calls


@pytest.mark.asyncio
async def test_local_provider_is_configured_without_resolving_any_key(monkeypatch):
    module = load_module("cortex_api_local_provider_keyless_test")

    async def forbidden(_provider):
        pytest.fail("the local provider must never resolve an external credential")

    monkeypatch.setattr(module, "resolve_provider_key", forbidden)
    embedding = {
        "embedding_provider": "local",
        "embedding_model": module.LOCAL_EMBED_MODEL,
        "embedding_dims": 768,
    }
    rerank = {
        "rerank_provider": "local",
        "rerank_model": module.LOCAL_RERANK_MODEL,
    }

    assert await module.provider_configured(embedding, "embedding") is True
    assert await module.provider_configured(rerank, "rerank") is True
    assert (
        await module.provider_configured(
            {**embedding, "embedding_dims": 1024}, "embedding"
        )
        is False
    )


@pytest.mark.asyncio
async def test_local_embedding_uses_internal_worker_without_auth_header(monkeypatch):
    module = load_module("cortex_api_local_embedding_transport_test")
    vector = [float(index) for index in range(module.LOCAL_EMBED_DIMS)]
    calls = arm_client(
        module,
        monkeypatch,
        FakeResponse(
            {
                "model": module.LOCAL_EMBED_MODEL,
                "dim": module.LOCAL_EMBED_DIMS,
                "vectors": [vector],
            }
        ),
    )

    async def forbidden(_provider):
        pytest.fail("the local embedding call must never resolve a provider key")

    monkeypatch.setattr(module, "resolve_provider_key", forbidden)
    result = await module._embed_text_with_config(
        "local embedding request",
        {
            "embedding_provider": "local",
            "embedding_model": module.LOCAL_EMBED_MODEL,
            "embedding_dims": 768,
            "embed_input_max_chars": 500,
            "embed_timeout_ms": 15000,
        },
    )

    assert result == vector
    assert calls == [
        {
            "url": f"{module.EMBED_WORKER_URL}/embed",
            "json": {
                "model": module.LOCAL_EMBED_MODEL,
                "texts": ["local embedding request"],
            },
            "client": {"timeout": 15.0},
        }
    ]
    assert "headers" not in calls[0]


@pytest.mark.asyncio
async def test_local_rerank_is_keyless_and_caps_documents_and_output(monkeypatch):
    module = load_module("cortex_api_local_rerank_transport_test")
    calls = arm_client(
        module,
        monkeypatch,
        FakeResponse(
            {
                "model": module.LOCAL_RERANK_MODEL,
                "results": [
                    {"index": 63, "relevance_score": 9.0},
                    {"index": 0, "relevance_score": 1.0},
                ],
            }
        ),
    )

    async def forbidden(_provider):
        pytest.fail("the local rerank call must never resolve a provider key")

    async def config(force: bool = False):
        return {
            "rerank_enabled": True,
            "rerank_provider": "local",
            "rerank_model": module.LOCAL_RERANK_MODEL,
            "rerank_input_max_chars": 500,
            "rerank_timeout_ms": 2500,
        }

    monkeypatch.setattr(module, "resolve_provider_key", forbidden)
    monkeypatch.setattr(module, "load_cortex_platform_config_cached", config)
    documents = [f"document {index}" for index in range(100)]

    result = await module.rerank_results("bounded local rerank", documents, top_n=2)

    assert result == [
        {"index": 63, "relevance_score": 9.0},
        {"index": 0, "relevance_score": 1.0},
    ]
    assert calls[0]["url"] == f"{module.EMBED_WORKER_URL}/rerank"
    assert calls[0]["json"]["documents"] == documents[:64]
    assert calls[0]["json"]["top_n"] == 2
    assert "headers" not in calls[0]


@pytest.mark.asyncio
async def test_local_transport_enforces_utf8_byte_bounds_before_worker(monkeypatch):
    module = load_module("cortex_api_local_utf8_bounds_test")
    vector = [0.0] * module.LOCAL_EMBED_DIMS
    calls = arm_client(
        module,
        monkeypatch,
        FakeResponse(
            {
                "model": module.LOCAL_EMBED_MODEL,
                "dim": module.LOCAL_EMBED_DIMS,
                "vectors": [vector],
            }
        ),
    )
    text = "\u20ac" * module.LOCAL_SEARCH_MAX_TEXT_BYTES

    result = await module._embed_text_with_config(
        text,
        {
            "embedding_provider": "local",
            "embedding_model": module.LOCAL_EMBED_MODEL,
            "embedding_dims": module.LOCAL_EMBED_DIMS,
            "embed_input_max_chars": len(text),
        },
    )

    sent = calls[0]["json"]["texts"][0]
    assert result == vector
    assert len(sent.encode("utf-8")) <= module.LOCAL_SEARCH_MAX_TEXT_BYTES
    assert sent == "\u20ac" * (module.LOCAL_SEARCH_MAX_TEXT_BYTES // 3)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("documents", "top_n"),
    [
        (["one", ""], 1),
        (["one", "two"], 0),
        (["one", "two"], True),
        (["\u20ac" * 16000] * 17, 2),
    ],
)
async def test_local_rerank_invalid_bounds_fail_without_worker_call(
    monkeypatch, documents, top_n
):
    module = load_module("cortex_api_local_rerank_input_contract_test")

    async def config(force: bool = False):
        return {
            "rerank_enabled": True,
            "rerank_provider": "local",
            "rerank_model": module.LOCAL_RERANK_MODEL,
            "rerank_input_max_chars": 100_000,
        }

    async def forbidden(*_args, **_kwargs):
        pytest.fail("invalid local input must fail before worker transport")

    monkeypatch.setattr(module, "load_cortex_platform_config_cached", config)
    monkeypatch.setattr(module, "_post_local_search_worker", forbidden)

    assert await module.rerank_results("query", documents, top_n=top_n) is None


@pytest.mark.asyncio
async def test_external_embedding_keeps_openkai_key_and_provider_transport(monkeypatch):
    module = load_module("cortex_api_external_embedding_regression_test")
    calls = arm_client(
        module,
        monkeypatch,
        FakeResponse({"data": [{"embedding": [0.1, 0.2, 0.3]}]}),
    )

    async def provider_key(provider):
        assert provider == "openrouter"
        return "unit-test-provider-key"

    monkeypatch.setattr(module, "resolve_provider_key", provider_key)
    result = await module._embed_text_with_config(
        "external embedding request",
        {
            "embedding_provider": "openrouter",
            "embedding_model": "external/model",
            "embedding_dims": 3,
            "embed_input_max_chars": 500,
            "embed_timeout_ms": 15000,
        },
    )

    assert result == [0.1, 0.2, 0.3]
    assert calls[0]["url"] == "https://openrouter.ai/api/v1/embeddings"
    assert calls[0]["headers"]["Authorization"] == "Bearer unit-test-provider-key"
    assert calls[0]["json"] == {
        "model": "external/model",
        "input": "external embedding request",
        "dimensions": 3,
    }


@pytest.mark.asyncio
async def test_external_rerank_keeps_openkai_key_and_provider_transport(monkeypatch):
    module = load_module("cortex_api_external_rerank_regression_test")
    calls = arm_client(
        module,
        monkeypatch,
        FakeResponse(
            {
                "results": [
                    {"index": 1, "relevance_score": 2.0},
                    {"index": 0, "relevance_score": 1.0},
                ]
            }
        ),
    )

    async def provider_key(provider):
        assert provider == "openrouter"
        return "unit-test-provider-key"

    async def config(force: bool = False):
        return {
            "rerank_enabled": True,
            "rerank_provider": "openrouter",
            "rerank_model": "external/reranker",
            "rerank_input_max_chars": 500,
            "rerank_timeout_ms": 2500,
        }

    monkeypatch.setattr(module, "resolve_provider_key", provider_key)
    monkeypatch.setattr(module, "load_cortex_platform_config_cached", config)
    result = await module.rerank_results(
        "external rerank request",
        ["first document", "second document"],
        top_n=2,
    )

    assert result == [
        {"index": 1, "relevance_score": 2.0},
        {"index": 0, "relevance_score": 1.0},
    ]
    assert calls == [
        {
            "url": "https://openrouter.ai/api/v1/rerank",
            "headers": {
                "Authorization": "Bearer unit-test-provider-key",
                "Content-Type": "application/json",
            },
            "json": {
                "model": "external/reranker",
                "query": "external rerank request",
                "documents": ["first document", "second document"],
                "top_n": 2,
            },
            "client": {"timeout": 2.5},
        }
    ]


@pytest.mark.asyncio
async def test_local_worker_response_size_and_vector_shape_fail_closed(monkeypatch):
    module = load_module("cortex_api_local_worker_output_bound_test")
    calls = arm_client(
        module,
        monkeypatch,
        FakeResponse(
            {"model": module.LOCAL_EMBED_MODEL, "dim": 768, "vectors": [[1.0]]},
            content=b"x" * (module.LOCAL_SEARCH_MAX_RESPONSE_BYTES + 1),
        ),
    )

    result = await module._embed_text_with_config(
        "bounded worker response",
        {
            "embedding_provider": "local",
            "embedding_model": module.LOCAL_EMBED_MODEL,
            "embedding_dims": 768,
        },
    )

    assert result is None
    assert len(calls) == 1


@pytest.mark.parametrize(
    "data",
    [
        {"model": "wrong", "dim": 768, "vectors": [[0.0] * 768]},
        {"model": "m", "dim": 768, "vectors": [[0.0] * 767]},
        {"model": "m", "dim": 768, "vectors": [[0.0] * 767 + [float("nan")]]},
        {"model": "m", "dim": 768, "vectors": [[0.0] * 767 + [True]]},
    ],
)
def test_local_embedding_response_contract_rejects_drift(data):
    module = load_module("cortex_api_local_embedding_output_contract_test")

    assert module._extract_local_embedding(data, model="m", dims=768) is None


@pytest.mark.parametrize(
    "rows",
    [
        [
            {"index": 0, "relevance_score": 2.0},
            {"index": 0, "relevance_score": 1.0},
        ],
        [{"index": 0, "relevance_score": 2.0}],
        [
            {"index": 0, "relevance_score": 1.0},
            {"index": 1, "relevance_score": 2.0},
        ],
    ],
)
def test_local_rerank_response_contract_rejects_drift(rows):
    module = load_module("cortex_api_local_rerank_output_contract_test")
    response = {"model": module.LOCAL_RERANK_MODEL, "results": rows}

    assert (
        module._extract_local_rerank_results(
            response,
            model=module.LOCAL_RERANK_MODEL,
            document_count=2,
            top_n=2,
        )
        is None
    )
