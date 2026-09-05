"""Graph-extract noise-reduction (2026-06-25).

deterministic_graph_extract used to regex-grab any /path fragment as an
"endpoint" entity (80% noise on real corpora) and emitted a per-row
"Cortex memory row <digest>" concept for otherwise-empty rows. Both are gone.
"""
import pytest

from main import (
    GraphExtractRequest,
    deterministic_graph_extract,
    graph_sanitize_llm_payload,
    graph_source_tables,
    graph_json_from_llm_text,
    graph_extract_domain_phrases,
    llm_graph_extract,
)


def _entities(text):
    ents, _ = deterministic_graph_extract(text)
    return ents


def test_endpoint_path_noise_is_rejected():
    ents = _entities("metrics at /views and /hour over /90/365 and /need: now")
    endpoints = [e["name"] for e in ents if e["type"] == "endpoint"]
    assert endpoints == [], endpoints


def test_real_api_endpoints_are_kept():
    ents = _entities("POST /api/chat then /beat/embeddings/backfill and /boot/{agent}")
    endpoints = {e["name"] for e in ents if e["type"] == "endpoint"}
    assert "/api/chat" in endpoints
    assert "/beat/embeddings/backfill" in endpoints
    assert "/boot/{agent}" in endpoints


def test_no_digest_fallback_concept_for_inert_text():
    ents = _entities("lorem ipsum dolor sit amet plain prose")
    assert not any(e["name"].startswith("Cortex memory row") for e in ents)


def test_project_memory_is_default_source_alias():
    req = GraphExtractRequest()

    assert req.source == "project_memory"
    assert graph_source_tables(req.source) == ("knowledge", "work_products")
    assert graph_source_tables("all") == ("decisions", "knowledge", "lessons", "work_products")


def test_unknown_graph_source_fails_loud():
    with pytest.raises(Exception) as exc:
        graph_source_tables("messages")

    assert "source must be one of" in str(exc.value)


def test_domain_phrases_are_extracted_without_project_dictionary():
    text = (
        "OPS and SLA need an Analytics dashboard for capacity management dashboard. "
        "The Metrics warehouse stores service reliability metrics and demand forecast dataset. "
        "Ignore noisy fragments /views /hour /90/365 /need:."
    )
    ents, rels = deterministic_graph_extract(text)
    names = {e["name"] for e in ents}
    endpoints = [e for e in ents if e["type"] == "endpoint"]

    assert "OPS" in names
    assert "SLA" in names
    assert "Analytics dashboard" in names
    assert "capacity management dashboard" in names
    assert "Metrics warehouse" in names
    assert "demand forecast dataset" in names
    assert len(endpoints) / max(len(ents), 1) < 0.2
    assert any(r["type"] == "relates_to" for r in rels)


def test_prose_instruction_fragments_do_not_become_domain_concepts():
    names = {e["name"] for e in _entities(
        "should probably update the model and corrupt the database before we run tests"
    )}

    assert "should probably update the model" not in names
    assert "corrupt the database" not in names
    assert "update the model" not in names
    assert "database" not in names


def test_common_all_caps_words_are_rejected_but_domain_acronyms_remain():
    names = {e["name"] for e in _entities(
        "GET POST NEW KEY ON OFF SKIP are commands; OPS and SLA are domain acronyms."
    )}

    assert {"GET", "POST", "NEW", "KEY", "ON", "OFF", "SKIP"}.isdisjoint(names)
    assert {"OPS", "SLA"}.issubset(names)


def test_title_case_slash_lists_are_not_endpoints():
    ents = _entities("Do not treat /Alpha/Beta/Gamma as an endpoint, but keep /api/chat.")
    endpoints = {e["name"] for e in ents if e["type"] == "endpoint"}

    assert "/Alpha/Beta/Gamma" not in endpoints
    assert "/api/chat" in endpoints


def test_phrase_filter_keeps_lowercase_domain_terms_with_suffix_anchor():
    phrases = set(graph_extract_domain_phrases(
        "service reliability metrics and demand forecast dataset drive capacity management dashboard"
    ))

    assert "service reliability metrics" in phrases
    assert "demand forecast dataset" in phrases
    assert "capacity management dashboard" in phrases


def test_llm_json_parse_errors_are_visible_to_callers():
    with pytest.raises(Exception):
        graph_json_from_llm_text("not-json")


@pytest.mark.asyncio
async def test_llm_graph_extract_marks_missing_key_as_degraded(monkeypatch):
    import main

    monkeypatch.setattr(main, "_ingestion_key", lambda _provider: "")

    ents, rels, model = await llm_graph_extract(
        "Extract this Analytics dashboard concept.",
        config={"analysis_provider": "openrouter", "analysis_model": "free/model"},
    )

    assert ents == []
    assert rels == []
    assert model == "openrouter:free/model:unavailable"


def test_llm_graph_payload_is_sanitized_to_allowed_graph_shape():
    ents, rels = graph_sanitize_llm_payload(
        {
            "entities": [
                {"name": "Revenue dashboard", "type": "dashboard"},
                {"name": "Analytics warehouse", "type": "service"},
            ],
            "relationships": [
                {
                    "source": "Revenue dashboard",
                    "target": "Analytics warehouse",
                    "type": "uses",
                    "description": "Dashboard reads curated metrics from the warehouse.",
                },
                {"source": "missing", "target": "Analytics warehouse", "type": "uses"},
            ],
        }
    )

    assert ("Revenue dashboard", "concept") in {(e["name"], e["type"]) for e in ents}
    assert ("Analytics warehouse", "service") in {(e["name"], e["type"]) for e in ents}
    assert rels == [
        {
            "source": "Revenue dashboard",
            "target": "Analytics warehouse",
            "type": "uses",
            "description": "Dashboard reads curated metrics from the warehouse.",
        }
    ]
