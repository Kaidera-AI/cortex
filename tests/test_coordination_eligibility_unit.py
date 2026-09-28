"""Pure unit tests for deterministic task dispatch eligibility (R09).

Eligibility is a pure function over a locked snapshot: dependencies must be
terminal, the wave must be open with all prior waves completed, and human
gates require a live approved gate approval. Zero or multiple responsible
owners fail explicitly.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from cortex_v2.coordination.eligibility import (
    DispatchEligibility,
    TaskDispatchSnapshot,
    WaveGate,
    WaveState,
    evaluate_dispatch,
)

NOW = datetime(2026, 9, 25, 12, 0, 0, tzinfo=UTC)
FUTURE = NOW + timedelta(hours=1)
PAST = NOW - timedelta(hours=1)


def gate(
    requires: bool = False,
    status: str | None = None,
    expires: datetime | None = None,
) -> WaveGate:
    return WaveGate(
        requires_human_gate=requires,
        approval_status=status,
        approval_expires_at=expires,
    )


def wave(
    status: str = "open",
    prior_completed: bool = True,
    human_gate: WaveGate | None = None,
) -> WaveState:
    return WaveState(
        status=status,
        prior_waves_completed=prior_completed,
        gate=human_gate or WaveGate(requires_human_gate=False, approval_status=None,
                                   approval_expires_at=None),
    )


def snapshot(**overrides: object) -> TaskDispatchSnapshot:
    base: dict[str, object] = {
        "task_status": "open",
        "dependency_statuses": ("completed",),
        "responsible_owner_count": 1,
        "wave": wave(),
        "now": NOW,
    }
    base.update(overrides)
    return TaskDispatchSnapshot(**base)  # type: ignore[arg-type]


def test_baseline_snapshot_is_eligible() -> None:
    result = evaluate_dispatch(snapshot())
    assert result == DispatchEligibility(eligible=True, reasons=())


def test_taskless_wave_is_eligible() -> None:
    result = evaluate_dispatch(snapshot(wave=None))
    assert result.eligible is True


@pytest.mark.parametrize("status", ["dispatched", "completed", "cancelled"])
def test_non_open_task_is_ineligible(status: str) -> None:
    result = evaluate_dispatch(snapshot(task_status=status))
    assert result.eligible is False
    assert result.reasons == ("task_not_open",)


def test_zero_responsible_owners_fails_explicitly() -> None:
    result = evaluate_dispatch(snapshot(responsible_owner_count=0))
    assert result.reasons == ("no_responsible_owner",)


def test_multiple_responsible_owners_fails_explicitly() -> None:
    result = evaluate_dispatch(snapshot(responsible_owner_count=2))
    assert result.reasons == ("ambiguous_responsibility",)


@pytest.mark.parametrize("dependency", ["open", "dispatched"])
def test_non_terminal_dependency_blocks_dispatch(dependency: str) -> None:
    result = evaluate_dispatch(
        snapshot(dependency_statuses=("completed", dependency))
    )
    assert result.reasons == ("dependency_not_terminal",)


@pytest.mark.parametrize("dependency", ["completed", "cancelled"])
def test_terminal_dependencies_allow_dispatch(dependency: str) -> None:
    result = evaluate_dispatch(snapshot(dependency_statuses=(dependency,)))
    assert result.eligible is True


def test_no_dependencies_is_eligible() -> None:
    result = evaluate_dispatch(snapshot(dependency_statuses=()))
    assert result.eligible is True


@pytest.mark.parametrize("status", ["pending", "completed"])
def test_closed_wave_blocks_dispatch(status: str) -> None:
    result = evaluate_dispatch(snapshot(wave=wave(status=status)))
    assert result.reasons == ("wave_not_open",)


def test_incomplete_prior_wave_blocks_dispatch() -> None:
    result = evaluate_dispatch(snapshot(wave=wave(prior_completed=False)))
    assert result.reasons == ("prior_wave_incomplete",)


@pytest.mark.parametrize("status", [None, "pending", "denied", "revoked"])
def test_unapproved_human_gate_blocks_dispatch(status: str | None) -> None:
    result = evaluate_dispatch(
        snapshot(wave=wave(human_gate=gate(True, status)))
    )
    assert result.reasons == ("human_gate_not_approved",)


def test_expired_human_gate_approval_blocks_dispatch() -> None:
    result = evaluate_dispatch(
        snapshot(wave=wave(human_gate=gate(True, "approved", PAST)))
    )
    assert result.reasons == ("human_gate_expired",)


def test_live_human_gate_approval_allows_dispatch() -> None:
    result = evaluate_dispatch(
        snapshot(wave=wave(human_gate=gate(True, "approved", FUTURE)))
    )
    assert result.eligible is True


def test_undated_human_gate_approval_stays_live() -> None:
    result = evaluate_dispatch(
        snapshot(wave=wave(human_gate=gate(True, "approved", None)))
    )
    assert result.eligible is True


def test_reasons_are_deterministically_sorted() -> None:
    result = evaluate_dispatch(
        snapshot(
            task_status="dispatched",
            responsible_owner_count=0,
            dependency_statuses=("open",),
            wave=wave(status="pending", prior_completed=False),
        )
    )
    assert result.eligible is False
    assert result.reasons == tuple(
        sorted(
            [
                "task_not_open",
                "no_responsible_owner",
                "dependency_not_terminal",
                "wave_not_open",
                "prior_wave_incomplete",
            ]
        )
    )


def test_evaluation_is_a_pure_function_of_the_snapshot() -> None:
    first = evaluate_dispatch(snapshot())
    second = evaluate_dispatch(snapshot())
    assert first == second
