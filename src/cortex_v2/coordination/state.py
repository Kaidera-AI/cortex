"""Pure handoff state machine and claim fencing (R07).

This module is deliberately free of database and transport imports. Use cases
call it with the clock of the enclosing database transaction (``now()``), so
lease decisions are made against server time, never a client clock.

Fencing model: every successful claim increments ``claim_generation`` and
appends an immutable claim row. All lease-bearing commands carry the
generation they were issued; a command whose generation differs from the
active one is rejected, so a stale claimant can never publish a return,
completion, release or renewal against a newer claim.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

HANDOFF_STATUSES = (
    "open",
    "claimed",
    "returned",
    "accepted",
    "rework",
    "failed",
    "abandoned",
    "withdrawn",
)
TERMINAL_HANDOFF_STATUSES = frozenset({"accepted", "abandoned", "withdrawn"})
CLAIMABLE_STATUSES = frozenset({"open", "rework"})
WITHDRAWABLE_STATUSES = frozenset(
    {"open", "claimed", "returned", "rework", "failed"}
)
HANDOFF_ACTIONS = (
    "claim",
    "renew_lease",
    "release",
    "return",
    "accept",
    "rework",
    "fail",
    "retry",
    "abandon",
    "withdraw",
)

REASON_UNKNOWN_ACTION = "unknown_action"
REASON_INVALID_TRANSITION = "invalid_transition"
REASON_ALREADY_CLAIMED = "handoff_already_claimed"
REASON_LEASE_EXPIRED = "lease_expired"
REASON_STALE_GENERATION = "stale_claim_generation"
REASON_GENERATION_MISMATCH = "claim_generation_mismatch"
REASON_CLAIM_NOT_ACTIVE = "claim_not_active"


@dataclass(frozen=True, slots=True)
class HandoffState:
    status: str
    claim_generation: int = 0
    active_lease_expires_at: datetime | None = None

    def lease_is_expired(self, now: datetime) -> bool:
        return (
            self.status == "claimed"
            and self.active_lease_expires_at is not None
            and self.active_lease_expires_at <= now
        )


def validate_transition(
    action: str, state: HandoffState, *, now: datetime
) -> str | None:
    """Return None when the action is legal, else a typed reason code."""
    if action not in HANDOFF_ACTIONS:
        return REASON_UNKNOWN_ACTION
    if state.status in TERMINAL_HANDOFF_STATUSES:
        return REASON_INVALID_TRANSITION

    if action == "claim":
        if state.status in CLAIMABLE_STATUSES:
            return None
        if state.status == "claimed":
            return None if state.lease_is_expired(now) else REASON_ALREADY_CLAIMED
        return REASON_INVALID_TRANSITION

    if action in ("renew_lease", "return"):
        if state.status != "claimed":
            return REASON_INVALID_TRANSITION
        return REASON_LEASE_EXPIRED if state.lease_is_expired(now) else None

    if action in ("release", "fail", "abandon"):
        return None if state.status == "claimed" else REASON_INVALID_TRANSITION

    if action in ("accept", "rework"):
        return None if state.status == "returned" else REASON_INVALID_TRANSITION

    if action == "retry":
        return None if state.status == "failed" else REASON_INVALID_TRANSITION

    if action == "withdraw":
        return (
            None
            if state.status in WITHDRAWABLE_STATUSES
            else REASON_INVALID_TRANSITION
        )

    return REASON_UNKNOWN_ACTION


def fencing_violation(
    *, expected_claim_generation: int, state: HandoffState
) -> str | None:
    """Return None when the expected generation matches the active claim."""
    if state.claim_generation == 0:
        return REASON_CLAIM_NOT_ACTIVE
    if expected_claim_generation < state.claim_generation:
        return REASON_STALE_GENERATION
    if expected_claim_generation > state.claim_generation:
        return REASON_GENERATION_MISMATCH
    return None
