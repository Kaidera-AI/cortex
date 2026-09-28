"""Outward ports for the retrieval planner.

Every provider- or module-facing dependency is a named port with typed
unavailability: a missing embedder, an uninstalled vector contract and a
dimension mismatch are distinct, reportable states — never a silent empty
result and never a mixed vector space (F07/F10).
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from typing import Any, Protocol, Sequence, runtime_checkable

import asyncpg

# SQLSTATEs that mean "the published processing contract cannot serve this
# request right now": undefined function/schema, or the typed 22023 the
# vector_candidates contract raises for an unavailable space or bad limit.
_VECTOR_CONTRACT_ABSENT = frozenset({"42883", "42P01", "3F000", "22023"})
_VECTOR_DIMENSION_MISMATCH = "23514"


class VectorStageUnavailable(RuntimeError):
    """The vector candidate contract cannot serve this search right now."""


class VectorDimensionMismatch(RuntimeError):
    """The query embedding does not match the space's declared dimensions."""


class QueryEmbeddingUnavailable(RuntimeError):
    """No embedder is configured or the embedder refused the query."""


class JobQueueUnavailable(RuntimeError):
    """The durable processing queue is not importable in this build."""


@dataclass(frozen=True, slots=True)
class QueryEmbed:
    """One query embedded in exactly one immutable space (F07)."""

    space_id: uuid.UUID
    vector: tuple[float, ...]
    model_revision: str


@dataclass(frozen=True, slots=True)
class VectorCandidate:
    content_id: uuid.UUID
    revision: int
    chunk_id: uuid.UUID
    distance: float


@runtime_checkable
class QueryEmbedder(Protocol):
    """Embeds the caller's query text — and nothing else — for one space."""

    async def embed_query(self, text: str, *, deadline: float) -> QueryEmbed: ...


@runtime_checkable
class VectorStage(Protocol):
    """Returns ranked candidates from one explicitly named space."""

    async def candidates(
        self,
        connection: asyncpg.Connection,
        *,
        space_id: uuid.UUID,
        query_vector: Sequence[float],
        limit: int,
    ) -> tuple[VectorCandidate, ...]: ...


@dataclass(frozen=True, slots=True)
class RerankItem:
    """One already-authorized, already-hydrated candidate for reranking."""

    identity: str
    snippet: str


@runtime_checkable
class Reranker(Protocol):
    """Optional bounded rerank port; disabled by default."""

    @property
    def name(self) -> str: ...

    @property
    def model_revision(self) -> str: ...

    async def rerank(
        self,
        query: str,
        candidates: tuple[RerankItem, ...],
        *,
        deadline: float,
    ) -> tuple[int, ...]:
        """Return a permutation of ``range(len(candidates))``."""


@dataclass(frozen=True, slots=True)
class ExtractedAssertion:
    """One deterministic relation observation with its exact evidence span."""

    subject_key: str
    subject_type: str
    relation: str
    object_key: str
    object_type: str
    span_start: int
    span_end: int


@runtime_checkable
class ExtractionPort(Protocol):
    """Deterministic (now) or model-backed (later) relation extraction."""

    profile: str

    def extract(self, body: str) -> tuple[ExtractedAssertion, ...]: ...


def vector_literal(vector: Sequence[float]) -> str:
    """pgvector text input; never truncates, pads or reorders dimensions."""
    for value in vector:
        if not math.isfinite(value):
            raise VectorDimensionMismatch("query vector contains a non-finite value")
    return "[" + ",".join(repr(float(value)) for value in vector) + "]"


class ProcessingVectorStage:
    """VectorStage over the published ``cortex_processing.vector_candidates``
    contract (invoker-rights, RLS-applied, active generation only)."""

    async def candidates(
        self,
        connection: asyncpg.Connection,
        *,
        space_id: uuid.UUID,
        query_vector: Sequence[float],
        limit: int,
    ) -> tuple[VectorCandidate, ...]:
        if not 1 <= limit <= 1000:
            raise VectorStageUnavailable("vector candidate limit is out of range")
        try:
            rows = await connection.fetch(
                """
                SELECT content_id, revision, chunk_id, distance
                  FROM cortex_processing.vector_candidates(
                       $1::uuid, $2::text::vector, $3::integer)
                """,
                space_id,
                vector_literal(query_vector),
                limit,
            )
        except asyncpg.PostgresError as exc:
            sqlstate = exc.sqlstate or ""
            if sqlstate in _VECTOR_CONTRACT_ABSENT:
                raise VectorStageUnavailable(
                    "the processing vector contract cannot serve this space"
                ) from exc
            if sqlstate == _VECTOR_DIMENSION_MISMATCH:
                raise VectorDimensionMismatch(
                    "query embedding dimensions do not match the space"
                ) from exc
            raise
        return tuple(
            VectorCandidate(
                content_id=row["content_id"],
                revision=row["revision"],
                chunk_id=row["chunk_id"],
                distance=float(row["distance"]),
            )
            for row in rows
        )


async def read_space_state(
    connection: asyncpg.Connection, space_id: uuid.UUID
) -> dict[str, Any] | None:
    """Explain vector coverage/degradation from the processing module's
    published ``space_state`` read model; None when it cannot be read."""
    try:
        row = await connection.fetchrow(
            """
            SELECT space_id, dimensions, metric, normalization, provider,
                   model_id, model_revision, active_generation_number, state
              FROM cortex_processing.space_state($1::uuid)
            """,
            space_id,
        )
    except asyncpg.PostgresError:
        return None
    if row is None:
        return None
    return {
        "space_id": str(row["space_id"]),
        "dimensions": row["dimensions"],
        "metric": row["metric"],
        "normalization": row["normalization"],
        "provider": row["provider"],
        "model_id": row["model_id"],
        "model_revision": row["model_revision"],
        "active_generation_number": row["active_generation_number"],
        "state": row["state"],
    }
