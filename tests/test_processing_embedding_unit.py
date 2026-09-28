"""Embedding provider unit tests: deterministic local hashing, the
OpenAI-compatible and Ollama adapters against injected fake transports, every
typed outcome mapping, shared output validation and routing selection.

Pure unit tests: no network, no database, no real provider calls. Remote
adapters are exercised through an injected fake ``HttpTransport``. Spaces
record the provider *family* ('hash-local' | 'openai-compatible' | 'ollama'),
never the versioned adapter id (F07/R24).
"""

from __future__ import annotations

import asyncio
import itertools
import json
import math
import time
import uuid

import pytest

from cortex_v2.processing.contracts import (
    EmbeddingProvider,
    Outcome,
    ParserLimits,
    ProviderCapabilities,
    SpaceContract,
)
from cortex_v2.processing.embedding import (
    PROVIDER_FAMILIES,
    PURPOSE_DOCUMENT,
    PURPOSE_QUERY,
    HashEmbeddingProvider,
    OllamaEmbeddingProvider,
    OpenAICompatibleEmbeddingProvider,
    hash_vector,
    select_provider,
    validate_provider_output,
)
from cortex_v2.processing.providers import (
    EgressDenied,
    HttpRequest,
    HttpResponse,
    TransportConnectionError,
    TransportTimeout,
)
from cortex_v2.processing.settings import (
    LocalInferenceSettings,
    LocalModelManifest,
    ProcessingSettings,
    ProviderRoleSettings,
    WorkerSettings,
)

SPACE_ID = uuid.UUID("2f6a0f4e-1c3b-5d7a-9e2f-4a6b8c0d1e3f")


def run(coroutine):
    return asyncio.run(coroutine)


def ahead(seconds: float = 30.0) -> float:
    return time.monotonic() + seconds


def embed_docs(provider, space, texts):
    """Run embed_documents with a fresh deadline."""
    return run(provider.embed_documents(space, texts, deadline=ahead()))


def make_space(dimensions=8, normalization="l2", **overrides) -> SpaceContract:
    base = dict(
        space_id=SPACE_ID,
        space_name="unit",
        provider="openai-compatible",
        model_id="embed-1",
        model_revision="rev-1",
        dimensions=dimensions,
        normalization=normalization,
        metric="cosine",
    )
    base.update(overrides)
    return SpaceContract(**base)


def make_role(**overrides) -> ProviderRoleSettings:
    base = dict(
        role="embedding",
        enabled=True,
        provider_kind="openai-compatible",
        base_url="https://api.embedding.test/v1",
        model="embed-1",
        credential_env="CORTEX_V2_EMBEDDING_API_KEY",
        credential_file=None,
        connect_timeout_seconds=2.0,
        read_timeout_seconds=5.0,
        max_batch=8,
        max_input_chars=1024,
        egress_allowlist=("api.embedding.test",),
        allow_private_for=(),
    )
    base.update(overrides)
    return ProviderRoleSettings(**base)


def make_ollama_role(**overrides) -> ProviderRoleSettings:
    base = dict(
        provider_kind="ollama",
        base_url="http://127.0.0.1:11434",
        model="nomic-embed-text",
        credential_env=None,
        egress_allowlist=("127.0.0.1",),
        allow_private_for=("127.0.0.1",),
    )
    base.update(overrides)
    return make_role(**base)


INSTALLED_MODEL = LocalModelManifest(
    model_id="nomic-embed-text",
    digest="sha256:9f2c",
    license="apache-2.0",
    platform="linux/arm64",
    status="installed",
    dimensions=None,
)


def manifest(model_id: str, status: str) -> LocalModelManifest:
    return LocalModelManifest(
        model_id, "sha256:aa", "apache-2.0", "linux/arm64", status, None
    )


def make_local(models=(INSTALLED_MODEL,), routing_policy="remote_allowed"):
    return LocalInferenceSettings(
        routing_policy=routing_policy,
        models=tuple(models),
        max_resident_models=2,
        concurrency_cpu=4,
        concurrency_gpu=0,
        batch_limit=16,
        idle_unload_seconds=300,
        offline=False,
    )


def make_worker() -> WorkerSettings:
    return WorkerSettings(
        role="embed",
        worker_id="unit-1",
        database_url_file="CORTEX_V2_DATABASE_URL_FILE",
        principal_id_file="CORTEX_V2_WORKER_PRINCIPAL_ID_FILE",
        executor_roles=("core",),
        handler_modules=(),
        poll_interval_seconds=1.0,
        lease_seconds=60,
        heartbeat_interval_seconds=10.0,
        attempt_deadline_seconds=300.0,
        max_concurrent_attempts=2,
        thread_pool_size=4,
        backoff_base_seconds=1.0,
        backoff_cap_seconds=30.0,
        queue_admission_limit=100,
        reap_interval_seconds=15.0,
        blob_root=None,
    )


def make_settings(embedding=None, *, routing_policy="remote_allowed", models=()):
    return ProcessingSettings(
        worker=make_worker(),
        providers=(
            embedding if embedding is not None else make_role(),
            ProviderRoleSettings(role="rerank"),
            ProviderRoleSettings(role="analysis"),
        ),
        local=make_local(models, routing_policy),
        parser_limits=ParserLimits(),
    )


def ollama_space(dimensions=8) -> SpaceContract:
    return make_space(
        dimensions=dimensions, provider="ollama", model_id="nomic-embed-text"
    )


class FakeTransport:
    """Injected ``HttpTransport`` spy: records requests, replays one result."""

    def __init__(self, response=None, error=None):
        self.requests: list[HttpRequest] = []
        self._response = response
        self._error = error

    async def __call__(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        if self._error is not None:
            raise self._error
        return self._response


def json_response(payload, status=200, headers=None) -> HttpResponse:
    merged = {"content-type": "application/json"}
    merged.update(headers or {})
    body = json.dumps(payload).encode()
    return HttpResponse(status=status, headers=merged, body=body)


def openai_payload(vectors, model="embed-1", **extra):
    payload = {
        "object": "list",
        "model": model,
        "data": [
            {"index": position, "embedding": list(vector), "object": "embedding"}
            for position, vector in enumerate(vectors)
        ],
        "usage": {"prompt_tokens": len(vectors), "total_tokens": len(vectors)},
    }
    payload.update(extra)
    return payload


def ollama_payload(vectors, model="nomic-embed-text", load_duration=0):
    return {
        "model": model,
        "embeddings": [list(vector) for vector in vectors],
        "load_duration": load_duration,
        "prompt_eval_count": len(vectors),
    }


def openai_provider(response=None, error=None, role=None, credential="test-key"):
    transport = FakeTransport(response, error)
    provider = OpenAICompatibleEmbeddingProvider(
        role if role is not None else make_role(),
        transport=transport,
        credential=credential,
    )
    return provider, transport


def ollama_provider(response=None, error=None, role=None, local=None, credential=None):
    transport = FakeTransport(response, error)
    provider = OllamaEmbeddingProvider(
        role if role is not None else make_ollama_role(),
        local=local if local is not None else make_local(),
        transport=transport,
        credential=credential,
    )
    return provider, transport


# ---------------------------------------------------------------------------
# HashEmbeddingProvider: determinism, separation, dimensions, normalization
# ---------------------------------------------------------------------------


def test_hash_provider_is_deterministic_across_instances():
    space = make_space(dimensions=64, provider="hash-local")
    first = embed_docs(HashEmbeddingProvider(), space, ("alpha", "beta"))
    second = embed_docs(HashEmbeddingProvider(), space, ("alpha", "beta"))
    assert first.ok and second.ok
    assert first.vectors == second.vectors
    assert all(isinstance(value, float) for value in first.vectors[0])


def test_hash_vectors_separate_different_texts():
    space = make_space(dimensions=256, provider="hash-local")
    texts = ("alpha", "beta", "gamma", "alpha beta")
    outcome = embed_docs(HashEmbeddingProvider(), space, texts)
    assert outcome.ok
    vectors = outcome.vectors
    assert len(set(vectors)) == len(texts)
    for i, j in itertools.combinations(range(len(texts)), 2):
        cosine = sum(a * b for a, b in zip(vectors[i], vectors[j], strict=True))
        assert abs(cosine) < 0.3  # l2-normalized: near-orthogonal separation


@pytest.mark.parametrize("dimensions", [1, 7, 100, 1536])
def test_hash_vector_has_exact_dimension_count_and_unit_norm(dimensions):
    space = make_space(dimensions=dimensions, provider="hash-local")
    outcome = embed_docs(HashEmbeddingProvider(), space, ("text",))
    assert outcome.ok
    vector = outcome.vectors[0]
    assert len(vector) == dimensions
    norm = math.sqrt(sum(value * value for value in vector))
    assert abs(norm - 1.0) < 1e-9


def test_hash_values_bounded_and_unnormalized_when_normalization_is_none():
    space = make_space(dimensions=32, normalization="none", provider="hash-local")
    outcome = embed_docs(HashEmbeddingProvider(), space, ("raw",))
    vector = outcome.vectors[0]
    assert all(-1.0 <= value < 1.0 for value in vector)
    norm = math.sqrt(sum(value * value for value in vector))
    assert abs(norm - 1.0) > 1e-6


@pytest.mark.parametrize("dimensions", [0, -3, 4097, 100_000])
def test_hash_provider_rejects_impossible_dimensions(dimensions):
    space = make_space(dimensions=dimensions, provider="hash-local")
    outcome = embed_docs(HashEmbeddingProvider(), space, ("text",))
    assert outcome.outcome is Outcome.PROVIDER_REJECTED
    assert outcome.vectors == ()
    assert "1..4096" in outcome.detail


def test_hash_query_and_document_purposes_diverge():
    provider = HashEmbeddingProvider()
    space = make_space(dimensions=64, provider="hash-local")
    docs = run(provider.embed_documents(space, ("same text",), deadline=ahead()))
    query = run(provider.embed_query(space, "same text", deadline=ahead()))
    assert docs.ok and query.ok
    assert docs.vectors[0] != query.vectors[0]


def test_hash_query_applies_space_query_prefix():
    space = make_space(dimensions=64, provider="hash-local", query_prefix="query: ")
    outcome = run(HashEmbeddingProvider().embed_query(space, "hello", deadline=ahead()))
    assert outcome.vectors == (hash_vector(space, PURPOSE_QUERY, "query: hello"),)


def test_hash_documents_apply_document_prefix():
    space = make_space(dimensions=16, provider="hash-local", document_prefix="doc|")
    outcome = embed_docs(HashEmbeddingProvider(), space, ("hello",))
    assert outcome.vectors == (hash_vector(space, PURPOSE_DOCUMENT, "doc|hello"),)


def test_hash_empty_batch_is_input_rejected():
    space = make_space(provider="hash-local")
    outcome = run(HashEmbeddingProvider().embed_documents(space, (), deadline=ahead()))
    assert outcome.outcome is Outcome.INPUT_REJECTED
    assert outcome.vectors == ()


def test_hash_capabilities_declare_local_offline():
    provider = HashEmbeddingProvider()
    caps = provider.capabilities()
    assert isinstance(caps, ProviderCapabilities)
    assert caps.provider_id == "hash-local@1"
    assert provider.family == "hash-local"
    assert caps.locality == "local"
    assert caps.offline is True
    assert caps.roles == ("embedding",)


def test_hash_provider_satisfies_embedding_port():
    assert isinstance(HashEmbeddingProvider(), EmbeddingProvider)


def test_provider_families_match_versioned_provider_ids():
    # The create-space advisory and select_provider's family check both rely
    # on provider_id prefixes staying equal to the canonical families.
    assert PROVIDER_FAMILIES == ("hash-local", "openai-compatible", "ollama")
    adapters = (
        HashEmbeddingProvider(),
        OpenAICompatibleEmbeddingProvider(make_role(), transport=FakeTransport(None)),
        OllamaEmbeddingProvider(
            make_ollama_role(), local=make_local(), transport=FakeTransport(None)
        ),
    )
    for provider, family in zip(adapters, PROVIDER_FAMILIES, strict=True):
        assert provider.family == family
        assert provider.provider_id.split("@", 1)[0] == family


# ---------------------------------------------------------------------------
# validate_provider_output (shared contract)
# ---------------------------------------------------------------------------


def test_validate_provider_output_accepts_matching_vectors():
    space = make_space(dimensions=3)
    vectors = ((0.1, 0.2, 0.3), (1.0, 0.0, -1.0))
    valid = validate_provider_output(space, ("a", "b"), vectors)
    assert valid is None


def test_validate_provider_output_rejects_count_dimension_and_finiteness():
    space = make_space(dimensions=3)
    rejected = Outcome.PROVIDER_REJECTED
    assert validate_provider_output(space, ("a", "b"), ((0.1, 0.2, 0.3),)) is rejected
    assert validate_provider_output(space, ("a",), ((0.1, 0.2),)) is rejected
    nan_vector = ((0.1, float("nan"), 0.3),)
    inf_vector = ((0.1, float("inf"), 0.3),)
    assert validate_provider_output(space, ("a",), nan_vector) is rejected
    assert validate_provider_output(space, ("a",), inf_vector) is rejected


# ---------------------------------------------------------------------------
# OpenAICompatibleEmbeddingProvider against a fake transport
# ---------------------------------------------------------------------------


def test_openai_happy_path_posts_model_and_input_without_dimensions():
    vectors = [[0.25] * 8, [0.5] * 8]
    provider, transport = openai_provider(json_response(openai_payload(vectors)))
    outcome = embed_docs(provider, make_space(dimensions=8), ("alpha", "beta"))
    assert outcome.ok
    assert outcome.vectors == (tuple(vectors[0]), tuple(vectors[1]))
    assert outcome.model_revision == "embed-1"
    assert outcome.usage == {"prompt_tokens": 2, "total_tokens": 2}
    request = transport.requests[0]
    assert request.method == "POST"
    assert request.url == "https://api.embedding.test/v1/embeddings"
    assert request.headers["Authorization"] == "Bearer test-key"
    assert request.headers["Content-Type"] == "application/json"
    # R24: the adapter must never ask the server to reinterpret dimensions.
    assert json.loads(request.body) == {"model": "embed-1", "input": ["alpha", "beta"]}
    assert 0 < request.timeout <= 5.0


def test_openai_rejects_index_order_mismatch():
    payload = openai_payload([[0.25] * 8, [0.5] * 8])
    payload["data"][0]["index"], payload["data"][1]["index"] = 1, 0
    provider, _ = openai_provider(json_response(payload))
    outcome = embed_docs(provider, make_space(dimensions=8), ("alpha", "beta"))
    assert outcome.outcome is Outcome.PROVIDER_REJECTED
    assert "index" in outcome.detail


def test_openai_rejects_row_count_mismatch():
    provider, _ = openai_provider(json_response(openai_payload([[0.25] * 8])))
    outcome = embed_docs(provider, make_space(dimensions=8), ("alpha", "beta"))
    assert outcome.outcome is Outcome.PROVIDER_REJECTED
    assert outcome.vectors == ()


def test_openai_rejects_wrong_embedding_length():
    provider, _ = openai_provider(
        json_response(openai_payload([[0.25] * 7, [0.5] * 7]))
    )
    outcome = embed_docs(provider, make_space(dimensions=8), ("alpha", "beta"))
    assert outcome.outcome is Outcome.PROVIDER_REJECTED
    assert "dimensions" in outcome.detail


def test_openai_rejects_non_finite_embeddings():
    raw = (
        b'{"model":"embed-1","data":[{"index":0,"embedding":'
        b"[NaN, 0.1, Infinity, 0.2, 0.3, 0.4, 0.5, 0.6]}]}"
    )
    response = HttpResponse(200, {"content-type": "application/json"}, raw)
    provider, _ = openai_provider(response)
    outcome = embed_docs(provider, make_space(dimensions=8), ("alpha",))
    assert outcome.outcome is Outcome.PROVIDER_REJECTED


def test_openai_rejects_non_numeric_embeddings():
    provider, _ = openai_provider(json_response(openai_payload([["0.25"] * 8])))
    outcome = embed_docs(provider, make_space(dimensions=8), ("alpha",))
    assert outcome.outcome is Outcome.PROVIDER_REJECTED


def test_openai_rejects_mismatched_model_field():
    provider, _ = openai_provider(
        json_response(openai_payload([[0.25] * 8], model="embed-2"))
    )
    outcome = embed_docs(provider, make_space(dimensions=8), ("alpha",))
    assert outcome.outcome is Outcome.PROVIDER_REJECTED
    assert "embed-2" in outcome.detail
    assert "embed-1" in outcome.detail


def test_openai_rejects_malformed_json_body():
    provider, _ = openai_provider(HttpResponse(200, {}, b"not json"))
    outcome = embed_docs(provider, make_space(dimensions=8), ("alpha",))
    assert outcome.outcome is Outcome.PROVIDER_REJECTED


def test_openai_maps_timeout_without_retrying():
    provider, transport = openai_provider(error=TransportTimeout("read timed out"))
    outcome = run(provider.embed_documents(make_space(), ("a",), deadline=ahead()))
    assert outcome.outcome is Outcome.PROVIDER_TIMEOUT
    assert len(transport.requests) == 1  # retries belong to the queue, never here


def test_openai_maps_connection_error_to_unavailable():
    provider, _ = openai_provider(error=TransportConnectionError("connect failed"))
    outcome = run(provider.embed_documents(make_space(), ("a",), deadline=ahead()))
    assert outcome.outcome is Outcome.PROVIDER_UNAVAILABLE


def test_openai_maps_egress_denial_to_configuration_missing():
    provider, _ = openai_provider(
        error=EgressDenied("host 'api.embedding.test' is not in the egress allowlist")
    )
    outcome = run(provider.embed_documents(make_space(), ("a",), deadline=ahead()))
    assert outcome.outcome is Outcome.CONFIGURATION_MISSING


def test_openai_maps_5xx_to_unavailable():
    provider, _ = openai_provider(json_response({"error": "boom"}, status=503))
    outcome = run(provider.embed_documents(make_space(), ("a",), deadline=ahead()))
    assert outcome.outcome is Outcome.PROVIDER_UNAVAILABLE
    assert "503" in outcome.detail


def test_openai_maps_other_4xx_to_rejected():
    provider, _ = openai_provider(json_response({"error": "bad"}, status=400))
    outcome = run(provider.embed_documents(make_space(), ("a",), deadline=ahead()))
    assert outcome.outcome is Outcome.PROVIDER_REJECTED
    assert "400" in outcome.detail


def test_openai_maps_429_to_degraded_with_bounded_retry_after():
    cases = [
        ({"Retry-After": "7"}, 7.0),
        ({}, 30.0),  # default
        ({"Retry-After": "not-a-number"}, 30.0),
        ({"Retry-After": "99999"}, 300.0),  # bounded above
        ({"Retry-After": "-5"}, 0.0),  # bounded below
        ({"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"}, 0.0),  # past HTTP-date
    ]
    for headers, expected in cases:
        provider, _ = openai_provider(
            json_response({"error": "slow"}, status=429, headers=headers)
        )
        outcome = run(provider.embed_documents(make_space(), ("a",), deadline=ahead()))
        assert outcome.outcome is Outcome.PROVIDER_DEGRADED
        assert outcome.retry_after_seconds == expected
        assert outcome.vectors == ()


def test_openai_disabled_role_never_calls_transport():
    provider, transport = openai_provider(
        json_response(openai_payload([[0.25] * 8])), role=make_role(enabled=False)
    )
    outcome = run(provider.embed_documents(make_space(), ("a",), deadline=ahead()))
    assert outcome.outcome is Outcome.CONFIGURATION_MISSING
    assert transport.requests == []


def test_openai_unconfigured_kind_never_calls_transport():
    provider, transport = openai_provider(
        json_response(openai_payload([[0.25] * 8])),
        role=make_role(provider_kind="none"),
    )
    outcome = run(provider.embed_documents(make_space(), ("a",), deadline=ahead()))
    assert outcome.outcome is Outcome.CONFIGURATION_MISSING
    assert transport.requests == []


def test_openai_missing_base_url_is_configuration_missing():
    provider, transport = openai_provider(
        json_response(openai_payload([[0.25] * 8])), role=make_role(base_url=None)
    )
    outcome = run(provider.embed_documents(make_space(), ("a",), deadline=ahead()))
    assert outcome.outcome is Outcome.CONFIGURATION_MISSING
    assert transport.requests == []


def test_openai_unresolvable_credential_never_calls_transport():
    role = make_role(credential_env="CORTEX_V2_DEFINITELY_UNSET_VARIABLE")
    provider, transport = openai_provider(
        json_response(openai_payload([[0.25] * 8])), role=role, credential=None
    )
    outcome = run(provider.embed_documents(make_space(), ("a",), deadline=ahead()))
    assert outcome.outcome is Outcome.CONFIGURATION_MISSING
    assert "CORTEX_V2_DEFINITELY_UNSET_VARIABLE" in outcome.detail
    assert transport.requests == []


def test_openai_missing_credential_reference_is_configuration_missing():
    role = make_role(credential_env=None, credential_file=None)
    provider, transport = openai_provider(
        json_response(openai_payload([[0.25] * 8])), role=role, credential=None
    )
    outcome = run(provider.embed_documents(make_space(), ("a",), deadline=ahead()))
    assert outcome.outcome is Outcome.CONFIGURATION_MISSING
    assert transport.requests == []


def test_openai_resolves_credential_from_environment(monkeypatch):
    monkeypatch.setenv("CORTEX_V2_EMBEDDING_API_KEY", "env-secret-key")
    provider, transport = openai_provider(
        json_response(openai_payload([[0.25] * 8])), credential=None
    )
    outcome = embed_docs(provider, make_space(dimensions=8), ("a",))
    assert outcome.ok
    assert transport.requests[0].headers["Authorization"] == "Bearer env-secret-key"


def test_openai_rejects_empty_and_oversized_batches():
    provider, transport = openai_provider(json_response(openai_payload([])))
    empty = run(provider.embed_documents(make_space(), (), deadline=ahead()))
    assert empty.outcome is Outcome.INPUT_REJECTED
    oversized = embed_docs(provider, make_space(), tuple(f"t{i}" for i in range(9)))
    assert oversized.outcome is Outcome.INPUT_REJECTED
    long_text = embed_docs(provider, make_space(), ("x" * 2048,))
    assert long_text.outcome is Outcome.INPUT_REJECTED
    assert transport.requests == []


def test_openai_expired_deadline_times_out_before_dispatch():
    provider, transport = openai_provider(json_response(openai_payload([[0.25] * 8])))
    outcome = run(
        provider.embed_documents(make_space(), ("a",), deadline=time.monotonic() - 1.0)
    )
    assert outcome.outcome is Outcome.PROVIDER_TIMEOUT
    assert transport.requests == []


def test_openai_embed_query_uses_query_prefix():
    provider, transport = openai_provider(json_response(openai_payload([[0.25] * 8])))
    space = make_space(dimensions=8, query_prefix="q: ")
    outcome = run(provider.embed_query(space, "hello", deadline=ahead()))
    assert outcome.ok
    assert json.loads(transport.requests[0].body)["input"] == ["q: hello"]


def test_openai_capabilities_and_port_shape():
    provider, _ = openai_provider(None)
    assert isinstance(provider, EmbeddingProvider)
    caps = provider.capabilities()
    assert caps.provider_id == "openai-compatible@1"
    assert provider.family == "openai-compatible"
    assert caps.locality == "remote"
    assert caps.offline is False
    assert caps.max_batch == 8
    assert caps.models == ("embed-1",)


def test_remote_constructors_do_not_raise_for_unconfigured_roles():
    blank = ProviderRoleSettings(role="embedding")  # disabled, kind 'none'
    openai_blank = OpenAICompatibleEmbeddingProvider(
        blank, transport=FakeTransport(None)
    )
    ollama_blank = OllamaEmbeddingProvider(
        blank, local=make_local(()), transport=FakeTransport(None)
    )
    for provider in (openai_blank, ollama_blank):
        outcome = run(provider.embed_documents(make_space(), ("a",), deadline=ahead()))
        assert outcome.outcome is Outcome.CONFIGURATION_MISSING


def test_sync_callable_transport_is_accepted():
    # Consumers may inject a plain callable returning HttpResponse directly.
    class SyncTransport:
        def __init__(self):
            self.requests = []

        def __call__(self, request):
            self.requests.append(request)
            return json_response(openai_payload([[0.25] * 8]))

    transport = SyncTransport()
    provider = OpenAICompatibleEmbeddingProvider(
        make_role(), transport=transport, credential="test-key"
    )
    outcome = embed_docs(provider, make_space(dimensions=8), ("a",))
    assert outcome.ok
    assert len(transport.requests) == 1


# ---------------------------------------------------------------------------
# OllamaEmbeddingProvider against a fake transport
# ---------------------------------------------------------------------------


def test_ollama_requires_installed_manifest_model():
    unusable = (
        (),
        (manifest("nomic-embed-text", "declared"),),
        (manifest("nomic-embed-text", "retired"),),
        (manifest("other-model", "installed"),),
    )
    for models in unusable:
        provider, transport = ollama_provider(
            json_response(ollama_payload([[0.25] * 8])), local=make_local(models)
        )
        outcome = embed_docs(provider, ollama_space(), ("a",))
        assert outcome.outcome is Outcome.EXECUTOR_NOT_ACTIVATED
        assert "nomic-embed-text" in outcome.detail
        assert transport.requests == []


def test_ollama_happy_path_hits_api_embed_without_auth():
    provider, transport = ollama_provider(json_response(ollama_payload([[0.25] * 8])))
    outcome = run(provider.embed_documents(ollama_space(), ("a",), deadline=ahead()))
    assert outcome.ok
    assert outcome.vectors == ((0.25,) * 8,)
    assert outcome.degraded == ()
    assert outcome.usage == {"prompt_tokens": 1}
    request = transport.requests[0]
    assert request.url == "http://127.0.0.1:11434/api/embed"
    assert json.loads(request.body) == {"model": "nomic-embed-text", "input": ["a"]}
    assert "Authorization" not in request.headers


def test_ollama_reports_cold_start_as_degraded():
    provider, _ = ollama_provider(
        json_response(ollama_payload([[0.25] * 8], load_duration=1_500_000_000))
    )
    outcome = run(provider.embed_documents(ollama_space(), ("a",), deadline=ahead()))
    assert outcome.ok
    assert outcome.degraded == ("cold_start",)


def test_ollama_accepts_activated_manifest_status():
    activated = LocalModelManifest(
        "nomic-embed-text", "sha256:aa", "apache-2.0", "linux/arm64", "activated", None
    )
    provider, _ = ollama_provider(
        json_response(ollama_payload([[0.25] * 8])), local=make_local((activated,))
    )
    outcome = run(provider.embed_documents(ollama_space(), ("a",), deadline=ahead()))
    assert outcome.ok


def test_ollama_validates_model_count_and_dimensions():
    mismatched_model = ollama_provider(
        json_response(ollama_payload([[0.25] * 8], model="other"))
    )[0]
    outcome = embed_docs(mismatched_model, ollama_space(), ("a",))
    assert outcome.outcome is Outcome.PROVIDER_REJECTED

    short_batch = ollama_provider(json_response(ollama_payload([[0.25] * 8])))[0]
    outcome = embed_docs(short_batch, ollama_space(), ("a", "b"))
    assert outcome.outcome is Outcome.PROVIDER_REJECTED

    wrong_dims = ollama_provider(json_response(ollama_payload([[0.25] * 7])))[0]
    outcome = run(wrong_dims.embed_documents(ollama_space(), ("a",), deadline=ahead()))
    assert outcome.outcome is Outcome.PROVIDER_REJECTED


def test_ollama_maps_status_and_transport_failures():
    degraded = ollama_provider(
        json_response({"error": "slow"}, status=429, headers={"Retry-After": "3"})
    )[0]
    outcome = run(degraded.embed_documents(ollama_space(), ("a",), deadline=ahead()))
    assert outcome.outcome is Outcome.PROVIDER_DEGRADED
    assert outcome.retry_after_seconds == 3.0

    unavailable = ollama_provider(json_response({"error": "boom"}, status=500))[0]
    outcome = run(unavailable.embed_documents(ollama_space(), ("a",), deadline=ahead()))
    assert outcome.outcome is Outcome.PROVIDER_UNAVAILABLE

    timed_out = ollama_provider(error=TransportTimeout("read timed out"))[0]
    outcome = run(timed_out.embed_documents(ollama_space(), ("a",), deadline=ahead()))
    assert outcome.outcome is Outcome.PROVIDER_TIMEOUT


def test_ollama_sends_authorization_when_credential_given():
    provider, transport = ollama_provider(
        json_response(ollama_payload([[0.25] * 8])), credential="tok"
    )
    outcome = run(provider.embed_documents(ollama_space(), ("a",), deadline=ahead()))
    assert outcome.ok
    assert transport.requests[0].headers["Authorization"] == "Bearer tok"


def test_ollama_unresolvable_credential_reference_is_configuration_missing():
    role = make_ollama_role(credential_env="CORTEX_V2_DEFINITELY_UNSET_VARIABLE")
    provider, transport = ollama_provider(
        json_response(ollama_payload([[0.25] * 8])), role=role, credential=None
    )
    outcome = run(provider.embed_documents(ollama_space(), ("a",), deadline=ahead()))
    assert outcome.outcome is Outcome.CONFIGURATION_MISSING
    assert transport.requests == []


def test_ollama_capabilities_declare_local():
    provider, _ = ollama_provider(None)
    caps = provider.capabilities()
    assert caps.provider_id == "ollama@1"
    assert provider.family == "ollama"
    assert caps.locality == "local"
    assert caps.offline is True
    assert caps.models == ("nomic-embed-text",)


# ---------------------------------------------------------------------------
# select_provider routing policy (R25) and space matching (F07/R24)
# ---------------------------------------------------------------------------


def hash_role() -> ProviderRoleSettings:
    return make_role(
        provider_kind="hash-local", base_url=None, model="", credential_env=None
    )


def test_select_provider_defaults_to_document_purpose():
    settings = make_settings(hash_role())
    space = make_space(dimensions=16, provider="hash-local")
    provider, reason = select_provider(settings, space)
    assert reason is None
    assert isinstance(provider, HashEmbeddingProvider)


def test_select_provider_disabled_routing_returns_typed_reason():
    settings = make_settings(routing_policy="disabled")
    provider, reason = select_provider(settings, make_space(), purpose=PURPOSE_DOCUMENT)
    assert provider is None
    assert reason == "routing_disabled"


def test_select_provider_unconfigured_role_returns_typed_reason():
    for role in (make_role(enabled=False), make_role(provider_kind="none")):
        settings = make_settings(role)
        provider, reason = select_provider(
            settings, make_space(), purpose=PURPOSE_QUERY
        )
        assert provider is None
        assert reason == "role_not_configured"


def test_select_provider_local_only_never_selects_remote():
    settings = make_settings(
        make_role(), routing_policy="local_only", models=(INSTALLED_MODEL,)
    )
    provider, reason = select_provider(settings, make_space(), purpose=PURPOSE_DOCUMENT)
    assert provider is None
    assert reason == "local_only_denies_remote"


def test_select_provider_remote_allowed_selects_remote_role():
    settings = make_settings(make_role(), routing_policy="remote_allowed")
    provider, reason = select_provider(settings, make_space(), purpose=PURPOSE_DOCUMENT)
    assert reason is None
    assert isinstance(provider, OpenAICompatibleEmbeddingProvider)


def test_select_provider_prefers_local_kinds():
    provider, reason = select_provider(
        make_settings(hash_role(), routing_policy="remote_allowed"),
        make_space(dimensions=16, provider="hash-local"),
        purpose=PURPOSE_DOCUMENT,
    )
    assert reason is None
    assert isinstance(provider, HashEmbeddingProvider)

    provider, reason = select_provider(
        make_settings(
            make_ollama_role(), routing_policy="local_only", models=(INSTALLED_MODEL,)
        ),
        ollama_space(),
        purpose=PURPOSE_DOCUMENT,
    )
    assert reason is None
    assert isinstance(provider, OllamaEmbeddingProvider)


def test_select_provider_ollama_without_installed_model_never_falls_back():
    for routing in ("local_only", "remote_allowed"):
        settings = make_settings(make_ollama_role(), routing_policy=routing)
        provider, reason = select_provider(
            settings, ollama_space(), purpose=PURPOSE_DOCUMENT
        )
        assert provider is None
        assert reason == "local_model_not_installed"


def test_select_provider_rejects_space_provider_family_mismatch():
    settings = make_settings(
        hash_role(), routing_policy="local_only", models=(INSTALLED_MODEL,)
    )
    space = make_space(dimensions=16, provider="openai-compatible")
    provider, reason = select_provider(settings, space, purpose=PURPOSE_DOCUMENT)
    assert provider is None
    assert isinstance(reason, str)
    assert reason == "space_provider_mismatch"
    assert "family" in reason.detail
    assert "openai-compatible" in reason.detail
    assert "hash-local" in reason.detail
    assert "embed-1" in reason.detail  # the space model_id is always named


def test_select_provider_rejects_configured_model_mismatch():
    settings = make_settings(make_role(), routing_policy="remote_allowed")
    space = make_space(model_id="embed-other")  # family matches, model does not
    provider, reason = select_provider(settings, space, purpose=PURPOSE_DOCUMENT)
    assert provider is None
    assert reason == "space_provider_mismatch"
    assert "model" in reason.detail
    assert "embed-other" in reason.detail
    assert "embed-1" in reason.detail


def test_select_provider_skips_model_check_for_unconfigured_hash_model():
    # hash-local roles carry no configured model: the family check still applies.
    settings = make_settings(hash_role())
    space = make_space(dimensions=16, provider="hash-local", model_id="anything")
    provider, reason = select_provider(settings, space, purpose=PURPOSE_DOCUMENT)
    assert reason is None
    assert isinstance(provider, HashEmbeddingProvider)


@pytest.mark.parametrize("dimensions", [0, 4097])
def test_select_provider_rejects_unsupported_dimensions(dimensions):
    settings = make_settings(hash_role())
    space = make_space(dimensions=dimensions, provider="hash-local")
    provider, reason = select_provider(settings, space, purpose=PURPOSE_DOCUMENT)
    assert provider is None
    assert reason == "dimension_unsupported"


def test_select_provider_rejects_manifest_dimension_mismatch():
    pinned = LocalModelManifest(
        "nomic-embed-text", "sha256:aa", "apache-2.0", "linux/arm64", "installed", 768
    )
    settings = make_settings(make_ollama_role(), models=(pinned,))
    provider, reason = select_provider(
        settings, ollama_space(), purpose=PURPOSE_DOCUMENT
    )
    assert provider is None
    assert reason == "dimension_unsupported"


def test_select_provider_rejects_unknown_purpose():
    with pytest.raises(ValueError):
        select_provider(make_settings(), make_space(), purpose="side-channel")


def test_selected_hash_provider_embeds_end_to_end():
    settings = make_settings(
        hash_role(), routing_policy="local_only", models=(INSTALLED_MODEL,)
    )
    space = make_space(dimensions=16, provider="hash-local")
    provider, reason = select_provider(settings, space, purpose=PURPOSE_QUERY)
    assert reason is None
    outcome = run(provider.embed_query(space, "hello", deadline=ahead()))
    assert outcome.ok
    assert len(outcome.vectors[0]) == 16
