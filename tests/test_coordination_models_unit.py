"""Pure unit tests for coordination request models.

Every coordination payload is a strict pydantic model: extra fields are
forbidden, strict scalars are required, NUL characters are rejected before
they can reach PostgreSQL text columns, and bounds match the CHECK
constraints declared in migrations/0003_coordination.sql.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from pydantic import ValidationError

from cortex_v2.coordination.models import (
    AbandonHandoffRequest,
    AcceptHandoffRequest,
    AddBoardTaskRequest,
    AssignTaskRequest,
    AuthorizeRelayRequest,
    ClaimHandoffRequest,
    CompleteTaskRequest,
    CreateBoardRequest,
    CreateEpicRequest,
    CreateHandoffRequest,
    CreateTaskRequest,
    CreateWaveRequest,
    DecideApprovalRequest,
    FailHandoffRequest,
    OpenWaveRequest,
    ReleaseHandoffRequest,
    RenewLeaseRequest,
    RequestApprovalRequest,
    ReturnHandoffRequest,
    ReworkHandoffRequest,
    RetryHandoffRequest,
    WithdrawHandoffRequest,
    WorkProductReference,
)

HANDOFF = uuid.UUID("11111111-1111-4111-8111-111111111111")
CONTENT = uuid.UUID("22222222-2222-4222-8222-222222222222")
APPROVAL = uuid.UUID("33333333-3333-4333-8333-333333333333")
SCOPE = uuid.UUID("44444444-4444-4444-8444-444444444444")
ACTOR = uuid.UUID("55555555-5555-4555-8555-555555555555")


def test_create_handoff_defaults() -> None:
    request = CreateHandoffRequest(title="ship it", brief="do the thing")
    assert request.dedup_key is None
    assert request.addressed_role is None
    assert request.addressed_actor_id is None
    assert request.task_id is None
    assert request.require_human_accept is False


def test_create_handoff_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        CreateHandoffRequest(title="t", brief="b", smuggled="x")


@pytest.mark.parametrize(
    "field,value",
    [("title", "x" * 257), ("brief", ""), ("dedup_key", "x" * 129)],
)
def test_create_handoff_bounds(field: str, value: str) -> None:
    kwargs: dict[str, object] = {"title": "t", "brief": "b"}
    kwargs[field] = value
    with pytest.raises(ValidationError):
        CreateHandoffRequest(**kwargs)


@pytest.mark.parametrize("field", ["title", "brief"])
def test_create_handoff_rejects_nul(field: str) -> None:
    kwargs: dict[str, object] = {"title": "t", "brief": "b"}
    kwargs[field] = f"bad\x00{field}"
    with pytest.raises(ValidationError):
        CreateHandoffRequest(**kwargs)


def test_create_handoff_rejects_non_strict_types() -> None:
    with pytest.raises(ValidationError):
        CreateHandoffRequest(title=1, brief=True)


def test_claim_lease_bounds() -> None:
    assert ClaimHandoffRequest().lease_seconds == 900
    with pytest.raises(ValidationError):
        ClaimHandoffRequest(lease_seconds=29)
    with pytest.raises(ValidationError):
        ClaimHandoffRequest(lease_seconds=86_401)


def test_renew_lease_requires_fencing_generation() -> None:
    with pytest.raises(ValidationError):
        RenewLeaseRequest()
    with pytest.raises(ValidationError):
        RenewLeaseRequest(expected_claim_generation=0)
    assert RenewLeaseRequest(expected_claim_generation=1).lease_seconds == 900


def test_release_and_retry_reason_bounds() -> None:
    assert ReleaseHandoffRequest(expected_claim_generation=1).reason is None
    with pytest.raises(ValidationError):
        ReleaseHandoffRequest(expected_claim_generation=1, reason="x" * 513)
    with pytest.raises(ValidationError):
        RetryHandoffRequest(reason="x" * 513)


def test_return_requires_summary_and_fencing() -> None:
    with pytest.raises(ValidationError):
        ReturnHandoffRequest(summary="done")
    with pytest.raises(ValidationError):
        ReturnHandoffRequest(expected_claim_generation=1, summary="")
    request = ReturnHandoffRequest(expected_claim_generation=2, summary="done")
    assert request.work_products == []


def test_return_work_product_bounds() -> None:
    reference = WorkProductReference(content_id=CONTENT)
    assert reference.revision is None
    assert reference.evidence_class == "work_product"
    with pytest.raises(ValidationError):
        WorkProductReference(content_id=CONTENT, revision=0)
    with pytest.raises(ValidationError):
        WorkProductReference(content_id=CONTENT, evidence_class="rumor")
    with pytest.raises(ValidationError):
        ReturnHandoffRequest(
            expected_claim_generation=1,
            summary="s",
            work_products=[{"content_id": str(CONTENT)}] * 17,
        )


def test_accept_and_rework_shapes() -> None:
    assert AcceptHandoffRequest().approval_id is None
    with pytest.raises(ValidationError):
        ReworkHandoffRequest(instructions="")
    assert ReworkHandoffRequest(instructions="fix the flange").note is None


def test_fail_requires_reason_and_fencing_generation() -> None:
    with pytest.raises(ValidationError):
        FailHandoffRequest()
    with pytest.raises(ValidationError):
        FailHandoffRequest(reason="provider exploded")
    request = FailHandoffRequest(
        expected_claim_generation=1, reason="provider exploded"
    )
    assert request.error_class is None
    with pytest.raises(ValidationError):
        AbandonHandoffRequest(reason="stuck")
    assert (
        AbandonHandoffRequest(expected_claim_generation=2, reason="stuck").reason
        == "stuck"
    )
    with pytest.raises(ValidationError):
        WithdrawHandoffRequest(reason="")


def test_approval_request_gate_kinds() -> None:
    request = RequestApprovalRequest(
        gate_kind="relay",
        relay_source_handoff_id=HANDOFF,
        relay_target_scope_id=SCOPE,
        relay_target_actor_id=ACTOR,
    )
    assert request.detail == {}
    assert request.expires_in_seconds is None
    with pytest.raises(ValidationError):
        RequestApprovalRequest(gate_kind="vibes")
    with pytest.raises(ValidationError):
        RequestApprovalRequest(gate_kind="wave", expires_in_seconds=0)


def test_decide_approval_literals() -> None:
    assert DecideApprovalRequest(decision="approved").note is None
    with pytest.raises(ValidationError):
        DecideApprovalRequest(decision="maybe")


def test_authorize_relay_requires_exact_ids() -> None:
    request = AuthorizeRelayRequest(
        approval_id=APPROVAL,
        source_handoff_id=HANDOFF,
        target_scope_id=SCOPE,
        target_actor_id=ACTOR,
    )
    assert request.target_scope_id == SCOPE
    assert AuthorizeRelayRequest(
        approval_id=APPROVAL,
        source_handoff_id=HANDOFF,
        target_scope_id=str(SCOPE),
        target_actor_id=ACTOR,
    ).target_scope_id == SCOPE
    with pytest.raises(ValidationError):
        AuthorizeRelayRequest(
            approval_id=APPROVAL,
            source_handoff_id=HANDOFF,
            target_scope_id="not-a-uuid",
            target_actor_id=ACTOR,
        )


def test_planning_models() -> None:
    assert CreateEpicRequest(name="epic").description is None
    with pytest.raises(ValidationError):
        CreateEpicRequest(name="x" * 129)
    with pytest.raises(ValidationError):
        CreateWaveRequest(epic_id=HANDOFF, wave_index=0)
    wave_request = CreateWaveRequest(epic_id=HANDOFF, wave_index=2)
    assert wave_request.requires_human_gate is False
    assert OpenWaveRequest().gate_approval_id is None
    with pytest.raises(ValidationError):
        CreateBoardRequest(name="")


def test_task_models() -> None:
    task = CreateTaskRequest(title="t")
    assert task.depends_on == []
    assert task.epic_id is None and task.wave_id is None
    with pytest.raises(ValidationError):
        CreateTaskRequest(title="t", depends_on=[str(HANDOFF)] * 33)
    with pytest.raises(ValidationError):
        AssignTaskRequest(actor_id=ACTOR, assignment_role="bystander")
    assert AssignTaskRequest(actor_id=ACTOR).assignment_role == "responsible"
    assert AddBoardTaskRequest(task_id=HANDOFF).column == "backlog"
    with pytest.raises(ValidationError):
        AddBoardTaskRequest(task_id=HANDOFF, column="x" * 65)
    assert CompleteTaskRequest().note is None


def test_wave_request_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        CreateWaveRequest(epic_id=HANDOFF, wave_index=1, autonomy="full")


def test_lease_seconds_type_is_strict() -> None:
    with pytest.raises(ValidationError):
        ClaimHandoffRequest(lease_seconds="900")
    with pytest.raises(ValidationError):
        ClaimHandoffRequest(lease_seconds=timedelta(minutes=15))
