"""Regression coverage for L5 artifact retrieval in /search."""

from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path

import pytest


@pytest.fixture
def api_module():
    src = Path(__file__).resolve().parent.parent / "main.py"
    spec = importlib.util.spec_from_file_location("cortex_api_search_under_test", src)
    assert spec and spec.loader, f"could not load spec for {src}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeSearchConn:
    def __init__(self):
        self.artifact_query_seen = False

    async def fetch(self, sql, *args):
        if "FROM artifacts" not in sql:
            return []

        self.artifact_query_seen = True
        assert args == ("artifact sentinel", "kaidera", None)
        return [
            {
                "id": "11111111-1111-1111-1111-111111111111",
                "text": "Artifact sentinel caption from a parsed PDF",
                "source_file": "/tmp/artifact-sentinel.pdf",
                "category": "pdf",
                "score": 0.75,
            }
        ]


def test_execute_search_includes_artifact_rows(api_module, monkeypatch):
    async def no_embedding(_query):
        return None

    async def no_graph(*_args, **_kwargs):
        return []

    monkeypatch.setattr(api_module, "embed_text", no_embedding)
    monkeypatch.setattr(api_module, "search_graph", no_graph)

    conn = FakeSearchConn()
    result = asyncio.run(
        api_module.execute_search(
            conn,
            "kaidera",
            "artifact sentinel",
            search_type="artifacts",
            rerank=False,
            graph=False,
            limit=5,
        )
    )

    assert conn.artifact_query_seen
    assert result["results"] == [
        {
            "id": "11111111-1111-1111-1111-111111111111",
            "text": "Artifact sentinel caption from a parsed PDF",
            "meta": "/tmp/artifact-sentinel.pdf",
            "category": "pdf",
            "source": "artifacts",
            "score": 0.75,
            "tier": "artifact",
        }
    ]


class FakeArtifactVectorConn:
    def __init__(self):
        self.vector_query_seen = False

    async def fetch(self, sql, *args):
        if "FROM artifacts" not in sql:
            return []
        if "embedding <=>" not in sql:
            return []
        self.vector_query_seen = True
        assert args == ("[0.1,0.2]", "kaidera", None)
        return [
            {
                "id": "22222222-2222-2222-2222-222222222222",
                "text": "Semantic transcript about the release boundary",
                "source_file": "/tmp/interview.webm",
                "category": "video",
                "score": 0.91,
            }
        ]


def test_artifact_search_returns_semantic_vector_hits(api_module, monkeypatch):
    async def query_embedding(_query, _project):
        return [0.1, 0.2]

    async def no_graph(*_args, **_kwargs):
        return []

    monkeypatch.setattr(api_module, "embed_query_cached", query_embedding)
    monkeypatch.setattr(api_module, "search_graph", no_graph)

    conn = FakeArtifactVectorConn()
    result = asyncio.run(
        api_module.execute_search(
            conn,
            "kaidera",
            "release boundary recording",
            search_type="artifacts",
            rerank=False,
            graph=False,
            limit=5,
        )
    )

    assert conn.vector_query_seen
    assert result["results"] == [
        {
            "id": "22222222-2222-2222-2222-222222222222",
            "text": "Semantic transcript about the release boundary",
            "meta": "/tmp/interview.webm",
            "category": "video",
            "source": "artifacts (semantic)",
            "score": 0.91,
            "tier": "artifact-vector",
        }
    ]


def test_type_specific_search_returns_lexical_hits_without_embedding(api_module, monkeypatch):
    class LexicalKnowledgeConn:
        async def fetch(self, sql, *args):
            if "FROM knowledge" in sql and "similarity" in sql:
                return [
                    (
                        "knowledge-1",
                        "Beat's job survived ingestion",
                        "/tmp/knowledge.md",
                        "rca-ingest-test",
                        "knowledge",
                    )
                ]
            return []

        async def fetchrow(self, *_args, **_kwargs):
            return None

        async def execute(self, *_args, **_kwargs):
            return "UPDATE 0"

    async def fail_if_embedding_called(_query):
        raise AssertionError("type-specific lexical hit should not call embed_text")

    monkeypatch.setattr(api_module, "embed_text", fail_if_embedding_called)

    result = asyncio.run(
        api_module.execute_search(
            LexicalKnowledgeConn(),
            "kaidera",
            "Beat's job",
            search_type="knowledge",
            rerank=True,
            graph=False,
            limit=5,
        )
    )

    assert result["reranked"] is False
    assert result["results"][0]["source"] == "knowledge"
    assert "Beat's job" in result["results"][0]["text"]


class FakeIdLookupConn:
    """Stage -1 id-prefix probe hits; ``swept`` flips True if execution ever falls
    through into the expensive BM25/trigram/pgvector/artifact sweep (any ``fetch``)."""

    def __init__(self):
        self.swept = False

    async def fetchrow(self, sql, *args):
        if "ILIKE" in sql:  # Stage -1 id-prefix probe
            return {
                "id": "abcd1234-0000-0000-0000-000000000000",
                "project": "kaidera-os",
                "created_at": "2026-06-03T00:00:00",
            }
        if "WHERE id::text = $1" in sql:  # content fetch for the matched row
            return ["the matched decision summary"]
        return None

    async def fetch(self, sql, *args):
        self.swept = True
        return []

    async def execute(self, sql, *args):
        return None


def test_exact_id_query_fast_paths_and_skips_the_sweep(api_module, monkeypatch):
    """A pure-hex/UUID query that matches a real row id returns immediately with the
    id-match and SKIPS the BM25+trigram+pgvector+graph+rerank sweep — the ~2s cost that
    OOM-killed the CLI on large projects. cortex.md routes exact IDs here, so this is
    the documented behaviour, not a heuristic guess."""

    async def no_embedding(_query):
        return None

    async def no_graph(*_args, **_kwargs):
        return []

    monkeypatch.setattr(api_module, "embed_text", no_embedding)
    monkeypatch.setattr(api_module, "search_graph", no_graph)

    conn = FakeIdLookupConn()
    result = asyncio.run(
        api_module.execute_search(conn, "kaidera-os", "abcd1234", rerank=True, limit=5)
    )

    assert conn.swept is False, "the expensive sweep ran instead of the exact-ID fast-path"
    assert result["reranked"] is False
    assert result["results"], "expected the exact-ID match to be returned"
    assert all(r["tier"] == "id" for r in result["results"])


def test_non_matching_hex_query_still_falls_through(api_module, monkeypatch):
    """A hex-shaped query that matches NO row id must NOT short-circuit — it falls
    through to the normal sweep so real content/semantic results are still found."""

    async def no_embedding(_query):
        return None

    async def no_graph(*_args, **_kwargs):
        return []

    monkeypatch.setattr(api_module, "embed_text", no_embedding)
    monkeypatch.setattr(api_module, "search_graph", no_graph)

    class NoIdMatchConn(FakeIdLookupConn):
        async def fetchrow(self, sql, *args):
            return None  # nothing matches the id probe

    conn = NoIdMatchConn()
    asyncio.run(api_module.execute_search(conn, "kaidera-os", "deadbeef", rerank=False, limit=5))
    assert conn.swept is True, "a non-matching hex query must still run the full sweep"


class RetrievalQualityFixtureConn:
    async def fetchval(self, sql, *args):
        if "to_regclass('public.work_products')" in sql:
            return True
        raise AssertionError(f"Unexpected fetchval SQL: {sql!r}")

    async def fetch(self, sql, *args):
        if "information_schema.columns" in sql and "table_name = 'work_products'" in sql:
            return []
        if "FROM work_products wp" in sql:
            return [
                {
                    "id": "11111111-1111-4111-8111-111111111111",
                    "project": "project-a",
                    "handoff_id": None,
                    "agent_name": "agent@project-a",
                    "activity_type": "task-completed",
                    "status": "current",
                    "title": "Approved dashboard baseline",
                    "summary": "Canonical completed-work brief for the dashboard baseline.",
                    "behavior_summary": "Use this answer before rediscovering raw memory.",
                    "architecture_notes": "",
                    "files_changed": ["docs/dashboard-baseline.md"],
                    "symbols_changed": [],
                    "subject_entities": ["dashboard baseline"],
                    "artifact_refs": [],
                    "tests_run": [],
                    "risks": [],
                    "followups": [],
                    "file_hashes": {},
                    "symbol_hashes": {},
                    "metadata": {},
                    "freshness_status": "current",
                    "projection_status": "projected",
                    "score": 0.2,
                }
            ]
        if "cortex_search_decisions_trigram" in sql:
            return [
                (
                    "decision-1",
                    "Older raw decision mentioning the dashboard baseline.",
                    "planning",
                    "agent",
                    "decisions",
                )
            ]
        if "quality_score" in sql and "FROM decisions" in sql:
            return [{"id": "decision-1", "quality_score": 0.9, "times_selected": 4}]
        return []

    async def fetchrow(self, *_args, **_kwargs):
        return None

    async def execute(self, *_args, **_kwargs):
        return "UPDATE 1"


def test_retrieval_quality_fixture_prefers_canonical_work_product_memory(api_module, monkeypatch):
    fixture_path = Path(__file__).resolve().parent / "fixtures" / "retrieval_quality.json"
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))["cases"][0]

    async def no_embedding(_query):
        return None

    async def no_graph(*_args, **_kwargs):
        return []

    monkeypatch.setattr(api_module, "embed_text", no_embedding)
    monkeypatch.setattr(api_module, "search_graph", no_graph)

    result = asyncio.run(
        api_module.execute_search(
            RetrievalQualityFixtureConn(),
            "project-a",
            fixture["query"],
            search_type="all",
            rerank=False,
            graph=False,
            limit=5,
        )
    )

    assert result["reranked"] is False
    assert result["results"][0]["source"] == fixture["expected_first_source"]
    assert result["results"][0]["tier"] == fixture["expected_first_tier"]
    assert "Approved dashboard baseline" in result["results"][0]["text"]
    assert any(item["source"] == "decisions" for item in result["results"][1:])
