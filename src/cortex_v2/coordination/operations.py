"""Versioned coordination operation registry (integration contract).

``OPERATIONS`` is the single list an integrator mounts generically onto a
transport (F05/F13): every transport calls the same application use case,
the same strict request model and the same typed receipt. Handler
signatures:

* ``scoped_write``: ``async (connection, context, idempotency_key, payload,
  path_params) -> tuple[int, dict, bool]`` (status, data, replayed)
* ``scoped_read``: ``async (connection, context, payload, path_params) ->
  dict``

Nothing in this package imports FastAPI, MCP or CLI code.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import asyncpg

from ..store import ScopeContext
from . import approvals, handoffs, planning, relays, work_products
from .models import (
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
)
from .repository import path_uuid, problem


def _read_filters(payload: Any) -> dict[str, Any]:
    if payload is None:
        return {}
    if isinstance(payload, Mapping):
        return dict(payload)
    raise problem("invalid_filter")


async def _handoff_create(
    connection: asyncpg.Connection,
    context: ScopeContext,
    idempotency_key: str,
    payload: CreateHandoffRequest,
    path_params: dict[str, Any],
) -> tuple[int, dict[str, Any], bool]:
    return await handoffs.create_handoff(
        connection, context, payload, idempotency_key
    )


async def _handoff_list(
    connection: asyncpg.Connection,
    context: ScopeContext,
    payload: Any,
    path_params: dict[str, Any],
) -> dict[str, Any]:
    return await handoffs.list_handoffs(
        connection, context, _read_filters(payload)
    )


async def _handoff_get(
    connection: asyncpg.Connection,
    context: ScopeContext,
    payload: Any,
    path_params: dict[str, Any],
) -> dict[str, Any]:
    return await handoffs.get_handoff(
        connection, context, path_uuid(path_params, "handoff_id")
    )


async def _handoff_claim(
    connection: asyncpg.Connection,
    context: ScopeContext,
    idempotency_key: str,
    payload: ClaimHandoffRequest,
    path_params: dict[str, Any],
) -> tuple[int, dict[str, Any], bool]:
    return await handoffs.claim_handoff(
        connection,
        context,
        path_uuid(path_params, "handoff_id"),
        payload,
        idempotency_key,
    )


async def _handoff_renew_lease(
    connection: asyncpg.Connection,
    context: ScopeContext,
    idempotency_key: str,
    payload: RenewLeaseRequest,
    path_params: dict[str, Any],
) -> tuple[int, dict[str, Any], bool]:
    return await handoffs.renew_lease(
        connection,
        context,
        path_uuid(path_params, "handoff_id"),
        payload,
        idempotency_key,
    )


async def _handoff_release(
    connection: asyncpg.Connection,
    context: ScopeContext,
    idempotency_key: str,
    payload: ReleaseHandoffRequest,
    path_params: dict[str, Any],
) -> tuple[int, dict[str, Any], bool]:
    return await handoffs.release_handoff(
        connection,
        context,
        path_uuid(path_params, "handoff_id"),
        payload,
        idempotency_key,
    )


async def _handoff_return(
    connection: asyncpg.Connection,
    context: ScopeContext,
    idempotency_key: str,
    payload: ReturnHandoffRequest,
    path_params: dict[str, Any],
) -> tuple[int, dict[str, Any], bool]:
    return await handoffs.return_handoff(
        connection,
        context,
        path_uuid(path_params, "handoff_id"),
        payload,
        idempotency_key,
    )


async def _handoff_accept(
    connection: asyncpg.Connection,
    context: ScopeContext,
    idempotency_key: str,
    payload: AcceptHandoffRequest,
    path_params: dict[str, Any],
) -> tuple[int, dict[str, Any], bool]:
    return await handoffs.accept_handoff(
        connection,
        context,
        path_uuid(path_params, "handoff_id"),
        payload,
        idempotency_key,
    )


async def _handoff_rework(
    connection: asyncpg.Connection,
    context: ScopeContext,
    idempotency_key: str,
    payload: ReworkHandoffRequest,
    path_params: dict[str, Any],
) -> tuple[int, dict[str, Any], bool]:
    return await handoffs.rework_handoff(
        connection,
        context,
        path_uuid(path_params, "handoff_id"),
        payload,
        idempotency_key,
    )


async def _handoff_retry(
    connection: asyncpg.Connection,
    context: ScopeContext,
    idempotency_key: str,
    payload: RetryHandoffRequest,
    path_params: dict[str, Any],
) -> tuple[int, dict[str, Any], bool]:
    return await handoffs.retry_handoff(
        connection,
        context,
        path_uuid(path_params, "handoff_id"),
        payload,
        idempotency_key,
    )


async def _handoff_fail(
    connection: asyncpg.Connection,
    context: ScopeContext,
    idempotency_key: str,
    payload: FailHandoffRequest,
    path_params: dict[str, Any],
) -> tuple[int, dict[str, Any], bool]:
    return await handoffs.fail_handoff(
        connection,
        context,
        path_uuid(path_params, "handoff_id"),
        payload,
        idempotency_key,
    )


async def _handoff_abandon(
    connection: asyncpg.Connection,
    context: ScopeContext,
    idempotency_key: str,
    payload: AbandonHandoffRequest,
    path_params: dict[str, Any],
) -> tuple[int, dict[str, Any], bool]:
    return await handoffs.abandon_handoff(
        connection,
        context,
        path_uuid(path_params, "handoff_id"),
        payload,
        idempotency_key,
    )


async def _handoff_withdraw(
    connection: asyncpg.Connection,
    context: ScopeContext,
    idempotency_key: str,
    payload: WithdrawHandoffRequest,
    path_params: dict[str, Any],
) -> tuple[int, dict[str, Any], bool]:
    return await handoffs.withdraw_handoff(
        connection,
        context,
        path_uuid(path_params, "handoff_id"),
        payload,
        idempotency_key,
    )


async def _approval_request(
    connection: asyncpg.Connection,
    context: ScopeContext,
    idempotency_key: str,
    payload: RequestApprovalRequest,
    path_params: dict[str, Any],
) -> tuple[int, dict[str, Any], bool]:
    return await approvals.request_approval(
        connection, context, payload, idempotency_key
    )


async def _approval_decide(
    connection: asyncpg.Connection,
    context: ScopeContext,
    idempotency_key: str,
    payload: DecideApprovalRequest,
    path_params: dict[str, Any],
) -> tuple[int, dict[str, Any], bool]:
    return await approvals.decide_approval(
        connection,
        context,
        path_uuid(path_params, "approval_id"),
        payload,
        idempotency_key,
    )


async def _approval_revoke(
    connection: asyncpg.Connection,
    context: ScopeContext,
    idempotency_key: str,
    payload: NoteRequest,
    path_params: dict[str, Any],
) -> tuple[int, dict[str, Any], bool]:
    return await approvals.revoke_approval(
        connection,
        context,
        path_uuid(path_params, "approval_id"),
        payload,
        idempotency_key,
    )


async def _approval_get(
    connection: asyncpg.Connection,
    context: ScopeContext,
    payload: Any,
    path_params: dict[str, Any],
) -> dict[str, Any]:
    return await approvals.get_approval(
        connection, context, path_uuid(path_params, "approval_id")
    )


async def _relay_authorize(
    connection: asyncpg.Connection,
    context: ScopeContext,
    idempotency_key: str,
    payload: AuthorizeRelayRequest,
    path_params: dict[str, Any],
) -> tuple[int, dict[str, Any], bool]:
    return await relays.authorize_relay(
        connection, context, payload, idempotency_key
    )


async def _relay_revoke(
    connection: asyncpg.Connection,
    context: ScopeContext,
    idempotency_key: str,
    payload: NoteRequest,
    path_params: dict[str, Any],
) -> tuple[int, dict[str, Any], bool]:
    return await relays.revoke_relay(
        connection, context, path_uuid(path_params, "relay_id"), payload,
        idempotency_key,
    )


async def _relay_dispatch(
    connection: asyncpg.Connection,
    context: ScopeContext,
    idempotency_key: str,
    payload: NoteRequest,
    path_params: dict[str, Any],
) -> tuple[int, dict[str, Any], bool]:
    return await relays.dispatch_relay(
        connection, context, path_uuid(path_params, "relay_id"), payload,
        idempotency_key,
    )


async def _relay_materialize(
    connection: asyncpg.Connection,
    context: ScopeContext,
    idempotency_key: str,
    payload: NoteRequest,
    path_params: dict[str, Any],
) -> tuple[int, dict[str, Any], bool]:
    return await relays.materialize_relay(
        connection, context, path_uuid(path_params, "relay_id"), payload,
        idempotency_key,
    )


async def _relay_get(
    connection: asyncpg.Connection,
    context: ScopeContext,
    payload: Any,
    path_params: dict[str, Any],
) -> dict[str, Any]:
    return await relays.get_relay(
        connection, context, path_uuid(path_params, "relay_id")
    )


async def _epic_create(
    connection: asyncpg.Connection,
    context: ScopeContext,
    idempotency_key: str,
    payload: CreateEpicRequest,
    path_params: dict[str, Any],
) -> tuple[int, dict[str, Any], bool]:
    return await planning.create_epic(
        connection, context, payload, idempotency_key
    )


async def _wave_create(
    connection: asyncpg.Connection,
    context: ScopeContext,
    idempotency_key: str,
    payload: CreateWaveRequest,
    path_params: dict[str, Any],
) -> tuple[int, dict[str, Any], bool]:
    return await planning.create_wave(
        connection, context, payload, idempotency_key
    )


async def _wave_open(
    connection: asyncpg.Connection,
    context: ScopeContext,
    idempotency_key: str,
    payload: OpenWaveRequest,
    path_params: dict[str, Any],
) -> tuple[int, dict[str, Any], bool]:
    return await planning.open_wave(
        connection, context, path_uuid(path_params, "wave_id"), payload,
        idempotency_key,
    )


async def _wave_complete(
    connection: asyncpg.Connection,
    context: ScopeContext,
    idempotency_key: str,
    payload: NoteRequest,
    path_params: dict[str, Any],
) -> tuple[int, dict[str, Any], bool]:
    return await planning.complete_wave(
        connection, context, path_uuid(path_params, "wave_id"), payload,
        idempotency_key,
    )


async def _board_create(
    connection: asyncpg.Connection,
    context: ScopeContext,
    idempotency_key: str,
    payload: CreateBoardRequest,
    path_params: dict[str, Any],
) -> tuple[int, dict[str, Any], bool]:
    return await planning.create_board(
        connection, context, payload, idempotency_key
    )


async def _board_task_add(
    connection: asyncpg.Connection,
    context: ScopeContext,
    idempotency_key: str,
    payload: AddBoardTaskRequest,
    path_params: dict[str, Any],
) -> tuple[int, dict[str, Any], bool]:
    return await planning.add_board_task(
        connection, context, path_uuid(path_params, "board_id"), payload,
        idempotency_key,
    )


async def _task_create(
    connection: asyncpg.Connection,
    context: ScopeContext,
    idempotency_key: str,
    payload: CreateTaskRequest,
    path_params: dict[str, Any],
) -> tuple[int, dict[str, Any], bool]:
    return await planning.create_task(
        connection, context, payload, idempotency_key
    )


async def _task_assign(
    connection: asyncpg.Connection,
    context: ScopeContext,
    idempotency_key: str,
    payload: AssignTaskRequest,
    path_params: dict[str, Any],
) -> tuple[int, dict[str, Any], bool]:
    return await planning.assign_task(
        connection, context, path_uuid(path_params, "task_id"), payload,
        idempotency_key,
    )


async def _task_dispatch(
    connection: asyncpg.Connection,
    context: ScopeContext,
    idempotency_key: str,
    payload: DispatchTaskRequest,
    path_params: dict[str, Any],
) -> tuple[int, dict[str, Any], bool]:
    return await planning.dispatch_task(
        connection, context, path_uuid(path_params, "task_id"), payload,
        idempotency_key,
    )


async def _task_complete(
    connection: asyncpg.Connection,
    context: ScopeContext,
    idempotency_key: str,
    payload: CompleteTaskRequest,
    path_params: dict[str, Any],
) -> tuple[int, dict[str, Any], bool]:
    return await planning.complete_task(
        connection, context, path_uuid(path_params, "task_id"), payload,
        idempotency_key,
    )


async def _task_cancel(
    connection: asyncpg.Connection,
    context: ScopeContext,
    idempotency_key: str,
    payload: CancelTaskRequest,
    path_params: dict[str, Any],
) -> tuple[int, dict[str, Any], bool]:
    return await planning.cancel_task(
        connection, context, path_uuid(path_params, "task_id"), payload,
        idempotency_key,
    )


async def _task_get(
    connection: asyncpg.Connection,
    context: ScopeContext,
    payload: Any,
    path_params: dict[str, Any],
) -> dict[str, Any]:
    return await planning.get_task(
        connection, context, path_uuid(path_params, "task_id")
    )


async def _task_list(
    connection: asyncpg.Connection,
    context: ScopeContext,
    payload: Any,
    path_params: dict[str, Any],
) -> dict[str, Any]:
    return await planning.list_tasks(
        connection, context, _read_filters(payload)
    )


async def _task_dispatch_eligibility(
    connection: asyncpg.Connection,
    context: ScopeContext,
    payload: Any,
    path_params: dict[str, Any],
) -> dict[str, Any]:
    return await planning.task_dispatch_eligibility(
        connection, context, path_uuid(path_params, "task_id")
    )


async def _work_product_receipt_get(
    connection: asyncpg.Connection,
    context: ScopeContext,
    payload: Any,
    path_params: dict[str, Any],
) -> dict[str, Any]:
    return await work_products.get_work_product_receipt(
        connection, context, path_uuid(path_params, "receipt_id")
    )


def _write(
    operation_id: str,
    path: str,
    request_model: type,
    handler: Any,
    summary: str,
    usage: str,
) -> dict[str, Any]:
    return {
        "operation_id": operation_id,
        "method": "POST",
        "path": path,
        "kind": "scoped_write",
        "request_model": request_model,
        "handler": handler,
        "summary": summary,
        "usage": usage,
    }


def _read(
    operation_id: str,
    path: str,
    handler: Any,
    summary: str,
    usage: str,
) -> dict[str, Any]:
    return {
        "operation_id": operation_id,
        "method": "GET",
        "path": path,
        "kind": "scoped_read",
        "request_model": None,
        "handler": handler,
        "summary": summary,
        "usage": usage,
    }


HANDOFFS = "/v1/coordination/handoffs"

OPERATIONS: list[dict[str, Any]] = [
    _write(
        "coordination.handoff.create",
        HANDOFFS,
        CreateHandoffRequest,
        _handoff_create,
        "Create a handoff: durable, addressed unit of coordination work.",
        "Use when work must move to another role, agent or project with a "
        "claim/return lifecycle; do not use for plain notes or decisions "
        "(record content instead) or to start processes directly.",
    ),
    _read(
        "coordination.handoff.list",
        HANDOFFS,
        _handoff_list,
        "List handoffs visible in the resolved scopes with keyset paging.",
        "Use when discovering claimable or in-flight work (filter by status); "
        "do not use as an event feed - poll the scoped feed for changes.",
    ),
    _read(
        "coordination.handoff.get",
        HANDOFFS + "/{handoff_id}",
        _handoff_get,
        "Read one handoff with claims, returns, reviews, receipts and audit.",
        "Use when resuming or reviewing a specific handoff; do not use to "
        "mutate state - every transition is its own explicit command.",
    ),
    _write(
        "coordination.handoff.claim",
        HANDOFFS + "/{handoff_id}:claim",
        ClaimHandoffRequest,
        _handoff_claim,
        "Atomically claim an open handoff with a lease and new generation.",
        "Use when starting work now and holding a lease; do not poll-claim in "
        "a tight loop, and an expired lease must be reclaimed, not extended.",
    ),
    _write(
        "coordination.handoff.lease.renew",
        HANDOFFS + "/{handoff_id}:renew-lease",
        RenewLeaseRequest,
        _handoff_renew_lease,
        "Extend the lease of the active claim, fenced by claim generation.",
        "Use when work continues past the lease horizon; do not use after "
        "expiry - the lease is gone and the handoff must be reclaimed.",
    ),
    _write(
        "coordination.handoff.release",
        HANDOFFS + "/{handoff_id}:release",
        ReleaseHandoffRequest,
        _handoff_release,
        "Release the active claim without returning work; handoff reopens.",
        "Use when abandoning execution but not the work itself; do not use to "
        "deliver results - return the handoff with evidence instead.",
    ),
    _write(
        "coordination.handoff.return",
        HANDOFFS + "/{handoff_id}:return",
        ReturnHandoffRequest,
        _handoff_return,
        "Return claimed work with a summary and work-product references.",
        "Use when work is done and evidence exists; do not use with a stale "
        "claim generation - it is fenced - and a return is an attestation, "
        "not proof of correctness.",
    ),
    _write(
        "coordination.handoff.accept",
        HANDOFFS + "/{handoff_id}:accept",
        AcceptHandoffRequest,
        _handoff_accept,
        "Accept a returned handoff, consuming a human-gate approval if set.",
        "Use when the returned work meets the acceptance rules; do not use on "
        "human-gated handoffs without the referenced approved gate.",
    ),
    _write(
        "coordination.handoff.rework",
        HANDOFFS + "/{handoff_id}:rework",
        ReworkHandoffRequest,
        _handoff_rework,
        "Send a returned handoff back for rework with explicit instructions.",
        "Use when the return does not meet acceptance rules; do not use to "
        "silently drop work - withdraw or cancel explicitly instead.",
    ),
    _write(
        "coordination.handoff.retry",
        HANDOFFS + "/{handoff_id}:retry",
        RetryHandoffRequest,
        _handoff_retry,
        "Reopen a failed handoff for a new claim cycle.",
        "Use when the failure cause is addressed and retry is worthwhile; do "
        "not use on abandoned or withdrawn handoffs - they are terminal.",
    ),
    _write(
        "coordination.handoff.fail",
        HANDOFFS + "/{handoff_id}:fail",
        FailHandoffRequest,
        _handoff_fail,
        "Record permanent failure of the active claim, fenced by generation.",
        "Use when work cannot complete and retry may follow later; do not use "
        "for transient problems - release or let the lease expire instead.",
    ),
    _write(
        "coordination.handoff.abandon",
        HANDOFFS + "/{handoff_id}:abandon",
        AbandonHandoffRequest,
        _handoff_abandon,
        "Abandon the active claim terminally without a work product.",
        "Use when the claimant gives up and no return will follow; do not use "
        "when another worker should continue - release reopens instead.",
    ),
    _write(
        "coordination.handoff.withdraw",
        HANDOFFS + "/{handoff_id}:withdraw",
        WithdrawHandoffRequest,
        _handoff_withdraw,
        "Withdraw a handoff permanently; creator or human owner/lead only.",
        "Use when the work itself is no longer wanted; do not use as a "
        "shortcut around rework - withdrawal is terminal.",
    ),
    _write(
        "coordination.approval.request",
        "/v1/coordination/approvals",
        RequestApprovalRequest,
        _approval_request,
        "Request a pending approval for a relay, wave or accept human gate.",
        "Use when a human gate must decide before work proceeds; do not use "
        "as a general comment thread - it binds one immutable subject.",
    ),
    _write(
        "coordination.approval.decide",
        "/v1/coordination/approvals/{approval_id}:decide",
        DecideApprovalRequest,
        _approval_decide,
        "Approve or deny a pending gate; human owner/lead authority only.",
        "Use when acting as the human gate for the exact recorded subject; do "
        "not use from agent principals - decisions require human authority.",
    ),
    _write(
        "coordination.approval.revoke",
        "/v1/coordination/approvals/{approval_id}:revoke",
        NoteRequest,
        _approval_revoke,
        "Revoke an approved gate before consumers execute against it.",
        "Use when permission must be withdrawn before consumption; do not use "
        "to rewrite history - decided denials stay as decided.",
    ),
    _read(
        "coordination.approval.get",
        "/v1/coordination/approvals/{approval_id}",
        _approval_get,
        "Read one approval with its immutable subjects and decision audit.",
        "Use when checking gate state before acting; do not infer approval "
        "from silence - only an approved, unexpired decision counts.",
    ),
    _write(
        "coordination.relay.authorize",
        "/v1/coordination/relays",
        AuthorizeRelayRequest,
        _relay_authorize,
        "Create a relay authorization bound to an approved exact target.",
        "Use when work must cross projects under an approved relay gate; do "
        "not use for same-scope work or targets differing from the approval.",
    ),
    _write(
        "coordination.relay.revoke",
        "/v1/coordination/relays/{relay_id}:revoke",
        NoteRequest,
        _relay_revoke,
        "Revoke an unconsumed relay authorization.",
        "Use when the relay must not happen after all; do not expect it to "
        "undo a dispatch - consumed relays are immutable history.",
    ),
    _write(
        "coordination.relay.dispatch",
        "/v1/coordination/relays/{relay_id}:dispatch",
        NoteRequest,
        _relay_dispatch,
        "Consume the authorization and write the durable source-side receipt.",
        "Use when the target project may begin, exactly once; do not "
        "dispatch without the still-approved gate - revocation and "
        "consumption race in one transaction and the receipt keeps "
        "immutable IDs only.",
    ),
    _write(
        "coordination.relay.materialize",
        "/v1/coordination/relays/{relay_id}:materialize",
        NoteRequest,
        _relay_materialize,
        "Create the linked target handoff from a dispatched relay receipt.",
        "Use when the target scope is ready to receive the relayed handoff, "
        "as the relay target actor or a human lead; do not use to duplicate "
        "work - one relay materializes exactly once.",
    ),
    _read(
        "coordination.relay.get",
        "/v1/coordination/relays/{relay_id}",
        _relay_get,
        "Read a relay: authorization, dispatch receipt and materialization.",
        "Use when proving what crossed projects and under which approval; do "
        "not treat the snapshot as live source-handoff state.",
    ),
    _write(
        "coordination.epic.create",
        "/v1/coordination/epics",
        CreateEpicRequest,
        _epic_create,
        "Create an epic that groups waves and tasks in one scope.",
        "Use when sequencing multi-wave work; do not use as a document - "
        "epics carry structure, not content.",
    ),
    _write(
        "coordination.wave.create",
        "/v1/coordination/waves",
        CreateWaveRequest,
        _wave_create,
        "Create an ordered wave inside an epic, optionally human-gated.",
        "Use when work must open only after earlier waves complete; do not "
        "change requires_human_gate later - it is an immutable input.",
    ),
    _write(
        "coordination.wave.open",
        "/v1/coordination/waves/{wave_id}:open",
        OpenWaveRequest,
        _wave_open,
        "Open a wave once prior waves completed and any human gate approved.",
        "Use when the wave may start dispatching; do not bypass the gate - an "
        "unapproved or expired gate keeps the wave pending.",
    ),
    _write(
        "coordination.wave.complete",
        "/v1/coordination/waves/{wave_id}:complete",
        NoteRequest,
        _wave_complete,
        "Complete an open wave whose tasks are all terminal.",
        "Use when every task completed or was cancelled deliberately; do not "
        "use while work is outstanding - it fails explicitly.",
    ),
    _write(
        "coordination.board.create",
        "/v1/coordination/boards",
        CreateBoardRequest,
        _board_create,
        "Create a board for organizing tasks into columns.",
        "Use when a human-facing view over tasks is needed; do not expect "
        "boards to affect dispatch eligibility - they are organization only.",
    ),
    _write(
        "coordination.board.task.add",
        "/v1/coordination/boards/{board_id}/tasks",
        AddBoardTaskRequest,
        _board_task_add,
        "Add a task to a board column.",
        "Use when surfacing a task on a board; do not use to change task "
        "state - transitions are explicit task commands.",
    ),
    _write(
        "coordination.task.create",
        "/v1/coordination/tasks",
        CreateTaskRequest,
        _task_create,
        "Create a task with optional epic/wave placement and dependencies.",
        "Use when defining dispatchable units of work; do not encode secret "
        "or huge payloads in the brief - attach content instead.",
    ),
    _write(
        "coordination.task.assign",
        "/v1/coordination/tasks/{task_id}:assign",
        AssignTaskRequest,
        _task_assign,
        "Assign a scope member actor as responsible or supporting.",
        "Use when fixing responsibility before dispatch; do not assign two "
        "responsible owners - dispatch fails explicitly on ambiguity.",
    ),
    _write(
        "coordination.task.dispatch",
        "/v1/coordination/tasks/{task_id}:dispatch",
        DispatchTaskRequest,
        _task_dispatch,
        "Dispatch an eligible task, recording deterministic eligibility proof.",
        "Use when dependencies are terminal, the wave is open and exactly one "
        "responsible owner exists; do not expect it to launch anything - "
        "KOS or a worker claims the resulting work.",
    ),
    _write(
        "coordination.task.complete",
        "/v1/coordination/tasks/{task_id}:complete",
        CompleteTaskRequest,
        _task_complete,
        "Mark an open or dispatched task completed.",
        "Use when the task's outcome exists and was delivered; do not use to "
        "close blocked work - cancel with a reason instead.",
    ),
    _write(
        "coordination.task.cancel",
        "/v1/coordination/tasks/{task_id}:cancel",
        CancelTaskRequest,
        _task_cancel,
        "Cancel an open or dispatched task with a recorded reason.",
        "Use when the task must not proceed; do not use silently - dependents "
        "see cancelled dependencies in their eligibility evidence.",
    ),
    _read(
        "coordination.task.get",
        "/v1/coordination/tasks/{task_id}",
        _task_get,
        "Read a task with assignments, dependencies, eligibility and audit.",
        "Use when checking why a task is or is not dispatchable; do not treat "
        "the eligibility snapshot as a reservation.",
    ),
    _read(
        "coordination.task.list",
        "/v1/coordination/tasks",
        _task_list,
        "List tasks with status/epic/wave filters and keyset paging.",
        "Use when scanning a backlog or wave; do not use as a change feed - "
        "poll the scoped feed for events.",
    ),
    _read(
        "coordination.task.dispatch_eligibility",
        "/v1/coordination/tasks/{task_id}/dispatch-eligibility",
        _task_dispatch_eligibility,
        "Evaluate deterministic dispatch eligibility without changing state.",
        "Use when checking blocking reasons before attempting a dispatch; "
        "do not cache the verdict - it reflects the transaction that "
        "computed it.",
    ),
    _read(
        "coordination.work_product.receipt.get",
        "/v1/coordination/work-products/{receipt_id}",
        _work_product_receipt_get,
        "Read a work-product receipt with freshness and verification links.",
        "Use when checking whether returned evidence is still fresh against "
        "its pinned revision; do not read self-reported attestation as "
        "independent correctness verification.",
    ),
]
