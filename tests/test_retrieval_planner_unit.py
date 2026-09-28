"""Retrieval planner unit tests (R14/F10): hybrid stages, canonical hit
identity, reciprocal-rank fusion, scope-before-provider filtering, deadlines,
rerank port semantics and explicit coverage/degradation reporting.

Pure unit tests: no database, no network. SQL-touching paths run through the
route-based FakeConnection; provider-facing ports are spies.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from test_retrieval_fakes import (
    PROJECT_SCOPE_ID,
    OTHER_SCOPE_ID,
    ContentRow,
    FakeClock,
    FakeConnection,
    Route,
    SpyEmbedder,
    SpyReranker,
    SpyVectorStage,
    hydration_responder,
    latest_revision_responder,
    make_context,
    vector_candidate,
)

from cortex_v2.retrieval.models import (
    HitCitation,
    InspectHitRequest,
    MemorySearchRequest,
)
from cortex_v2.retrieval.planner import (
    FUSION_K,
    HitKey,
    RetrievalPlanner,
    reciprocal_rank_fusion,
)
from cortex_v2.store import ApiProblem


def run(coroutine):
    return asyncio.run(coroutine)


def key(content_id: uuid.UUID, revision: int = 1, span: tuple[int, int] = (0, 10)):
    return HitKey(
        scope_id=PROJECT_SCOPE_ID,
        content_id=content_id,
        revision=revision,
        span_start=span[0],
        span_end=span[1],
    )


# ---------------------------------------------------------------------------
# Fusion mathematics (pure)
# ---------------------------------------------------------------------------


def test_fusion_single_stage_uses_reciprocal_rank():
    a = key(uuid.uuid4())
    fused = reciprocal_rank_fusion({"lexical": [a]}, k=FUSION_K)
    assert len(fused) == 1
    assert fused[0].key == a
    assert fused[0].score == pytest.approx(1 / (FUSION_K + 1))
    assert fused[0].stages == ("lexical",)


def test_fusion_accumulates_stage_contributions_by_identity():
    a = key(uuid.uuid4())
    b = key(uuid.uuid4())
    fused = reciprocal_rank_fusion({"lexical": [a, b], "trigram": [b]}, k=FUSION_K)
    scores = {hit.key: hit.score for hit in fused}
    assert scores[b] == pytest.approx(1 / 61 + 1 / 62)
    assert scores[a] == pytest.approx(1 / 61)
    assert fused[0].key == b


def test_fusion_ties_break_more_stages_then_content_id():
    a = key(uuid.UUID("00000000-0000-4000-8000-00000000000a"))
    b = key(uuid.UUID("00000000-0000-4000-8000-00000000000b"))
    # Identical scores (one rank-1 contribution each): deterministic order.
    fused = reciprocal_rank_fusion({"lexical": [a], "trigram": [b]}, k=FUSION_K)
    assert [hit.key for hit in fused] == [a, b]
    again = reciprocal_rank_fusion({"lexical": [a], "trigram": [b]}, k=FUSION_K)
    assert [hit.key for hit in again] == [a, b]


def test_fusion_never_dedupes_distinct_spans_of_one_revision():
    content = uuid.uuid4()
    span_a = key(content, 1, (0, 10))
    span_b = key(content, 1, (10, 20))
    fused = reciprocal_rank_fusion({"graph": [span_a, span_b]})
    assert {hit.key for hit in fused} == {span_a, span_b}


# ---------------------------------------------------------------------------
# Planner orchestration
# ---------------------------------------------------------------------------


def build_corpus():
    lex = ContentRow(
        PROJECT_SCOPE_ID, uuid.uuid4(), 1, "knowledge", "ledger migration decision"
    )
    tri = ContentRow(
        PROJECT_SCOPE_ID, uuid.uuid4(), 1, "decision", "ledger rollback plan"
    )
    vec = ContentRow(PROJECT_SCOPE_ID, uuid.uuid4(), 1, "lesson", "schema drift lesson")
    graph_hit = ContentRow(
        PROJECT_SCOPE_ID, uuid.uuid4(), 1, "knowledge", "payments depends on ledger"
    )
    foreign = ContentRow(
        OTHER_SCOPE_ID, uuid.uuid4(), 1, "knowledge", "other project secret"
    )
    authorized = {
        (row.content_id, row.revision): row
        for row in (lex, tri, vec, graph_hit)
    }
    return lex, tri, vec, graph_hit, foreign, authorized


def planner_connection(corpus, *, expansion_rows=None, coverage=(4, 4)):
    lex, tri, vec, graph_hit, foreign, authorized = corpus
    conn = FakeConnection()
    conn.routes.extend(
        [
            Route(
                "content_lexical_documents",
                lambda *args: [lex.as_row()],
            ),
            Route(
                "public.similarity(",
                lambda *args: [
                    {
                        "scope_id": tri.scope_id,
                        "content_id": tri.content_id,
                        "revision": tri.revision,
                        "source_text": tri.body_text,
                        "score": 0.42,
                    },
                    # Row from a foreign scope: RLS would hide it; hydration
                    # must drop it even though the projection returned it.
                    {
                        "scope_id": foreign.scope_id,
                        "content_id": foreign.content_id,
                        "revision": foreign.revision,
                        "source_text": foreign.body_text,
                        "score": 0.40,
                    },
                ],
            ),
            Route(
                "AS coverage",
                lambda *args: [
                    {
                        "document_count": coverage[0],
                        "current_content_count": coverage[1],
                    }
                ],
            ),
            Route(
                "WITH ORDINALITY AS target",
                hydration_responder(authorized),
            ),
            Route(
                "max(latest.revision)",
                latest_revision_responder(
                    {row.content_id: row for row in (lex, tri, vec, graph_hit)}
                ),
            ),
            Route(
                "graph_assertion_evidence AS evidence",
                lambda *args: list(expansion_rows or []),
            ),
        ]
    )
    return conn


def search_request(**overrides):
    defaults = {
        "query": "ledger migration",
        "intent": "concept",
        "limit": 10,
        "graph_hops": 1,
        "deadline_ms": 5000,
    }
    defaults.update(overrides)
    return MemorySearchRequest(**defaults)


def test_concept_search_completes_all_stages_and_explains_them():
    corpus = build_corpus()
    conn = planner_connection(corpus)
    embedder = SpyEmbedder()
    vec_stage = SpyVectorStage(
        [vector_candidate(corpus[2].content_id, 1, 0.11)],
    )
    planner = RetrievalPlanner(embedder=embedder, vector_stage=vec_stage)
    result = run(planner.search(conn, make_context(), search_request()))

    by_stage = {entry["stage"]: entry for entry in result["stages"]}
    assert by_stage["lexical"]["status"] == "completed"
    assert by_stage["trigram"]["status"] == "completed"
    assert by_stage["vector"]["status"] == "completed"
    assert by_stage["exact"]["status"] == "skipped"
    assert by_stage["graph"]["status"] == "completed"
    assert set(result["stages_completed"]) == {"lexical", "trigram", "vector", "graph"}
    assert result["fusion"] == {"method": "reciprocal_rank_fusion", "k": FUSION_K}
    assert result["intent"] == "concept"
    hit_ids = {hit["citation"]["content_id"] for hit in result["hits"]}
    lex, tri, vec = corpus[0], corpus[1], corpus[2]
    assert {str(lex.content_id), str(tri.content_id), str(vec.content_id)} <= hit_ids


def test_scope_is_filtered_before_any_provider_call():
    corpus = build_corpus()
    lex, tri, vec, graph_hit, foreign, authorized = corpus
    conn = planner_connection(corpus)
    embedder = SpyEmbedder()
    vec_stage = SpyVectorStage(
        [
            vector_candidate(vec.content_id, 1, 0.11),
            vector_candidate(foreign.content_id, 1, 0.12),
        ]
    )
    reranker = SpyReranker(reverse=False)
    planner = RetrievalPlanner(
        embedder=embedder, vector_stage=vec_stage, reranker=reranker
    )
    result = run(planner.search(conn, make_context(), search_request()))

    # The embedder only ever sees the caller's query text, never content.
    assert embedder.embedded_texts == ["ledger migration"]
    # The vector stage receives exactly the space the embedder produced.
    assert vec_stage.calls[0]["space_id"] == embedder.space_id
    # The foreign-scope candidate never reaches the response or the reranker.
    assert str(foreign.content_id) not in str(result["hits"])
    assert all(foreign.body_text not in snippet for snippet in reranker.seen_snippets)
    assert result["coverage"]["filtered_unauthorized_or_missing"] >= 1
    # Existence-safe: the report carries counts, not foreign identifiers.
    assert str(foreign.content_id) not in str(result["coverage"])
    assert str(foreign.content_id) not in str(result["degraded"])


def test_no_silent_vector_space_mixing():
    corpus = build_corpus()
    conn = planner_connection(corpus)
    embedder = SpyEmbedder()
    planner = RetrievalPlanner(
        embedder=embedder,
        vector_stage=SpyVectorStage([vector_candidate(corpus[2].content_id)]),
    )
    result = run(planner.search(conn, make_context(), search_request()))
    space = result["coverage"]["vector_space"]
    assert space == {
        "space_id": str(embedder.space_id),
        "model_revision": "fake-embed-1",
    }

    # Without a configured embedder there is no vector coverage claim at all.
    conn2 = planner_connection(corpus)
    bare = RetrievalPlanner()
    result2 = run(bare.search(conn2, make_context(), search_request()))
    assert result2["coverage"]["vector_space"] is None
    assert "vectors_not_configured" in result2["degraded"]


def test_known_item_intent_does_not_call_vector_or_graph_stages():
    corpus = build_corpus()
    conn = planner_connection(corpus)
    embedder = SpyEmbedder()
    vec_stage = SpyVectorStage([vector_candidate(corpus[2].content_id)])
    planner = RetrievalPlanner(embedder=embedder, vector_stage=vec_stage)
    result = run(
        planner.search(conn, make_context(), search_request(intent="known-item"))
    )
    assert embedder.embedded_texts == []
    assert vec_stage.calls == []
    names = {entry["stage"] for entry in result["stages"]}
    assert "vector" not in names
    assert "graph" not in names


def test_exact_id_seed_resolves_deterministically():
    corpus = build_corpus()
    lex, tri, vec, graph_hit, foreign, authorized = corpus
    conn = planner_connection(corpus)
    planner = RetrievalPlanner()
    result = run(
        planner.search(
            conn,
            make_context(),
            search_request(
                intent="known-item",
                query=str(lex.content_id),
                content_ids=[lex.content_id, foreign.content_id],
            ),
        )
    )
    by_stage = {entry["stage"]: entry for entry in result["stages"]}
    assert by_stage["exact"]["status"] == "completed"
    assert by_stage["exact"]["hit_count"] == 1  # foreign id invisible, counted
    assert result["hits"][0]["citation"]["content_id"] == str(lex.content_id)
    assert result["coverage"]["filtered_unauthorized_or_missing"] >= 1


def test_hit_identity_dedupes_across_stages():
    corpus = build_corpus()
    lex, tri, vec, graph_hit, foreign, authorized = corpus
    same = ContentRow(PROJECT_SCOPE_ID, lex.content_id, 1, "knowledge", lex.body_text)
    authorized = dict(authorized)
    conn = FakeConnection()
    conn.routes.extend(
        [
            Route("content_lexical_documents", lambda *args: [lex.as_row()]),
            Route(
                "public.similarity(",
                lambda *args: [
                    {
                        "scope_id": same.scope_id,
                        "content_id": same.content_id,
                        "revision": same.revision,
                        "source_text": same.body_text,
                        "score": 0.4,
                    }
                ],
            ),
            Route(
                "AS coverage",
                lambda *args: [{"document_count": 1, "current_content_count": 1}],
            ),
            Route("WITH ORDINALITY AS target", hydration_responder(authorized)),
            Route("max(latest.revision)", lambda *args: []),
        ]
    )
    planner = RetrievalPlanner()
    result = run(
        planner.search(conn, make_context(), search_request(intent="known-item"))
    )
    matching = [
        hit
        for hit in result["hits"]
        if hit["citation"]["content_id"] == str(lex.content_id)
    ]
    assert len(matching) == 1
    assert set(matching[0]["stages"]) >= {"lexical", "trigram"}


def test_vector_stage_unavailable_degrades_without_failing_the_search():
    corpus = build_corpus()
    conn = planner_connection(corpus)
    planner = RetrievalPlanner(
        embedder=SpyEmbedder(), vector_stage=SpyVectorStage([], unavailable=True)
    )
    result = run(planner.search(conn, make_context(), search_request()))
    by_stage = {entry["stage"]: entry for entry in result["stages"]}
    assert by_stage["vector"]["status"] == "unavailable"
    assert "vector_stage_unavailable" in result["degraded"]
    assert result["hits"], "lexical results survive vector unavailability"


def test_vector_stage_error_is_reported_not_swallowed():
    corpus = build_corpus()
    conn = planner_connection(corpus)
    planner = RetrievalPlanner(
        embedder=SpyEmbedder(), vector_stage=SpyVectorStage([], fail=True)
    )
    result = run(planner.search(conn, make_context(), search_request()))
    by_stage = {entry["stage"]: entry for entry in result["stages"]}
    assert by_stage["vector"]["status"] == "failed"
    assert "vector_stage_failed" in result["degraded"]


def test_rerank_disabled_by_default():
    corpus = build_corpus()
    conn = planner_connection(corpus)
    planner = RetrievalPlanner(embedder=SpyEmbedder(), vector_stage=SpyVectorStage([]))
    result = run(planner.search(conn, make_context(), search_request()))
    assert result["rerank"]["status"] == "disabled"
    assert "rerank_failed" not in result["degraded"]


def test_rerank_failure_is_reported_and_fusion_order_survives():
    corpus = build_corpus()
    conn = planner_connection(corpus)
    planner = RetrievalPlanner(
        embedder=SpyEmbedder(),
        vector_stage=SpyVectorStage([]),
        reranker=SpyReranker(fail=True),
    )
    result = run(planner.search(conn, make_context(), search_request()))
    assert result["rerank"]["status"] == "failed"
    assert "rerank_failed" in result["degraded"]
    scores = [hit["score"] for hit in result["hits"]]
    assert scores == sorted(scores, reverse=True)


def test_rerank_success_reorders_and_sees_only_authorized_snippets():
    corpus = build_corpus()
    lex, tri, vec, graph_hit, foreign, authorized = corpus
    conn = planner_connection(corpus)
    reranker = SpyReranker(reverse=True)
    planner = RetrievalPlanner(
        embedder=SpyEmbedder(), vector_stage=SpyVectorStage([]), reranker=reranker
    )
    result = run(planner.search(conn, make_context(), search_request()))
    assert result["rerank"]["status"] == "ok"
    assert result["rerank"]["model_revision"] == "fake-rerank-1"
    assert reranker.seen_snippets
    assert all(foreign.body_text not in snippet for snippet in reranker.seen_snippets)


def test_deadline_skips_remaining_stages_with_explicit_degradation():
    corpus = build_corpus()
    conn = planner_connection(corpus)
    clock = FakeClock(step=0.02)
    planner = RetrievalPlanner(
        embedder=SpyEmbedder(), vector_stage=SpyVectorStage([]), clock=clock
    )
    result = run(
        planner.search(conn, make_context(), search_request(deadline_ms=50))
    )
    statuses = {entry["stage"]: entry["status"] for entry in result["stages"]}
    assert "deadline_skipped" in statuses.values()
    assert "deadline_exhausted" in result["degraded"]
    assert result["cost"]["total_elapsed_ms"] >= 0


def test_trigram_projection_staleness_is_reported():
    corpus = build_corpus()
    conn = planner_connection(corpus, coverage=(1, 4))
    planner = RetrievalPlanner()
    result = run(
        planner.search(conn, make_context(), search_request(intent="known-item"))
    )
    projection = result["coverage"]["trigram_projection"]
    assert projection == {
        "document_count": 1,
        "current_content_count": 4,
        "stale": True,
    }
    assert "trigram_projection_stale" in result["degraded"]


def test_non_current_content_is_excluded_by_default_and_counted():
    corpus = build_corpus()
    lex, tri, vec, graph_hit, foreign, authorized = corpus
    stale = ContentRow(
        PROJECT_SCOPE_ID, uuid.uuid4(), 1, "knowledge", "stale superseded guidance",
        status="superseded",
    )
    authorized[(stale.content_id, stale.revision)] = stale
    conn = planner_connection(corpus)
    conn.routes[0] = Route(
        "content_lexical_documents", lambda *args: [lex.as_row(), stale.as_row()]
    )
    planner = RetrievalPlanner()
    result = run(planner.search(conn, make_context(), search_request()))
    assert str(stale.content_id) not in str(result["hits"])
    assert result["coverage"]["filtered_non_current"] >= 1

    conn2 = planner_connection(corpus)
    conn2.routes[0] = Route(
        "content_lexical_documents", lambda *args: [lex.as_row(), stale.as_row()]
    )
    result2 = run(
        planner.search(conn2, make_context(), search_request(include_stale=True))
    )
    assert str(stale.content_id) in str(result2["hits"])
    stale_hit = next(
        hit
        for hit in result2["hits"]
        if hit["citation"]["content_id"] == str(stale.content_id)
    )
    assert stale_hit["status"] == "superseded"


def test_content_class_filter_applies_to_every_stage():
    corpus = build_corpus()
    lex, tri, vec, graph_hit, foreign, authorized = corpus
    conn = planner_connection(corpus)
    planner = RetrievalPlanner()
    result = run(
        planner.search(
            conn,
            make_context(),
            search_request(intent="known-item", content_classes=["decision"]),
        )
    )
    classes = {hit["content_class"] for hit in result["hits"]}
    assert classes <= {"decision"}
    assert result["coverage"]["filtered_content_class"] >= 1


def test_graph_expansion_contributes_evidence_span_hits():
    corpus = build_corpus()
    lex, tri, vec, graph_hit, foreign, authorized = corpus
    expansion = [
        {
            "scope_id": PROJECT_SCOPE_ID,
            "content_id": graph_hit.content_id,
            "source_revision": 1,
            "span_start": 0,
            "span_end": 30,
            "relation": "depends_on",
            "subject_key": "payments",
            "object_key": "ledger",
            "assertion_id": uuid.uuid4(),
        }
    ]
    conn = planner_connection(corpus, expansion_rows=expansion)
    planner = RetrievalPlanner(embedder=SpyEmbedder(), vector_stage=SpyVectorStage([]))
    result = run(planner.search(conn, make_context(), search_request()))
    by_stage = {entry["stage"]: entry for entry in result["stages"]}
    assert by_stage["graph"]["status"] == "completed"
    graph_hits = [hit for hit in result["hits"] if "graph" in hit["stages"]]
    assert graph_hits
    citation = graph_hits[0]["citation"]
    assert citation["content_id"] == str(graph_hit.content_id)
    assert (citation["span_start"], citation["span_end"]) == (0, 30)


def test_graph_stage_skips_without_seed_hits():
    corpus = build_corpus()
    conn = planner_connection(corpus)
    conn.routes[0] = Route("content_lexical_documents", lambda *args: [])
    conn.routes[1] = Route("public.similarity(", lambda *args: [])
    planner = RetrievalPlanner(embedder=SpyEmbedder(), vector_stage=SpyVectorStage([]))
    result = run(planner.search(conn, make_context(), search_request()))
    by_stage = {entry["stage"]: entry for entry in result["stages"]}
    assert by_stage["graph"]["status"] == "skipped"


def test_read_scope_mismatch_fails_closed():
    corpus = build_corpus()
    conn = planner_connection(corpus)
    planner = RetrievalPlanner()
    with pytest.raises(ApiProblem) as excinfo:
        run(
            planner.search(
                conn,
                make_context(),
                search_request(read_scopes=["some-other-scope"]),
            )
        )
    assert excinfo.value.status == 422


# ---------------------------------------------------------------------------
# memory.inspect-hit
# ---------------------------------------------------------------------------


def inspect_routes(row: ContentRow, assertions=()):
    conn = FakeConnection()
    conn.routes.extend(
        [
            Route(
                "WITH ORDINALITY AS target",
                hydration_responder({(row.content_id, row.revision): row}),
            ),
            Route("graph_assertions AS assertion", lambda *args: list(assertions)),
        ]
    )
    return conn


def test_inspect_hit_returns_original_span_and_supporting_assertions():
    row = ContentRow(
        PROJECT_SCOPE_ID,
        uuid.uuid4(),
        2,
        "decision",
        "Payments depends on Ledger for double-entry bookkeeping",
    )
    assertion = {
        "assertion_id": uuid.uuid4(),
        "relation": "depends_on",
        "subject_key": "payments",
        "subject_type": "concept",
        "object_key": "ledger",
        "object_type": "concept",
        "origin": "extracted",
        "status": "active",
        "retraction_reason": None,
        "span_start": 0,
        "span_end": 30,
    }
    conn = inspect_routes(row, [assertion])
    planner = RetrievalPlanner()
    request = InspectHitRequest(
        citation=HitCitation(
            content_id=row.content_id, revision=2, span_start=0, span_end=30
        )
    )
    result = run(planner.inspect_hit(conn, make_context(), request))
    assert result["citation"]["content_id"] == str(row.content_id)
    assert result["citation"]["revision"] == 2
    assert result["span_text"] == row.body_text[0:30]
    assert result["body"] == row.body_text
    assert result["status"] == "current"
    assert result["content_hash"] == row.content_hash.hex()
    assert result["supporting_assertions"][0]["relation"] == "depends_on"


def test_inspect_hit_rejects_span_outside_the_original():
    row = ContentRow(PROJECT_SCOPE_ID, uuid.uuid4(), 1, "knowledge", "short")
    conn = inspect_routes(row)
    planner = RetrievalPlanner()
    request = InspectHitRequest(
        citation=HitCitation(
            content_id=row.content_id, revision=1, span_start=0, span_end=999
        )
    )
    with pytest.raises(ApiProblem) as excinfo:
        run(planner.inspect_hit(conn, make_context(), request))
    assert excinfo.value.status == 422
    assert excinfo.value.code == "invalid_citation_span"


def test_inspect_hit_unknown_or_unauthorized_is_a_uniform_404():
    conn = FakeConnection()
    conn.add("WITH ORDINALITY AS target", [])
    conn.add("graph_assertions AS assertion", [])
    planner = RetrievalPlanner()
    request = InspectHitRequest(
        citation=HitCitation(content_id=uuid.uuid4(), revision=1)
    )
    with pytest.raises(ApiProblem) as excinfo:
        run(planner.inspect_hit(conn, make_context(), request))
    assert excinfo.value.status == 404
    assert excinfo.value.code == "hit_not_found"
