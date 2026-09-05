import asyncio
import importlib.util
import json
from pathlib import Path
import sys
import threading
import time
import types

import pytest


WORKER_ROOT = Path(__file__).resolve().parents[1]
WORKER_PATH = WORKER_ROOT / "worker.py"
PINS_PATH = WORKER_ROOT / "model-pins.json"


def load_worker(name: str):
    spec = importlib.util.spec_from_file_location(name, WORKER_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_middleware_stack_actually_builds():
    """Starlette instantiates middleware as cls(app=app, ...) -- by KEYWORD.

    RequestBodyLimitMiddleware named its first parameter `application`, so building the
    stack raised

        TypeError: RequestBodyLimitMiddleware.__init__() got an unexpected keyword
        argument 'app'

    on every request. The worker answered /health with 500 and never became healthy, while
    the rest of the appliance came up around it.

    Every other test here constructs the middleware POSITIONALLY, which is not how the
    framework does it, so all of them passed. Build the real stack the way the server does
    -- that is the thing that was broken.
    """
    import inspect

    module = load_worker("local_search_middleware_stack_test")
    module.app.build_middleware_stack()

    # The signature itself, so a rename is caught before anyone reads a traceback.
    first = list(inspect.signature(module.RequestBodyLimitMiddleware.__init__).parameters)[1]
    assert first == "app", (
        f"Starlette passes app= by keyword; first parameter is named {first!r}"
    )


@pytest.mark.asyncio
async def test_raw_http_body_is_bounded_even_without_content_length():
    module = load_worker("local_search_streamed_body_limit_test")
    messages = iter(
        [
            {
                "type": "http.request",
                "body": b"x" * module.MAX_REQUEST_BODY_BYTES,
                "more_body": True,
            },
            {"type": "http.request", "body": b"x", "more_body": False},
        ]
    )
    sent = []

    async def receive():
        return next(messages)

    async def send(message):
        sent.append(message)

    async def consume(_scope, limited_receive, _send):
        while True:
            message = await limited_receive()
            if not message.get("more_body"):
                return

    middleware = module.RequestBodyLimitMiddleware(
        consume,
        max_body_bytes=module.MAX_REQUEST_BODY_BYTES,
    )
    await middleware({"type": "http", "headers": []}, receive, send)

    start = next(message for message in sent if message["type"] == "http.response.start")
    assert start["status"] == 413


@pytest.mark.asyncio
async def test_embed_accepts_only_the_pinned_configured_model(monkeypatch):
    module = load_worker("local_search_pinned_embed_model_test")
    monkeypatch.setattr(
        module,
        "_get_embedding_model",
        lambda _name: pytest.fail("a rejected model must never be loaded"),
    )

    with pytest.raises(module.HTTPException) as exc_info:
        await module.embed(module.EmbedBody(texts=["hello"], model="arbitrary/model"))

    assert exc_info.value.status_code == 400
    assert module.DEFAULT_EMBED_MODEL in exc_info.value.detail


@pytest.mark.asyncio
async def test_rerank_accepts_only_the_pinned_configured_model(monkeypatch):
    module = load_worker("local_search_pinned_rerank_model_test")
    monkeypatch.setattr(
        module,
        "_get_rerank_model",
        lambda _name: pytest.fail("a rejected model must never be loaded"),
    )

    with pytest.raises(module.HTTPException) as exc_info:
        await module.rerank(
            module.RerankBody(
                query="query",
                documents=["one", "two"],
                model="arbitrary/model",
            )
        )

    assert exc_info.value.status_code == 400
    assert module.DEFAULT_RERANK_MODEL in exc_info.value.detail


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("texts", "detail"),
    [
        (["x"] * 65, "at most 64 texts"),
        (["x" * (16 * 1024 + 1)], "16384-byte limit"),
        (["x" * (8 * 1024)] * 33, "combined text"),
        ([""], "must be non-empty"),
        (["\u20ac" * 5462], "16384-byte limit"),
    ],
)
async def test_embed_rejects_invalid_or_oversized_batches_before_model_use(
    monkeypatch, texts, detail
):
    module = load_worker("local_search_bounded_embed_request_test")
    monkeypatch.setattr(
        module,
        "_encode",
        lambda *_args: pytest.fail("invalid input must be rejected before inference"),
    )

    with pytest.raises(module.HTTPException) as exc_info:
        await module.embed(module.EmbedBody(texts=texts))

    assert exc_info.value.status_code in {400, 413}
    assert detail in exc_info.value.detail


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "detail"),
    [
        ({"query": "", "documents": ["one"]}, "query must be non-empty"),
        ({"query": "x" * (16 * 1024 + 1), "documents": ["one"]}, "query exceeds"),
        ({"query": "q", "documents": ["x"] * 65}, "at most 64 documents"),
        ({"query": "q", "documents": []}, "documents must be non-empty"),
        ({"query": "q", "documents": [""]}, "must be non-empty"),
        ({"query": "q", "documents": ["x" * (16 * 1024 + 1)]}, "16384-byte limit"),
        ({"query": "q", "documents": ["\u20ac" * 5462]}, "16384-byte limit"),
        ({"query": "q", "documents": ["one"], "top_n": 0}, "top_n must be"),
        ({"query": "q", "documents": ["one"], "top_n": 65}, "top_n must be"),
    ],
)
async def test_rerank_rejects_invalid_or_oversized_requests_before_model_use(
    monkeypatch, body, detail
):
    module = load_worker("local_search_bounded_rerank_request_test")
    monkeypatch.setattr(
        module,
        "_rerank",
        lambda *_args: pytest.fail("invalid input must be rejected before inference"),
    )

    with pytest.raises(module.HTTPException) as exc_info:
        await module.rerank(module.RerankBody(**body))

    assert exc_info.value.status_code in {400, 413}
    assert detail in exc_info.value.detail


@pytest.mark.asyncio
async def test_embed_uses_the_pinned_default_model_and_exact_output_shape(monkeypatch):
    module = load_worker("local_search_default_embed_model_test")
    seen = []
    vector = [float(index) for index in range(module.EXPECTED_EMBED_DIMS)]

    class Vectors:
        def tolist(self):
            return [vector]

    class Model:
        def encode(self, texts, **kwargs):
            seen.append((texts, kwargs))
            return Vectors()

    monkeypatch.setattr(
        module,
        "_get_embedding_model",
        lambda name: (seen.append(name), Model())[1],
    )

    result = await module.embed(module.EmbedBody(texts=["hello"]))

    assert seen == [
        module.DEFAULT_EMBED_MODEL,
        (
            ["hello"],
            {
                "batch_size": 16,
                "convert_to_numpy": True,
                "show_progress_bar": False,
            },
        ),
    ]
    assert result == {
        "model": module.DEFAULT_EMBED_MODEL,
        "dim": 768,
        "vectors": [vector],
    }


@pytest.mark.asyncio
async def test_rerank_returns_bounded_stable_order_and_original_indices(monkeypatch):
    module = load_worker("local_search_rerank_order_test")
    seen = []

    class Scores:
        def tolist(self):
            return [0.1, 9.0, 9.0]

    class Model:
        def predict(self, pairs, **kwargs):
            seen.append((pairs, kwargs))
            return Scores()

    monkeypatch.setattr(
        module,
        "_get_rerank_model",
        lambda name: (seen.append(name), Model())[1],
    )

    result = await module.rerank(
        module.RerankBody(
            query="secure restore",
            documents=["irrelevant", "best first", "best second"],
            top_n=2,
        )
    )

    assert seen == [
        module.DEFAULT_RERANK_MODEL,
        (
            [
                ("secure restore", "irrelevant"),
                ("secure restore", "best first"),
                ("secure restore", "best second"),
            ],
            {
                "batch_size": 8,
                "convert_to_numpy": True,
                "show_progress_bar": False,
            },
        ),
    ]
    assert result == {
        "model": module.DEFAULT_RERANK_MODEL,
        "results": [
            {"index": 1, "relevance_score": 9.0},
            {"index": 2, "relevance_score": 9.0},
        ],
    }


def test_models_load_only_from_baked_local_paths_without_remote_code(monkeypatch):
    module = load_worker("local_search_offline_model_load_test")
    seen = {}

    class EmbedModel:
        def __init__(self, path, **kwargs):
            seen["embedding"] = (path, kwargs)

    class RerankModel:
        def __init__(self, path, **kwargs):
            seen["rerank"] = (path, kwargs)

    fake_dependency = types.ModuleType("sentence_transformers")
    fake_dependency.SentenceTransformer = EmbedModel
    fake_dependency.CrossEncoder = RerankModel
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_dependency)

    module._get_embedding_model(module.DEFAULT_EMBED_MODEL)
    module._get_rerank_model(module.DEFAULT_RERANK_MODEL)

    assert seen == {
        "embedding": (
            "/opt/kaidera-models/embedding",
            {"trust_remote_code": False, "local_files_only": True},
        ),
        "rerank": (
            "/opt/kaidera-models/rerank",
            {
                "max_length": 512,
                "trust_remote_code": False,
                "local_files_only": True,
            },
        ),
    }


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("CORTEX_DEFAULT_MODEL", "arbitrary/model"),
        ("CORTEX_DEFAULT_MODEL_REVISION", "main"),
        ("CORTEX_DEFAULT_RERANK_MODEL", "arbitrary/reranker"),
        ("CORTEX_DEFAULT_RERANK_MODEL_REVISION", "main"),
    ],
)
def test_runtime_cannot_override_pinned_model_artifacts(monkeypatch, name, value):
    monkeypatch.setenv(name, value)

    with pytest.raises(RuntimeError, match="immutable"):
        load_worker(f"local_search_immutable_{name.lower()}_test")


@pytest.mark.asyncio
async def test_embedding_and_rerank_share_one_off_loop_operation_slot(monkeypatch):
    module = load_worker("local_search_shared_single_flight_test")
    lock = threading.Lock()
    active = 0
    peak = 0
    thread_ids = []

    def enter():
        nonlocal active, peak
        thread_ids.append(threading.get_ident())
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.02)
        with lock:
            active -= 1

    def encode(_model_name, _texts):
        enter()
        return [[0.0] * module.EXPECTED_EMBED_DIMS]

    def rerank(_model_name, _query, _documents, _top_n):
        enter()
        return [{"index": 0, "relevance_score": 1.0}]

    monkeypatch.setattr(module, "_encode", encode)
    monkeypatch.setattr(module, "_rerank", rerank)
    event_loop_thread = threading.get_ident()

    await asyncio.gather(
        module.embed(module.EmbedBody(texts=["one"])),
        module.rerank(module.RerankBody(query="q", documents=["one"])),
    )

    assert peak == 1
    assert thread_ids and all(thread_id != event_loop_thread for thread_id in thread_ids)


@pytest.mark.parametrize(
    "raw",
    [
        [[0.0] * 767],
        [[0.0] * 767 + [float("nan")]],
        [[0.0] * 767 + [True]],
    ],
)
def test_embedding_output_is_exactly_bounded_and_finite(monkeypatch, raw):
    module = load_worker("local_search_embed_output_bound_test")

    class Model:
        def encode(self, *_args, **_kwargs):
            return raw

    monkeypatch.setattr(module, "_get_embedding_model", lambda _name: Model())

    with pytest.raises(RuntimeError):
        module._encode(module.DEFAULT_EMBED_MODEL, ["one"])


def test_rerank_output_count_and_scores_are_bounded(monkeypatch):
    module = load_worker("local_search_rerank_output_bound_test")

    class Model:
        def predict(self, *_args, **_kwargs):
            return [float("inf"), 1.0]

    monkeypatch.setattr(module, "_get_rerank_model", lambda _name: Model())

    with pytest.raises(RuntimeError, match="non-finite"):
        module._rerank(module.DEFAULT_RERANK_MODEL, "q", ["one", "two"], 2)


@pytest.mark.asyncio
async def test_health_reports_dependencies_caches_and_baked_artifacts(monkeypatch):
    module = load_worker("local_search_dependency_health_test")
    monkeypatch.setattr(
        module,
        "_dependency_versions",
        lambda: ({"torch": "2"}, ["sentence_transformers"]),
    )
    monkeypatch.setattr(module, "_cache_is_writable", lambda _path: False)
    monkeypatch.setattr(
        module,
        "_model_artifacts_available",
        lambda: {"embedding": True, "rerank": False, "pins": True, "receipt": True},
    )

    result = await module.health()

    assert result["ok"] is False
    assert result["missing_dependencies"] == ["sentence_transformers"]
    assert result["cache_writable"] == {"hf": False, "sentence_transformers": False}
    assert result["model_artifacts"]["rerank"] is False
    assert result["embedding_model_revision"] == module.PINNED_EMBED_MODEL_REVISION
    assert result["rerank_model_revision"] == module.PINNED_RERANK_MODEL_REVISION


def test_model_pin_inventory_is_exact_and_safetensors_only():
    pins = json.loads(PINS_PATH.read_text(encoding="utf-8"))
    assert pins["schema_version"] == 1
    embedding = pins["models"]["embedding"]
    rerank = pins["models"]["rerank"]
    assert (embedding["repo_id"], embedding["revision"], embedding["dimensions"]) == (
        "sentence-transformers/all-mpnet-base-v2",
        "e8c3b32edf5434bc2275fc9bab85f82640a19130",
        768,
    )
    assert (rerank["repo_id"], rerank["revision"]) == (
        "cross-encoder/ms-marco-MiniLM-L6-v2",
        "233902d25c440f23af6f7d6e94d2946bac0bee0a",
    )
    assert {item["path"] for item in embedding["artifacts"]} == {
        "1_Pooling/config.json",
        "config.json",
        "config_sentence_transformers.json",
        "model.safetensors",
        "modules.json",
        "sentence_bert_config.json",
        "special_tokens_map.json",
        "tokenizer.json",
        "tokenizer_config.json",
        "vocab.txt",
    }
    assert {item["path"] for item in rerank["artifacts"]} == {
        "config.json",
        "model.safetensors",
        "special_tokens_map.json",
        "tokenizer.json",
        "tokenizer_config.json",
        "vocab.txt",
    }
    weights = {
        role: next(
            item for item in pin["artifacts"] if item["path"] == "model.safetensors"
        )
        for role, pin in pins["models"].items()
    }
    assert weights == {
        "embedding": {
            "path": "model.safetensors",
            "sha256": "78c0197b6159d92658e319bc1d72e4c73a9a03dd03815e70e555c5ef05615658",
            "size": 437971872,
        },
        "rerank": {
            "path": "model.safetensors",
            "sha256": "821d1aa69520101d6e0737f78a042ae25b19e5cb9160701909d10434f4aeb0ae",
            "size": 90870598,
        },
    }
    for pin in pins["models"].values():
        assert pin["license"] == "Apache-2.0"
        for artifact in pin["artifacts"]:
            assert set(artifact) == {"path", "sha256", "size"}
            assert len(artifact["sha256"]) == 64
            assert artifact["size"] > 0
            assert not artifact["path"].endswith((".bin", ".onnx", ".py"))
