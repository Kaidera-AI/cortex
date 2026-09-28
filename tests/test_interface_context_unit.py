"""Unit tests for budgeted context composition (R05, F05).

Pure composition only: mandatory policy material must survive tiny budgets
or fail with an explicit budget error; optional history truncates explicitly.
"""

from __future__ import annotations

import json

import pytest

from cortex_v2.interface.context import (
    CONTEXT_COMPOSER_VERSION,
    Section,
    compose_sections,
)
from cortex_v2.store import ApiProblem


def mandatory(name: str, data) -> Section:
    return Section(name=name, data=data, mandatory=True, priority=100)


def optional(name: str, data, priority: int) -> Section:
    return Section(name=name, data=data, mandatory=False, priority=priority)


def size(data) -> int:
    return len(json.dumps(
        data, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode("utf-8"))


def test_mandatory_sections_survive_and_optional_truncates_explicitly():
    identity = mandatory("identity", {"principal_id": "p", "scope": "s"})
    policy = mandatory("policy", {"revision": 3, "allowed_roles": ["lead"]})
    rules = mandatory("mandatory_rules", [{"slug": "no-force-push", "body": "x" * 40}])
    recall = optional("recall", {"hits": [{"body": "h" * 500}] * 10}, 30)
    budget = (
        size(identity.data) + size(policy.data) + size(rules.data) + 400
    )
    composed, report, truncations = compose_sections(
        [identity, policy, rules, recall], budget
    )
    assert composed["identity"] == identity.data
    assert composed["policy"] == policy.data
    assert composed["mandatory_rules"] == rules.data
    assert report["mandatory_bytes"] <= budget
    assert report["composed_bytes"] <= budget
    assert any(t["section"] == "recall" and t["dropped"] > 0 for t in truncations)
    assert len(composed["recall"]["hits"]) < 10


def test_budget_too_small_for_mandatory_is_explicit_error():
    identity = mandatory("identity", {"principal_id": "p" * 100})
    policy = mandatory("policy", {"rules": ["r" * 500]})
    with pytest.raises(ApiProblem) as excinfo:
        compose_sections([identity, policy], 64)
    problem = excinfo.value
    assert problem.status == 422
    assert problem.code == "budget_exhausted"
    assert str(size(identity.data) + size(policy.data)) in problem.message or (
        "requires" in problem.message
    )


def test_optional_sections_drop_in_priority_order():
    small = optional("recommendations", {"items": [{"operation_id": "a"}]}, 60)
    big = optional("recall", {"hits": [{"body": "y" * 900}]}, 30)
    identity = mandatory("identity", {"principal_id": "p"})
    budget = size(identity.data) + size(small.data) + 120
    composed, report, truncations = compose_sections([small, big, identity], budget)
    assert composed["recommendations"] == small.data
    dropped = {t["section"] for t in truncations}
    assert "recall" in dropped
    assert report["composed_bytes"] <= budget


def test_oversized_optional_persona_is_omitted_at_budget_boundary():
    identity = mandatory("identity", {"principal_id": "p"})
    persona = optional("persona", {"body": "x" * 300}, 50)
    budget = size({"identity": identity.data}) + 1

    composed, report, truncations = compose_sections(
        [identity, persona], budget
    )

    assert composed == {"identity": identity.data}
    assert report["composed_bytes"] <= budget
    assert truncations == [{"section": "persona", "kept": 0, "dropped": 1}]


def test_hard_budget_counts_serialized_context_structure():
    identity = mandatory("identity", {"principal_id": "p"})
    policy = mandatory("policy", {"revision": 0})
    values_only = size(identity.data) + size(policy.data)

    with pytest.raises(ApiProblem) as excinfo:
        compose_sections([identity, policy], values_only)

    assert excinfo.value.code == "budget_exhausted"


def test_composed_bytes_equal_encoded_context_size():
    identity = mandatory("identity", {"principal_id": "p"})
    recommendations = optional(
        "recommendations",
        {"items": [{"operation_id": f"op-{number}"} for number in range(4)]},
        60,
    )
    budget = size({"identity": identity.data}) + 40

    context, report, truncations = compose_sections(
        [identity, recommendations], budget
    )

    assert report["mandatory_bytes"] == size({"identity": identity.data})
    assert report["composed_bytes"] == size(context)
    assert size(context) <= budget
    assert truncations

def test_composition_is_deterministic():
    sections = [
        mandatory("identity", {"b": 2, "a": 1}),
        optional("recall", {"hits": [{"i": i} for i in range(5)]}, 30),
    ]
    first = compose_sections(list(sections), 10_000)
    second = compose_sections(list(sections), 10_000)
    assert json.dumps(first[0], sort_keys=True) == json.dumps(second[0], sort_keys=True)
    assert first[1] == second[1]


def test_list_sections_truncate_itemwise_with_explicit_record():
    identity = mandatory("identity", {"principal_id": "p"})
    skills = optional(
        "skills",
        {"items": [{"slug": f"s{i}", "when_to_use": "w" * 50} for i in range(20)]},
        40,
    )
    budget = size(identity.data) + size({"items": [{"slug": "s0",
                                                    "when_to_use": "w" * 50}]}) + 150
    composed, report, truncations = compose_sections([identity, skills], budget)
    kept = composed["skills"]["items"]
    assert 0 < len(kept) < 20
    record = next(t for t in truncations if t["section"] == "skills")
    assert record["kept"] == len(kept)
    assert record["dropped"] == 20 - len(kept)


def test_composer_version_is_pinned():
    assert CONTEXT_COMPOSER_VERSION == "cortex.context.v1"
