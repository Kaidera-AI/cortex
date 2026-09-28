"""Unit tests for deterministic capability recommendations and the worker-use
evaluation corpus/harness (R27, W5, F13)."""

from __future__ import annotations

from cortex_v2.interface.evaluation import (
    CORPUS_VERSION,
    WORKER_EVAL_CASES,
    evaluate_case,
    recommend_operations,
    run_evaluation,
)


def ids(recommendations):
    return [item.operation_id for item in recommendations]


def test_code_change_recommends_impact_assessment():
    recs = recommend_operations(
        "code_change", has_repository_change=True, graph_state="fresh"
    )
    assert "code.assess-change" in ids(recs)
    assessment = next(r for r in recs if r.operation_id == "code.assess-change")
    assert assessment.reason
    assert assessment.expected_evidence
    assert assessment.cost_class


def test_prose_only_task_abstains_from_code_graph():
    recs = recommend_operations("prose_edit")
    assert "code.assess-change" not in ids(recs)
    assert "graph.explore" not in ids(recs)


def test_stale_graph_is_reported_never_no_impact():
    recs = recommend_operations(
        "code_change", has_repository_change=True, graph_state="stale"
    )
    assessment = next(r for r in recs if r.operation_id == "code.assess-change")
    assert assessment.coverage_warning
    assert "stale" in assessment.coverage_warning
    assert any(
        action in ("request_index_refresh", "attach_partial_evidence",
                   "authorized_waiver")
        for action in assessment.recovery_actions
    )


def test_missing_graph_offers_refresh_or_waiver_not_silence():
    recs = recommend_operations(
        "code_change", has_repository_change=True, graph_state="missing"
    )
    assessment = next(r for r in recs if r.operation_id == "code.assess-change")
    assert assessment.coverage_warning
    assert assessment.recovery_actions


def test_debug_named_symbol_prefers_exact_lookup():
    recs = recommend_operations("debug_symbol", has_symbol=True)
    chosen = ids(recs)
    assert "memory.search" in chosen
    search = next(r for r in recs if r.operation_id == "memory.search")
    assert search.arguments_hint.get("intent") in ("known-item", "symbol")
    assert "memory.search.vector" not in chosen


def test_architecture_decision_uses_hybrid_recall_and_inspection():
    recs = recommend_operations("architecture")
    chosen = ids(recs)
    assert "memory.search" in chosen
    assert "evidence.inspect" in chosen


def test_record_or_return_recommends_record_not_search():
    recs = recommend_operations("record_or_return")
    chosen = ids(recs)
    assert "memory.record" in chosen
    assert "memory.search" not in chosen


def test_corpus_is_versioned_and_cases_are_wellformed():
    assert CORPUS_VERSION == "cortex.eval-corpus.v1"
    assert len(WORKER_EVAL_CASES) >= 10
    seen = set()
    for case in WORKER_EVAL_CASES:
        assert case["case_id"] not in seen
        seen.add(case["case_id"])
        assert case["intent"]
        assert "must_recommend" in case["expected"]
        assert "must_not_recommend" in case["expected"]


def test_every_corpus_case_passes_with_the_bundled_engine():
    failures = []
    for case in WORKER_EVAL_CASES:
        verdict = evaluate_case(case)
        if not verdict["passed"]:
            failures.append(verdict)
    assert failures == []


def test_run_evaluation_reports_honest_metrics():
    report = run_evaluation()
    assert report["corpus_version"] == CORPUS_VERSION
    assert report["totals"]["cases"] == len(WORKER_EVAL_CASES)
    assert report["totals"]["passed"] == len(WORKER_EVAL_CASES)
    assert report["metrics"]["eligible_with_valid_evidence_rate"] == 1.0
    assert report["metrics"]["inappropriate_call_rate"] == 0.0
    assert report["metrics"]["missed_dependency_rate"] == 0.0


def test_broken_engine_is_measured_not_hidden():
    def broken_engine(intent, **evidence):
        return ()

    report = run_evaluation(engine=broken_engine)
    assert report["totals"]["passed"] < report["totals"]["cases"]
    assert report["metrics"]["eligible_with_valid_evidence_rate"] < 1.0
