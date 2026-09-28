"""Pure honesty rules for verification records (R19).

A verdict is either evidence-backed or explicitly UNVERIFIABLE. 'verified'
requires at least one source/revision citation and at least one readback
evidence item; 'contradicted' requires citations; 'unverifiable' requires an
explicit reason. Subjects are exactly typed - one subject field matching the
subject kind, nothing else. An agent's bare assertion can therefore never be
laundered into a verified result.
"""

from __future__ import annotations

from .models import RecordVerificationRequest

SUBJECT_FIELDS = {
    "work_product_receipt": "subject_receipt_id",
    "handoff_return": "subject_handoff_id",
    "claim": "claim",
}
ALL_SUBJECT_FIELDS = ("subject_receipt_id", "subject_handoff_id", "claim")

SUBJECT_MISMATCH = "subject_mismatch"
VERIFIED_REQUIRES_CITATIONS = "verified_requires_citations"
VERIFIED_REQUIRES_READBACK = "verified_requires_readback_evidence"
CONTRADICTED_REQUIRES_CITATIONS = "contradicted_requires_citations"
UNVERIFIABLE_REQUIRES_REASON = "unverifiable_requires_reason"


def validation_violation(request: RecordVerificationRequest) -> str | None:
    required_field = SUBJECT_FIELDS[request.subject_kind]
    provided = [
        field
        for field in ALL_SUBJECT_FIELDS
        if getattr(request, field) is not None
    ]
    if provided != [required_field]:
        return SUBJECT_MISMATCH

    if request.verdict == "verified":
        if not request.citations:
            return VERIFIED_REQUIRES_CITATIONS
        if not request.readback_evidence:
            return VERIFIED_REQUIRES_READBACK
    elif request.verdict == "contradicted":
        if not request.citations:
            return CONTRADICTED_REQUIRES_CITATIONS
    elif request.verdict == "unverifiable":
        if not request.unverifiable_reason:
            return UNVERIFIABLE_REQUIRES_REASON
    return None
