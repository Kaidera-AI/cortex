"""Embedding providers behind the frozen ``EmbeddingProvider`` port.

Three adapters share one output contract (R24): a replacement provider must
never reinterpret existing vectors, so every remote response is validated
against the :class:`~cortex_v2.processing.contracts.SpaceContract` (row
count, index order, dimension, finiteness, model identity) and any mismatch
is a typed ``provider_rejected`` outcome. Transports never retry — retry
disposition belongs to the queue.

Routing (R25) lives in :func:`select_provider`: ``local_only`` never falls
back to a remote provider, ``disabled`` selects nothing, and
``remote_allowed`` prefers local kinds. Spaces record the provider *family*
(:data:`PROVIDER_FAMILIES`), so selection matches ``space.provider`` against
the versioned adapter id's family prefix and the space ``model_id`` against
the role's configured model; mismatches are typed
``space_provider_mismatch`` with an operator-facing ``.detail``.
"""

from __future__ import annotations

import email.utils
import hashlib
import inspect
import json
import math
import os
import time
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone

from .. import config
from .contracts import (
    EmbeddingOutcome,
    EmbeddingProvider,
    Outcome,
    ProviderCapabilities,
    SpaceContract,
)
from .providers import (
    EgressDenied,
    HttpRequest,
    HttpResponse,
    HttpTransport,
    TransportError,
    TransportResponseTooLarge,
    TransportTimeout,
    urllib_transport,
)
from .settings import (
    PROVIDER_KIND_HASH_LOCAL,
    PROVIDER_KIND_NONE,
    PROVIDER_KIND_OLLAMA,
    PROVIDER_KIND_OPENAI_COMPATIBLE,
    ROUTING_DISABLED,
    ROUTING_LOCAL_ONLY,
    USABLE_MODEL_STATUSES,
    LocalInferenceSettings,
    ProcessingSettings,
    ProviderRoleSettings,
)

PROVIDER_HASH_LOCAL = "hash-local@1"
PROVIDER_OPENAI_COMPATIBLE = "openai-compatible@1"
PROVIDER_OLLAMA = "ollama@1"

#: Canonical provider families (N06): the ``provider_id`` prefix of every
#: adapter below and the token stored in ``embedding_spaces.provider``.
PROVIDER_FAMILIES = (
    PROVIDER_KIND_HASH_LOCAL,
    PROVIDER_KIND_OPENAI_COMPATIBLE,
    PROVIDER_KIND_OLLAMA,
)

PURPOSE_DOCUMENT = "document"
PURPOSE_QUERY = "query"
PURPOSES = (PURPOSE_DOCUMENT, PURPOSE_QUERY)

ROLE_EMBEDDING = "embedding"

MIN_DIMENSIONS = 1
MAX_DIMENSIONS = 4096

DEFAULT_RETRY_AFTER_SECONDS = 30.0
MAX_RETRY_AFTER_SECONDS = 300.0

REASON_ROUTING_DISABLED = "routing_disabled"
REASON_LOCAL_MODEL_NOT_INSTALLED = "local_model_not_installed"
REASON_LOCAL_ONLY_DENIES_REMOTE = "local_only_denies_remote"
REASON_ROLE_NOT_CONFIGURED = "role_not_configured"
REASON_SPACE_PROVIDER_MISMATCH = "space_provider_mismatch"
REASON_DIMENSION_UNSUPPORTED = "dimension_unsupported"


class SelectionReason(str):
    """Exact typed reason string carrying an operator-facing ``.detail``.

    String equality, hashing and serialization are unaffected; the detail
    distinguishes a family mismatch (needs a new space) from a model
    mismatch (needs a config fix) and always names ``space.model_id``.
    """

    __slots__ = ("detail",)

    def __new__(cls, reason: str, detail: str | None = None) -> SelectionReason:
        value = super().__new__(cls, reason)
        value.detail = detail
        return value


def validate_provider_output(
    space: SpaceContract,
    texts: Sequence[str],
    vectors: Sequence[Sequence[float]],
) -> Outcome | None:
    """Shared output contract: row count, dimension and finiteness.

    Returns ``None`` when the vectors honour the space, otherwise the typed
    outcome the caller must report.
    """
    if len(vectors) != len(texts):
        return Outcome.PROVIDER_REJECTED
    for vector in vectors:
        if len(vector) != space.dimensions:
            return Outcome.PROVIDER_REJECTED
        for value in vector:
            if not math.isfinite(value):
                return Outcome.PROVIDER_REJECTED
    return None


def resolve_credential(role: ProviderRoleSettings) -> str | None:
    """Resolve a role credential from its env/secret-file reference.

    Only names are configured; the value is read at call time and never
    logged or embedded in an outcome detail.
    """
    if role.credential_env:
        value = os.environ.get(role.credential_env, "")
        return value or None
    if role.credential_file:
        try:
            return config.read_secret_path(role.credential_file).decode("utf-8")
        except config.ConfigurationError:
            return None
    return None


def hash_vector(space: SpaceContract, purpose: str, text: str) -> tuple[float, ...]:
    """Deterministic vector: successive sha256 digests over a canonical string.

    The canonical string binds the vector to the space identity, provider,
    model, revision and purpose, so nothing outside that exact contract can
    reproduce it. Bytes map to [-1, 1); L2 normalization is applied when the
    space demands it.
    """
    dimensions = space.dimensions
    canonical = "\x1f".join(
        (
            str(space.space_id),
            space.provider,
            space.model_id,
            space.model_revision or "",
            purpose,
            text,
        )
    ).encode("utf-8")
    values: list[float] = []
    counter = 0
    while len(values) < dimensions:
        digest = hashlib.sha256(
            canonical + b"\x1f" + counter.to_bytes(4, "big")
        ).digest()
        for offset in range(0, len(digest), 4):
            word = int.from_bytes(digest[offset : offset + 4], "big")
            values.append(word / 2_147_483_648.0 - 1.0)
            if len(values) == dimensions:
                break
        counter += 1
    if space.normalization == "l2":
        norm = math.sqrt(sum(value * value for value in values))
        if norm > 0.0:
            values = [value / norm for value in values]
    return tuple(values)


def _prepare(
    space: SpaceContract, texts: Sequence[str], purpose: str
) -> tuple[str, ...]:
    if purpose == PURPOSE_QUERY:
        return tuple(space.query_text(text) for text in texts)
    return tuple(space.document_text(text) for text in texts)


class HashEmbeddingProvider:
    """Offline deterministic embedder for local-only routing and test spaces."""

    provider_id = PROVIDER_HASH_LOCAL
    family = PROVIDER_KIND_HASH_LOCAL
    locality = "local"

    def __init__(self, *, max_batch: int = 256, max_input_chars: int = 65_536) -> None:
        self._max_batch = max_batch
        self._max_input_chars = max_input_chars

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider_id=self.provider_id,
            locality=self.locality,
            roles=(ROLE_EMBEDDING,),
            max_batch=self._max_batch,
            max_input_chars=self._max_input_chars,
            offline=True,
        )

    async def embed_documents(
        self,
        space: SpaceContract,
        texts: tuple[str, ...],
        *,
        deadline: float,
    ) -> EmbeddingOutcome:
        return self._embed(space, texts, PURPOSE_DOCUMENT)

    async def embed_query(
        self,
        space: SpaceContract,
        text: str,
        *,
        deadline: float,
    ) -> EmbeddingOutcome:
        return self._embed(space, (text,), PURPOSE_QUERY)

    def _embed(
        self, space: SpaceContract, texts: Sequence[str], purpose: str
    ) -> EmbeddingOutcome:
        if not texts:
            return EmbeddingOutcome(
                Outcome.INPUT_REJECTED, detail="empty embedding batch"
            )
        if len(texts) > self._max_batch:
            return EmbeddingOutcome(
                Outcome.INPUT_REJECTED,
                detail=f"batch of {len(texts)} exceeds max_batch {self._max_batch}",
            )
        if not MIN_DIMENSIONS <= space.dimensions <= MAX_DIMENSIONS:
            return EmbeddingOutcome(
                Outcome.PROVIDER_REJECTED,
                detail=(
                    f"space dimensions {space.dimensions} are outside "
                    f"{MIN_DIMENSIONS}..{MAX_DIMENSIONS}; hash-local never "
                    "truncates or pads"
                ),
            )
        prepared = _prepare(space, texts, purpose)
        if any(len(text) > self._max_input_chars for text in prepared):
            return EmbeddingOutcome(
                Outcome.INPUT_REJECTED,
                detail=f"a text exceeds max_input_chars {self._max_input_chars}",
            )
        vectors = tuple(hash_vector(space, purpose, text) for text in prepared)
        invalid = validate_provider_output(space, prepared, vectors)
        if invalid is not None:  # unreachable: derivation is dimension-exact
            return EmbeddingOutcome(invalid, detail="derived vector failed validation")
        return EmbeddingOutcome(
            Outcome.OK, vectors=vectors, model_revision=space.model_revision
        )


def _header(headers: Mapping[str, str], name: str) -> str | None:
    for key, value in headers.items():
        if key.lower() == name:
            return value
    return None


def _retry_after_seconds(headers: Mapping[str, str]) -> float:
    """Bounded Retry-After parsing: numeric seconds or HTTP-date, default 30."""
    raw = _header(headers, "retry-after")
    if raw is None:
        return DEFAULT_RETRY_AFTER_SECONDS
    raw = raw.strip()
    try:
        seconds: float | None = float(raw)
    except ValueError:
        seconds = None
    if seconds is None or not math.isfinite(seconds):
        try:
            moment = email.utils.parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            return DEFAULT_RETRY_AFTER_SECONDS
        if moment is None:
            return DEFAULT_RETRY_AFTER_SECONDS
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        seconds = (moment - datetime.now(timezone.utc)).total_seconds()
    return min(max(seconds, 0.0), MAX_RETRY_AFTER_SECONDS)


def _excerpt(body: bytes, limit: int = 200) -> str:
    text = body.decode("utf-8", "replace").strip()
    return text[:limit] or "<empty body>"


def _coerce_vector(raw: object) -> tuple[float, ...] | None:
    """Strict JSON-number list -> float tuple; anything else is None."""
    if not isinstance(raw, list):
        return None
    values: list[float] = []
    for item in raw:
        if isinstance(item, bool) or not isinstance(item, int | float):
            return None
        values.append(float(item))
    return tuple(values)


def _usage(raw: object) -> dict[str, int]:
    if not isinstance(raw, dict):
        return {}
    return {
        key: value
        for key, value in raw.items()
        if isinstance(key, str)
        and isinstance(value, int)
        and not isinstance(value, bool)
    }


def _json_object(response: HttpResponse) -> dict | EmbeddingOutcome:
    try:
        document = json.loads(response.body)
    except ValueError:
        return EmbeddingOutcome(
            Outcome.PROVIDER_REJECTED, detail="provider response is not valid JSON"
        )
    if not isinstance(document, dict):
        return EmbeddingOutcome(
            Outcome.PROVIDER_REJECTED, detail="provider response is not a JSON object"
        )
    return document


def _status_failure(response: HttpResponse) -> EmbeddingOutcome | None:
    status = response.status
    if 200 <= status < 300:
        return None
    if status == 429:
        return EmbeddingOutcome(
            Outcome.PROVIDER_DEGRADED,
            detail="provider is rate limiting this role",
            retry_after_seconds=_retry_after_seconds(response.headers),
        )
    if 500 <= status <= 599:
        return EmbeddingOutcome(
            Outcome.PROVIDER_UNAVAILABLE, detail=f"provider returned status {status}"
        )
    return EmbeddingOutcome(
        Outcome.PROVIDER_REJECTED,
        detail=f"provider returned status {status}: {_excerpt(response.body)}",
    )


class _RoleEmbeddingProvider:
    """Shared plumbing for the HTTP adapters.

    Constructors never raise for a disabled or unconfigured role; every
    configuration problem surfaces as a typed outcome from ``embed_*``.
    """

    provider_id = ""
    family = ""
    locality = "remote"
    required_kind = ""
    endpoint_suffix = ""
    requires_credential = True

    def __init__(
        self,
        role: ProviderRoleSettings,
        *,
        transport: HttpTransport | None = None,
        credential: str | None = None,
    ) -> None:
        self._role = role
        self._credential = credential
        self._transport = transport
        if self._transport is None:
            self._transport = urllib_transport(role.egress_policy())

    def capabilities(self) -> ProviderCapabilities:
        role = self._role
        return ProviderCapabilities(
            provider_id=self.provider_id,
            locality=self.locality,
            roles=(role.role,),
            max_batch=role.max_batch,
            max_input_chars=role.max_input_chars,
            reports_usage=True,
            offline=self.locality == "local",
            models=(role.model,) if role.model else (),
        )

    def _preflight(self, texts: Sequence[str]) -> EmbeddingOutcome | None:
        role = self._role
        if not role.enabled:
            return EmbeddingOutcome(
                Outcome.CONFIGURATION_MISSING,
                detail=f"provider role '{role.role}' is disabled",
            )
        if role.provider_kind != self.required_kind:
            return EmbeddingOutcome(
                Outcome.CONFIGURATION_MISSING,
                detail=(
                    f"provider role '{role.role}' is configured as "
                    f"'{role.provider_kind}', not '{self.required_kind}'"
                ),
            )
        if not role.base_url:
            return EmbeddingOutcome(
                Outcome.CONFIGURATION_MISSING,
                detail=f"provider role '{role.role}' has no base_url configured",
            )
        if not texts:
            return EmbeddingOutcome(
                Outcome.INPUT_REJECTED, detail="empty embedding batch"
            )
        if len(texts) > role.max_batch:
            return EmbeddingOutcome(
                Outcome.INPUT_REJECTED,
                detail=f"batch of {len(texts)} exceeds max_batch {role.max_batch}",
            )
        if any(len(text) > role.max_input_chars for text in texts):
            return EmbeddingOutcome(
                Outcome.INPUT_REJECTED,
                detail=f"a text exceeds max_input_chars {role.max_input_chars}",
            )
        return None

    def _authorization(self, headers: dict[str, str]) -> EmbeddingOutcome | None:
        role = self._role
        credential = self._credential or resolve_credential(role)
        if credential:
            headers["Authorization"] = f"Bearer {credential}"
            return None
        if self.requires_credential or role.credential_env or role.credential_file:
            reference = role.credential_env or role.credential_file or "credential"
            return EmbeddingOutcome(
                Outcome.CONFIGURATION_MISSING,
                detail=(
                    f"credential '{reference}' for provider role '{role.role}' "
                    "is unresolvable"
                ),
            )
        return None

    async def _post(
        self, payload: dict[str, object], *, deadline: float
    ) -> HttpResponse | EmbeddingOutcome:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return EmbeddingOutcome(
                Outcome.PROVIDER_TIMEOUT, detail="deadline passed before dispatch"
            )
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "cortex-v2-processing/1",
        }
        failure = self._authorization(headers)
        if failure is not None:
            return failure
        request = HttpRequest(
            "POST",
            f"{self._role.base_url.rstrip('/')}{self.endpoint_suffix}",
            headers=headers,
            body=json.dumps(payload).encode("utf-8"),
            timeout=min(remaining, self._role.read_timeout_seconds),
        )
        try:
            result = self._transport(request)
            if inspect.isawaitable(result):
                result = await result
            return result
        except EgressDenied as exc:
            return EmbeddingOutcome(
                Outcome.CONFIGURATION_MISSING, detail=f"egress denied: {exc.reason}"
            )
        except TransportTimeout as exc:
            return EmbeddingOutcome(Outcome.PROVIDER_TIMEOUT, detail=exc.reason)
        except TransportResponseTooLarge as exc:
            return EmbeddingOutcome(Outcome.PROVIDER_REJECTED, detail=exc.reason)
        except TransportError as exc:
            return EmbeddingOutcome(Outcome.PROVIDER_UNAVAILABLE, detail=exc.reason)

    def _model_failure(self, model: object) -> EmbeddingOutcome | None:
        if model == self._role.model:
            return None
        return EmbeddingOutcome(
            Outcome.PROVIDER_REJECTED,
            detail=(
                f"response model {model!r} does not match the configured model "
                f"{self._role.model!r}"
            ),
        )


class OpenAICompatibleEmbeddingProvider(_RoleEmbeddingProvider):
    """Adapter for the OpenAI ``/embeddings`` contract.

    The request never carries a ``dimensions`` field (R24): the space pins
    the dimensionality and the response must already match it.
    """

    provider_id = PROVIDER_OPENAI_COMPATIBLE
    family = PROVIDER_KIND_OPENAI_COMPATIBLE
    locality = "remote"
    required_kind = PROVIDER_KIND_OPENAI_COMPATIBLE
    endpoint_suffix = "/embeddings"
    requires_credential = True

    async def embed_documents(
        self,
        space: SpaceContract,
        texts: tuple[str, ...],
        *,
        deadline: float,
    ) -> EmbeddingOutcome:
        return await self._embed(space, texts, PURPOSE_DOCUMENT, deadline)

    async def embed_query(
        self,
        space: SpaceContract,
        text: str,
        *,
        deadline: float,
    ) -> EmbeddingOutcome:
        return await self._embed(space, (text,), PURPOSE_QUERY, deadline)

    async def _embed(
        self,
        space: SpaceContract,
        texts: Sequence[str],
        purpose: str,
        deadline: float,
    ) -> EmbeddingOutcome:
        failure = self._preflight(texts)
        if failure is not None:
            return failure
        prepared = _prepare(space, texts, purpose)
        payload = {"model": self._role.model, "input": list(prepared)}
        response = await self._post(payload, deadline=deadline)
        if isinstance(response, EmbeddingOutcome):
            return response
        failure = _status_failure(response)
        if failure is not None:
            return failure
        return self._interpret(space, prepared, response)

    def _interpret(
        self, space: SpaceContract, texts: Sequence[str], response: HttpResponse
    ) -> EmbeddingOutcome:
        document = _json_object(response)
        if isinstance(document, EmbeddingOutcome):
            return document
        model = document.get("model")
        failure = self._model_failure(model)
        if failure is not None:
            return failure
        data = document.get("data")
        if not isinstance(data, list) or len(data) != len(texts):
            got = len(data) if isinstance(data, list) else "a non-list payload"
            return EmbeddingOutcome(
                Outcome.PROVIDER_REJECTED,
                detail=f"expected {len(texts)} embedding rows, got {got}",
            )
        vectors: list[tuple[float, ...]] = []
        for position, item in enumerate(data):
            if not isinstance(item, dict) or item.get("index") != position:
                return EmbeddingOutcome(
                    Outcome.PROVIDER_REJECTED,
                    detail=(
                        f"embedding row {position} carries the wrong index order; "
                        "row order must match the input batch"
                    ),
                )
            vector = _coerce_vector(item.get("embedding"))
            if vector is None:
                return EmbeddingOutcome(
                    Outcome.PROVIDER_REJECTED,
                    detail=f"embedding row {position} is not a list of JSON numbers",
                )
            vectors.append(vector)
        invalid = validate_provider_output(space, texts, vectors)
        if invalid is not None:
            return EmbeddingOutcome(
                invalid,
                detail=(
                    "embeddings do not match the space contract "
                    f"({space.dimensions} dimensions, finite values)"
                ),
            )
        return EmbeddingOutcome(
            Outcome.OK,
            vectors=tuple(vectors),
            model_revision=model if isinstance(model, str) else None,
            usage=_usage(document.get("usage")),
        )


class OllamaEmbeddingProvider(_RoleEmbeddingProvider):
    """Adapter for a local Ollama ``/api/embed`` server (R25).

    The model must be present in the configured local manifests with a
    usable status; a cold start (the response reports a load) surfaces as
    ``degraded=("cold_start",)`` on an otherwise OK outcome.
    """

    provider_id = PROVIDER_OLLAMA
    family = PROVIDER_KIND_OLLAMA
    locality = "local"
    required_kind = PROVIDER_KIND_OLLAMA
    endpoint_suffix = "/api/embed"
    requires_credential = False

    def __init__(
        self,
        role: ProviderRoleSettings,
        *,
        local: LocalInferenceSettings,
        transport: HttpTransport | None = None,
        credential: str | None = None,
    ) -> None:
        super().__init__(role, transport=transport, credential=credential)
        self._local = local

    async def embed_documents(
        self,
        space: SpaceContract,
        texts: tuple[str, ...],
        *,
        deadline: float,
    ) -> EmbeddingOutcome:
        return await self._embed(space, texts, PURPOSE_DOCUMENT, deadline)

    async def embed_query(
        self,
        space: SpaceContract,
        text: str,
        *,
        deadline: float,
    ) -> EmbeddingOutcome:
        return await self._embed(space, (text,), PURPOSE_QUERY, deadline)

    def _activation_failure(self) -> EmbeddingOutcome | None:
        model = self._role.model
        if not model:
            return EmbeddingOutcome(
                Outcome.EXECUTOR_NOT_ACTIVATED,
                detail=f"provider role '{self._role.role}' has no model configured",
            )
        manifest = self._local.manifest_for(model)
        if manifest is None or manifest.status not in USABLE_MODEL_STATUSES:
            return EmbeddingOutcome(
                Outcome.EXECUTOR_NOT_ACTIVATED,
                detail=(
                    f"local model '{model}' is not present with status "
                    "'installed'/'activated' in the configured model manifests"
                ),
            )
        return None

    async def _embed(
        self,
        space: SpaceContract,
        texts: Sequence[str],
        purpose: str,
        deadline: float,
    ) -> EmbeddingOutcome:
        failure = self._preflight(texts)
        if failure is not None:
            return failure
        failure = self._activation_failure()
        if failure is not None:
            return failure
        prepared = _prepare(space, texts, purpose)
        payload = {"model": self._role.model, "input": list(prepared)}
        response = await self._post(payload, deadline=deadline)
        if isinstance(response, EmbeddingOutcome):
            return response
        failure = _status_failure(response)
        if failure is not None:
            return failure
        return self._interpret(space, prepared, response)

    def _interpret(
        self, space: SpaceContract, texts: Sequence[str], response: HttpResponse
    ) -> EmbeddingOutcome:
        document = _json_object(response)
        if isinstance(document, EmbeddingOutcome):
            return document
        model = document.get("model")
        failure = self._model_failure(model)
        if failure is not None:
            return failure
        raw = document.get("embeddings")
        if not isinstance(raw, list) or len(raw) != len(texts):
            got = len(raw) if isinstance(raw, list) else "a non-list payload"
            return EmbeddingOutcome(
                Outcome.PROVIDER_REJECTED,
                detail=f"expected {len(texts)} embeddings, got {got}",
            )
        vectors: list[tuple[float, ...]] = []
        for position, item in enumerate(raw):
            vector = _coerce_vector(item)
            if vector is None:
                return EmbeddingOutcome(
                    Outcome.PROVIDER_REJECTED,
                    detail=f"embedding {position} is not a list of JSON numbers",
                )
            vectors.append(vector)
        invalid = validate_provider_output(space, texts, vectors)
        if invalid is not None:
            return EmbeddingOutcome(
                invalid,
                detail=(
                    "embeddings do not match the space contract "
                    f"({space.dimensions} dimensions, finite values)"
                ),
            )
        degraded = ("cold_start",) if _cold_start(document) else ()
        usage: dict[str, int] = {}
        count = document.get("prompt_eval_count")
        if isinstance(count, int) and not isinstance(count, bool):
            usage["prompt_tokens"] = count
        return EmbeddingOutcome(
            Outcome.OK,
            vectors=tuple(vectors),
            model_revision=model if isinstance(model, str) else None,
            degraded=degraded,
            usage=usage,
        )


def _cold_start(document: dict) -> bool:
    load = document.get("load_duration")
    return isinstance(load, int | float) and not isinstance(load, bool) and load > 0


def _build_provider(
    role: ProviderRoleSettings, local: LocalInferenceSettings
) -> EmbeddingProvider | None:
    if role.provider_kind == PROVIDER_KIND_HASH_LOCAL:
        return HashEmbeddingProvider(
            max_batch=role.max_batch, max_input_chars=role.max_input_chars
        )
    if role.provider_kind == PROVIDER_KIND_OLLAMA:
        return OllamaEmbeddingProvider(role, local=local)
    if role.provider_kind == PROVIDER_KIND_OPENAI_COMPATIBLE:
        return OpenAICompatibleEmbeddingProvider(role)
    return None


def select_provider(
    settings: ProcessingSettings,
    space: SpaceContract,
    *,
    purpose: str = PURPOSE_DOCUMENT,
) -> tuple[EmbeddingProvider | None, str | None]:
    """Route the embedding role under the R25 policy and the F07/R24 space.

    Returns ``(provider, None)`` on success, otherwise ``(None, reason)``
    where reason is one of the exact typed codes (:data:`REASON_*`) — a
    :class:`SelectionReason` string whose ``.detail`` explains the decision.
    ``local_only`` never falls back to a remote provider; ``disabled``
    selects nothing; ``remote_allowed`` prefers local kinds.
    """
    if purpose not in PURPOSES:
        raise ValueError(f"unknown embedding purpose '{purpose}'")
    local = settings.local
    if local.routing_policy == ROUTING_DISABLED:
        return None, SelectionReason(
            REASON_ROUTING_DISABLED, "local inference routing is disabled"
        )
    role = settings.provider_for_role(ROLE_EMBEDDING)
    if role is None or not role.enabled or role.provider_kind == PROVIDER_KIND_NONE:
        return None, SelectionReason(
            REASON_ROLE_NOT_CONFIGURED,
            "the embedding provider role is disabled or has no provider kind",
        )
    if (
        role.provider_kind == PROVIDER_KIND_OPENAI_COMPATIBLE
        and local.routing_policy == ROUTING_LOCAL_ONLY
    ):
        return None, SelectionReason(
            REASON_LOCAL_ONLY_DENIES_REMOTE,
            "routing policy local_only forbids the remote provider kind "
            f"'{role.provider_kind}'; no local fallback is configured",
        )
    if role.provider_kind == PROVIDER_KIND_OLLAMA and not local.has_installed_model(
        role.model
    ):
        return None, SelectionReason(
            REASON_LOCAL_MODEL_NOT_INSTALLED,
            f"local model '{role.model}' is not installed or activated",
        )
    provider = _build_provider(role, local)
    if provider is None:
        return None, SelectionReason(
            REASON_ROLE_NOT_CONFIGURED,
            f"provider kind '{role.provider_kind}' has no adapter",
        )
    if space.provider != provider.family:
        return None, SelectionReason(
            REASON_SPACE_PROVIDER_MISMATCH,
            f"space provider family '{space.provider}' does not match adapter "
            f"family '{provider.family}' (space model_id '{space.model_id}'); "
            "the space needs its contracted provider or the role config is wrong",
        )
    if role.model and role.model != space.model_id:
        return None, SelectionReason(
            REASON_SPACE_PROVIDER_MISMATCH,
            f"space model_id '{space.model_id}' does not match the configured "
            f"model '{role.model}' for provider family '{provider.family}'; "
            "existing vectors must not be reinterpreted (R24)",
        )
    if not MIN_DIMENSIONS <= space.dimensions <= MAX_DIMENSIONS:
        return None, SelectionReason(
            REASON_DIMENSION_UNSUPPORTED,
            f"space dimensions {space.dimensions} are outside "
            f"{MIN_DIMENSIONS}..{MAX_DIMENSIONS}",
        )
    if role.provider_kind == PROVIDER_KIND_OLLAMA:
        manifest = local.manifest_for(role.model)
        if (
            manifest is not None
            and manifest.dimensions is not None
            and manifest.dimensions != space.dimensions
        ):
            return None, SelectionReason(
                REASON_DIMENSION_UNSUPPORTED,
                f"local model '{role.model}' is pinned to {manifest.dimensions} "
                f"dimensions but the space requires {space.dimensions}",
            )
    return provider, None


__all__ = [
    "DEFAULT_RETRY_AFTER_SECONDS",
    "MAX_DIMENSIONS",
    "MAX_RETRY_AFTER_SECONDS",
    "MIN_DIMENSIONS",
    "PROVIDER_FAMILIES",
    "PROVIDER_HASH_LOCAL",
    "PROVIDER_OLLAMA",
    "PROVIDER_OPENAI_COMPATIBLE",
    "PURPOSES",
    "PURPOSE_DOCUMENT",
    "PURPOSE_QUERY",
    "REASON_DIMENSION_UNSUPPORTED",
    "REASON_LOCAL_MODEL_NOT_INSTALLED",
    "REASON_LOCAL_ONLY_DENIES_REMOTE",
    "REASON_ROLE_NOT_CONFIGURED",
    "REASON_ROUTING_DISABLED",
    "REASON_SPACE_PROVIDER_MISMATCH",
    "HashEmbeddingProvider",
    "OllamaEmbeddingProvider",
    "OpenAICompatibleEmbeddingProvider",
    "SelectionReason",
    "hash_vector",
    "resolve_credential",
    "select_provider",
    "validate_provider_output",
]
