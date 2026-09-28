"""Strict request models for coordination operations (R07-R10).

Every model forbids extra fields, uses strict scalars and rejects NUL
characters before they can reach PostgreSQL text columns. Bounds mirror the
CHECK constraints in migrations/0003_coordination.sql so a payload that
passes model validation cannot fail a length check at insert time.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, Field, StrictBool, StrictInt, StrictStr

from ..models import StrictInput


def _reject_nul(value: str) -> str:
    if "\x00" in value:
        raise ValueError("text must not contain a NUL character")
    return value


ShortText = Annotated[
    StrictStr, Field(min_length=1, max_length=64), AfterValidator(_reject_nul)
]
NameText = Annotated[
    StrictStr, Field(min_length=1, max_length=128), AfterValidator(_reject_nul)
]
TitleText = Annotated[
    StrictStr, Field(min_length=1, max_length=256), AfterValidator(_reject_nul)
]
ReasonText = Annotated[
    StrictStr, Field(min_length=1, max_length=512), AfterValidator(_reject_nul)
]
BriefText = Annotated[
    StrictStr, Field(min_length=1, max_length=65_536), AfterValidator(_reject_nul)
]

GateKind = Literal["relay", "wave", "handoff_accept"]
EvidenceClass = Literal[
    "work_product", "test_output", "review_note", "artifact", "document", "other"
]
AssignmentRole = Literal["responsible", "supporting"]


class CreateHandoffRequest(StrictInput):
    title: TitleText
    brief: BriefText
    dedup_key: Annotated[
        StrictStr, Field(min_length=1, max_length=128), AfterValidator(_reject_nul)
    ] | None = None
    addressed_role: ShortText | None = None
    addressed_actor_id: uuid.UUID | None = None
    task_id: uuid.UUID | None = None
    require_human_accept: StrictBool = False


class ClaimHandoffRequest(StrictInput):
    lease_seconds: StrictInt = Field(default=900, ge=30, le=86_400)


class RenewLeaseRequest(StrictInput):
    expected_claim_generation: StrictInt = Field(ge=1)
    lease_seconds: StrictInt = Field(default=900, ge=30, le=86_400)


class ReleaseHandoffRequest(StrictInput):
    expected_claim_generation: StrictInt = Field(ge=1)
    reason: ReasonText | None = None


class WorkProductReference(StrictInput):
    content_id: uuid.UUID
    revision: StrictInt | None = Field(default=None, ge=1)
    evidence_class: EvidenceClass = "work_product"


class ReturnHandoffRequest(StrictInput):
    expected_claim_generation: StrictInt = Field(ge=1)
    summary: BriefText
    work_products: list[WorkProductReference] = Field(
        default_factory=list, max_length=16
    )


class AcceptHandoffRequest(StrictInput):
    approval_id: uuid.UUID | None = None
    note: ReasonText | None = None


class ReworkHandoffRequest(StrictInput):
    instructions: BriefText
    note: ReasonText | None = None


class RetryHandoffRequest(StrictInput):
    reason: ReasonText | None = None


class FailHandoffRequest(StrictInput):
    expected_claim_generation: StrictInt = Field(ge=1)
    reason: ReasonText
    error_class: ShortText | None = None


class AbandonHandoffRequest(StrictInput):
    expected_claim_generation: StrictInt = Field(ge=1)
    reason: ReasonText | None = None


class WithdrawHandoffRequest(StrictInput):
    reason: ReasonText


class RequestApprovalRequest(StrictInput):
    gate_kind: GateKind
    subject_handoff_id: uuid.UUID | None = None
    subject_wave_id: uuid.UUID | None = None
    relay_source_handoff_id: uuid.UUID | None = None
    relay_target_scope_id: uuid.UUID | None = None
    relay_target_actor_id: uuid.UUID | None = None
    detail: dict[str, Any] = Field(default_factory=dict)
    expires_in_seconds: StrictInt | None = Field(
        default=None, ge=1, le=31_536_000
    )


class DecideApprovalRequest(StrictInput):
    decision: Literal["approved", "denied"]
    note: ReasonText | None = None


class NoteRequest(StrictInput):
    note: ReasonText | None = None


class AuthorizeRelayRequest(StrictInput):
    approval_id: uuid.UUID
    source_handoff_id: uuid.UUID
    target_scope_id: uuid.UUID
    target_actor_id: uuid.UUID


class CreateEpicRequest(StrictInput):
    name: NameText
    description: ReasonText | None = None


class CreateWaveRequest(StrictInput):
    epic_id: uuid.UUID
    wave_index: StrictInt = Field(ge=1)
    requires_human_gate: StrictBool = False


class OpenWaveRequest(StrictInput):
    gate_approval_id: uuid.UUID | None = None


class CreateBoardRequest(StrictInput):
    name: NameText


class AddBoardTaskRequest(StrictInput):
    task_id: uuid.UUID
    column: ShortText = "backlog"


class CreateTaskRequest(StrictInput):
    title: TitleText
    brief: BriefText | None = None
    epic_id: uuid.UUID | None = None
    wave_id: uuid.UUID | None = None
    depends_on: list[uuid.UUID] = Field(default_factory=list, max_length=32)


class AssignTaskRequest(StrictInput):
    actor_id: uuid.UUID
    assignment_role: AssignmentRole = "responsible"


class DispatchTaskRequest(StrictInput):
    note: ReasonText | None = None


class CompleteTaskRequest(StrictInput):
    note: ReasonText | None = None


class CancelTaskRequest(StrictInput):
    reason: ReasonText
