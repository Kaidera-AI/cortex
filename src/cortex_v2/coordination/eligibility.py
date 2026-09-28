"""Pure deterministic task dispatch eligibility (R09).

``evaluate_dispatch`` is a pure function over a snapshot that the use-case
layer loads inside one locked database transaction, so the verdict is
deterministic for identical inputs and can be unit-tested without a
database. A task dispatches only when it is open, has exactly one
responsible owner, every dependency is terminal (completed or cancelled),
its wave is open with all prior waves completed, and any human gate holds a
live approved gate approval.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

TERMINAL_TASK_STATUSES = frozenset({"completed", "cancelled"})

REASON_TASK_NOT_OPEN = "task_not_open"
REASON_NO_RESPONSIBLE_OWNER = "no_responsible_owner"
REASON_AMBIGUOUS_RESPONSIBILITY = "ambiguous_responsibility"
REASON_DEPENDENCY_NOT_TERMINAL = "dependency_not_terminal"
REASON_WAVE_NOT_OPEN = "wave_not_open"
REASON_PRIOR_WAVE_INCOMPLETE = "prior_wave_incomplete"
REASON_HUMAN_GATE_NOT_APPROVED = "human_gate_not_approved"
REASON_HUMAN_GATE_EXPIRED = "human_gate_expired"


@dataclass(frozen=True, slots=True)
class WaveGate:
    requires_human_gate: bool
    approval_status: str | None
    approval_expires_at: datetime | None


@dataclass(frozen=True, slots=True)
class WaveState:
    status: str
    prior_waves_completed: bool
    gate: WaveGate


@dataclass(frozen=True, slots=True)
class TaskDispatchSnapshot:
    task_status: str
    dependency_statuses: tuple[str, ...]
    responsible_owner_count: int
    wave: WaveState | None
    now: datetime


@dataclass(frozen=True, slots=True)
class DispatchEligibility:
    eligible: bool
    reasons: tuple[str, ...]


def evaluate_dispatch(snapshot: TaskDispatchSnapshot) -> DispatchEligibility:
    reasons: list[str] = []

    if snapshot.task_status != "open":
        reasons.append(REASON_TASK_NOT_OPEN)

    if snapshot.responsible_owner_count == 0:
        reasons.append(REASON_NO_RESPONSIBLE_OWNER)
    elif snapshot.responsible_owner_count > 1:
        reasons.append(REASON_AMBIGUOUS_RESPONSIBILITY)

    if any(
        status not in TERMINAL_TASK_STATUSES
        for status in snapshot.dependency_statuses
    ):
        reasons.append(REASON_DEPENDENCY_NOT_TERMINAL)

    wave = snapshot.wave
    if wave is not None:
        if wave.status != "open":
            reasons.append(REASON_WAVE_NOT_OPEN)
        if not wave.prior_waves_completed:
            reasons.append(REASON_PRIOR_WAVE_INCOMPLETE)
        if wave.gate.requires_human_gate:
            if wave.gate.approval_status != "approved":
                reasons.append(REASON_HUMAN_GATE_NOT_APPROVED)
            elif (
                wave.gate.approval_expires_at is not None
                and wave.gate.approval_expires_at <= snapshot.now
            ):
                reasons.append(REASON_HUMAN_GATE_EXPIRED)

    reasons.sort()
    return DispatchEligibility(eligible=not reasons, reasons=tuple(reasons))
