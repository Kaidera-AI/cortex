"""Synthetic v1 rows matching public.handoffs/decisions DDL fields."""

from __future__ import annotations

import json
import uuid

import pytest

from cortex_v2.converter import ConversionPolicy, classify_row

SCOPE = uuid.UUID("a96570de-c066-452b-9696-e2377e8e6551")
PRINCIPAL = uuid.UUID("5dd378bd-e1de-465e-8240-a02b95426446")
INSTALLATION = uuid.UUID("247c715c-a845-4d83-8504-be0c3b536b47")


def policy_bytes(*, handoff_status: dict[str, str] | None = None) -> bytes:
    return json.dumps(
        {
            "version": "synthetic-v1",
            "installation_id": str(INSTALLATION),
            "projects": {"fixture-project": {
                "scope_id": str(SCOPE),
                "principal_id": str(PRINCIPAL),
            }},
            "handoff_status": handoff_status or {"pending": "open"},
        },
        sort_keys=True,
    ).encode()


def test_policy_fingerprint_binds_lifecycle_mapping() -> None:
    first = ConversionPolicy.from_bytes(policy_bytes())
    second = ConversionPolicy.from_bytes(policy_bytes(handoff_status={
        "pending": "open", "released": "open"
    }))
    assert first.sha256 != second.sha256


def test_canonical_memory_and_pending_handoff_keep_source_identity() -> None:
    policy = ConversionPolicy.from_bytes(policy_bytes())
    decision_id = uuid.uuid4()
    handoff_id = uuid.uuid4()
    decision = classify_row("public.decisions", {
        "id": decision_id, "project": "fixture-project", "summary": "Synthetic choice",
        "agent_name": "fixture-writer",
    }, policy)
    handoff = classify_row("public.handoffs", {
        "id": handoff_id, "project": "fixture-project", "summary": "Synthetic relay",
        "from_agent": "fixture-writer", "to_role": "implementer", "status": "pending",
    }, policy)
    assert (
        decision.outcome, decision.target_id, decision.target_relation, decision.body
    ) == (
        "migrated", decision_id, "cortex_core.content_items", "Synthetic choice"
    )
    assert (handoff.outcome, handoff.target_id, handoff.status) == (
        "migrated", handoff_id, "open"
    )
    assert handoff.scope_id == decision.scope_id == SCOPE


def test_ambiguous_claim_and_unknown_scope_are_quarantined_not_rewritten() -> None:
    policy = ConversionPolicy.from_bytes(policy_bytes())
    claim = classify_row("public.handoffs", {
        "id": uuid.uuid4(), "project": "fixture-project", "summary": "Pending claim",
        "from_agent": "fixture", "to_role": "implementer", "status": "claimed",
    }, policy)
    unknown = classify_row("public.decisions", {
        "id": uuid.uuid4(), "project": "other-project", "summary": "No owner",
    }, policy)
    assert (claim.outcome, claim.reason, claim.target_id) == (
        "quarantined", "ambiguous_lifecycle", None
    )
    assert (unknown.outcome, unknown.reason, unknown.target_id) == (
        "quarantined", "unmapped_scope", None
    )


def test_secret_namespace_never_returns_canonical_body() -> None:
    policy = ConversionPolicy.from_bytes(policy_bytes())
    outcome = classify_row("cortex_auth.tokens", {
        "id": uuid.uuid4(), "token_hash": "synthetic-sentinel"
    }, policy)
    assert (outcome.outcome, outcome.reason, outcome.body) == (
        "skipped", "retained_legacy_credentials", None
    )
    assert "synthetic-sentinel" not in repr(outcome)


def test_policy_rejects_claimed_mapping_without_expiring_lease() -> None:
    with pytest.raises(ValueError, match="unsafe handoff mapping"):
        ConversionPolicy.from_bytes(policy_bytes(handoff_status={"claimed": "claimed"}))
