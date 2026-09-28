"""Hybrid retrieval planner (R14/F10).

One bounded planner chooses deterministic stages per intent — exact-ID,
lexical (the W1 canonical projection), trigram, vector (through the
processing module's published contract) and bounded memory-graph expansion.
Invariants enforced here:

* scope filtering happens before any provider/model call, and candidates are
  re-hydrated from canonical revisions before they are exposed or sent to a
  reranker;
* hit identity is (scope, content, revision, span) — never display text;
* ranked stage lists are merged with documented reciprocal-rank fusion;
* reranking is an optional bounded port, disabled by default, and its failure
  is reported instead of silently skipped;
* every stage outcome, coverage fact and degradation is explained in the
  response; a vector score is a ranking signal, never a truth claim, and
  vector spaces are never mixed silently.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Sequence

import asyncpg

from ..content import search_content
from ..store import ApiProblem, ScopeContext
from .models import InspectHitRequest, MemorySearchRequest
from .ports import (
    ProcessingVectorStage,
    QueryEmbeddingUnavailable,
    QueryEmbedder,
    Reranker,
    RerankItem,
    VectorDimensionMismatch,
    VectorStage,
    VectorStageUnavailable,
)

FUSION_K = 60
MAX_EXCERPT_CHARS = 640

INTENT_STAGES: dict[str, tuple[str, ...]] = {
    "known-item": ("exact", "lexical", "trigram"),
    "symbol": ("exact", "lexical", "trigram"),
    "concept": ("exact", "lexical", "trigram", "vector", "graph"),
    "relationship": ("exact", "lexical", "trigram", "vector", "graph"),
    "work-history": ("exact", "lexical", "trigram"),
}


@dataclass(frozen=True, slots=True)
class HitKey:
    """Canonical hit identity: source revision plus citation span."""

    scope_id: uuid.UUID
    content_id: uuid.UUID
    revision: int
    span_start: int
    span_end: int


@dataclass(frozen=True, slots=True)
class FusedHit:
    key: HitKey
    score: float
    stages: tuple[str, ...]
    ranks: tuple[tuple[str, int], ...]


def reciprocal_rank_fusion(
    ranked: dict[str, Sequence[HitKey]], k: int = FUSION_K
) -> list[FusedHit]:
    """Documented rank fusion: score = sum over stages of 1/(k + rank).

    Ties break deterministically on stage count, content id, newest revision
    and span position — never on insertion order or display text.
    """
    contributions: dict[HitKey, dict[str, int]] = {}
    for stage, keys in ranked.items():
        for rank, key in enumerate(keys, start=1):
            ranks = contributions.setdefault(key, {})
            ranks[stage] = min(ranks.get(stage, rank), rank)
    fused = [
        FusedHit(
            key=key,
            score=sum(1.0 / (k + rank) for rank in ranks.values()),
            stages=tuple(sorted(ranks)),
            ranks=tuple(sorted(ranks.items())),
        )
        for key, ranks in contributions.items()
    ]
    fused.sort(
        key=lambda hit: (
            -hit.score,
            -len(hit.stages),
            hit.key.content_id.bytes,
            -hit.key.revision,
            hit.key.span_start,
            hit.key.span_end,
        )
    )
    return fused


@dataclass(slots=True)
class _StageOutcome:
    status: str
    keys: list[HitKey]
    detail: str | None = None
    degraded: tuple[str, ...] = ()


_HYDRATE_SELECT = """
        SELECT r.scope_id, r.content_id, r.revision, i.content_class,
               r.body_text, r.content_hash, r.authored_at,
               cortex_core.content_current_status(r.scope_id, r.content_id)
                   AS status
"""


async def _fetch_latest_revisions(
    connection: asyncpg.Connection, content_ids: Sequence[uuid.UUID]
) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in await connection.fetch(
            """
            SELECT r.scope_id, r.content_id, r.revision, i.content_class,
                   r.body_text, r.content_hash, r.authored_at,
                   cortex_core.content_current_status(r.scope_id, r.content_id)
                       AS status
              FROM cortex_core.content_revisions AS r
              JOIN cortex_core.content_items AS i
                ON i.scope_id = r.scope_id
               AND i.content_id = r.content_id
             WHERE r.content_id = ANY($1::uuid[])
               AND r.revision = (
                   SELECT max(latest.revision)
                     FROM cortex_core.content_revisions AS latest
                    WHERE latest.scope_id = r.scope_id
                      AND latest.content_id = r.content_id
               )
             ORDER BY array_position($1::uuid[], r.content_id)
            """,
            list(content_ids),
        )
    ]


async def _hydrate_targets(
    connection: asyncpg.Connection,
    scope_ids: Sequence[uuid.UUID],
    targets: Sequence[tuple[uuid.UUID, int]],
) -> dict[tuple[uuid.UUID, int], dict[str, Any]]:
    """Canonical re-check of derived candidates: only revisions visible to
    the caller's authorized scopes come back (RLS plus an explicit scope
    predicate). Missing targets are counted, never named."""
    unique = list(dict.fromkeys(targets))
    if not unique:
        return {}
    rows = await connection.fetch(
        _HYDRATE_SELECT
        + """
          FROM unnest($2::uuid[], $3::integer[])
               WITH ORDINALITY AS target(content_id, revision, ord)
          JOIN cortex_core.content_revisions AS r
            ON r.content_id = target.content_id
           AND r.revision = target.revision
           AND r.scope_id = ANY($1::uuid[])
          JOIN cortex_core.content_items AS i
            ON i.scope_id = r.scope_id
           AND i.content_id = r.content_id
         ORDER BY target.ord
        """,
        list(scope_ids),
        [content_id for content_id, _ in unique],
        [revision for _, revision in unique],
    )
    return {(row["content_id"], row["revision"]): dict(row) for row in rows}


async def _trigram_coverage(
    connection: asyncpg.Connection, scope_ids: Sequence[uuid.UUID]
) -> dict[str, Any]:
    row = await connection.fetchrow(
        """
        SELECT (
                   SELECT count(*)
                     FROM cortex_retrieval.trigram_documents AS coverage
                    WHERE coverage.scope_id = ANY($1::uuid[])
               ) AS document_count,
               (
                   SELECT count(*)
                     FROM cortex_core.content_items AS coverage_items
                    WHERE coverage_items.scope_id = ANY($1::uuid[])
                      AND cortex_core.content_current_status(
                              coverage_items.scope_id, coverage_items.content_id
                          ) = 'current'
               ) AS current_content_count
        """,
        list(scope_ids),
    )
    document_count = int(row["document_count"])
    current_count = int(row["current_content_count"])
    return {
        "document_count": document_count,
        "current_content_count": current_count,
        "stale": document_count < current_count,
    }


async def _trigram_search(
    connection: asyncpg.Connection,
    query: str,
    scope_ids: Sequence[uuid.UUID],
    limit: int,
) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in await connection.fetch(
            """
            SELECT t.scope_id, t.content_id, t.revision, t.source_text,
                   public.similarity(t.source_text, $1) AS score
              FROM cortex_retrieval.trigram_documents AS t
             WHERE t.scope_id = ANY($2::uuid[])
               AND t.source_text OPERATOR(public.%) $1
             ORDER BY score DESC, t.content_id
             LIMIT $3
            """,
            query,
            list(scope_ids),
            limit,
        )
    ]


async def _graph_expand(
    connection: asyncpg.Connection,
    scope_ids: Sequence[uuid.UUID],
    seed_content_ids: Sequence[uuid.UUID],
    limit: int,
) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in await connection.fetch(
            """
            WITH seed_assertions AS (
                SELECT DISTINCT evidence.assertion_id, evidence.scope_id
                  FROM cortex_retrieval.graph_assertion_evidence AS evidence
                 WHERE evidence.scope_id = ANY($1::uuid[])
                   AND evidence.content_id = ANY($2::uuid[])
            ), seed_entities AS (
                SELECT DISTINCT entity.entity_id
                  FROM cortex_retrieval.graph_assertions AS seeded
                  JOIN seed_assertions
                    ON seed_assertions.assertion_id = seeded.assertion_id
                   AND seed_assertions.scope_id = seeded.scope_id
                  CROSS JOIN LATERAL (
                      VALUES (seeded.subject_entity_id),
                             (seeded.object_entity_id)
                  ) AS entity(entity_id)
                 WHERE seeded.status = 'active'
                   AND seeded.scope_id = ANY($1::uuid[])
            )
            SELECT DISTINCT evidence.scope_id, evidence.content_id,
                   evidence.source_revision, evidence.span_start,
                   evidence.span_end, evidence.evidence_seq,
                   assertion.assertion_id,
                   assertion.relation,
                   subject.entity_key AS subject_key,
                   object.entity_key AS object_key
              FROM cortex_retrieval.graph_assertions AS assertion
              JOIN seed_entities
                ON seed_entities.entity_id = assertion.subject_entity_id
                OR seed_entities.entity_id = assertion.object_entity_id
              JOIN cortex_retrieval.graph_entities AS subject
                ON subject.scope_id = assertion.scope_id
               AND subject.entity_id = assertion.subject_entity_id
              JOIN cortex_retrieval.graph_entities AS object
                ON object.scope_id = assertion.scope_id
               AND object.entity_id = assertion.object_entity_id
              JOIN cortex_retrieval.graph_assertion_evidence AS evidence
                ON evidence.scope_id = assertion.scope_id
               AND evidence.assertion_id = assertion.assertion_id
             WHERE assertion.status = 'active'
               AND assertion.scope_id = ANY($1::uuid[])
             ORDER BY assertion.assertion_id, evidence.evidence_seq
             LIMIT $3
            """,
            list(scope_ids),
            list(seed_content_ids),
            limit,
        )
    ]


class RetrievalPlanner:
    """Bounded hybrid retrieval with explicit explanation."""

    def __init__(
        self,
        *,
        embedder: QueryEmbedder | None = None,
        vector_stage: VectorStage | None = None,
        reranker: Reranker | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._embedder = embedder
        self._vector_stage = vector_stage
        self._reranker = reranker
        self._clock = clock

    # -- search ------------------------------------------------------------

    async def search(
        self,
        connection: asyncpg.Connection,
        context: ScopeContext,
        request: MemorySearchRequest,
    ) -> dict[str, Any]:
        _check_scope_selection(context, request.read_scopes)
        scope_ids = [scope.scope_id for scope in context.read_scopes]
        scope_id_set = set(scope_ids)
        started = self._clock()
        deadline = started + request.deadline_ms / 1000.0

        plan = INTENT_STAGES[request.intent]
        stage_entries: list[dict[str, Any]] = []
        ranked: dict[str, list[HitKey]] = {}
        rows_by_key: dict[HitKey, dict[str, Any]] = {}
        counters = {
            "unauthorized_or_missing": 0,
            "non_current": 0,
            "content_class": 0,
        }
        degraded: list[str] = []
        coverage: dict[str, Any] = {
            "trigram_projection": None,
            "vector_space": None,
        }
        deadline_reported = False

        def admit(row: dict[str, Any]) -> bool:
            """Post-hydration admission: scope, freshness and type filters.
            Filtered candidates are counted, never named."""
            if row["scope_id"] not in scope_id_set:
                counters["unauthorized_or_missing"] += 1
                return False
            if not request.include_stale and row["status"] != "current":
                counters["non_current"] += 1
                return False
            if (
                request.content_classes is not None
                and row["content_class"] not in request.content_classes
            ):
                counters["content_class"] += 1
                return False
            return True

        def whole_document_key(row: dict[str, Any]) -> HitKey:
            return HitKey(
                scope_id=row["scope_id"],
                content_id=row["content_id"],
                revision=row["revision"],
                span_start=0,
                span_end=len(row["body_text"]),
            )

        def record(name: str, outcome: _StageOutcome, elapsed_ms: float) -> None:
            entry: dict[str, Any] = {
                "stage": name,
                "status": outcome.status,
                "hit_count": len(outcome.keys),
                "elapsed_ms": round(elapsed_ms, 3),
            }
            if outcome.detail:
                entry["detail"] = outcome.detail
            stage_entries.append(entry)
            if outcome.keys:
                ranked[name] = outcome.keys
            for code in outcome.degraded:
                if code not in degraded:
                    degraded.append(code)

        def out_of_time(name: str) -> bool:
            nonlocal deadline_reported
            if self._clock() < deadline:
                return False
            record(
                name,
                _StageOutcome(
                    "deadline_skipped",
                    [],
                    detail="the request deadline was exhausted before this stage",
                ),
                0.0,
            )
            if not deadline_reported:
                deadline_reported = True
                degraded.append("deadline_exhausted")
            return True

        async def hydrate_stage(
            targets: Sequence[tuple[uuid.UUID, int]],
            spans: dict[tuple[uuid.UUID, int], tuple[int, int]] | None = None,
        ) -> list[HitKey]:
            rows = await _hydrate_targets(connection, scope_ids, targets)
            counters["unauthorized_or_missing"] += len(
                set(targets) - set(rows)
            )
            keys: list[HitKey] = []
            for content_id, revision in dict.fromkeys(targets):
                row = rows.get((content_id, revision))
                if row is None or not admit(row):
                    continue
                if spans is not None:
                    span_start, span_end = spans[(content_id, revision)]
                else:
                    span_start, span_end = 0, len(row["body_text"])
                key = HitKey(
                    scope_id=row["scope_id"],
                    content_id=content_id,
                    revision=revision,
                    span_start=span_start,
                    span_end=span_end,
                )
                rows_by_key[key] = row
                if key not in keys:
                    keys.append(key)
            return keys

        # -- exact-ID stage -------------------------------------------------
        if "exact" in plan:
            if out_of_time("exact"):
                pass
            else:
                before = self._clock()
                seeds = list(dict.fromkeys(request.content_ids or []))
                try:
                    parsed = uuid.UUID(request.query)
                except ValueError:
                    parsed = None
                if parsed is not None and parsed not in seeds:
                    seeds.append(parsed)
                if not seeds:
                    record(
                        "exact",
                        _StageOutcome(
                            "skipped", [], detail="no exact-id seed in the request"
                        ),
                        0.0,
                    )
                else:
                    rows = await _fetch_latest_revisions(connection, seeds)
                    counters["unauthorized_or_missing"] += len(seeds) - len(rows)
                    keys: list[HitKey] = []
                    for row in rows:
                        if not admit(row):
                            continue
                        key = whole_document_key(row)
                        rows_by_key[key] = row
                        keys.append(key)
                    record(
                        "exact",
                        _StageOutcome("completed", keys),
                        (self._clock() - before) * 1000.0,
                    )

        # -- lexical stage (reuses the W1 canonical projection) -------------
        if "lexical" in plan and not out_of_time("lexical"):
            before = self._clock()
            rows = await search_content(
                connection, request.query, request.limit * 2, request.include_stale
            )
            keys = []
            for row in rows:
                candidate = {
                    "scope_id": uuid.UUID(row["scope_id"]),
                    "content_id": uuid.UUID(row["content_id"]),
                    "revision": row["citation"]["revision"],
                    "content_class": row["content_class"],
                    "body_text": row["body"],
                    "content_hash": row["content_hash"],
                    "authored_at": row["authored_at"],
                    "status": row["status"],
                }
                if not admit(candidate):
                    continue
                key = whole_document_key(candidate)
                rows_by_key[key] = candidate
                keys.append(key)
            record(
                "lexical",
                _StageOutcome("completed", keys),
                (self._clock() - before) * 1000.0,
            )

        # -- trigram stage --------------------------------------------------
        if "trigram" in plan and not out_of_time("trigram"):
            before = self._clock()
            projection = await _trigram_coverage(connection, scope_ids)
            coverage["trigram_projection"] = projection
            stage_degraded: tuple[str, ...] = ()
            if projection["stale"]:
                stage_degraded = ("trigram_projection_stale",)
            rows = await _trigram_search(
                connection, request.query, scope_ids, request.limit * 2
            )
            targets: list[tuple[uuid.UUID, int]] = []
            for row in rows:
                if row["scope_id"] not in scope_id_set:
                    counters["unauthorized_or_missing"] += 1
                    continue
                targets.append((row["content_id"], row["revision"]))
            keys = await hydrate_stage(targets)
            record(
                "trigram",
                _StageOutcome("completed", keys, degraded=stage_degraded),
                (self._clock() - before) * 1000.0,
            )

        # -- vector stage (scope-filtered query embedding, one space) -------
        if "vector" in plan and not out_of_time("vector"):
            before = self._clock()
            outcome = await self._run_vector_stage(
                connection, request, coverage, hydrate_stage, deadline
            )
            record("vector", outcome, (self._clock() - before) * 1000.0)

        # -- bounded graph expansion ----------------------------------------
        if "graph" in plan and not out_of_time("graph"):
            before = self._clock()
            if request.graph_hops == 0:
                record(
                    "graph",
                    _StageOutcome(
                        "skipped", [], detail="graph expansion disabled by request"
                    ),
                    0.0,
                )
            else:
                seeds = list(
                    dict.fromkeys(
                        key.content_id for keys in ranked.values() for key in keys
                    )
                )
                if not seeds:
                    record(
                        "graph",
                        _StageOutcome(
                            "skipped", [], detail="no seed hits to expand from"
                        ),
                        0.0,
                    )
                else:
                    keys = []
                    seen_content = set(seeds)
                    frontier = seeds
                    for _hop in range(request.graph_hops):
                        if not frontier:
                            break
                        rows = await _graph_expand(
                            connection, scope_ids, frontier, request.limit * 2
                        )
                        spans: dict[tuple[uuid.UUID, int], tuple[int, int]] = {}
                        targets = []
                        next_frontier: list[uuid.UUID] = []
                        for row in rows:
                            target = (row["content_id"], row["source_revision"])
                            spans[target] = (row["span_start"], row["span_end"])
                            targets.append(target)
                            if target[0] not in seen_content:
                                seen_content.add(target[0])
                                next_frontier.append(target[0])
                        keys.extend(await hydrate_stage(targets, spans))
                        frontier = next_frontier
                    record(
                        "graph",
                        _StageOutcome("completed", list(dict.fromkeys(keys))),
                        (self._clock() - before) * 1000.0,
                    )

        fused = reciprocal_rank_fusion(ranked)
        hits = [
            _build_hit(item, rows_by_key[item.key])
            for item in fused[: request.limit]
            if item.key in rows_by_key
        ]
        rerank_state = await self._maybe_rerank(
            request.query, hits, deadline, degraded
        )
        total_elapsed = (self._clock() - started) * 1000.0
        return {
            "hits": hits,
            "intent": request.intent,
            "limit": request.limit,
            "stages": stage_entries,
            "stages_completed": [
                entry["stage"] for entry in stage_entries
                if entry["status"] == "completed"
            ],
            "fusion": {"method": "reciprocal_rank_fusion", "k": FUSION_K},
            "rerank": rerank_state,
            "coverage": {
                "read_scopes": [scope.alias for scope in context.read_scopes],
                "selected_scope": context.selected.alias,
                "trigram_projection": coverage["trigram_projection"],
                "vector_space": coverage["vector_space"],
                "filtered_unauthorized_or_missing": counters[
                    "unauthorized_or_missing"
                ],
                "filtered_non_current": counters["non_current"],
                "filtered_content_class": counters["content_class"],
            },
            "degraded": degraded,
            "cost": {"total_elapsed_ms": round(total_elapsed, 3)},
        }

    async def _run_vector_stage(
        self,
        connection: asyncpg.Connection,
        request: MemorySearchRequest,
        coverage: dict[str, Any],
        hydrate_stage: Callable[..., Any],
        deadline: float,
    ) -> _StageOutcome:
        if self._embedder is None:
            return _StageOutcome(
                "unavailable",
                [],
                detail="no query embedder is configured for this deployment",
                degraded=("vectors_not_configured",),
            )
        try:
            embed = await self._embedder.embed_query(
                request.query, deadline=deadline
            )
        except QueryEmbeddingUnavailable as exc:
            return _StageOutcome(
                "unavailable", [], detail=str(exc), degraded=("vectors_not_configured",)
            )
        coverage["vector_space"] = {
            "space_id": str(embed.space_id),
            "model_revision": embed.model_revision,
        }
        stage = self._vector_stage or ProcessingVectorStage()
        try:
            candidates = await stage.candidates(
                connection,
                space_id=embed.space_id,
                query_vector=embed.vector,
                limit=request.limit * 2,
            )
        except VectorStageUnavailable as exc:
            return _StageOutcome(
                "unavailable",
                [],
                detail=str(exc),
                degraded=("vector_stage_unavailable",),
            )
        except VectorDimensionMismatch as exc:
            return _StageOutcome(
                "failed", [], detail=str(exc), degraded=("vector_dimension_mismatch",)
            )
        except Exception:
            return _StageOutcome(
                "failed",
                [],
                detail="the vector candidate contract raised an unexpected error",
                degraded=("vector_stage_failed",),
            )
        ordered = sorted(
            candidates, key=lambda item: (item.distance, item.content_id.bytes)
        )
        keys = await hydrate_stage(
            [(item.content_id, item.revision) for item in ordered],
        )
        return _StageOutcome("completed", keys)

    async def _maybe_rerank(
        self,
        query: str,
        hits: list[dict[str, Any]],
        deadline: float,
        degraded: list[str],
    ) -> dict[str, Any]:
        if self._reranker is None:
            return {"status": "disabled"}
        if not hits:
            return {"status": "skipped_no_hits"}
        remaining = deadline - self._clock()
        if remaining <= 0:
            if "deadline_exhausted" not in degraded:
                degraded.append("deadline_exhausted")
            return {"status": "deadline_skipped"}
        items = tuple(
            RerankItem(
                identity=_citation_identity(hit["citation"]),
                snippet=hit["excerpt"],
            )
            for hit in hits
        )
        try:
            order = await self._reranker.rerank(query, items, deadline=remaining)
            if sorted(order) != list(range(len(hits))):
                raise ValueError("reranker returned an invalid permutation")
            hits[:] = [hits[index] for index in order]
        except Exception:
            if "rerank_failed" not in degraded:
                degraded.append("rerank_failed")
            return {
                "status": "failed",
                "reranker": getattr(self._reranker, "name", "unknown"),
            }
        return {
            "status": "ok",
            "reranker": getattr(self._reranker, "name", "unknown"),
            "model_revision": getattr(self._reranker, "model_revision", None),
        }

    # -- inspect -------------------------------------------------------------

    async def inspect_hit(
        self,
        connection: asyncpg.Connection,
        context: ScopeContext,
        request: InspectHitRequest,
    ) -> dict[str, Any]:
        citation = request.citation
        readable = {scope.scope_id for scope in context.read_scopes}
        if citation.scope_id is not None and citation.scope_id not in readable:
            raise _hit_not_found()
        scope_ids = (
            [citation.scope_id] if citation.scope_id is not None else list(readable)
        )
        rows = await _hydrate_targets(
            connection, scope_ids, [(citation.content_id, citation.revision)]
        )
        row = rows.get((citation.content_id, citation.revision))
        if row is None:
            raise _hit_not_found()
        body = row["body_text"]
        span_start = 0 if citation.span_start is None else citation.span_start
        span_end = len(body) if citation.span_end is None else citation.span_end
        if span_start < 0 or span_end > len(body) or span_end <= span_start:
            raise ApiProblem(
                422,
                "invalid_citation_span",
                "The citation span lies outside the preserved original.",
            )
        assertions: list[dict[str, Any]] = []
        if request.include_assertions:
            assertion_rows = await connection.fetch(
                """
                SELECT assertion.assertion_id, assertion.relation,
                       assertion.origin, assertion.status,
                       assertion.retraction_reason,
                       subject.entity_key AS subject_key,
                       subject.entity_type AS subject_type,
                       object.entity_key AS object_key,
                       object.entity_type AS object_type,
                       evidence.span_start, evidence.span_end
                  FROM cortex_retrieval.graph_assertion_evidence AS evidence
                  JOIN cortex_retrieval.graph_assertions AS assertion
                    ON assertion.scope_id = evidence.scope_id
                   AND assertion.assertion_id = evidence.assertion_id
                  JOIN cortex_retrieval.graph_entities AS subject
                    ON subject.scope_id = assertion.scope_id
                   AND subject.entity_id = assertion.subject_entity_id
                  JOIN cortex_retrieval.graph_entities AS object
                    ON object.scope_id = assertion.scope_id
                   AND object.entity_id = assertion.object_entity_id
                 WHERE evidence.scope_id = ANY($1::uuid[])
                   AND evidence.content_id = $2
                   AND evidence.source_revision = $3
                 ORDER BY assertion.assertion_id, evidence.evidence_seq
                 LIMIT 32
                """,
                scope_ids,
                citation.content_id,
                citation.revision,
            )
            assertions = [
                {
                    "assertion_id": str(item["assertion_id"]),
                    "relation": item["relation"],
                    "origin": item["origin"],
                    "status": item["status"],
                    "retraction_reason": item["retraction_reason"],
                    "subject_key": item["subject_key"],
                    "subject_type": item["subject_type"],
                    "object_key": item["object_key"],
                    "object_type": item["object_type"],
                    "span_start": item["span_start"],
                    "span_end": item["span_end"],
                }
                for item in assertion_rows
            ]
        return {
            "citation": {
                "scope_id": str(row["scope_id"]),
                "content_id": str(row["content_id"]),
                "revision": row["revision"],
                "span_start": span_start,
                "span_end": span_end,
            },
            "content_class": row["content_class"],
            "status": row["status"],
            "authored_at": _iso(row["authored_at"]),
            "content_hash": _hex(row["content_hash"]),
            "body": body,
            "span_text": body[span_start:span_end],
            "supporting_assertions": assertions,
            "degraded": [],
        }


def _citation_identity(citation: dict[str, Any]) -> str:
    return ":".join(
        (
            citation["scope_id"],
            citation["content_id"],
            str(citation["revision"]),
            str(citation["span_start"]),
            str(citation["span_end"]),
        )
    )


def _build_hit(item: FusedHit, row: dict[str, Any]) -> dict[str, Any]:
    body = row["body_text"]
    excerpt = body[item.key.span_start : item.key.span_end][:MAX_EXCERPT_CHARS]
    return {
        "citation": {
            "scope_id": str(item.key.scope_id),
            "content_id": str(item.key.content_id),
            "revision": item.key.revision,
            "span_start": item.key.span_start,
            "span_end": item.key.span_end,
        },
        "content_class": row["content_class"],
        "status": row["status"],
        "authored_at": _iso(row["authored_at"]),
        "excerpt": excerpt,
        "score": round(item.score, 9),
        "stages": list(item.stages),
        "ranks": {stage: rank for stage, rank in item.ranks},
    }


def _iso(value: Any) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _hex(value: Any) -> str:
    return value.hex() if hasattr(value, "hex") else str(value)


def _check_scope_selection(
    context: ScopeContext, requested: Sequence[str] | None
) -> None:
    if requested is None:
        return
    if set(requested) != {scope.alias for scope in context.read_scopes}:
        raise ApiProblem(
            422,
            "invalid_scope_selection",
            "The requested read scopes do not match the resolved authorization "
            "context.",
        )


def _hit_not_found() -> ApiProblem:
    return ApiProblem(
        404,
        "hit_not_found",
        "The cited hit is unavailable in the selected scopes.",
    )
