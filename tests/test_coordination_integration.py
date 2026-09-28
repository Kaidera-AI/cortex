"""DB-backed integration suite for the Coordination module (R07-R10).

These tests exercise the coordination use cases directly against a fresh
full-v2 candidate database (migrations 0001-0003 applied) through the
non-owner ``cortex_v2_app`` role with transaction-local context, exactly as
the mounted transport would. They are gated on environment variables and
skip cleanly elsewhere.

Required environment (integration phase, fresh candidate DB only):
  CORTEX_V2_TEST_DATABASE_URL            postgresql://cortex_v2_app@.../cortex_v2
  CORTEX_V2_TEST_MIGRATOR_DATABASE_URL   postgresql://cortex_v2_migrator@.../cortex_v2

Fixture needs (created by this module through the migrator role, unique per
run): one installation, project scopes A/B/C with primary aliases, an
installation-owner human principal, a human lead, two agent workers, scope
grants, actors, bindings and memberships. No credentials/tokens are needed
because the suite calls use cases below the transport auth layer.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Coroutine

import asyncpg
import pytest

DATABASE_URL = os.environ.get("CORTEX_V2_TEST_DATABASE_URL")
MIGRATOR_URL = os.environ.get("CORTEX_V2_TEST_MIGRATOR_DATABASE_URL")

if not DATABASE_URL or not MIGRATOR_URL:
    pytest.skip(
        "coordination integration suite requires a candidate database "
        "(CORTEX_V2_TEST_DATABASE_URL / CORTEX_V2_TEST_MIGRATOR_DATABASE_URL)",
        allow_module_level=True,
    )

from cortex_v2 import content as content_uc  # noqa: E402
from cortex_v2.coordination import (  # noqa: E402
    approvals as approval_uc,
    handoffs as handoff_uc,
    planning as planning_uc,
    relays as relay_uc,
    work_products as work_product_uc,
)
from cortex_v2.coordination.models import (  # noqa: E402
    AbandonHandoffRequest,
    AcceptHandoffRequest,
    AddBoardTaskRequest,
    AssignTaskRequest,
    AuthorizeRelayRequest,
    CancelTaskRequest,
    ClaimHandoffRequest,
    CompleteTaskRequest,
    CreateBoardRequest,
    CreateEpicRequest,
    CreateHandoffRequest,
    CreateTaskRequest,
    CreateWaveRequest,
    DecideApprovalRequest,
    DispatchTaskRequest,
    FailHandoffRequest,
    NoteRequest,
    OpenWaveRequest,
    ReleaseHandoffRequest,
    RenewLeaseRequest,
    RequestApprovalRequest,
    RetryHandoffRequest,
    ReturnHandoffRequest,
    ReworkHandoffRequest,
    WithdrawHandoffRequest,
    WorkProductReference,
)
from cortex_v2.models import (  # noqa: E402
    ContentStatusRequest,
    CreateContentRequest,
    ReviseContentRequest,
)
from cortex_v2.store import ApiProblem, Principal, resolve_scopes  # noqa: E402


@dataclass(frozen=True)
class Fixture:
    installation_id: uuid.UUID
    scope_a: uuid.UUID
    scope_b: uuid.UUID
    scope_c: uuid.UUID
    alias_a: str
    alias_b: str
    alias_c: str
    owner: uuid.UUID
    reviewer: uuid.UUID
    worker_a: uuid.UUID
    worker_b: uuid.UUID
    owner_actor: uuid.UUID
    reviewer_actor: uuid.UUID
    worker_a_actor: uuid.UUID
    worker_b_actor: uuid.UUID


async def _seed() -> Fixture:
    suffix = uuid.uuid4().hex[:10]
    fixture = Fixture(
        installation_id=uuid.uuid4(),
        scope_a=uuid.uuid4(),
        scope_b=uuid.uuid4(),
        scope_c=uuid.uuid4(),
        alias_a=f"coord-int-a-{suffix}",
        alias_b=f"coord-int-b-{suffix}",
        alias_c=f"coord-int-c-{suffix}",
        owner=uuid.uuid4(),
        reviewer=uuid.uuid4(),
        worker_a=uuid.uuid4(),
        worker_b=uuid.uuid4(),
        owner_actor=uuid.uuid4(),
        reviewer_actor=uuid.uuid4(),
        worker_a_actor=uuid.uuid4(),
        worker_b_actor=uuid.uuid4(),
    )
    connection = await asyncpg.connect(MIGRATOR_URL)
    try:
        async with connection.transaction():
            await connection.execute(
                "INSERT INTO cortex_auth.installations(installation_id, display_name)"
                " VALUES ($1, $2)",
                fixture.installation_id,
                f"coord-int-{suffix}",
            )
            for principal, name in (
                (fixture.owner, "coord-owner"),
                (fixture.reviewer, "coord-reviewer"),
                (fixture.worker_a, "coord-worker-a"),
                (fixture.worker_b, "coord-worker-b"),
            ):
                await connection.execute(
                    "INSERT INTO cortex_auth.principals"
                    "(principal_id, installation_id, principal_name, status)"
                    " VALUES ($1, $2, $3, 'active')",
                    principal,
                    fixture.installation_id,
                    f"{name}-{suffix}",
                )
            # installation_owners references principals; insert after them.
            await connection.execute(
                "INSERT INTO cortex_auth.installation_owners"
                "(installation_id, principal_id) VALUES ($1, $2)",
                fixture.installation_id,
                fixture.owner,
            )
            for scope, alias in (
                (fixture.scope_a, fixture.alias_a),
                (fixture.scope_b, fixture.alias_b),
                (fixture.scope_c, fixture.alias_c),
            ):
                await connection.execute(
                    "INSERT INTO cortex_core.scopes"
                    "(scope_id, scope_kind, display_name) VALUES ($1, 'project', $2)",
                    scope,
                    alias,
                )
                await connection.execute(
                    "INSERT INTO cortex_core.scope_aliases"
                    "(alias, scope_id, is_primary) VALUES ($1, $2, true)",
                    alias,
                    scope,
                )
            grants = [
                (fixture.owner, fixture.scope_a, True, True, True),
                (fixture.owner, fixture.scope_b, True, True, True),
                (fixture.owner, fixture.scope_c, True, True, True),
                (fixture.reviewer, fixture.scope_a, True, True, True),
                (fixture.reviewer, fixture.scope_b, True, True, True),
                (fixture.worker_a, fixture.scope_a, True, True, False),
                (fixture.worker_a, fixture.scope_c, True, True, False),
                (fixture.worker_b, fixture.scope_a, True, True, False),
                (fixture.worker_b, fixture.scope_b, True, True, False),
            ]
            for principal, scope, can_read, can_write, can_publish in grants:
                await connection.execute(
                    "INSERT INTO cortex_auth.scope_grants"
                    "(principal_id, scope_id, can_read, can_write, can_publish)"
                    " VALUES ($1, $2, $3, $4, $5)",
                    principal,
                    scope,
                    can_read,
                    can_write,
                    can_publish,
                )
            actors = [
                (fixture.owner_actor, "human", "coord-owner"),
                (fixture.reviewer_actor, "human", "coord-reviewer"),
                (fixture.worker_a_actor, "agent", "coord-worker-a"),
                (fixture.worker_b_actor, "agent", "coord-worker-b"),
            ]
            for actor, kind, name in actors:
                await connection.execute(
                    "INSERT INTO cortex_auth.actors"
                    "(actor_id, installation_id, actor_kind, display_name)"
                    " VALUES ($1, $2, $3, $4)",
                    actor,
                    fixture.installation_id,
                    kind,
                    f"{name}-{suffix}",
                )
            bindings = [
                (fixture.owner_actor, fixture.owner),
                (fixture.reviewer_actor, fixture.reviewer),
                (fixture.worker_a_actor, fixture.worker_a),
                (fixture.worker_b_actor, fixture.worker_b),
            ]
            for actor, principal in bindings:
                await connection.execute(
                    "INSERT INTO cortex_auth.actor_bindings"
                    "(actor_id, principal_id, bound_by_principal_id)"
                    " VALUES ($1, $2, $3)",
                    actor,
                    principal,
                    fixture.owner,
                )
            memberships = [
                (fixture.scope_a, fixture.owner_actor, "owner"),
                (fixture.scope_a, fixture.reviewer_actor, "lead"),
                (fixture.scope_a, fixture.worker_a_actor, "member"),
                (fixture.scope_a, fixture.worker_b_actor, "member"),
                (fixture.scope_b, fixture.owner_actor, "owner"),
                (fixture.scope_b, fixture.reviewer_actor, "lead"),
                (fixture.scope_b, fixture.worker_b_actor, "member"),
                (fixture.scope_c, fixture.owner_actor, "owner"),
                (fixture.scope_c, fixture.worker_a_actor, "member"),
            ]
            for scope, actor, role in memberships:
                await connection.execute(
                    "INSERT INTO cortex_auth.memberships"
                    "(scope_id, actor_id, membership_role) VALUES ($1, $2, $3)",
                    scope,
                    actor,
                    role,
                )
    finally:
        await connection.close()
    return fixture


FIXTURE = asyncio.run(_seed())


async def _command(
    principal_id: uuid.UUID,
    alias: str,
    *,
    write: bool,
    work: Callable[[asyncpg.Connection, Any], Coroutine[Any, Any, Any]],
    read_aliases: list[str] | None = None,
) -> Any:
    connection = await asyncpg.connect(DATABASE_URL)
    try:
        async with connection.transaction():
            await connection.execute(
                "SELECT set_config('cortex.principal_id', $1, true)",
                str(principal_id),
            )
            context = await resolve_scopes(
                connection,
                Principal(principal_id, FIXTURE.installation_id),
                alias,
                read_aliases or [alias],
                write=write,
            )
            return await work(connection, context)
    finally:
        await connection.close()


def as_owner(work, alias=None, write=True, read_aliases=None):
    return _command(
        FIXTURE.owner, alias or FIXTURE.alias_a,
        write=write, work=work, read_aliases=read_aliases,
    )


def as_worker_a(work, alias=None, write=True, read_aliases=None):
    return _command(
        FIXTURE.worker_a, alias or FIXTURE.alias_a,
        write=write, work=work, read_aliases=read_aliases,
    )


def as_worker_b(work, alias=None, write=True, read_aliases=None):
    return _command(
        FIXTURE.worker_b, alias or FIXTURE.alias_a,
        write=write, work=work, read_aliases=read_aliases,
    )


def as_reviewer(work, alias=None, write=True, read_aliases=None):
    return _command(
        FIXTURE.reviewer, alias or FIXTURE.alias_a,
        write=write, work=work, read_aliases=read_aliases,
    )


async def _migrator(query: str, *args: Any) -> Any:
    connection = await asyncpg.connect(MIGRATOR_URL)
    try:
        return await connection.fetchval(query, *args)
    finally:
        await connection.close()


LEASE_SECONDS = 30


def _wait_for_lease_expiry() -> None:
    """Leases expire by wall clock; database triggers forbid backdating."""
    time.sleep(LEASE_SECONDS + 1)


def _create_request(**overrides: Any) -> CreateHandoffRequest:
    base: dict[str, Any] = {"title": "integration handoff", "brief": "do work"}
    base.update(overrides)
    return CreateHandoffRequest(**base)


async def _create_handoff(context_principal, request=None, key=None, alias=None):
    payload = request or _create_request()
    return await _command(
        context_principal,
        alias or FIXTURE.alias_a,
        write=True,
        work=lambda conn, ctx: handoff_uc.create_handoff(
            conn, ctx, payload, key or f"key-{uuid.uuid4()}"
        ),
    )


def _code(excinfo) -> str:
    return excinfo.value.code


# ---------------------------------------------------------------------------
# R07: creation, deduplication, idempotency, atomic claim, fencing.
# ---------------------------------------------------------------------------


def test_create_replays_receipt_and_conflicts_on_new_payload() -> None:
    key = f"key-{uuid.uuid4()}"
    status, receipt, replayed = asyncio.run(
        _create_handoff(FIXTURE.worker_a, _create_request(title="t1"), key)
    )
    assert (status, replayed) == (201, False)

    status2, receipt2, replayed2 = asyncio.run(
        _create_handoff(FIXTURE.worker_a, _create_request(title="t1"), key)
    )
    assert (status2, replayed2) == (200, True)
    assert receipt2["handoff_id"] == receipt["handoff_id"]

    with pytest.raises(ApiProblem) as conflict:
        asyncio.run(
            _create_handoff(FIXTURE.worker_a, _create_request(title="other"), key)
        )
    assert _code(conflict) == "idempotency_key_reused"


def test_duplicate_dedup_key_is_rejected() -> None:
    dedup = f"dedup-{uuid.uuid4()}"
    asyncio.run(_create_handoff(FIXTURE.worker_a, _create_request(dedup_key=dedup)))
    with pytest.raises(ApiProblem) as conflict:
        asyncio.run(
            _create_handoff(FIXTURE.worker_a, _create_request(dedup_key=dedup))
        )
    assert _code(conflict) == "duplicate_handoff"


def test_concurrent_claim_has_exactly_one_winner() -> None:
    _status, receipt, _ = asyncio.run(_create_handoff(FIXTURE.worker_a))
    handoff_id = uuid.UUID(receipt["handoff_id"])

    async def race() -> list[Any]:
        def claim(worker: uuid.UUID):
            return _command(
                worker,
                FIXTURE.alias_a,
                write=True,
                work=lambda conn, ctx: handoff_uc.claim_handoff(
                    conn, ctx, handoff_id, ClaimHandoffRequest(lease_seconds=300),
                    f"claim-{uuid.uuid4()}",
                ),
            )

        barrier = asyncio.Barrier(2)

        async def gated(worker: uuid.UUID):
            await barrier.wait()
            return await claim(worker)

        return await asyncio.gather(
            gated(FIXTURE.worker_a),
            gated(FIXTURE.worker_b),
            return_exceptions=True,
        )

    results = asyncio.run(race())
    successes = [r for r in results if not isinstance(r, BaseException)]
    failures = [r for r in results if isinstance(r, ApiProblem)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert failures[0].code == "handoff_already_claimed"
    assert successes[0][1]["claim_generation"] == 1

    claims = asyncio.run(
        _migrator(
            "SELECT count(*) FROM cortex_coord.claims "
            "WHERE scope_id = $1 AND handoff_id = $2",
            FIXTURE.scope_a,
            handoff_id,
        )
    )
    assert claims == 1


def test_claim_is_addressed() -> None:
    _status, receipt, _ = asyncio.run(
        _create_handoff(
            FIXTURE.owner,
            _create_request(addressed_actor_id=FIXTURE.worker_a_actor),
        )
    )
    handoff_id = uuid.UUID(receipt["handoff_id"])
    with pytest.raises(ApiProblem) as denied:
        asyncio.run(
            as_worker_b(
                lambda conn, ctx: handoff_uc.claim_handoff(
                    conn, ctx, handoff_id, ClaimHandoffRequest(),
                    f"claim-{uuid.uuid4()}",
                )
            )
        )
    assert _code(denied) == "not_addressed_to_claimant"

    status, claim_receipt, _ = asyncio.run(
        as_worker_a(
            lambda conn, ctx: handoff_uc.claim_handoff(
                conn, ctx, handoff_id, ClaimHandoffRequest(),
                f"claim-{uuid.uuid4()}",
            )
        )
    )
    assert status == 200
    assert claim_receipt["status"] == "claimed"


def test_lease_renewal_extends_and_fences() -> None:
    _status, receipt, _ = asyncio.run(_create_handoff(FIXTURE.worker_a))
    handoff_id = uuid.UUID(receipt["handoff_id"])
    _status, claim, _ = asyncio.run(
        as_worker_a(
            lambda conn, ctx: handoff_uc.claim_handoff(
                conn, ctx, handoff_id, ClaimHandoffRequest(lease_seconds=60),
                f"claim-{uuid.uuid4()}",
            )
        )
    )
    first_lease = claim["lease_expires_at"]

    _status, renewed, _ = asyncio.run(
        as_worker_a(
            lambda conn, ctx: handoff_uc.renew_lease(
                conn, ctx, handoff_id,
                RenewLeaseRequest(expected_claim_generation=1, lease_seconds=600),
                f"renew-{uuid.uuid4()}",
            )
        )
    )
    assert renewed["lease_expires_at"] > first_lease

    with pytest.raises(ApiProblem) as stale:
        asyncio.run(
            as_worker_a(
                lambda conn, ctx: handoff_uc.renew_lease(
                    conn, ctx, handoff_id,
                    RenewLeaseRequest(expected_claim_generation=7,
                                      lease_seconds=600),
                    f"renew-{uuid.uuid4()}",
                )
            )
        )
    assert _code(stale) == "claim_generation_mismatch"


def test_expired_lease_blocks_publish_and_reclaim_fences_stale_worker() -> None:
    _s, h1, _ = asyncio.run(_create_handoff(FIXTURE.worker_a))
    _s, h2, _ = asyncio.run(_create_handoff(FIXTURE.worker_a))
    first = uuid.UUID(h1["handoff_id"])
    second = uuid.UUID(h2["handoff_id"])

    asyncio.run(
        as_worker_a(
            lambda conn, ctx: handoff_uc.claim_handoff(
                conn, ctx, first, ClaimHandoffRequest(
                    lease_seconds=LEASE_SECONDS),
                f"claim-{uuid.uuid4()}",
            )
        )
    )
    asyncio.run(
        as_worker_a(
            lambda conn, ctx: handoff_uc.claim_handoff(
                conn, ctx, second, ClaimHandoffRequest(
                    lease_seconds=LEASE_SECONDS),
                f"claim-{uuid.uuid4()}",
            )
        )
    )
    _wait_for_lease_expiry()

    # Expired lease: renew and return are refused; release is allowed cleanup.
    with pytest.raises(ApiProblem) as renew_denied:
        asyncio.run(
            as_worker_a(
                lambda conn, ctx: handoff_uc.renew_lease(
                    conn, ctx, first,
                    RenewLeaseRequest(expected_claim_generation=1),
                    f"renew-{uuid.uuid4()}",
                )
            )
        )
    assert _code(renew_denied) == "lease_expired"
    with pytest.raises(ApiProblem) as return_denied:
        asyncio.run(
            as_worker_a(
                lambda conn, ctx: handoff_uc.return_handoff(
                    conn, ctx, first,
                    ReturnHandoffRequest(expected_claim_generation=1,
                                         summary="late"),
                    f"return-{uuid.uuid4()}",
                )
            )
        )
    assert _code(return_denied) == "lease_expired"

    # Second handoff: worker B reclaims the expired lease (generation 2),
    # then stale worker A can neither return nor release.
    _status, reclaimed, _ = asyncio.run(
        as_worker_b(
            lambda conn, ctx: handoff_uc.claim_handoff(
                conn, ctx, second, ClaimHandoffRequest(lease_seconds=300),
                f"claim-{uuid.uuid4()}",
            )
        )
    )
    assert reclaimed["claim_generation"] == 2
    assert reclaimed["reclaimed_after_expiry"] is True

    with pytest.raises(ApiProblem) as stale_return:
        asyncio.run(
            as_worker_a(
                lambda conn, ctx: handoff_uc.return_handoff(
                    conn, ctx, second,
                    ReturnHandoffRequest(expected_claim_generation=1,
                                         summary="stale"),
                    f"return-{uuid.uuid4()}",
                )
            )
        )
    assert _code(stale_return) == "stale_claim_generation"
    with pytest.raises(ApiProblem) as stale_release:
        asyncio.run(
            as_worker_a(
                lambda conn, ctx: handoff_uc.release_handoff(
                    conn, ctx, second,
                    ReleaseHandoffRequest(expected_claim_generation=1),
                    f"release-{uuid.uuid4()}",
                )
            )
        )
    assert _code(stale_release) == "stale_claim_generation"

    # The reclaimed claim can still publish normally.
    status, returned, _ = asyncio.run(
        as_worker_b(
            lambda conn, ctx: handoff_uc.return_handoff(
                conn, ctx, second,
                ReturnHandoffRequest(expected_claim_generation=2,
                                     summary="fresh work"),
                f"return-{uuid.uuid4()}",
            )
        )
    )
    assert status == 200
    assert returned["status"] == "returned"

    ended = asyncio.run(
        _migrator(
            "SELECT end_reason FROM cortex_coord.claims WHERE scope_id = $1 "
            "AND handoff_id = $2 AND claim_generation = 1",
            FIXTURE.scope_a,
            second,
        )
    )
    assert ended == "lease_expired"


def test_return_replay_does_not_create_second_handback() -> None:
    _s, created, _ = asyncio.run(_create_handoff(FIXTURE.worker_a))
    handoff_id = uuid.UUID(created["handoff_id"])
    asyncio.run(
        as_worker_a(
            lambda conn, ctx: handoff_uc.claim_handoff(
                conn, ctx, handoff_id, ClaimHandoffRequest(), f"k-{uuid.uuid4()}"
            )
        )
    )
    key = f"return-{uuid.uuid4()}"
    payload = ReturnHandoffRequest(expected_claim_generation=1, summary="done")
    status1, receipt1, replay1 = asyncio.run(
        as_worker_a(
            lambda conn, ctx: handoff_uc.return_handoff(
                conn, ctx, handoff_id, payload, key
            )
        )
    )
    status2, receipt2, replay2 = asyncio.run(
        as_worker_a(
            lambda conn, ctx: handoff_uc.return_handoff(
                conn, ctx, handoff_id, payload, key
            )
        )
    )
    assert (status1, replay1) == (200, False)
    assert (status2, replay2) == (200, True)
    assert receipt2["return_seq"] == receipt1["return_seq"] == 1
    count = asyncio.run(
        _migrator(
            "SELECT count(*) FROM cortex_coord.returns WHERE scope_id = $1 "
            "AND handoff_id = $2",
            FIXTURE.scope_a,
            handoff_id,
        )
    )
    assert count == 1


def test_full_lifecycle_and_terminal_withdrawal() -> None:
    _s, created, _ = asyncio.run(
        _create_handoff(FIXTURE.worker_a, _create_request(title="lifecycle"))
    )
    handoff_id = uuid.UUID(created["handoff_id"])

    async def lifecycle() -> None:
        await as_worker_a(
            lambda conn, ctx: handoff_uc.claim_handoff(
                conn, ctx, handoff_id, ClaimHandoffRequest(), f"k-{uuid.uuid4()}"
            )
        )
        await as_worker_a(
            lambda conn, ctx: handoff_uc.return_handoff(
                conn, ctx, handoff_id,
                ReturnHandoffRequest(expected_claim_generation=1,
                                     summary="results"),
                f"k-{uuid.uuid4()}",
            )
        )
        status, accepted, _ = await as_reviewer(
            lambda conn, ctx: handoff_uc.accept_handoff(
                conn, ctx, handoff_id, AcceptHandoffRequest(note="lgtm"),
                f"k-{uuid.uuid4()}",
            )
        )
        assert status == 200
        assert accepted["status"] == "accepted"

    asyncio.run(lifecycle())

    with pytest.raises(ApiProblem) as frozen:
        asyncio.run(
            as_worker_a(
                lambda conn, ctx: handoff_uc.claim_handoff(
                    conn, ctx, handoff_id, ClaimHandoffRequest(),
                    f"k-{uuid.uuid4()}",
                )
            )
        )
    assert _code(frozen) == "invalid_transition"

    view = asyncio.run(
        as_owner(
            lambda conn, ctx: handoff_uc.get_handoff(conn, ctx, handoff_id),
            write=False,
        )
    )
    assert [c["claim_generation"] for c in view["claims"]] == [1]
    assert view["claims"][0]["end_reason"] == "returned"
    assert [r["decision"] for r in view["reviews"]] == ["accept"]
    assert len(view["audit"]) >= 4
    assert {e["action"] for e in view["audit"]} >= {
        "handoff.created", "handoff.claimed", "handoff.returned",
        "handoff.accept",
    }


def test_fail_retry_abandon_withdraw_paths() -> None:
    # fail -> retry -> open
    _s, created, _ = asyncio.run(_create_handoff(FIXTURE.worker_a))
    handoff_id = uuid.UUID(created["handoff_id"])
    asyncio.run(
        as_worker_a(
            lambda conn, ctx: handoff_uc.claim_handoff(
                conn, ctx, handoff_id, ClaimHandoffRequest(), f"k-{uuid.uuid4()}"
            )
        )
    )
    _s, failed, _ = asyncio.run(
        as_worker_a(
            lambda conn, ctx: handoff_uc.fail_handoff(
                conn, ctx, handoff_id,
                FailHandoffRequest(expected_claim_generation=1,
                                   reason="provider down",
                                   error_class="transient"),
                f"k-{uuid.uuid4()}",
            )
        )
    )
    assert failed["status"] == "failed"
    _s, retried, _ = asyncio.run(
        as_owner(
            lambda conn, ctx: handoff_uc.retry_handoff(
                conn, ctx, handoff_id, RetryHandoffRequest(reason="try again"),
                f"k-{uuid.uuid4()}",
            )
        )
    )
    assert retried["status"] == "open"

    # abandon is terminal
    _s, second, _ = asyncio.run(_create_handoff(FIXTURE.worker_a))
    second_id = uuid.UUID(second["handoff_id"])
    asyncio.run(
        as_worker_a(
            lambda conn, ctx: handoff_uc.claim_handoff(
                conn, ctx, second_id, ClaimHandoffRequest(), f"k-{uuid.uuid4()}"
            )
        )
    )
    _s, abandoned, _ = asyncio.run(
        as_worker_a(
            lambda conn, ctx: handoff_uc.abandon_handoff(
                conn, ctx, second_id, AbandonHandoffRequest(expected_claim_generation=1, reason="stuck"),
                f"k-{uuid.uuid4()}",
            )
        )
    )
    assert abandoned["status"] == "abandoned"
    with pytest.raises(ApiProblem) as frozen:
        asyncio.run(
            as_owner(
                lambda conn, ctx: handoff_uc.retry_handoff(
                    conn, ctx, second_id, RetryHandoffRequest(),
                    f"k-{uuid.uuid4()}",
                )
            )
        )
    assert _code(frozen) == "invalid_transition"

    # withdraw: creator or human lead only
    _s, third, _ = asyncio.run(_create_handoff(FIXTURE.owner))
    third_id = uuid.UUID(third["handoff_id"])
    with pytest.raises(ApiProblem) as denied:
        asyncio.run(
            as_worker_a(
                lambda conn, ctx: handoff_uc.withdraw_handoff(
                    conn, ctx, third_id, WithdrawHandoffRequest(reason="nope"),
                    f"k-{uuid.uuid4()}",
                )
            )
        )
    assert _code(denied) == "withdraw_not_allowed"
    _s, withdrawn, _ = asyncio.run(
        as_reviewer(
            lambda conn, ctx: handoff_uc.withdraw_handoff(
                conn, ctx, third_id, WithdrawHandoffRequest(reason="obsolete"),
                f"k-{uuid.uuid4()}",
            )
        )
    )
    assert withdrawn["status"] == "withdrawn"


def test_rework_then_reclaim_new_generation() -> None:
    _s, created, _ = asyncio.run(_create_handoff(FIXTURE.worker_a))
    handoff_id = uuid.UUID(created["handoff_id"])

    async def flow() -> None:
        await as_worker_a(
            lambda conn, ctx: handoff_uc.claim_handoff(
                conn, ctx, handoff_id, ClaimHandoffRequest(), f"k-{uuid.uuid4()}"
            )
        )
        await as_worker_a(
            lambda conn, ctx: handoff_uc.return_handoff(
                conn, ctx, handoff_id,
                ReturnHandoffRequest(expected_claim_generation=1,
                                     summary="attempt 1"),
                f"k-{uuid.uuid4()}",
            )
        )
        _s, reworked, _ = await as_reviewer(
            lambda conn, ctx: handoff_uc.rework_handoff(
                conn, ctx, handoff_id,
                ReworkHandoffRequest(instructions="add tests"),
                f"k-{uuid.uuid4()}",
            )
        )
        assert reworked["status"] == "rework"
        _s, reclaimed, _ = await as_worker_a(
            lambda conn, ctx: handoff_uc.claim_handoff(
                conn, ctx, handoff_id, ClaimHandoffRequest(), f"k-{uuid.uuid4()}"
            )
        )
        assert reclaimed["claim_generation"] == 2

    asyncio.run(flow())


# ---------------------------------------------------------------------------
# R07/R09: approvals and human gates.
# ---------------------------------------------------------------------------


def _request_accept_approval(handoff_id: uuid.UUID) -> str:
    _s, receipt, _ = asyncio.run(
        as_owner(
            lambda conn, ctx: approval_uc.request_approval(
                conn, ctx,
                RequestApprovalRequest(
                    gate_kind="handoff_accept", subject_handoff_id=handoff_id
                ),
                f"k-{uuid.uuid4()}",
            )
        )
    )
    return receipt["aggregate_id"]


def test_human_accept_gate() -> None:
    _s, created, _ = asyncio.run(
        _create_handoff(
            FIXTURE.owner, _create_request(require_human_accept=True)
        )
    )
    handoff_id = uuid.UUID(created["handoff_id"])
    asyncio.run(
        as_worker_a(
            lambda conn, ctx: handoff_uc.claim_handoff(
                conn, ctx, handoff_id, ClaimHandoffRequest(), f"k-{uuid.uuid4()}"
            )
        )
    )
    asyncio.run(
        as_worker_a(
            lambda conn, ctx: handoff_uc.return_handoff(
                conn, ctx, handoff_id,
                ReturnHandoffRequest(expected_claim_generation=1,
                                     summary="gated work"),
                f"k-{uuid.uuid4()}",
            )
        )
    )

    with pytest.raises(ApiProblem) as missing:
        asyncio.run(
            as_reviewer(
                lambda conn, ctx: handoff_uc.accept_handoff(
                    conn, ctx, handoff_id, AcceptHandoffRequest(),
                    f"k-{uuid.uuid4()}",
                )
            )
        )
    assert _code(missing) == "human_review_required"

    approval_id = uuid.UUID(_request_accept_approval(handoff_id))

    with pytest.raises(ApiProblem) as agent_denied:
        asyncio.run(
            as_worker_a(
                lambda conn, ctx: approval_uc.decide_approval(
                    conn, ctx, approval_id,
                    DecideApprovalRequest(decision="approved"),
                    f"k-{uuid.uuid4()}",
                )
            )
        )
    assert _code(agent_denied) == "human_authority_required"

    _s, decided, _ = asyncio.run(
        as_reviewer(
            lambda conn, ctx: approval_uc.decide_approval(
                conn, ctx, approval_id,
                DecideApprovalRequest(decision="approved", note="reviewed"),
                f"k-{uuid.uuid4()}",
            )
        )
    )
    assert decided["decision"] == "approved"

    _s, accepted, _ = asyncio.run(
        as_reviewer(
            lambda conn, ctx: handoff_uc.accept_handoff(
                conn, ctx, handoff_id,
                AcceptHandoffRequest(approval_id=approval_id),
                f"k-{uuid.uuid4()}",
            )
        )
    )
    assert accepted["status"] == "accepted"


def test_approval_subject_binding_and_revocation() -> None:
    _s, first, _ = asyncio.run(
        _create_handoff(FIXTURE.owner, _create_request(require_human_accept=True))
    )
    _s, second, _ = asyncio.run(
        _create_handoff(FIXTURE.owner, _create_request(require_human_accept=True))
    )
    first_id = uuid.UUID(first["handoff_id"])
    second_id = uuid.UUID(second["handoff_id"])

    async def prepare(handoff_id: uuid.UUID) -> None:
        await as_worker_a(
            lambda conn, ctx: handoff_uc.claim_handoff(
                conn, ctx, handoff_id, ClaimHandoffRequest(), f"k-{uuid.uuid4()}"
            )
        )
        await as_worker_a(
            lambda conn, ctx: handoff_uc.return_handoff(
                conn, ctx, handoff_id,
                ReturnHandoffRequest(expected_claim_generation=1,
                                     summary="work"),
                f"k-{uuid.uuid4()}",
            )
        )

    asyncio.run(prepare(first_id))
    asyncio.run(prepare(second_id))

    approval_id = uuid.UUID(_request_accept_approval(first_id))
    asyncio.run(
        as_reviewer(
            lambda conn, ctx: approval_uc.decide_approval(
                conn, ctx, approval_id,
                DecideApprovalRequest(decision="approved"),
                f"k-{uuid.uuid4()}",
            )
        )
    )

    # Approval for the first handoff cannot accept the second.
    with pytest.raises(ApiProblem) as mismatch:
        asyncio.run(
            as_reviewer(
                lambda conn, ctx: handoff_uc.accept_handoff(
                    conn, ctx, second_id,
                    AcceptHandoffRequest(approval_id=approval_id),
                    f"k-{uuid.uuid4()}",
                )
            )
        )
    assert _code(mismatch) == "approval_target_mismatch"

    # Revocation before consumption blocks acceptance of the first.
    asyncio.run(
        as_reviewer(
            lambda conn, ctx: approval_uc.revoke_approval(
                conn, ctx, approval_id, NoteRequest(note="changed mind"),
                f"k-{uuid.uuid4()}",
            )
        )
    )
    with pytest.raises(ApiProblem) as revoked:
        asyncio.run(
            as_reviewer(
                lambda conn, ctx: handoff_uc.accept_handoff(
                    conn, ctx, first_id,
                    AcceptHandoffRequest(approval_id=approval_id),
                    f"k-{uuid.uuid4()}",
                )
            )
        )
    assert _code(revoked) == "approval_not_approved"


# ---------------------------------------------------------------------------
# R08: cross-project relay with immutable IDs surviving alias renames.
# ---------------------------------------------------------------------------


def _rename_alias(scope_id: uuid.UUID, new_alias: str) -> None:
    async def rename(conn, _ctx):
        await conn.fetchrow(
            "SELECT * FROM cortex_auth.rename_scope_alias($1, $2, $3)",
            FIXTURE.owner,
            scope_id,
            new_alias,
        )
    asyncio.run(as_owner(rename))


def test_relay_requires_exact_approved_target() -> None:
    _s, created, _ = asyncio.run(_create_handoff(FIXTURE.owner))
    handoff_id = uuid.UUID(created["handoff_id"])

    _s, approval_receipt, _ = asyncio.run(
        as_owner(
            lambda conn, ctx: approval_uc.request_approval(
                conn, ctx,
                RequestApprovalRequest(
                    gate_kind="relay",
                    relay_source_handoff_id=handoff_id,
                    relay_target_scope_id=FIXTURE.scope_b,
                    relay_target_actor_id=FIXTURE.worker_b_actor,
                ),
                f"k-{uuid.uuid4()}",
            ),
            read_aliases=[FIXTURE.alias_a, FIXTURE.alias_b],
        )
    )
    approval_id = uuid.UUID(approval_receipt["aggregate_id"])
    asyncio.run(
        as_reviewer(
            lambda conn, ctx: approval_uc.decide_approval(
                conn, ctx, approval_id,
                DecideApprovalRequest(decision="approved"),
                f"k-{uuid.uuid4()}",
            )
        )
    )

    # A different target than the approved immutable IDs is denied.
    with pytest.raises(ApiProblem) as wrong_target:
        asyncio.run(
            as_owner(
                lambda conn, ctx: relay_uc.authorize_relay(
                    conn, ctx,
                    AuthorizeRelayRequest(
                        approval_id=approval_id,
                        source_handoff_id=handoff_id,
                        target_scope_id=FIXTURE.scope_b,
                        target_actor_id=FIXTURE.worker_a_actor,
                    ),
                    f"k-{uuid.uuid4()}",
                )
            )
        )
    assert _code(wrong_target) == "approval_target_mismatch"

    # Unapproved (pending) approvals cannot authorize either.
    _s, pending_receipt, _ = asyncio.run(
        as_owner(
            lambda conn, ctx: approval_uc.request_approval(
                conn, ctx,
                RequestApprovalRequest(
                    gate_kind="relay",
                    relay_source_handoff_id=handoff_id,
                    relay_target_scope_id=FIXTURE.scope_b,
                    relay_target_actor_id=FIXTURE.worker_b_actor,
                ),
                f"k-{uuid.uuid4()}",
            ),
            read_aliases=[FIXTURE.alias_a, FIXTURE.alias_b],
        )
    )
    with pytest.raises(ApiProblem) as pending:
        asyncio.run(
            as_owner(
                lambda conn, ctx: relay_uc.authorize_relay(
                    conn, ctx,
                    AuthorizeRelayRequest(
                        approval_id=uuid.UUID(pending_receipt["aggregate_id"]),
                        source_handoff_id=handoff_id,
                        target_scope_id=FIXTURE.scope_b,
                        target_actor_id=FIXTURE.worker_b_actor,
                    ),
                    f"k-{uuid.uuid4()}",
                )
            )
        )
    assert _code(pending) == "approval_not_approved"


def test_relay_flow_preserves_immutable_ids_across_renames() -> None:
    _s, created, _ = asyncio.run(
        _create_handoff(FIXTURE.owner, _create_request(title="relay me"))
    )
    handoff_id = uuid.UUID(created["handoff_id"])
    _s, approval_receipt, _ = asyncio.run(
        as_owner(
            lambda conn, ctx: approval_uc.request_approval(
                conn, ctx,
                RequestApprovalRequest(
                    gate_kind="relay",
                    relay_source_handoff_id=handoff_id,
                    relay_target_scope_id=FIXTURE.scope_b,
                    relay_target_actor_id=FIXTURE.worker_b_actor,
                ),
                f"k-{uuid.uuid4()}",
            ),
            read_aliases=[FIXTURE.alias_a, FIXTURE.alias_b],
        )
    )
    approval_id = uuid.UUID(approval_receipt["aggregate_id"])
    asyncio.run(
        as_reviewer(
            lambda conn, ctx: approval_uc.decide_approval(
                conn, ctx, approval_id,
                DecideApprovalRequest(decision="approved"),
                f"k-{uuid.uuid4()}",
            )
        )
    )
    _s, authorized, _ = asyncio.run(
        as_owner(
            lambda conn, ctx: relay_uc.authorize_relay(
                conn, ctx,
                AuthorizeRelayRequest(
                    approval_id=approval_id,
                    source_handoff_id=handoff_id,
                    target_scope_id=FIXTURE.scope_b,
                    target_actor_id=FIXTURE.worker_b_actor,
                ),
                f"k-{uuid.uuid4()}",
            )
        )
    )
    relay_id = uuid.UUID(authorized["aggregate_id"])

    # Duplicate authorization for the same immutable target is denied.
    with pytest.raises(ApiProblem) as duplicate:
        asyncio.run(
            as_owner(
                lambda conn, ctx: relay_uc.authorize_relay(
                    conn, ctx,
                    AuthorizeRelayRequest(
                        approval_id=approval_id,
                        source_handoff_id=handoff_id,
                        target_scope_id=FIXTURE.scope_b,
                        target_actor_id=FIXTURE.worker_b_actor,
                    ),
                    f"k-{uuid.uuid4()}",
                )
            )
        )
    assert _code(duplicate) == "duplicate_relay_target"

    # Rename BOTH projects; immutable IDs must not move.
    suffix = uuid.uuid4().hex[:8]
    new_alias_a = f"coord-renamed-a-{suffix}"
    new_alias_b = f"coord-renamed-b-{suffix}"
    _rename_alias(FIXTURE.scope_a, new_alias_a)
    _rename_alias(FIXTURE.scope_b, new_alias_b)

    dispatch_key = f"k-{uuid.uuid4()}"
    status, dispatched, replayed = asyncio.run(
        as_owner(
            lambda conn, ctx: relay_uc.dispatch_relay(
                conn, ctx, relay_id, NoteRequest(), dispatch_key
            ),
            alias=new_alias_a,
        )
    )
    assert (status, replayed) == (200, False)
    assert dispatched["source_scope_id"] == str(FIXTURE.scope_a)
    assert dispatched["source_handoff_id"] == str(handoff_id)
    assert dispatched["target_scope_id"] == str(FIXTURE.scope_b)
    assert dispatched["target_actor_id"] == str(FIXTURE.worker_b_actor)

    # Replay of the dispatch returns the same durable receipt.
    _s2, replay_receipt, replayed2 = asyncio.run(
        as_owner(
            lambda conn, ctx: relay_uc.dispatch_relay(
                conn, ctx, relay_id, NoteRequest(), dispatch_key
            ),
            alias=new_alias_a,
        )
    )
    assert replayed2 is True
    assert replay_receipt == dispatched

    # A second dispatch under a new key is refused: consumed.
    with pytest.raises(ApiProblem) as consumed:
        asyncio.run(
            as_owner(
                lambda conn, ctx: relay_uc.dispatch_relay(
                    conn, ctx, relay_id, NoteRequest(), f"k-{uuid.uuid4()}"
                ),
                alias=new_alias_a,
            )
        )
    assert _code(consumed) == "relay_not_authorized"

    # Materialize in the renamed target scope by the target worker.
    status, materialized, _ = asyncio.run(
        _command(
            FIXTURE.worker_b,
            new_alias_b,
            write=True,
            work=lambda conn, ctx: relay_uc.materialize_relay(
                conn, ctx, relay_id, NoteRequest(), f"k-{uuid.uuid4()}"
            ),
            read_aliases=[new_alias_a, new_alias_b],
        )
    )
    assert status == 201
    target_handoff_id = uuid.UUID(materialized["handoff_id"])
    assert materialized["source_scope_id"] == str(FIXTURE.scope_a)
    assert materialized["source_handoff_id"] == str(handoff_id)

    # Duplicate materialization is refused.
    with pytest.raises(ApiProblem) as again:
        asyncio.run(
            _command(
                FIXTURE.worker_b,
                new_alias_b,
                write=True,
                work=lambda conn, ctx: relay_uc.materialize_relay(
                    conn, ctx, relay_id, NoteRequest(), f"k-{uuid.uuid4()}"
                ),
                read_aliases=[new_alias_a, new_alias_b],
            )
        )
    assert _code(again) == "relay_already_materialized"

    # The target handoff carries the relay provenance and is claimable.
    view = asyncio.run(
        _command(
            FIXTURE.worker_b,
            new_alias_b,
            write=False,
            work=lambda conn, ctx: handoff_uc.get_handoff(
                conn, ctx, target_handoff_id
            ),
        )
    )
    assert view["relay"]["relay_id"] == str(relay_id)
    assert view["relay"]["source_scope_id"] == str(FIXTURE.scope_a)
    assert view["relay"]["source_handoff_id"] == str(handoff_id)
    assert view["title"] == "relay me"
    assert view["addressed_actor_id"] == str(FIXTURE.worker_b_actor)

    # The durable receipt stayed readable in the source scope.
    relay_view = asyncio.run(
        as_owner(
            lambda conn, ctx: relay_uc.get_relay(conn, ctx, relay_id),
            alias=new_alias_a,
            write=False,
        )
    )
    assert relay_view["status"] == "consumed"
    assert relay_view["dispatch_receipt"]["target_scope_id"] == str(
        FIXTURE.scope_b
    )


def test_relay_revocation_blocks_dispatch() -> None:
    _s, created, _ = asyncio.run(_create_handoff(FIXTURE.owner))
    handoff_id = uuid.UUID(created["handoff_id"])
    _s, approval_receipt, _ = asyncio.run(
        as_owner(
            lambda conn, ctx: approval_uc.request_approval(
                conn, ctx,
                RequestApprovalRequest(
                    gate_kind="relay",
                    relay_source_handoff_id=handoff_id,
                    relay_target_scope_id=FIXTURE.scope_b,
                    relay_target_actor_id=FIXTURE.worker_b_actor,
                ),
                f"k-{uuid.uuid4()}",
            ),
            read_aliases=[FIXTURE.alias_a, FIXTURE.alias_b],
        )
    )
    approval_id = uuid.UUID(approval_receipt["aggregate_id"])
    asyncio.run(
        as_reviewer(
            lambda conn, ctx: approval_uc.decide_approval(
                conn, ctx, approval_id,
                DecideApprovalRequest(decision="approved"),
                f"k-{uuid.uuid4()}",
            )
        )
    )
    _s, authorized, _ = asyncio.run(
        as_owner(
            lambda conn, ctx: relay_uc.authorize_relay(
                conn, ctx,
                AuthorizeRelayRequest(
                    approval_id=approval_id,
                    source_handoff_id=handoff_id,
                    target_scope_id=FIXTURE.scope_b,
                    target_actor_id=FIXTURE.worker_b_actor,
                ),
                f"k-{uuid.uuid4()}",
            )
        )
    )
    relay_id = uuid.UUID(authorized["aggregate_id"])

    asyncio.run(
        as_reviewer(
            lambda conn, ctx: approval_uc.revoke_approval(
                conn, ctx, approval_id, NoteRequest(note="halt"),
                f"k-{uuid.uuid4()}",
            )
        )
    )
    with pytest.raises(ApiProblem) as blocked:
        asyncio.run(
            as_owner(
                lambda conn, ctx: relay_uc.dispatch_relay(
                    conn, ctx, relay_id, NoteRequest(), f"k-{uuid.uuid4()}"
                )
            )
        )
    assert _code(blocked) == "approval_not_approved"


# ---------------------------------------------------------------------------
# R09: epics/waves/boards/tasks and deterministic dispatch eligibility.
# ---------------------------------------------------------------------------


def test_wave_gates_dependencies_and_responsibility() -> None:
    _s, epic, _ = asyncio.run(
        as_owner(
            lambda conn, ctx: planning_uc.create_epic(
                conn, ctx, CreateEpicRequest(name="epic-1"), f"k-{uuid.uuid4()}"
            )
        )
    )
    epic_id = uuid.UUID(epic["aggregate_id"])
    _s, wave1, _ = asyncio.run(
        as_owner(
            lambda conn, ctx: planning_uc.create_wave(
                conn, ctx,
                CreateWaveRequest(epic_id=epic_id, wave_index=1),
                f"k-{uuid.uuid4()}",
            )
        )
    )
    wave1_id = uuid.UUID(wave1["aggregate_id"])
    _s, wave2, _ = asyncio.run(
        as_owner(
            lambda conn, ctx: planning_uc.create_wave(
                conn, ctx,
                CreateWaveRequest(epic_id=epic_id, wave_index=2,
                                  requires_human_gate=True),
                f"k-{uuid.uuid4()}",
            )
        )
    )
    wave2_id = uuid.UUID(wave2["aggregate_id"])

    async def create_task(title: str, wave_id: uuid.UUID,
                          depends_on: list[uuid.UUID] | None = None):
        _s, receipt, _ = await as_owner(
            lambda conn, ctx: planning_uc.create_task(
                conn, ctx,
                CreateTaskRequest(title=title, epic_id=epic_id,
                                  wave_id=wave_id,
                                  depends_on=depends_on or []),
                f"k-{uuid.uuid4()}",
            )
        )
        return uuid.UUID(receipt["aggregate_id"])

    t1 = asyncio.run(create_task("t1", wave1_id))
    t2 = asyncio.run(create_task("t2", wave1_id, [t1]))
    t3 = asyncio.run(create_task("t3", wave2_id))

    # Out-of-order wave work remains ineligible.
    eligibility = asyncio.run(
        as_owner(
            lambda conn, ctx: planning_uc.task_dispatch_eligibility(
                conn, ctx, t1
            ),
            write=False,
        )
    )
    assert eligibility["eligible"] is False
    assert "wave_not_open" in eligibility["reasons"]

    with pytest.raises(ApiProblem) as gated:
        asyncio.run(
            as_owner(
                lambda conn, ctx: planning_uc.open_wave(
                    conn, ctx, wave2_id, OpenWaveRequest(), f"k-{uuid.uuid4()}"
                )
            )
        )
    assert _code(gated) in ("prior_wave_incomplete", "gate_approval_required")

    asyncio.run(
        as_owner(
            lambda conn, ctx: planning_uc.open_wave(
                conn, ctx, wave1_id, OpenWaveRequest(), f"k-{uuid.uuid4()}"
            )
        )
    )

    # Zero responsible owners fails explicitly.
    eligibility = asyncio.run(
        as_owner(
            lambda conn, ctx: planning_uc.task_dispatch_eligibility(
                conn, ctx, t1
            ),
            write=False,
        )
    )
    assert eligibility["eligible"] is False
    assert "no_responsible_owner" in eligibility["reasons"]

    # Dependencies must be terminal.
    asyncio.run(
        as_owner(
            lambda conn, ctx: planning_uc.assign_task(
                conn, ctx, t2,
                AssignTaskRequest(actor_id=FIXTURE.worker_a_actor),
                f"k-{uuid.uuid4()}",
            )
        )
    )
    eligibility = asyncio.run(
        as_owner(
            lambda conn, ctx: planning_uc.task_dispatch_eligibility(
                conn, ctx, t2
            ),
            write=False,
        )
    )
    assert "dependency_not_terminal" in eligibility["reasons"]
    with pytest.raises(ApiProblem) as blocked:
        asyncio.run(
            as_owner(
                lambda conn, ctx: planning_uc.dispatch_task(
                    conn, ctx, t2, DispatchTaskRequest(), f"k-{uuid.uuid4()}"
                )
            )
        )
    assert _code(blocked) == "dispatch_not_eligible"
    assert "dependency_not_terminal" in blocked.value.message

    # Multiple responsible owners fail explicitly.
    asyncio.run(
        as_owner(
            lambda conn, ctx: planning_uc.assign_task(
                conn, ctx, t1,
                AssignTaskRequest(actor_id=FIXTURE.worker_a_actor),
                f"k-{uuid.uuid4()}",
            )
        )
    )
    asyncio.run(
        as_owner(
            lambda conn, ctx: planning_uc.assign_task(
                conn, ctx, t1,
                AssignTaskRequest(actor_id=FIXTURE.worker_b_actor),
                f"k-{uuid.uuid4()}",
            )
        )
    )
    eligibility = asyncio.run(
        as_owner(
            lambda conn, ctx: planning_uc.task_dispatch_eligibility(
                conn, ctx, t1
            ),
            write=False,
        )
    )
    assert eligibility["reasons"] == ["ambiguous_responsibility"]

    # Dispatch t1 once responsibility is unambiguous is impossible here
    # (both assignments stay active); use t3's wave flow for the happy path.
    asyncio.run(
        as_owner(
            lambda conn, ctx: planning_uc.cancel_task(
                conn, ctx, t1, CancelTaskRequest(reason="replan"),
                f"k-{uuid.uuid4()}",
            )
        )
    )
    # t2 dependency is now terminal (cancelled) -> eligible once wave open.
    eligibility = asyncio.run(
        as_owner(
            lambda conn, ctx: planning_uc.task_dispatch_eligibility(
                conn, ctx, t2
            ),
            write=False,
        )
    )
    assert eligibility["eligible"] is True
    _s, dispatched, _ = asyncio.run(
        as_owner(
            lambda conn, ctx: planning_uc.dispatch_task(
                conn, ctx, t2, DispatchTaskRequest(note="go"),
                f"k-{uuid.uuid4()}",
            )
        )
    )
    assert dispatched["status"] == "dispatched"
    with pytest.raises(ApiProblem) as twice:
        asyncio.run(
            as_owner(
                lambda conn, ctx: planning_uc.dispatch_task(
                    conn, ctx, t2, DispatchTaskRequest(), f"k-{uuid.uuid4()}"
                )
            )
        )
    assert "task_not_open" in twice.value.message

    # Wave 1 completes only when all tasks are terminal.
    asyncio.run(
        as_owner(
            lambda conn, ctx: planning_uc.complete_task(
                conn, ctx, t2, CompleteTaskRequest(note="done"),
                f"k-{uuid.uuid4()}",
            )
        )
    )
    asyncio.run(
        as_owner(
            lambda conn, ctx: planning_uc.complete_wave(
                conn, ctx, wave1_id, NoteRequest(), f"k-{uuid.uuid4()}"
            )
        )
    )

    # Human-gated wave 2 opens only with an approved, live gate.
    with pytest.raises(ApiProblem) as needs_gate:
        asyncio.run(
            as_owner(
                lambda conn, ctx: planning_uc.open_wave(
                    conn, ctx, wave2_id, OpenWaveRequest(),
                    f"k-{uuid.uuid4()}",
                )
            )
        )
    assert _code(needs_gate) == "gate_approval_required"

    _s, gate_receipt, _ = asyncio.run(
        as_owner(
            lambda conn, ctx: approval_uc.request_approval(
                conn, ctx,
                RequestApprovalRequest(gate_kind="wave",
                                       subject_wave_id=wave2_id),
                f"k-{uuid.uuid4()}",
            )
        )
    )
    gate_id = uuid.UUID(gate_receipt["aggregate_id"])
    asyncio.run(
        as_reviewer(
            lambda conn, ctx: approval_uc.decide_approval(
                conn, ctx, gate_id,
                DecideApprovalRequest(decision="approved"),
                f"k-{uuid.uuid4()}",
            )
        )
    )
    _s, opened, _ = asyncio.run(
        as_owner(
            lambda conn, ctx: planning_uc.open_wave(
                conn, ctx, wave2_id,
                OpenWaveRequest(gate_approval_id=gate_id),
                f"k-{uuid.uuid4()}",
            )
        )
    )
    assert opened["status"] == "open"

    asyncio.run(
        as_owner(
            lambda conn, ctx: planning_uc.assign_task(
                conn, ctx, t3,
                AssignTaskRequest(actor_id=FIXTURE.worker_b_actor),
                f"k-{uuid.uuid4()}",
            )
        )
    )
    eligibility = asyncio.run(
        as_owner(
            lambda conn, ctx: planning_uc.task_dispatch_eligibility(
                conn, ctx, t3
            ),
            write=False,
        )
    )
    assert eligibility["eligible"] is True


def test_boards_organize_tasks() -> None:
    _s, board, _ = asyncio.run(
        as_owner(
            lambda conn, ctx: planning_uc.create_board(
                conn, ctx, CreateBoardRequest(name="board-1"),
                f"k-{uuid.uuid4()}",
            )
        )
    )
    board_id = uuid.UUID(board["aggregate_id"])
    _s, task, _ = asyncio.run(
        as_owner(
            lambda conn, ctx: planning_uc.create_task(
                conn, ctx, CreateTaskRequest(title="boarded"),
                f"k-{uuid.uuid4()}",
            )
        )
    )
    task_id = uuid.UUID(task["aggregate_id"])
    asyncio.run(
        as_owner(
            lambda conn, ctx: planning_uc.add_board_task(
                conn, ctx, board_id,
                AddBoardTaskRequest(task_id=task_id, column="todo"),
                f"k-{uuid.uuid4()}",
            )
        )
    )
    with pytest.raises(ApiProblem) as duplicate:
        asyncio.run(
            as_owner(
                lambda conn, ctx: planning_uc.add_board_task(
                    conn, ctx, board_id,
                    AddBoardTaskRequest(task_id=task_id),
                    f"k-{uuid.uuid4()}",
                )
            )
        )
    assert _code(duplicate) == "duplicate_board_task"
    view = asyncio.run(
        as_owner(
            lambda conn, ctx: planning_uc.get_task(conn, ctx, task_id),
            write=False,
        )
    )
    assert view["boards"] == [{"board_id": str(board_id), "column": "todo"}]


def test_writer_policy_gates_coordination_writes() -> None:
    # Scope C only: enact an owner-only writer policy, then a member policy.
    async def enact(roles: list[str], expected: int) -> None:
        await as_owner(
            lambda conn, _ctx: conn.fetchval(
                "SELECT cortex_auth.enact_writer_policy($1, $2, $3::text[], $4)",
                FIXTURE.owner,
                FIXTURE.scope_c,
                roles,
                expected,
            )
        )

    asyncio.run(enact(["owner"], 0))
    with pytest.raises(ApiProblem) as denied:
        asyncio.run(
            _command(
                FIXTURE.worker_a,
                FIXTURE.alias_c,
                write=True,
                work=lambda conn, ctx: handoff_uc.create_handoff(
                    conn, ctx, _create_request(), f"k-{uuid.uuid4()}"
                ),
            )
        )
    assert _code(denied) == "writer_policy_denied"

    asyncio.run(enact(["owner", "member"], 1))
    status, _receipt, _ = asyncio.run(
        _command(
            FIXTURE.worker_a,
            FIXTURE.alias_c,
            write=True,
            work=lambda conn, ctx: handoff_uc.create_handoff(
                conn, ctx, _create_request(), f"k-{uuid.uuid4()}"
            ),
        )
    )
    assert status == 201


# ---------------------------------------------------------------------------
# R10: work-product receipts, freshness and honest attestation.
# ---------------------------------------------------------------------------


def test_work_product_receipts_track_freshness() -> None:
    # Create canonical content through the W1 memory use cases.
    _s, content_receipt, _ = asyncio.run(
        as_worker_a(
            lambda conn, ctx: content_uc.create_content(
                conn, ctx,
                CreateContentRequest(
                    content_class="work_product",
                    payload={
                        "kind": "report",
                        "summary": "results",
                        "references": [],
                    },
                    body="initial work product",
                ),
                f"k-{uuid.uuid4()}",
            )
        )
    )
    content_id = uuid.UUID(content_receipt["content_id"])

    _s, created, _ = asyncio.run(_create_handoff(FIXTURE.worker_a))
    handoff_id = uuid.UUID(created["handoff_id"])
    asyncio.run(
        as_worker_a(
            lambda conn, ctx: handoff_uc.claim_handoff(
                conn, ctx, handoff_id, ClaimHandoffRequest(), f"k-{uuid.uuid4()}"
            )
        )
    )
    _s, returned, _ = asyncio.run(
        as_worker_a(
            lambda conn, ctx: handoff_uc.return_handoff(
                conn, ctx, handoff_id,
                ReturnHandoffRequest(
                    expected_claim_generation=1,
                    summary="with work product",
                    work_products=[
                        WorkProductReference(content_id=content_id)
                    ],
                ),
                f"k-{uuid.uuid4()}",
            )
        )
    )
    receipt_id = uuid.UUID(
        returned["work_product_receipts"][0]["receipt_id"]
    )

    view = asyncio.run(
        as_worker_a(
            lambda conn, ctx: work_product_uc.get_work_product_receipt(
                conn, ctx, receipt_id
            ),
            write=False,
        )
    )
    assert view["freshness"] == "fresh"
    assert view["attestation"] == "self_reported"
    assert view["pinned"]["revision"] == 1

    # A revision supersedes the pinned bytes -> stale.
    asyncio.run(
        as_worker_a(
            lambda conn, ctx: content_uc.revise_content(
                conn, ctx, content_id,
                ReviseContentRequest(
                    payload={
                        "kind": "report",
                        "summary": "results v2",
                        "references": [],
                    },
                    body="revised work product",
                    expected_revision=1,
                ),
                f"k-{uuid.uuid4()}",
            )
        )
    )
    view = asyncio.run(
        as_worker_a(
            lambda conn, ctx: work_product_uc.get_work_product_receipt(
                conn, ctx, receipt_id
            ),
            write=False,
        )
    )
    assert view["freshness"] == "stale"
    assert view["current"]["revision"] == 2

    # Invalidated source content -> source_unavailable.
    asyncio.run(
        as_worker_a(
            lambda conn, ctx: content_uc.change_status(
                conn, ctx, content_id, "invalidate", "bad data", None,
                f"k-{uuid.uuid4()}",
            )
        )
    )
    view = asyncio.run(
        as_worker_a(
            lambda conn, ctx: work_product_uc.get_work_product_receipt(
                conn, ctx, receipt_id
            ),
            write=False,
        )
    )
    assert view["freshness"] == "source_unavailable"


def test_return_rejects_non_current_or_foreign_content() -> None:
    _s, created, _ = asyncio.run(_create_handoff(FIXTURE.worker_a))
    handoff_id = uuid.UUID(created["handoff_id"])
    asyncio.run(
        as_worker_a(
            lambda conn, ctx: handoff_uc.claim_handoff(
                conn, ctx, handoff_id, ClaimHandoffRequest(), f"k-{uuid.uuid4()}"
            )
        )
    )
    with pytest.raises(ApiProblem) as missing:
        asyncio.run(
            as_worker_a(
                lambda conn, ctx: handoff_uc.return_handoff(
                    conn, ctx, handoff_id,
                    ReturnHandoffRequest(
                        expected_claim_generation=1,
                        summary="ghost product",
                        work_products=[
                            WorkProductReference(content_id=uuid.uuid4())
                        ],
                    ),
                    f"k-{uuid.uuid4()}",
                )
            )
        )
    assert _code(missing) == "content_not_found"


# ---------------------------------------------------------------------------
# Isolation, RLS and transactional atomicity (F03/F04, N01/N02).
# ---------------------------------------------------------------------------


def test_rls_fails_closed_without_context() -> None:
    async def naked_count() -> int:
        connection = await asyncpg.connect(DATABASE_URL)
        try:
            return await connection.fetchval(
                "SELECT count(*) FROM cortex_coord.handoffs"
            )
        finally:
            await connection.close()

    assert asyncio.run(naked_count()) == 0


def test_scope_selection_denies_ungranted_scope() -> None:
    with pytest.raises(ApiProblem) as denied:
        asyncio.run(
            _command(
                FIXTURE.worker_a,
                FIXTURE.alias_b,
                write=False,
                work=lambda conn, ctx: handoff_uc.list_handoffs(conn, ctx, {}),
            )
        )
    assert _code(denied) == "scope_not_found"


def test_every_command_commits_audit_outbox_and_receipt() -> None:
    _s, created, _ = asyncio.run(
        _create_handoff(FIXTURE.worker_a, _create_request(title="atomicity"))
    )
    handoff_id = uuid.UUID(created["handoff_id"])
    key = f"k-{uuid.uuid4()}"
    asyncio.run(
        as_worker_a(
            lambda conn, ctx: handoff_uc.claim_handoff(
                conn, ctx, handoff_id, ClaimHandoffRequest(), key
            )
        )
    )

    async def counts() -> tuple[int, int, int]:
        connection = await asyncpg.connect(MIGRATOR_URL)
        try:
            audit = await connection.fetchval(
                "SELECT count(*) FROM cortex_coord.audit_log WHERE scope_id = $1"
                " AND aggregate_kind = 'handoff' AND aggregate_id = $2",
                FIXTURE.scope_a,
                handoff_id,
            )
            outbox = await connection.fetchval(
                "SELECT count(*) FROM cortex_coord.outbox_events"
                " WHERE scope_id = $1 AND aggregate_id = $2",
                FIXTURE.scope_a,
                handoff_id,
            )
            receipt = await connection.fetchval(
                "SELECT count(*) FROM cortex_core.command_receipts"
                " WHERE operation = 'coordination.handoff.claim'"
                " AND idempotency_key = $1",
                key,
            )
            return audit, outbox, receipt
        finally:
            await connection.close()

    audit, outbox, receipt = asyncio.run(counts())
    assert audit == 2  # created + claimed
    assert outbox == 2
    assert receipt == 1


def test_outbox_payloads_carry_aggregate_versions() -> None:
    _s, created, _ = asyncio.run(_create_handoff(FIXTURE.worker_a))
    handoff_id = uuid.UUID(created["handoff_id"])
    asyncio.run(
        as_worker_a(
            lambda conn, ctx: handoff_uc.claim_handoff(
                conn, ctx, handoff_id, ClaimHandoffRequest(), f"k-{uuid.uuid4()}"
            )
        )
    )

    async def payloads() -> list[Any]:
        connection = await asyncpg.connect(MIGRATOR_URL)
        try:
            return await connection.fetch(
                "SELECT event_type, payload FROM cortex_coord.outbox_events"
                " WHERE scope_id = $1 AND aggregate_id = $2 ORDER BY created_at",
                FIXTURE.scope_a,
                handoff_id,
            )
        finally:
            await connection.close()

    rows = asyncio.run(payloads())
    assert [row["event_type"] for row in rows] == [
        "coordination.handoff.created",
        "coordination.handoff.claimed",
    ]
    versions = [
        json.loads(row["payload"])["aggregate_version"] for row in rows
    ]
    assert versions == [1, 2]


def test_list_and_keyset_pagination() -> None:
    for index in range(3):
        asyncio.run(
            _create_handoff(
                FIXTURE.worker_a, _create_request(title=f"page-{index}")
            )
        )
    page1 = asyncio.run(
        as_worker_a(
            lambda conn, ctx: handoff_uc.list_handoffs(
                conn, ctx, {"limit": 2}
            ),
            write=False,
        )
    )
    assert len(page1["items"]) == 2
    assert page1["next_cursor"] is not None
    page2 = asyncio.run(
        as_worker_a(
            lambda conn, ctx: handoff_uc.list_handoffs(
                conn, ctx,
                {"limit": 2, "cursor": page1["next_cursor"]},
            ),
            write=False,
        )
    )
    first_ids = {item["handoff_id"] for item in page1["items"]}
    second_ids = {item["handoff_id"] for item in page2["items"]}
    assert first_ids.isdisjoint(second_ids)
