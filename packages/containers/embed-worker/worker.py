"""cortex-local-search-worker — bounded local embedding and rerank APIs.

Both models are fetched at image build from immutable revisions and loaded only
from the image. They share one CPU-operation slot because they have the same
release, trust, scaling, and failure boundary in the single-appliance profile.
"""

from __future__ import annotations

import asyncio
import importlib
import math
import os
from pathlib import Path
import tempfile
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict


PINNED_EMBED_MODEL = "sentence-transformers/all-mpnet-base-v2"
PINNED_EMBED_MODEL_REVISION = "e8c3b32edf5434bc2275fc9bab85f82640a19130"
PINNED_RERANK_MODEL = "cross-encoder/ms-marco-MiniLM-L6-v2"
PINNED_RERANK_MODEL_REVISION = "233902d25c440f23af6f7d6e94d2946bac0bee0a"
EXPECTED_EMBED_DIMS = 768

DEFAULT_EMBED_MODEL = os.environ.get("CORTEX_DEFAULT_MODEL", PINNED_EMBED_MODEL)
DEFAULT_EMBED_MODEL_REVISION = os.environ.get(
    "CORTEX_DEFAULT_MODEL_REVISION", PINNED_EMBED_MODEL_REVISION
)
DEFAULT_RERANK_MODEL = os.environ.get(
    "CORTEX_DEFAULT_RERANK_MODEL", PINNED_RERANK_MODEL
)
DEFAULT_RERANK_MODEL_REVISION = os.environ.get(
    "CORTEX_DEFAULT_RERANK_MODEL_REVISION", PINNED_RERANK_MODEL_REVISION
)
if (
    DEFAULT_EMBED_MODEL != PINNED_EMBED_MODEL
    or DEFAULT_EMBED_MODEL_REVISION != PINNED_EMBED_MODEL_REVISION
    or DEFAULT_RERANK_MODEL != PINNED_RERANK_MODEL
    or DEFAULT_RERANK_MODEL_REVISION != PINNED_RERANK_MODEL_REVISION
):
    raise RuntimeError("the local-search model identities and revisions are immutable")

MODEL_ROOT = Path("/opt/kaidera-models")
EMBED_MODEL_PATH = MODEL_ROOT / "embedding"
RERANK_MODEL_PATH = MODEL_ROOT / "rerank"
MODEL_RECEIPT_PATH = MODEL_ROOT / "kaidera-model-receipt.json"
MODEL_PINS_PATH = MODEL_ROOT / "model-pins.json"

MAX_TEXTS = 64
MAX_RERANK_DOCUMENTS = 64
MAX_TEXT_BYTES = 16 * 1024
MAX_BATCH_TEXT_BYTES = 256 * 1024
MAX_REQUEST_BODY_BYTES = 2 * 1024 * 1024
EMBED_BATCH_SIZE = 16
RERANK_BATCH_SIZE = 8

app = FastAPI(title="cortex-local-search-worker", version="0.2.0")
_model_cache: dict[str, object] = {}
_model_operation = asyncio.Semaphore(1)


class _RequestBodyTooLarge(Exception):
    pass


class RequestBodyLimitMiddleware:
    """Reject declared or streamed bodies above the worker's finite HTTP bound."""

    # The first parameter MUST be named `app`: Starlette's build_middleware_stack calls
    # cls(app=app, *args, **kwargs) -- by KEYWORD. Naming it `application` made every
    # request raise
    #   TypeError: RequestBodyLimitMiddleware.__init__() got an unexpected keyword
    #   argument 'app'
    # so the worker answered /health with 500 and never became healthy, while the rest of
    # the appliance came up around it. The contract test builds this class POSITIONALLY,
    # so it passed throughout -- the component was covered and the wiring was not.
    def __init__(self, app, *, max_body_bytes: int):
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        headers = {
            key.lower(): value
            for key, value in scope.get("headers", [])
        }
        declared = headers.get(b"content-length")
        if declared is not None:
            try:
                declared_bytes = int(declared)
            except ValueError:
                response = JSONResponse(
                    {"detail": "invalid content-length"}, status_code=400
                )
                await response(scope, receive, send)
                return
            if declared_bytes > self.max_body_bytes:
                response = JSONResponse(
                    {"detail": "request body is too large"}, status_code=413
                )
                await response(scope, receive, send)
                return

        received = 0

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message.get("type") == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_body_bytes:
                    raise _RequestBodyTooLarge
            return message

        try:
            await self.app(scope, limited_receive, send)
        except _RequestBodyTooLarge:
            response = JSONResponse(
                {"detail": "request body is too large"}, status_code=413
            )
            await response(scope, receive, send)


app.add_middleware(
    RequestBodyLimitMiddleware,
    max_body_bytes=MAX_REQUEST_BODY_BYTES,
)


class _StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class EmbedBody(_StrictBody):
    texts: list[str]
    model: Optional[str] = None


class RerankBody(_StrictBody):
    query: str
    documents: list[str]
    top_n: int = 8
    model: Optional[str] = None


def _get_embedding_model(name: str):
    if name != DEFAULT_EMBED_MODEL:
        raise ValueError("only the configured embedding model may be loaded")
    if name not in _model_cache:
        from sentence_transformers import SentenceTransformer

        _model_cache[name] = SentenceTransformer(
            str(EMBED_MODEL_PATH),
            trust_remote_code=False,
            local_files_only=True,
        )
    return _model_cache[name]


def _get_rerank_model(name: str):
    if name != DEFAULT_RERANK_MODEL:
        raise ValueError("only the configured rerank model may be loaded")
    if name not in _model_cache:
        from sentence_transformers import CrossEncoder

        _model_cache[name] = CrossEncoder(
            str(RERANK_MODEL_PATH),
            max_length=512,
            trust_remote_code=False,
            local_files_only=True,
        )
    return _model_cache[name]


def _as_list(value: Any) -> list[Any]:
    if hasattr(value, "tolist"):
        value = value.tolist()
    if not isinstance(value, list):
        raise RuntimeError("model returned a non-list result")
    return value


def _finite_float(value: Any) -> float:
    if isinstance(value, bool):
        raise RuntimeError("model returned a boolean instead of a numeric score")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("model returned a non-numeric value") from exc
    if not math.isfinite(number):
        raise RuntimeError("model returned a non-finite value")
    return number


def _encode(model_name: str, texts: list[str]) -> list[list[float]]:
    model = _get_embedding_model(model_name)
    raw_vectors = _as_list(
        model.encode(
            texts,
            batch_size=EMBED_BATCH_SIZE,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
    )
    if len(raw_vectors) != len(texts):
        raise RuntimeError("embedding model returned the wrong vector count")
    vectors: list[list[float]] = []
    for raw_vector in raw_vectors:
        vector = [_finite_float(value) for value in _as_list(raw_vector)]
        if len(vector) != EXPECTED_EMBED_DIMS:
            raise RuntimeError(
                f"embedding model returned {len(vector)} dimensions; "
                f"expected {EXPECTED_EMBED_DIMS}"
            )
        vectors.append(vector)
    return vectors


def _rerank(
    model_name: str,
    query: str,
    documents: list[str],
    top_n: int,
) -> list[dict[str, float | int]]:
    model = _get_rerank_model(model_name)
    raw_scores = _as_list(
        model.predict(
            [(query, document) for document in documents],
            batch_size=RERANK_BATCH_SIZE,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
    )
    if len(raw_scores) != len(documents):
        raise RuntimeError("rerank model returned the wrong score count")
    scores: list[float] = []
    for raw_score in raw_scores:
        if isinstance(raw_score, list) or hasattr(raw_score, "tolist"):
            values = _as_list(raw_score)
            if len(values) != 1:
                raise RuntimeError("rerank model returned a non-scalar score")
            raw_score = values[0]
        scores.append(_finite_float(raw_score))
    ordered = sorted(range(len(scores)), key=lambda index: (-scores[index], index))
    return [
        {"index": index, "relevance_score": scores[index]}
        for index in ordered[: min(top_n, len(ordered))]
    ]


def _dependency_versions() -> tuple[dict[str, str], list[str]]:
    versions: dict[str, str] = {}
    missing: list[str] = []
    for name in ("sentence_transformers", "torch"):
        try:
            module = importlib.import_module(name)
            versions[name] = str(getattr(module, "__version__", "available"))
        except Exception:
            missing.append(name)
    return versions, missing


def _cache_is_writable(path: str) -> bool:
    try:
        with tempfile.TemporaryFile(dir=path):
            return True
    except OSError:
        return False


def _model_artifacts_available() -> dict[str, bool]:
    return {
        "embedding": (EMBED_MODEL_PATH / "model.safetensors").is_file(),
        "rerank": (RERANK_MODEL_PATH / "model.safetensors").is_file(),
        "pins": MODEL_PINS_PATH.is_file(),
        "receipt": MODEL_RECEIPT_PATH.is_file(),
    }


def _bounded_texts(texts: list[str], *, label: str, maximum: int) -> None:
    if not texts:
        raise HTTPException(400, f"{label} must be non-empty")
    if len(texts) > maximum:
        raise HTTPException(413, f"at most {maximum} {label} are allowed per request")
    total_bytes = 0
    for index, text in enumerate(texts):
        text_bytes = len(text.encode("utf-8"))
        if text_bytes == 0:
            raise HTTPException(400, f"{label}[{index}] must be non-empty")
        if text_bytes > MAX_TEXT_BYTES:
            raise HTTPException(
                413,
                f"{label}[{index}] exceeds the {MAX_TEXT_BYTES}-byte limit",
            )
        total_bytes += text_bytes
        if total_bytes > MAX_BATCH_TEXT_BYTES:
            raise HTTPException(
                413,
                f"combined text exceeds the {MAX_BATCH_TEXT_BYTES}-byte limit",
            )


@app.get("/health")
async def health():
    versions, missing = await asyncio.to_thread(_dependency_versions)
    caches = {
        "hf": _cache_is_writable(os.environ.get("HF_HOME", "/var/lib/cortex/models/hf")),
        "sentence_transformers": _cache_is_writable(
            os.environ.get(
                "SENTENCE_TRANSFORMERS_HOME",
                "/var/lib/cortex/models/sentence-transformers",
            )
        ),
    }
    artifacts = _model_artifacts_available()
    return {
        "ok": not missing and all(caches.values()) and all(artifacts.values()),
        "embedding_model": DEFAULT_EMBED_MODEL,
        "embedding_model_revision": DEFAULT_EMBED_MODEL_REVISION,
        "rerank_model": DEFAULT_RERANK_MODEL,
        "rerank_model_revision": DEFAULT_RERANK_MODEL_REVISION,
        "loaded_models": sorted(_model_cache),
        "dependency_versions": versions,
        "missing_dependencies": missing,
        "cache_writable": caches,
        "model_artifacts": artifacts,
    }


@app.post("/embed")
async def embed(body: EmbedBody):
    _bounded_texts(body.texts, label="texts", maximum=MAX_TEXTS)
    model_name = body.model or DEFAULT_EMBED_MODEL
    if model_name != DEFAULT_EMBED_MODEL:
        raise HTTPException(
            400,
            f"model must match the configured embedding model {DEFAULT_EMBED_MODEL!r}",
        )

    async with _model_operation:
        vectors = await asyncio.to_thread(_encode, model_name, body.texts)
    return {"model": model_name, "dim": EXPECTED_EMBED_DIMS, "vectors": vectors}


@app.post("/rerank")
async def rerank(body: RerankBody):
    query_bytes = len(body.query.encode("utf-8"))
    if query_bytes == 0:
        raise HTTPException(400, "query must be non-empty")
    if query_bytes > MAX_TEXT_BYTES:
        raise HTTPException(413, f"query exceeds the {MAX_TEXT_BYTES}-byte limit")
    _bounded_texts(
        body.documents,
        label="documents",
        maximum=MAX_RERANK_DOCUMENTS,
    )
    combined_bytes = query_bytes + sum(
        len(document.encode("utf-8")) for document in body.documents
    )
    if combined_bytes > MAX_BATCH_TEXT_BYTES:
        raise HTTPException(
            413,
            f"combined query and documents exceed the {MAX_BATCH_TEXT_BYTES}-byte limit",
        )
    if body.top_n < 1 or body.top_n > MAX_RERANK_DOCUMENTS:
        raise HTTPException(
            400,
            f"top_n must be between 1 and {MAX_RERANK_DOCUMENTS}",
        )
    model_name = body.model or DEFAULT_RERANK_MODEL
    if model_name != DEFAULT_RERANK_MODEL:
        raise HTTPException(
            400,
            f"model must match the configured rerank model {DEFAULT_RERANK_MODEL!r}",
        )

    async with _model_operation:
        results = await asyncio.to_thread(
            _rerank,
            model_name,
            body.query,
            body.documents,
            body.top_n,
        )
    return {"model": model_name, "results": results}
