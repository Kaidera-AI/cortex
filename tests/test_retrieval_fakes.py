"""Shared fakes and corpus builders for retrieval unit tests.

The retrieval unit suite runs without a database: SQL-touching use cases are
exercised through ``FakeConnection`` route tables, and provider-facing ports
(vector stage, embedder, reranker, job queue) are spies. This module contains
no test functions; the ``test_retrieval_*_unit`` modules import from it.
"""

from __future__ import annotations

import datetime
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from cortex_v2.store import Principal, Scope, ScopeContext

# ---------------------------------------------------------------------------
# Fake connection
# ---------------------------------------------------------------------------


@dataclass
class Route:
    marker: str
    responder: Callable[..., Any]


class FakeConnection:
    """asyncpg-shaped stub routing queries by unique SQL marker substrings."""

    def __init__(self, routes: list[Route] | None = None):
        self.routes: list[Route] = list(routes or [])
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.executed: list[str] = []

    def add(self, marker: str, responder: Callable[..., Any] | list) -> None:
        if not callable(responder):
            rows = list(responder)
            responder = lambda *args: list(rows)  # noqa: E731
        self.routes.append(Route(marker, responder))

    def _respond(self, query: str, args: tuple[Any, ...]) -> Any:
        self.calls.append((query, args))
        for route in self.routes:
            if route.marker in query:
                return route.responder(*args)
        raise AssertionError(f"unrouted query in fake connection: {query[:160]}")

    async def fetch(self, query: str, *args: Any) -> list:
        return list(self._respond(query, args))

    async def fetchrow(self, query: str, *args: Any):
        rows = self._respond(query, args)
        return rows[0] if rows else None

    async def fetchval(self, query: str, *args: Any):
        rows = self._respond(query, args)
        if not rows:
            return None
        row = rows[0]
        if isinstance(row, dict):
            return next(iter(row.values()))
        return row

    async def execute(self, query: str, *args: Any) -> str:
        self.executed.append(query)
        self._respond(query, args)
        return "OK"

    def queries(self) -> list[str]:
        return [query for query, _ in self.calls]


# ---------------------------------------------------------------------------
# Scope context fixture
# ---------------------------------------------------------------------------

PROJECT_SCOPE_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
OTHER_SCOPE_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")
PRINCIPAL_ID = uuid.UUID("33333333-3333-4333-8333-333333333333")
INSTALLATION_ID = uuid.UUID("44444444-4444-4444-8444-444444444444")


def make_context(
    *, read_scope_ids: tuple[uuid.UUID, ...] = (PROJECT_SCOPE_ID,)
) -> ScopeContext:
    principal = Principal(PRINCIPAL_ID, INSTALLATION_ID)
    selected = Scope(
        alias="proj",
        scope_id=PROJECT_SCOPE_ID,
        kind="project",
        can_read=True,
        can_write=True,
        can_publish=False,
    )
    read_scopes = [selected]
    for scope_id in read_scope_ids:
        if scope_id != PROJECT_SCOPE_ID:
            read_scopes.append(
                Scope(
                    alias=f"scope-{str(scope_id)[:4]}",
                    scope_id=scope_id,
                    kind="project",
                    can_read=True,
                    can_write=False,
                    can_publish=False,
                )
            )
    return ScopeContext(principal, selected, tuple(read_scopes))


# ---------------------------------------------------------------------------
# Content rows / hydration
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ContentRow:
    scope_id: uuid.UUID
    content_id: uuid.UUID
    revision: int
    content_class: str
    body_text: str
    status: str = "current"
    authored_at: datetime.datetime = field(
        default_factory=lambda: datetime.datetime(
            2026, 9, 25, 12, 0, 0, tzinfo=datetime.timezone.utc
        )
    )

    @property
    def content_hash(self) -> bytes:
        return b"\xab" * 32

    def as_row(self) -> dict[str, Any]:
        return {
            "scope_id": self.scope_id,
            "content_id": self.content_id,
            "revision": self.revision,
            "content_class": self.content_class,
            "body_text": self.body_text,
            "content_hash": self.content_hash,
            "authored_at": self.authored_at,
            "status": self.status,
            "score": 0.5,
        }


def hydration_responder(
    authorized: dict[tuple[uuid.UUID, int], ContentRow],
) -> Callable[..., list[dict[str, Any]]]:
    """Simulates RLS-filtered hydration of (content_id, revision) targets.

    Rows whose (content_id, revision) is not in ``authorized`` are invisible,
    exactly as row level security would hide them from the application role.
    """

    def respond(scope_ids, content_ids, revisions) -> list[dict[str, Any]]:
        rows = []
        for content_id, revision in zip(content_ids, revisions, strict=True):
            entry = authorized.get((content_id, revision))
            if entry is not None:
                rows.append(entry.as_row())
        return rows

    return respond


def latest_revision_responder(
    authorized: dict[uuid.UUID, ContentRow],
) -> Callable[..., list[dict[str, Any]]]:
    def respond(content_ids) -> list[dict[str, Any]]:
        return [
            authorized[content_id].as_row()
            for content_id in content_ids
            if content_id in authorized
        ]

    return respond


# ---------------------------------------------------------------------------
# Spy ports
# ---------------------------------------------------------------------------


class SpyEmbedder:
    def __init__(
        self,
        *,
        space_id: uuid.UUID | None = None,
        vector: tuple[float, ...] = (0.1, 0.2, 0.3),
        fail: bool = False,
    ):
        self.space_id = space_id or uuid.UUID("55555555-5555-4555-8555-555555555555")
        self.vector = vector
        self.fail = fail
        self.embedded_texts: list[str] = []

    async def embed_query(self, text: str, *, deadline: float):
        from cortex_v2.retrieval.ports import QueryEmbed, QueryEmbeddingUnavailable

        self.embedded_texts.append(text)
        if self.fail:
            raise QueryEmbeddingUnavailable("provider not configured")
        return QueryEmbed(
            space_id=self.space_id, vector=self.vector, model_revision="fake-embed-1"
        )


class SpyVectorStage:
    def __init__(self, rows, *, unavailable: bool = False, fail: bool = False):
        self.rows = list(rows)
        self.unavailable = unavailable
        self.fail = fail
        self.calls: list[dict[str, Any]] = []

    async def candidates(self, connection, *, space_id, query_vector, limit):
        from cortex_v2.retrieval.ports import VectorStageUnavailable

        self.calls.append(
            {"space_id": space_id, "query_vector": query_vector, "limit": limit}
        )
        if self.unavailable:
            raise VectorStageUnavailable("cortex_processing contract is not installed")
        if self.fail:
            raise RuntimeError("vector stage exploded")
        return tuple(self.rows[:limit])


class SpyReranker:
    def __init__(self, *, fail: bool = False, reverse: bool = True):
        self.fail = fail
        self.reverse = reverse
        self.seen_snippets: list[str] = []

    @property
    def name(self) -> str:
        return "spy-reranker"

    @property
    def model_revision(self) -> str:
        return "fake-rerank-1"

    async def rerank(self, query: str, candidates, *, deadline: float):
        if self.fail:
            raise RuntimeError("reranker timed out")
        self.seen_snippets.extend(candidate.snippet for candidate in candidates)
        order = list(range(len(candidates)))
        return tuple(reversed(order) if self.reverse else order)


class FakeClock:
    """Monotonic clock advancing by ``step`` seconds on every read."""

    def __init__(self, step: float = 0.0):
        self.now = 1000.0
        self.step = step

    def __call__(self) -> float:
        value = self.now
        self.now += self.step
        return value


class FakeRegistry:
    """Mirrors cortex_v2.processing.registry.register_job_handler (final)."""

    def __init__(self):
        self.registered: list[dict[str, Any]] = []

    def register_job_handler(
        self,
        kind: str,
        role: str,
        handler: Any,
        *,
        publisher: Any = None,
        summary: str = "",
        required_intent: tuple[str, ...] = (),
    ) -> None:
        self.registered.append(
            {
                "kind": kind,
                "role": role,
                "handler": handler,
                "publisher": publisher,
                "summary": summary,
                "required_intent": tuple(required_intent),
            }
        )


def make_attempt_context(
    *,
    job_kind: str,
    scope_id: uuid.UUID = PROJECT_SCOPE_ID,
    content_id: uuid.UUID | None = None,
    source_revision: int = 1,
    payload: dict[str, Any] | None = None,
):
    from cortex_v2.processing.contracts import AttemptContext

    return AttemptContext(
        job_id=uuid.uuid4(),
        job_kind=job_kind,
        scope_id=scope_id,
        installation_id=INSTALLATION_ID,
        principal_id=PRINCIPAL_ID,
        content_id=content_id,
        source_revision=source_revision,
        fencing_epoch=3,
        lease_owner="worker-graph-1",
        attempt_id=uuid.uuid4(),
        role="graph",
        intent={"payload": payload or {}},
        deadline=1000.0,
        attempt_number=1,
    )


class FakeAttemptServices:
    """Implements the AttemptServices methods graph handlers may call."""

    role = "graph"

    def __init__(self, source=None):
        self.source = source
        self.blocking_calls: list[str] = []

    async def read_source(self, context):
        return self.source

    async def run_blocking(self, function, *args):
        self.blocking_calls.append(getattr(function, "__name__", repr(function)))
        return function(*args)


def vector_candidate(
    content_id: uuid.UUID, revision: int = 1, distance: float = 0.25
):
    from cortex_v2.retrieval.ports import VectorCandidate

    return VectorCandidate(
        content_id=content_id,
        revision=revision,
        chunk_id=uuid.uuid4(),
        distance=distance,
    )
