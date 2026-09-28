"""Strict request models for evidence/claim verification (R19)."""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, Field, StrictInt, StrictStr

from ..models import StrictInput


def _reject_nul(value: str) -> str:
    if "\x00" in value:
        raise ValueError("text must not contain a NUL character")
    return value


ShortText = Annotated[
    StrictStr, Field(min_length=1, max_length=64), AfterValidator(_reject_nul)
]
NoteText = Annotated[
    StrictStr, Field(min_length=1, max_length=512), AfterValidator(_reject_nul)
]
ObservedText = Annotated[
    StrictStr, Field(min_length=1, max_length=65_536), AfterValidator(_reject_nul)
]

SubjectKind = Literal["work_product_receipt", "handoff_return", "claim"]
Verdict = Literal["verified", "unverifiable", "contradicted"]
VerificationMethod = Literal[
    "content_readback",
    "test_execution",
    "command_output",
    "independent_recomputation",
    "external_source",
    "manual_review",
]
ReadbackKind = Literal[
    "content_readback",
    "command_output",
    "test_execution",
    "query_result",
    "external_response",
]


class Citation(StrictInput):
    content_id: uuid.UUID
    revision: StrictInt = Field(ge=1)
    scope_id: uuid.UUID | None = None
    note: NoteText | None = None


class ReadbackItem(StrictInput):
    kind: ReadbackKind
    reference: NoteText
    observed: ObservedText


class RecordVerificationRequest(StrictInput):
    subject_kind: SubjectKind
    subject_receipt_id: uuid.UUID | None = None
    subject_handoff_id: uuid.UUID | None = None
    claim: ObservedText | None = None
    verdict: Verdict
    method: VerificationMethod
    citations: list[Citation] = Field(default_factory=list, max_length=32)
    readback_evidence: list[ReadbackItem] = Field(
        default_factory=list, max_length=16
    )
    unverifiable_reason: NoteText | None = None
    tool_name: ShortText | None = None
    tool_version: ShortText | None = None
    detail: dict[str, Any] = Field(default_factory=dict)
