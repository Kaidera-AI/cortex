"""Deterministic capability recommendations and the worker-use evaluation
corpus/harness (R27, W5, F13).

The engine implements the deterministic recommendation table from
worker-api.md §3: recommendations are data, not permission to execute. The
evaluation harness measures correct tool choice and abstention over a
versioned corpus — eligible tasks with valid evidence over eligible tasks,
not raw usage counts.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

CORPUS_VERSION = "cortex.eval-corpus.v1"

GRAPH_STATES = ("fresh", "stale", "missing", "unknown")


@dataclass(frozen=True, slots=True)
class Recommendation:
    operation_id: str
    reason: str
    expected_evidence: str
    cost_class: str
    arguments_hint: dict[str, Any] = field(default_factory=dict)
    coverage_warning: str | None = None
    recovery_actions: tuple[str, ...] = ()
    degraded_note: str | None = None

    def as_dict(self) -> dict[str, Any]:
        data = {
            "operation_id": self.operation_id,
            "reason": self.reason,
            "expected_evidence": self.expected_evidence,
            "cost_class": self.cost_class,
            "arguments_hint": dict(self.arguments_hint),
        }
        if self.coverage_warning:
            data["coverage_warning"] = self.coverage_warning
        if self.recovery_actions:
            data["recovery_actions"] = list(self.recovery_actions)
        if self.degraded_note:
            data["degraded_note"] = self.degraded_note
        return data


_STALE_WARNING = (
    "graph coverage is stale: a zero-hit result is a lower bound on impact, "
    "never 'safe to change'"
)
_MISSING_WARNING = (
    "no graph coverage exists for this change: impact is unknown, never "
    "'no impact'"
)
_GRAPH_RECOVERY = ("request_index_refresh", "attach_partial_evidence",
                   "authorized_waiver")


def _graph_warning(graph_state: str) -> str | None:
    if graph_state == "stale":
        return _STALE_WARNING
    if graph_state in ("missing", "unknown"):
        return _MISSING_WARNING
    return None


def recommend_operations(
    intent: str,
    *,
    has_repository_change: bool = False,
    has_symbol: bool = False,
    graph_state: str = "fresh",
    provider_state: str = "available",
    prior_assessment_digest_changed: bool = False,
) -> tuple[Recommendation, ...]:
    """Deterministic intent/evidence → recommendation mapping."""
    recommendations: list[Recommendation] = []
    if intent == "code_change":
        warning = _graph_warning(graph_state) if has_repository_change else None
        assess_reason = (
            "a code change with a repository/change reference "
            "ordinarily requires a fresh change-bound impact "
            "assessment before implementation and before return"
        )
        if prior_assessment_digest_changed:
            assess_reason = (
                "the change digest changed: the prior assessment is invalid "
                "for it; refresh the assessment instead of citing stale "
                "evidence. " + assess_reason
            )
        recommendations.append(
            Recommendation(
                operation_id="code.assess-change",
                reason=assess_reason,
                expected_evidence=(
                    "assessment bound to the change digest and graph revision "
                    "with coverage, callers and verification candidates"
                ),
                cost_class="expensive-async",
                arguments_hint={"require_fresh": True},
                coverage_warning=warning,
                recovery_actions=_GRAPH_RECOVERY if warning else (),
            )
        )
        recommendations.append(
            Recommendation(
                operation_id="memory.search",
                reason="retrieve prior decisions and work-product briefs for "
                "the affected modules",
                expected_evidence="cited decisions/receipts with revisions",
                cost_class="moderate",
                arguments_hint={"intent": "work-history"},
                degraded_note=(
                    "semantic stage unavailable; lexical recall only"
                    if provider_state != "available"
                    else None
                ),
            )
        )
    elif intent == "debug_symbol":
        recommendations.append(
            Recommendation(
                operation_id="memory.search",
                reason="exact/symbol/lexical lookup first; embed only if the "
                "query shape needs it",
                expected_evidence="exact record ids/spans for the symbol",
                cost_class="cheap",
                arguments_hint={
                    "intent": "symbol" if has_symbol else "known-item"
                },
            )
        )
        warning = _graph_warning(graph_state)
        recommendations.append(
            Recommendation(
                operation_id="graph.explore",
                reason="inspect callers/dependencies of the named symbol",
                expected_evidence="code-graph edges with snapshot identity",
                cost_class="moderate",
                arguments_hint={"graph": "code", "max_hops": 2},
                coverage_warning=warning,
                recovery_actions=_GRAPH_RECOVERY if warning else (),
            )
        )
    elif intent == "architecture":
        recommendations.extend(
            (
                Recommendation(
                    operation_id="memory.search",
                    reason="hybrid recall over prior architectural decisions",
                    expected_evidence="authored decisions with source "
                    "revisions",
                    cost_class="moderate",
                    arguments_hint={"intent": "concept"},
                    degraded_note=(
                        "semantic stage unavailable; lexical recall only"
                        if provider_state != "available"
                        else None
                    ),
                ),
                Recommendation(
                    operation_id="graph.explore",
                    reason="bounded memory-graph expansion for thematic "
                    "relationships; a memory edge is not a proven code call",
                    expected_evidence="assertions with supporting source "
                    "revisions",
                    cost_class="moderate",
                    arguments_hint={"graph": "memory", "max_hops": 2},
                ),
                Recommendation(
                    operation_id="evidence.inspect",
                    reason="inspect the load-bearing originals before relying "
                    "on any hit",
                    expected_evidence="original body, hash and provenance",
                    cost_class="cheap",
                ),
            )
        )
        if has_repository_change:
            recommendations.append(
                Recommendation(
                    operation_id="code.assess-change",
                    reason="the refactor touches code; assess the affected "
                    "modules",
                    expected_evidence="change-bound assessment with coverage",
                    cost_class="expensive-async",
                    coverage_warning=_graph_warning(graph_state),
                    recovery_actions=(
                        _GRAPH_RECOVERY if _graph_warning(graph_state) else ()
                    ),
                )
            )
    elif intent == "research":
        recommendations.append(
            Recommendation(
                operation_id="memory.search",
                reason="hybrid lexical/vector recall for a conceptual "
                "question",
                expected_evidence="ranked hits with stage explanation",
                cost_class="moderate",
                arguments_hint={"intent": "concept"},
                degraded_note=(
                    "semantic stage unavailable; lexical recall only"
                    if provider_state != "available"
                    else None
                ),
            )
        )
        recommendations.append(
            Recommendation(
                operation_id="evidence.inspect",
                reason="vector similarity is not truth; inspect originals "
                "before load-bearing claims",
                expected_evidence="original body and revision",
                cost_class="cheap",
            )
        )
    elif intent == "record_or_return":
        recommendations.append(
            Recommendation(
                operation_id="memory.record",
                reason="record the known decision/lesson with exact "
                "references; do not re-run broad search to look busy",
                expected_evidence="typed receipt with record identity",
                cost_class="cheap",
            )
        )
        recommendations.append(
            Recommendation(
                operation_id="coordination.return",
                reason="return completed work through the normal "
                "return/review lifecycle; no direct-complete shortcut exists",
                expected_evidence="return receipt linked to the handoff",
                cost_class="cheap",
            )
        )
    # prose_edit and any unknown intent: abstain from code-graph work.
    return tuple(recommendations)


WORKER_EVAL_CASES: tuple[dict[str, Any], ...] = (
    {
        "case_id": "cross-module-function-change",
        "intent": "code_change",
        "evidence": {"has_repository_change": True, "graph_state": "fresh"},
        "expected": {
            "must_recommend": ["code.assess-change"],
            "must_not_recommend": ["memory.search.vector"],
            "abstain": False,
        },
    },
    {
        "case_id": "same-files-changed-diff",
        "intent": "code_change",
        "evidence": {"has_repository_change": True, "graph_state": "fresh",
                     "prior_assessment_digest_changed": True},
        "expected": {
            "must_recommend": ["code.assess-change"],
            "must_not_recommend": [],
            "abstain": False,
            "note": "an updated diff invalidates assessment reuse",
        },
    },
    {
        "case_id": "prose-documentation-task",
        "intent": "prose_edit",
        "evidence": {},
        "expected": {
            "must_recommend": [],
            "must_not_recommend": ["code.assess-change", "graph.explore"],
            "abstain": True,
        },
    },
    {
        "case_id": "stale-graph-code-change",
        "intent": "code_change",
        "evidence": {"has_repository_change": True, "graph_state": "stale"},
        "expected": {
            "must_recommend": ["code.assess-change"],
            "must_not_recommend": [],
            "abstain": False,
            "requires_coverage_warning": True,
            "requires_recovery_actions": True,
        },
    },
    {
        "case_id": "missing-graph-code-change",
        "intent": "code_change",
        "evidence": {"has_repository_change": True, "graph_state": "missing"},
        "expected": {
            "must_recommend": ["code.assess-change"],
            "must_not_recommend": [],
            "abstain": False,
            "requires_coverage_warning": True,
            "requires_recovery_actions": True,
        },
    },
    {
        "case_id": "semantic-architecture-question",
        "intent": "architecture",
        "evidence": {},
        "expected": {
            "must_recommend": ["memory.search", "evidence.inspect"],
            "must_not_recommend": ["memory.search.vector"],
            "abstain": False,
        },
    },
    {
        "case_id": "exact-symbol-debug",
        "intent": "debug_symbol",
        "evidence": {"has_symbol": True},
        "expected": {
            "must_recommend": ["memory.search"],
            "must_not_recommend": ["memory.search.vector"],
            "abstain": False,
            "search_intent_in": ["symbol", "known-item"],
        },
    },
    {
        "case_id": "record-known-decision",
        "intent": "record_or_return",
        "evidence": {},
        "expected": {
            "must_recommend": ["memory.record"],
            "must_not_recommend": ["memory.search"],
            "abstain": False,
        },
    },
    {
        "case_id": "research-conceptual-question",
        "intent": "research",
        "evidence": {},
        "expected": {
            "must_recommend": ["memory.search"],
            "must_not_recommend": ["code.assess-change"],
            "abstain": False,
        },
    },
    {
        "case_id": "research-with-provider-outage",
        "intent": "research",
        "evidence": {"provider_state": "unavailable"},
        "expected": {
            "must_recommend": ["memory.search"],
            "must_not_recommend": [],
            "abstain": False,
            "requires_degraded_note": True,
        },
    },
    {
        "case_id": "debug-with-missing-graph",
        "intent": "debug_symbol",
        "evidence": {"has_symbol": True, "graph_state": "missing"},
        "expected": {
            "must_recommend": ["memory.search", "graph.explore"],
            "must_not_recommend": [],
            "abstain": False,
            "requires_coverage_warning": True,
        },
    },
    {
        "case_id": "refactor-with-repository-change",
        "intent": "architecture",
        "evidence": {"has_repository_change": True, "graph_state": "fresh"},
        "expected": {
            "must_recommend": ["memory.search", "code.assess-change"],
            "must_not_recommend": [],
            "abstain": False,
        },
    },
)

Engine = Callable[..., Sequence[Recommendation]]


def evaluate_case(case: dict[str, Any], engine: Engine = recommend_operations
                  ) -> dict[str, Any]:
    expected = case["expected"]
    selected = tuple(engine(case["intent"], **case.get("evidence", {})))
    selected_ids = [item.operation_id for item in selected]
    missed = [
        op for op in expected["must_recommend"] if op not in selected_ids
    ]
    inappropriate = [
        op for op in expected["must_not_recommend"] if op in selected_ids
    ]
    checks_failed: list[str] = []
    if missed:
        checks_failed.append(f"missed:{','.join(missed)}")
    if inappropriate:
        checks_failed.append(f"inappropriate:{','.join(inappropriate)}")
    if expected.get("abstain") and selected_ids:
        checks_failed.append(f"abstention-violated:{','.join(selected_ids)}")
    if expected.get("requires_coverage_warning") and not any(
        item.coverage_warning
        for item in selected
        if item.operation_id in expected["must_recommend"]
    ):
        checks_failed.append("missing-coverage-warning")
    if expected.get("requires_recovery_actions") and not any(
        item.recovery_actions
        for item in selected
        if item.operation_id in expected["must_recommend"]
    ):
        checks_failed.append("missing-recovery-actions")
    if expected.get("requires_degraded_note") and not any(
        item.degraded_note for item in selected
    ):
        checks_failed.append("missing-degraded-note")
    if expected.get("search_intent_in"):
        search = next(
            (item for item in selected
             if item.operation_id == "memory.search"),
            None,
        )
        hint = search.arguments_hint.get("intent") if search else None
        if hint not in expected["search_intent_in"]:
            checks_failed.append(f"wrong-search-intent:{hint}")
    return {
        "case_id": case["case_id"],
        "passed": not checks_failed,
        "selected": selected_ids,
        "missed": missed,
        "inappropriate": inappropriate,
        "checks_failed": checks_failed,
    }


def run_evaluation(engine: Engine = recommend_operations) -> dict[str, Any]:
    verdicts = [evaluate_case(case, engine) for case in WORKER_EVAL_CASES]
    eligible = [
        case for case in WORKER_EVAL_CASES
        if not case["expected"].get("abstain")
    ]
    eligible_ids = {case["case_id"] for case in eligible}
    eligible_passed = [v for v in verdicts if v["case_id"] in eligible_ids]
    abstain_cases = [
        case for case in WORKER_EVAL_CASES
        if case["expected"].get("abstain")
    ]
    abstain_verdicts = [
        v for v in verdicts
        if v["case_id"] in {c["case_id"] for c in abstain_cases}
    ]
    total_must = sum(
        len(case["expected"]["must_recommend"]) for case in eligible
    )
    total_missed = sum(len(v["missed"]) for v in eligible_passed)
    total_selected = sum(len(v["selected"]) for v in verdicts)
    total_inappropriate = sum(len(v["inappropriate"]) for v in verdicts)
    return {
        "corpus_version": CORPUS_VERSION,
        "totals": {
            "cases": len(verdicts),
            "passed": sum(1 for v in verdicts if v["passed"]),
            "failed": sum(1 for v in verdicts if not v["passed"]),
        },
        "metrics": {
            "eligible_with_valid_evidence_rate": (
                sum(1 for v in eligible_passed if v["passed"]) / len(eligible)
                if eligible
                else 0.0
            ),
            "abstention_correct_rate": (
                sum(1 for v in abstain_verdicts if v["passed"])
                / len(abstain_verdicts)
                if abstain_verdicts
                else 1.0
            ),
            "missed_dependency_rate": (
                total_missed / total_must if total_must else 0.0
            ),
            "inappropriate_call_rate": (
                total_inappropriate / total_selected if total_selected else 0.0
            ),
        },
        "verdicts": verdicts,
    }
