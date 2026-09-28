"""Pure unit tests for verification request models and honesty rules (R19).

A verification is either evidence-backed or explicitly UNVERIFIABLE. The
pure rules: 'verified' requires at least one source/revision citation and at
least one readback evidence item; 'contradicted' requires citations;
'unverifiable' requires an explicit reason; subjects are exactly typed.
No result laundering: an agent's bare assertion can never become 'verified'.
"""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from cortex_v2.verification.models import (
    Citation,
    ReadbackItem,
    RecordVerificationRequest,
)
from cortex_v2.verification.rules import validation_violation

RECEIPT = uuid.UUID("11111111-1111-4111-8111-111111111111")
HANDOFF = uuid.UUID("22222222-2222-4222-8222-222222222222")
CONTENT = uuid.UUID("33333333-3333-4333-8333-333333333333")


def citation() -> dict:
    return {"content_id": str(CONTENT), "revision": 2}


def readback() -> dict:
    return {
        "kind": "content_readback",
        "reference": f"content:{CONTENT}:2",
        "observed": "sha256 matches the pinned work-product hash",
    }


def verified_receipt_request(**overrides) -> RecordVerificationRequest:
    base = {
        "subject_kind": "work_product_receipt",
        "subject_receipt_id": RECEIPT,
        "verdict": "verified",
        "method": "content_readback",
        "citations": [citation()],
        "readback_evidence": [readback()],
    }
    base.update(overrides)
    return RecordVerificationRequest(**base)


def test_verified_request_passes_rules() -> None:
    assert validation_violation(verified_receipt_request()) is None


def test_verified_without_readback_is_rejected() -> None:
    violation = validation_violation(
        verified_receipt_request(readback_evidence=[])
    )
    assert violation == "verified_requires_readback_evidence"


def test_verified_without_citations_is_rejected() -> None:
    violation = validation_violation(verified_receipt_request(citations=[]))
    assert violation == "verified_requires_citations"


def test_contradicted_requires_citations() -> None:
    violation = validation_violation(
        verified_receipt_request(verdict="contradicted", citations=[])
    )
    assert violation == "contradicted_requires_citations"
    assert (
        validation_violation(verified_receipt_request(verdict="contradicted"))
        is None
    )


def test_unverifiable_requires_explicit_reason() -> None:
    violation = validation_violation(
        verified_receipt_request(verdict="unverifiable")
    )
    assert violation == "unverifiable_requires_reason"
    assert (
        validation_violation(
            verified_receipt_request(
                verdict="unverifiable",
                unverifiable_reason="source system offline",
            )
        )
        is None
    )


def test_subject_fields_are_exactly_typed() -> None:
    assert (
        validation_violation(
            RecordVerificationRequest(
                subject_kind="claim",
                claim="all tests passed",
                verdict="unverifiable",
                method="manual_review",
                unverifiable_reason="no test output retained",
            )
        )
        is None
    )
    assert (
        validation_violation(
            RecordVerificationRequest(
                subject_kind="claim",
                verdict="unverifiable",
                method="manual_review",
                unverifiable_reason="none",
            )
        )
        == "subject_mismatch"
    )
    assert (
        validation_violation(
            RecordVerificationRequest(
                subject_kind="work_product_receipt",
                subject_receipt_id=RECEIPT,
                subject_handoff_id=HANDOFF,
                verdict="unverifiable",
                method="manual_review",
                unverifiable_reason="none",
            )
        )
        == "subject_mismatch"
    )
    assert (
        validation_violation(
            RecordVerificationRequest(
                subject_kind="handoff_return",
                subject_handoff_id=HANDOFF,
                verdict="unverifiable",
                method="manual_review",
                unverifiable_reason="none",
            )
        )
        is None
    )


def test_models_are_strict() -> None:
    with pytest.raises(ValidationError):
        RecordVerificationRequest(
            subject_kind="work_product_receipt",
            subject_receipt_id=RECEIPT,
            verdict="verified",
            method="content_readback",
            citations=[citation()],
            readback_evidence=[readback()],
            laundered=True,
        )
    with pytest.raises(ValidationError):
        RecordVerificationRequest(
            subject_kind="vibes",
            verdict="verified",
            method="content_readback",
        )
    with pytest.raises(ValidationError):
        Citation(content_id=CONTENT, revision=0)
    with pytest.raises(ValidationError):
        ReadbackItem(kind="telepathy", reference="r", observed="o")
    with pytest.raises(ValidationError):
        ReadbackItem(kind="content_readback", reference="", observed="o")


def test_citation_may_name_another_scope() -> None:
    item = Citation(content_id=CONTENT, revision=1, scope_id=HANDOFF)
    assert item.scope_id == HANDOFF
    assert Citation(content_id=CONTENT, revision=1).scope_id is None


def test_bare_assertion_can_never_be_verified() -> None:
    # An agent claim with no citations and no readback is at best
    # unverifiable, and only with an explicit reason.
    request = RecordVerificationRequest(
        subject_kind="claim",
        claim="the migration succeeded",
        verdict="verified",
        method="manual_review",
    )
    assert validation_violation(request) == "verified_requires_citations"
