"""Pure unit tests for the coordination handoff state machine (R07).

These tests execute only in the throwaway unit-test container; they never
touch a database. They pin the legal handoff transitions, lease-expiry
reclaim semantics and the claim-generation fencing rules that guarantee a
stale claimant can never publish against a newer claim.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from cortex_v2.coordination.state import (
    HANDOFF_STATUSES,
    TERMINAL_HANDOFF_STATUSES,
    HandoffState,
    fencing_violation,
    validate_transition,
)

NOW = datetime(2026, 9, 25, 12, 0, 0, tzinfo=UTC)
FUTURE = NOW + timedelta(minutes=15)
PAST = NOW - timedelta(minutes=1)


def state(status: str, generation: int = 0, expires: datetime | None = None) -> HandoffState:
    return HandoffState(
        status=status,
        claim_generation=generation,
        active_lease_expires_at=expires,
    )


def claimed(generation: int = 1, expires: datetime = FUTURE) -> HandoffState:
    return state("claimed", generation, expires)


def test_statuses_are_closed_sets() -> None:
    assert HANDOFF_STATUSES == (
        "open",
        "claimed",
        "returned",
        "accepted",
        "rework",
        "failed",
        "abandoned",
        "withdrawn",
    )
    assert TERMINAL_HANDOFF_STATUSES == frozenset({"accepted", "abandoned", "withdrawn"})


@pytest.mark.parametrize("status", ["open", "rework"])
def test_claim_from_claimable_statuses_is_legal(status: str) -> None:
    assert validate_transition("claim", state(status), now=NOW) is None


def test_claim_from_claimed_with_live_lease_is_rejected() -> None:
    assert (
        validate_transition("claim", claimed(expires=FUTURE), now=NOW)
        == "handoff_already_claimed"
    )


def test_claim_from_claimed_with_expired_lease_is_reclaim() -> None:
    assert validate_transition("claim", claimed(expires=PAST), now=NOW) is None


def test_claim_boundary_lease_expiry_is_expired() -> None:
    assert (
        validate_transition("claim", claimed(expires=NOW), now=NOW) is None
    )


@pytest.mark.parametrize(
    "status", ["returned", "failed", "accepted", "abandoned", "withdrawn"]
)
def test_claim_from_non_claimable_statuses_is_invalid(status: str) -> None:
    assert validate_transition("claim", state(status), now=NOW) == "invalid_transition"


def test_renew_lease_requires_live_claim() -> None:
    assert validate_transition("renew_lease", claimed(), now=NOW) is None
    assert (
        validate_transition("renew_lease", claimed(expires=PAST), now=NOW)
        == "lease_expired"
    )
    assert (
        validate_transition("renew_lease", state("open"), now=NOW)
        == "invalid_transition"
    )


@pytest.mark.parametrize("expires", [FUTURE, PAST])
def test_release_is_allowed_from_claimed_regardless_of_expiry(expires: datetime) -> None:
    assert validate_transition("release", claimed(expires=expires), now=NOW) is None


def test_release_from_open_is_invalid() -> None:
    assert validate_transition("release", state("open"), now=NOW) == "invalid_transition"


def test_return_requires_live_lease() -> None:
    assert validate_transition("return", claimed(), now=NOW) is None
    assert validate_transition("return", claimed(expires=PAST), now=NOW) == "lease_expired"
    assert (
        validate_transition("return", state("returned", 1), now=NOW)
        == "invalid_transition"
    )


@pytest.mark.parametrize("action,to_status", [("accept", "returned"), ("rework", "returned")])
def test_review_actions_require_returned(action: str, to_status: str) -> None:
    assert validate_transition(action, state(to_status), now=NOW) is None
    assert validate_transition(action, claimed(), now=NOW) == "invalid_transition"


@pytest.mark.parametrize("expires", [FUTURE, PAST])
def test_fail_and_abandon_are_allowed_from_claimed(expires: datetime) -> None:
    assert validate_transition("fail", claimed(expires=expires), now=NOW) is None
    assert validate_transition("abandon", claimed(expires=expires), now=NOW) is None


def test_retry_requires_failed() -> None:
    assert validate_transition("retry", state("failed"), now=NOW) is None
    assert validate_transition("retry", claimed(), now=NOW) == "invalid_transition"


@pytest.mark.parametrize(
    "status", ["open", "claimed", "returned", "rework", "failed"]
)
def test_withdraw_from_non_terminal_statuses(status: str) -> None:
    assert validate_transition("withdraw", state(status), now=NOW) is None


@pytest.mark.parametrize("status", sorted(TERMINAL_HANDOFF_STATUSES))
def test_terminal_statuses_reject_every_action(status: str) -> None:
    for action in (
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
    ):
        assert validate_transition(action, state(status), now=NOW) == "invalid_transition"


def test_unknown_action_is_rejected() -> None:
    assert validate_transition("teleport", state("open"), now=NOW) == "unknown_action"


def test_fencing_accepts_matching_generation() -> None:
    assert fencing_violation(expected_claim_generation=2, state=claimed(2)) is None


def test_fencing_rejects_stale_generation() -> None:
    assert (
        fencing_violation(expected_claim_generation=1, state=claimed(2))
        == "stale_claim_generation"
    )


def test_fencing_rejects_future_generation() -> None:
    assert (
        fencing_violation(expected_claim_generation=3, state=claimed(2))
        == "claim_generation_mismatch"
    )


def test_fencing_rejects_never_claimed_handoff() -> None:
    assert (
        fencing_violation(expected_claim_generation=1, state=state("open"))
        == "claim_not_active"
    )


def test_stale_generation_cannot_return_after_reclaim() -> None:
    reclaimed = claimed(generation=2, expires=FUTURE)
    assert fencing_violation(expected_claim_generation=1, state=reclaimed) == (
        "stale_claim_generation"
    )
    assert validate_transition("return", reclaimed, now=NOW) is None
