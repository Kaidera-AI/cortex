"""Epics, waves, boards and tasks with deterministic dispatch eligibility
(R09).

Dispatch eligibility is evaluated by the pure function in ``eligibility.py``
over a snapshot loaded inside the dispatch transaction with the task row
locked, so the verdict is deterministic and races are excluded. Out-of-order
wave work stays ineligible, zero or multiple responsible owners fail
explicitly, and human gates require a live approved gate approval.
Coordination never launches processes: a dispatch records eligibility
evidence and state, it does not start anything.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import asyncpg

from ..store import ApiProblem, ScopeContext
from . import repository
from .approvals import load_approval_for_gate
from .commands import begin, begin_digest, committed_receipt, finish
from .eligibility import (
    TaskDispatchSnapshot,
    WaveGate,
    WaveState,
    evaluate_dispatch,
)
from .models import (
    AddBoardTaskRequest,
    AssignTaskRequest,
    CancelTaskRequest,
    CompleteTaskRequest,
    CreateBoardRequest,
    CreateEpicRequest,
    CreateTaskRequest,
    CreateWaveRequest,
    DispatchTaskRequest,
    NoteRequest,
    OpenWaveRequest,
)
from .repository import problem

TASK_COLUMNS = """
    t.scope_id, t.task_id, t.epic_id, t.wave_id, t.title, t.brief, t.status,
    t.revision, t.created_by_principal, t.created_at, t.updated_at
"""


async def _simple_create(
    connection: asyncpg.Connection,
    context: ScopeContext,
    *,
    operation: str,
    idempotency_key: str,
    aggregate_kind: str,
    aggregate_id: uuid.UUID,
    request_body: dict[str, Any],
    sql: str,
    params: tuple[Any, ...],
    action: str,
    event_type: str,
    detail: dict[str, Any],
    receipt_extra: dict[str, Any],
    conflict_code: str | None = None,
) -> tuple[int, dict[str, Any], bool]:
    digest = begin_digest(operation, context, {"request": request_body})
    previous, replayed = await begin(
        connection, context, operation, idempotency_key, digest
    )
    if replayed and previous is not None:
        return 200, previous, True

    policy_revision = await repository.declare_policy_revision(connection, context)
    try:
        created_at = await connection.fetchval(sql, *params)
    except asyncpg.UniqueViolationError as exc:
        raise problem(conflict_code or "coordination_state_conflict") from exc
    await repository.record_change(
        connection,
        scope_id=context.selected.scope_id,
        aggregate_kind=aggregate_kind,
        aggregate_id=aggregate_id,
        action=action,
        event_type=event_type,
        actor_principal_id=context.principal.principal_id,
        detail=detail,
        policy_revision=policy_revision,
    )
    receipt = committed_receipt(
        operation,
        context,
        policy_revision,
        aggregate_kind,
        aggregate_id,
        created_at=created_at.isoformat(),
        **receipt_extra,
    )
    return await finish(
        connection, context, operation, idempotency_key, digest, receipt, 201
    )


async def create_epic(
    connection: asyncpg.Connection,
    context: ScopeContext,
    payload: CreateEpicRequest,
    idempotency_key: str,
) -> tuple[int, dict[str, Any], bool]:
    epic_id = uuid.uuid4()
    return await _simple_create(
        connection,
        context,
        operation="coordination.epic.create",
        idempotency_key=idempotency_key,
        aggregate_kind="epic",
        aggregate_id=epic_id,
        request_body=payload.model_dump(mode="json"),
        sql="""
            INSERT INTO cortex_coord.epics
                (scope_id, epic_id, name, description, created_by_principal)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING created_at
        """,
        params=(
            context.selected.scope_id,
            epic_id,
            payload.name,
            payload.description,
            context.principal.principal_id,
        ),
        action="epic.created",
        event_type="coordination.epic.created",
        detail={"name": payload.name},
        receipt_extra={"epic_id": str(epic_id), "name": payload.name},
    )


async def create_wave(
    connection: asyncpg.Connection,
    context: ScopeContext,
    payload: CreateWaveRequest,
    idempotency_key: str,
) -> tuple[int, dict[str, Any], bool]:
    operation = "coordination.wave.create"
    digest = begin_digest(
        operation, context, {"request": payload.model_dump(mode="json")}
    )
    previous, replayed = await begin(
        connection, context, operation, idempotency_key, digest
    )
    if replayed and previous is not None:
        return 200, previous, True

    policy_revision = await repository.declare_policy_revision(connection, context)
    scope_id = context.selected.scope_id
    epic = await connection.fetchrow(
        "SELECT epic_id, status FROM cortex_coord.epics "
        "WHERE scope_id = $1 AND epic_id = $2",
        scope_id,
        payload.epic_id,
    )
    if epic is None:
        raise problem("epic_not_found")
    if epic["status"] != "active":
        raise problem("epic_not_active")

    wave_id = uuid.uuid4()
    try:
        created_at = await connection.fetchval(
            """
            INSERT INTO cortex_coord.waves
                (scope_id, wave_id, epic_id, wave_index, requires_human_gate,
                 created_by_principal)
            VALUES ($1, $2, $3, $4, $5, $6)
            RETURNING created_at
            """,
            scope_id,
            wave_id,
            payload.epic_id,
            payload.wave_index,
            payload.requires_human_gate,
            context.principal.principal_id,
        )
    except asyncpg.UniqueViolationError as exc:
        raise problem("wave_index_duplicate") from exc

    await repository.record_change(
        connection,
        scope_id=scope_id,
        aggregate_kind="wave",
        aggregate_id=wave_id,
        action="wave.created",
        event_type="coordination.wave.created",
        actor_principal_id=context.principal.principal_id,
        detail={
            "epic_id": str(payload.epic_id),
            "wave_index": payload.wave_index,
            "requires_human_gate": payload.requires_human_gate,
        },
        policy_revision=policy_revision,
    )
    receipt = committed_receipt(
        operation,
        context,
        policy_revision,
        "wave",
        wave_id,
        epic_id=str(payload.epic_id),
        wave_index=payload.wave_index,
        requires_human_gate=payload.requires_human_gate,
        status="pending",
        created_at=created_at.isoformat(),
    )
    return await finish(
        connection, context, operation, idempotency_key, digest, receipt, 201
    )


async def create_board(
    connection: asyncpg.Connection,
    context: ScopeContext,
    payload: CreateBoardRequest,
    idempotency_key: str,
) -> tuple[int, dict[str, Any], bool]:
    board_id = uuid.uuid4()
    return await _simple_create(
        connection,
        context,
        operation="coordination.board.create",
        idempotency_key=idempotency_key,
        aggregate_kind="board",
        aggregate_id=board_id,
        request_body=payload.model_dump(mode="json"),
        sql="""
            INSERT INTO cortex_coord.boards
                (scope_id, board_id, name, created_by_principal)
            VALUES ($1, $2, $3, $4)
            RETURNING created_at
        """,
        params=(
            context.selected.scope_id,
            board_id,
            payload.name,
            context.principal.principal_id,
        ),
        action="board.created",
        event_type="coordination.board.created",
        detail={"name": payload.name},
        receipt_extra={"board_id": str(board_id), "name": payload.name},
    )


async def create_task(
    connection: asyncpg.Connection,
    context: ScopeContext,
    payload: CreateTaskRequest,
    idempotency_key: str,
) -> tuple[int, dict[str, Any], bool]:
    operation = "coordination.task.create"
    digest = begin_digest(
        operation, context, {"request": payload.model_dump(mode="json")}
    )
    previous, replayed = await begin(
        connection, context, operation, idempotency_key, digest
    )
    if replayed and previous is not None:
        return 200, previous, True

    policy_revision = await repository.declare_policy_revision(connection, context)
    scope_id = context.selected.scope_id
    epic_id = payload.epic_id
    if payload.wave_id is not None:
        wave = await connection.fetchrow(
            "SELECT wave_id, epic_id FROM cortex_coord.waves "
            "WHERE scope_id = $1 AND wave_id = $2",
            scope_id,
            payload.wave_id,
        )
        if wave is None:
            raise problem("wave_not_found")
        if epic_id is not None and epic_id != wave["epic_id"]:
            raise problem("task_wave_mismatch")
        epic_id = wave["epic_id"]
    if epic_id is not None:
        epic = await connection.fetchrow(
            "SELECT epic_id FROM cortex_coord.epics "
            "WHERE scope_id = $1 AND epic_id = $2",
            scope_id,
            epic_id,
        )
        if epic is None:
            raise problem("epic_not_found")

    task_id = uuid.uuid4()
    depends_on = list(dict.fromkeys(payload.depends_on))
    try:
        created_at = await connection.fetchval(
            """
            INSERT INTO cortex_coord.tasks
                (scope_id, task_id, epic_id, wave_id, title, brief,
                 created_by_principal)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            RETURNING created_at
            """,
            scope_id,
            task_id,
            epic_id,
            payload.wave_id,
            payload.title,
            payload.brief,
            context.principal.principal_id,
        )
        for dependency_id in depends_on:
            await connection.execute(
                """
                INSERT INTO cortex_coord.task_dependencies
                    (scope_id, task_id, depends_on_task_id)
                VALUES ($1, $2, $3)
                """,
                scope_id,
                task_id,
                dependency_id,
            )
    except asyncpg.ForeignKeyViolationError as exc:
        raise problem("task_not_found") from exc
    except asyncpg.CheckViolationError as exc:
        raise problem("dependency_cycle") from exc

    await repository.record_change(
        connection,
        scope_id=scope_id,
        aggregate_kind="task",
        aggregate_id=task_id,
        action="task.created",
        event_type="coordination.task.created",
        actor_principal_id=context.principal.principal_id,
        detail={
            "title": payload.title,
            "epic_id": str(epic_id) if epic_id else None,
            "wave_id": str(payload.wave_id) if payload.wave_id else None,
            "depends_on": [str(item) for item in depends_on],
        },
        policy_revision=policy_revision,
    )
    receipt = committed_receipt(
        operation,
        context,
        policy_revision,
        "task",
        task_id,
        title=payload.title,
        epic_id=str(epic_id) if epic_id else None,
        wave_id=str(payload.wave_id) if payload.wave_id else None,
        depends_on=[str(item) for item in depends_on],
        status="open",
        created_at=created_at.isoformat(),
    )
    return await finish(
        connection, context, operation, idempotency_key, digest, receipt, 201
    )


async def load_task(
    connection: asyncpg.Connection, scope_id: uuid.UUID, task_id: uuid.UUID
) -> dict[str, Any]:
    """Non-locking read; FOR UPDATE under a read-only context would be
    filtered by the UPDATE policy and misreport task_not_found."""
    row = await connection.fetchrow(
        f"SELECT {TASK_COLUMNS} FROM cortex_coord.tasks AS t "
        "WHERE t.scope_id = $1 AND t.task_id = $2",
        scope_id,
        task_id,
    )
    if row is None:
        raise problem("task_not_found")
    return dict(row)


async def _lock_task(
    connection: asyncpg.Connection, scope_id: uuid.UUID, task_id: uuid.UUID
) -> dict[str, Any]:
    row = await connection.fetchrow(
        f"SELECT {TASK_COLUMNS} FROM cortex_coord.tasks AS t "
        "WHERE t.scope_id = $1 AND t.task_id = $2 FOR UPDATE",
        scope_id,
        task_id,
    )
    if row is None:
        raise problem("task_not_found")
    return dict(row)


async def add_board_task(
    connection: asyncpg.Connection,
    context: ScopeContext,
    board_id: uuid.UUID,
    payload: AddBoardTaskRequest,
    idempotency_key: str,
) -> tuple[int, dict[str, Any], bool]:
    operation = "coordination.board.task.add"
    digest = begin_digest(
        operation,
        context,
        {
            "board_id": str(board_id),
            "request": payload.model_dump(mode="json"),
        },
    )
    previous, replayed = await begin(
        connection, context, operation, idempotency_key, digest
    )
    if replayed and previous is not None:
        return 200, previous, True

    policy_revision = await repository.declare_policy_revision(connection, context)
    scope_id = context.selected.scope_id
    board = await connection.fetchrow(
        "SELECT board_id FROM cortex_coord.boards "
        "WHERE scope_id = $1 AND board_id = $2",
        scope_id,
        board_id,
    )
    if board is None:
        raise problem("board_not_found")
    await _lock_task(connection, scope_id, payload.task_id)
    try:
        added_at = await connection.fetchval(
            """
            INSERT INTO cortex_coord.board_tasks
                (scope_id, board_id, task_id, column_name,
                 added_by_principal)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING added_at
            """,
            scope_id,
            board_id,
            payload.task_id,
            payload.column,
            context.principal.principal_id,
        )
    except asyncpg.UniqueViolationError as exc:
        raise problem("duplicate_board_task") from exc

    await repository.record_change(
        connection,
        scope_id=scope_id,
        aggregate_kind="task",
        aggregate_id=payload.task_id,
        action="task.board_added",
        event_type="coordination.task.board_added",
        actor_principal_id=context.principal.principal_id,
        detail={"board_id": str(board_id), "column": payload.column},
        policy_revision=policy_revision,
    )
    receipt = committed_receipt(
        operation,
        context,
        policy_revision,
        "board",
        board_id,
        task_id=str(payload.task_id),
        column=payload.column,
        added_at=added_at.isoformat(),
    )
    return await finish(
        connection, context, operation, idempotency_key, digest, receipt, 201
    )


async def assign_task(
    connection: asyncpg.Connection,
    context: ScopeContext,
    task_id: uuid.UUID,
    payload: AssignTaskRequest,
    idempotency_key: str,
) -> tuple[int, dict[str, Any], bool]:
    operation = "coordination.task.assign"
    digest = begin_digest(
        operation,
        context,
        {"task_id": str(task_id), "request": payload.model_dump(mode="json")},
    )
    previous, replayed = await begin(
        connection, context, operation, idempotency_key, digest
    )
    if replayed and previous is not None:
        return 200, previous, True

    policy_revision = await repository.declare_policy_revision(connection, context)
    scope_id = context.selected.scope_id
    task = await _lock_task(connection, scope_id, task_id)
    if task["status"] in ("completed", "cancelled"):
        raise problem("invalid_transition")
    try:
        assigned_at = await connection.fetchval(
            """
            INSERT INTO cortex_coord.task_assignments
                (scope_id, task_id, actor_id, assignment_role,
                 assigned_by_principal)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING assigned_at
            """,
            scope_id,
            task_id,
            payload.actor_id,
            payload.assignment_role,
            context.principal.principal_id,
        )
    except asyncpg.UniqueViolationError as exc:
        raise problem("duplicate_assignment") from exc
    except asyncpg.ForeignKeyViolationError as exc:
        raise problem("addressed_actor_unavailable") from exc

    await repository.record_change(
        connection,
        scope_id=scope_id,
        aggregate_kind="task",
        aggregate_id=task_id,
        action="task.assigned",
        event_type="coordination.task.assigned",
        actor_principal_id=context.principal.principal_id,
        detail={
            "actor_id": str(payload.actor_id),
            "assignment_role": payload.assignment_role,
        },
        policy_revision=policy_revision,
    )
    receipt = committed_receipt(
        operation,
        context,
        policy_revision,
        "task",
        task_id,
        actor_id=str(payload.actor_id),
        assignment_role=payload.assignment_role,
        assigned_at=assigned_at.isoformat(),
    )
    return await finish(
        connection, context, operation, idempotency_key, digest, receipt, 201
    )


async def load_dispatch_snapshot(
    connection: asyncpg.Connection,
    scope_id: uuid.UUID,
    task: dict[str, Any],
    now: datetime,
) -> TaskDispatchSnapshot:
    dependency_rows = await connection.fetch(
        """
        SELECT dep.status
          FROM cortex_coord.task_dependencies AS d
          JOIN cortex_coord.tasks AS dep
            ON dep.scope_id = d.scope_id AND dep.task_id = d.depends_on_task_id
         WHERE d.scope_id = $1 AND d.task_id = $2
        """,
        scope_id,
        task["task_id"],
    )
    responsible_count = await connection.fetchval(
        "SELECT count(*) FROM cortex_coord.task_assignments "
        "WHERE scope_id = $1 AND task_id = $2 "
        "AND assignment_role = 'responsible' AND retired_at IS NULL",
        scope_id,
        task["task_id"],
    )
    wave_state: WaveState | None = None
    if task["wave_id"] is not None:
        wave = await connection.fetchrow(
            "SELECT wave_id, epic_id, wave_index, status, requires_human_gate, "
            "gate_approval_id FROM cortex_coord.waves "
            "WHERE scope_id = $1 AND wave_id = $2",
            scope_id,
            task["wave_id"],
        )
        if wave is None:
            raise problem("wave_not_found")
        prior_open = await connection.fetchval(
            "SELECT count(*) FROM cortex_coord.waves "
            "WHERE scope_id = $1 AND epic_id = $2 AND wave_index < $3 "
            "AND status <> 'completed'",
            scope_id,
            wave["epic_id"],
            wave["wave_index"],
        )
        gate_status: str | None = None
        gate_expires: datetime | None = None
        if wave["gate_approval_id"] is not None:
            gate = await connection.fetchrow(
                "SELECT decision, expires_at FROM cortex_coord.approvals "
                "WHERE scope_id = $1 AND approval_id = $2",
                scope_id,
                wave["gate_approval_id"],
            )
            if gate is not None:
                gate_status = gate["decision"]
                gate_expires = gate["expires_at"]
        wave_state = WaveState(
            status=wave["status"],
            prior_waves_completed=prior_open == 0,
            gate=WaveGate(
                requires_human_gate=wave["requires_human_gate"],
                approval_status=gate_status,
                approval_expires_at=gate_expires,
            ),
        )
    return TaskDispatchSnapshot(
        task_status=task["status"],
        dependency_statuses=tuple(row["status"] for row in dependency_rows),
        responsible_owner_count=int(responsible_count),
        wave=wave_state,
        now=now,
    )


def _snapshot_evidence(snapshot: TaskDispatchSnapshot) -> dict[str, Any]:
    return {
        "task_status": snapshot.task_status,
        "dependency_statuses": list(snapshot.dependency_statuses),
        "responsible_owner_count": snapshot.responsible_owner_count,
        "wave": (
            None
            if snapshot.wave is None
            else {
                "status": snapshot.wave.status,
                "prior_waves_completed": snapshot.wave.prior_waves_completed,
                "requires_human_gate": snapshot.wave.gate.requires_human_gate,
                "gate_approval_status": snapshot.wave.gate.approval_status,
                "gate_approval_expires_at": repository.iso(
                    snapshot.wave.gate.approval_expires_at
                ),
            }
        ),
    }


async def dispatch_task(
    connection: asyncpg.Connection,
    context: ScopeContext,
    task_id: uuid.UUID,
    payload: DispatchTaskRequest,
    idempotency_key: str,
) -> tuple[int, dict[str, Any], bool]:
    operation = "coordination.task.dispatch"
    digest = begin_digest(
        operation,
        context,
        {"task_id": str(task_id), "request": payload.model_dump(mode="json")},
    )
    previous, replayed = await begin(
        connection, context, operation, idempotency_key, digest
    )
    if replayed and previous is not None:
        return 200, previous, True

    policy_revision = await repository.declare_policy_revision(connection, context)
    scope_id = context.selected.scope_id
    task = await _lock_task(connection, scope_id, task_id)
    now = await repository.transaction_now(connection)
    snapshot = await load_dispatch_snapshot(connection, scope_id, task, now)
    eligibility = evaluate_dispatch(snapshot)
    if not eligibility.eligible:
        status, code, base = (
            repository.PROBLEMS["dispatch_not_eligible"][0],
            "dispatch_not_eligible",
            repository.PROBLEMS["dispatch_not_eligible"][1],
        )
        raise ApiProblem(
            status,
            code,
            f"{base} Reasons: {', '.join(eligibility.reasons)}.",
        )

    updated = await connection.execute(
        """
        UPDATE cortex_coord.tasks
           SET status = 'dispatched', revision = revision + 1, updated_at = now()
         WHERE scope_id = $1 AND task_id = $2 AND status = 'open'
           AND revision = $3
        """,
        scope_id,
        task_id,
        task["revision"],
    )
    if updated == "UPDATE 0":
        raise problem("coordination_state_conflict")

    await repository.record_change(
        connection,
        scope_id=scope_id,
        aggregate_kind="task",
        aggregate_id=task_id,
        action="task.dispatched",
        event_type="coordination.task.dispatched",
        actor_principal_id=context.principal.principal_id,
        detail={
            "note": payload.note,
            "eligibility_evidence": _snapshot_evidence(snapshot),
        },
        policy_revision=policy_revision,
    )
    receipt = committed_receipt(
        operation,
        context,
        policy_revision,
        "task",
        task_id,
        status="dispatched",
        eligibility_evidence=_snapshot_evidence(snapshot),
    )
    return await finish(
        connection, context, operation, idempotency_key, digest, receipt, 200
    )


async def _task_transition(
    connection: asyncpg.Connection,
    context: ScopeContext,
    *,
    operation: str,
    task_id: uuid.UUID,
    payload: CompleteTaskRequest | CancelTaskRequest | NoteRequest,
    idempotency_key: str,
    allowed_from: tuple[str, ...],
    to_status: str,
    action: str,
    event_type: str,
    detail: dict[str, Any],
) -> tuple[int, dict[str, Any], bool]:
    digest = begin_digest(
        operation,
        context,
        {"task_id": str(task_id), "request": payload.model_dump(mode="json")},
    )
    previous, replayed = await begin(
        connection, context, operation, idempotency_key, digest
    )
    if replayed and previous is not None:
        return 200, previous, True

    policy_revision = await repository.declare_policy_revision(connection, context)
    scope_id = context.selected.scope_id
    task = await _lock_task(connection, scope_id, task_id)
    if task["status"] not in allowed_from:
        raise problem("invalid_transition")
    updated = await connection.execute(
        """
        UPDATE cortex_coord.tasks
           SET status = $3, revision = revision + 1, updated_at = now()
         WHERE scope_id = $1 AND task_id = $2 AND revision = $4
        """,
        scope_id,
        task_id,
        to_status,
        task["revision"],
    )
    if updated == "UPDATE 0":
        raise problem("coordination_state_conflict")
    await repository.record_change(
        connection,
        scope_id=scope_id,
        aggregate_kind="task",
        aggregate_id=task_id,
        action=action,
        event_type=event_type,
        actor_principal_id=context.principal.principal_id,
        detail={"from_status": task["status"], **detail},
        policy_revision=policy_revision,
    )
    receipt = committed_receipt(
        operation,
        context,
        policy_revision,
        "task",
        task_id,
        status=to_status,
        prior_status=task["status"],
        **detail,
    )
    return await finish(
        connection, context, operation, idempotency_key, digest, receipt, 200
    )


async def complete_task(
    connection: asyncpg.Connection,
    context: ScopeContext,
    task_id: uuid.UUID,
    payload: CompleteTaskRequest,
    idempotency_key: str,
) -> tuple[int, dict[str, Any], bool]:
    return await _task_transition(
        connection,
        context,
        operation="coordination.task.complete",
        task_id=task_id,
        payload=payload,
        idempotency_key=idempotency_key,
        allowed_from=("open", "dispatched"),
        to_status="completed",
        action="task.completed",
        event_type="coordination.task.completed",
        detail={"note": payload.note},
    )


async def cancel_task(
    connection: asyncpg.Connection,
    context: ScopeContext,
    task_id: uuid.UUID,
    payload: CancelTaskRequest,
    idempotency_key: str,
) -> tuple[int, dict[str, Any], bool]:
    return await _task_transition(
        connection,
        context,
        operation="coordination.task.cancel",
        task_id=task_id,
        payload=payload,
        idempotency_key=idempotency_key,
        allowed_from=("open", "dispatched"),
        to_status="cancelled",
        action="task.cancelled",
        event_type="coordination.task.cancelled",
        detail={"reason": payload.reason},
    )


async def open_wave(
    connection: asyncpg.Connection,
    context: ScopeContext,
    wave_id: uuid.UUID,
    payload: OpenWaveRequest,
    idempotency_key: str,
) -> tuple[int, dict[str, Any], bool]:
    operation = "coordination.wave.open"
    digest = begin_digest(
        operation,
        context,
        {"wave_id": str(wave_id), "request": payload.model_dump(mode="json")},
    )
    previous, replayed = await begin(
        connection, context, operation, idempotency_key, digest
    )
    if replayed and previous is not None:
        return 200, previous, True

    policy_revision = await repository.declare_policy_revision(connection, context)
    scope_id = context.selected.scope_id
    wave = await connection.fetchrow(
        "SELECT scope_id, wave_id, epic_id, wave_index, status, "
        "requires_human_gate, gate_approval_id "
        "FROM cortex_coord.waves WHERE scope_id = $1 AND wave_id = $2 "
        "FOR UPDATE",
        scope_id,
        wave_id,
    )
    if wave is None:
        raise problem("wave_not_found")
    if wave["status"] != "pending":
        raise problem("invalid_transition")
    prior_open = await connection.fetchval(
        "SELECT count(*) FROM cortex_coord.waves "
        "WHERE scope_id = $1 AND epic_id = $2 AND wave_index < $3 "
        "AND status <> 'completed'",
        scope_id,
        wave["epic_id"],
        wave["wave_index"],
    )
    if prior_open > 0:
        raise problem("prior_wave_incomplete")

    gate_approval_id = wave["gate_approval_id"]
    if wave["requires_human_gate"]:
        if payload.gate_approval_id is None:
            raise problem("gate_approval_required")
        await load_approval_for_gate(
            connection,
            scope_id=scope_id,
            approval_id=payload.gate_approval_id,
            gate_kind="wave",
            subject_wave_id=wave_id,
        )
        gate_approval_id = payload.gate_approval_id
    elif payload.gate_approval_id is not None:
        raise problem("approval_subject_mismatch")

    updated = await connection.execute(
        """
        UPDATE cortex_coord.waves
           SET status = 'open', gate_approval_id = $3, updated_at = now()
         WHERE scope_id = $1 AND wave_id = $2 AND status = 'pending'
        """,
        scope_id,
        wave_id,
        gate_approval_id,
    )
    if updated == "UPDATE 0":
        raise problem("coordination_state_conflict")
    await repository.record_change(
        connection,
        scope_id=scope_id,
        aggregate_kind="wave",
        aggregate_id=wave_id,
        action="wave.opened",
        event_type="coordination.wave.opened",
        actor_principal_id=context.principal.principal_id,
        detail={
            "epic_id": str(wave["epic_id"]),
            "wave_index": wave["wave_index"],
            "requires_human_gate": wave["requires_human_gate"],
            "gate_approval_id": str(gate_approval_id) if gate_approval_id else None,
        },
        policy_revision=policy_revision,
    )
    receipt = committed_receipt(
        operation,
        context,
        policy_revision,
        "wave",
        wave_id,
        status="open",
        gate_approval_id=str(gate_approval_id) if gate_approval_id else None,
    )
    return await finish(
        connection, context, operation, idempotency_key, digest, receipt, 200
    )


async def complete_wave(
    connection: asyncpg.Connection,
    context: ScopeContext,
    wave_id: uuid.UUID,
    payload: NoteRequest,
    idempotency_key: str,
) -> tuple[int, dict[str, Any], bool]:
    operation = "coordination.wave.complete"
    digest = begin_digest(
        operation,
        context,
        {"wave_id": str(wave_id), "request": payload.model_dump(mode="json")},
    )
    previous, replayed = await begin(
        connection, context, operation, idempotency_key, digest
    )
    if replayed and previous is not None:
        return 200, previous, True

    policy_revision = await repository.declare_policy_revision(connection, context)
    scope_id = context.selected.scope_id
    wave = await connection.fetchrow(
        "SELECT wave_id, status FROM cortex_coord.waves "
        "WHERE scope_id = $1 AND wave_id = $2 FOR UPDATE",
        scope_id,
        wave_id,
    )
    if wave is None:
        raise problem("wave_not_found")
    if wave["status"] != "open":
        raise problem("invalid_transition")
    unfinished = await connection.fetchval(
        "SELECT count(*) FROM cortex_coord.tasks "
        "WHERE scope_id = $1 AND wave_id = $2 "
        "AND status NOT IN ('completed', 'cancelled')",
        scope_id,
        wave_id,
    )
    if unfinished > 0:
        raise problem("wave_tasks_not_terminal")
    updated = await connection.execute(
        """
        UPDATE cortex_coord.waves
           SET status = 'completed', updated_at = now()
         WHERE scope_id = $1 AND wave_id = $2 AND status = 'open'
        """,
        scope_id,
        wave_id,
    )
    if updated == "UPDATE 0":
        raise problem("coordination_state_conflict")
    await repository.record_change(
        connection,
        scope_id=scope_id,
        aggregate_kind="wave",
        aggregate_id=wave_id,
        action="wave.completed",
        event_type="coordination.wave.completed",
        actor_principal_id=context.principal.principal_id,
        detail={"note": payload.note},
        policy_revision=policy_revision,
    )
    receipt = committed_receipt(
        operation, context, policy_revision, "wave", wave_id, status="completed"
    )
    return await finish(
        connection, context, operation, idempotency_key, digest, receipt, 200
    )


async def get_task(
    connection: asyncpg.Connection,
    context: ScopeContext,
    task_id: uuid.UUID,
) -> dict[str, Any]:
    scope_id = context.selected.scope_id
    task = await load_task(connection, scope_id, task_id)
    now = await repository.transaction_now(connection)
    snapshot = await load_dispatch_snapshot(connection, scope_id, task, now)
    eligibility = evaluate_dispatch(snapshot)
    assignments = await connection.fetch(
        "SELECT actor_id, assignment_role, assigned_by_principal, assigned_at, "
        "retired_at FROM cortex_coord.task_assignments "
        "WHERE scope_id = $1 AND task_id = $2 ORDER BY assigned_at, actor_id",
        scope_id,
        task_id,
    )
    dependencies = await connection.fetch(
        """
        SELECT d.depends_on_task_id, dep.status, dep.title
          FROM cortex_coord.task_dependencies AS d
          JOIN cortex_coord.tasks AS dep
            ON dep.scope_id = d.scope_id AND dep.task_id = d.depends_on_task_id
         WHERE d.scope_id = $1 AND d.task_id = $2
         ORDER BY d.depends_on_task_id
        """,
        scope_id,
        task_id,
    )
    boards = await connection.fetch(
        "SELECT board_id, column_name FROM cortex_coord.board_tasks "
        "WHERE scope_id = $1 AND task_id = $2 ORDER BY board_id",
        scope_id,
        task_id,
    )
    audit = await connection.fetch(
        "SELECT audit_seq, action, actor_principal_id, detail, created_at "
        "FROM cortex_coord.audit_log "
        "WHERE scope_id = $1 AND aggregate_kind = 'task' AND aggregate_id = $2 "
        "ORDER BY audit_seq",
        scope_id,
        task_id,
    )
    return {
        "task_id": str(task["task_id"]),
        "scope_id": str(task["scope_id"]),
        "epic_id": str(task["epic_id"]) if task["epic_id"] else None,
        "wave_id": str(task["wave_id"]) if task["wave_id"] else None,
        "title": task["title"],
        "brief": task["brief"],
        "status": task["status"],
        "revision": task["revision"],
        "created_by_principal": str(task["created_by_principal"]),
        "created_at": task["created_at"].isoformat(),
        "updated_at": task["updated_at"].isoformat(),
        "assignments": [
            {
                "actor_id": str(row["actor_id"]),
                "assignment_role": row["assignment_role"],
                "assigned_by_principal": str(row["assigned_by_principal"]),
                "assigned_at": row["assigned_at"].isoformat(),
                "retired_at": repository.iso(row["retired_at"]),
            }
            for row in assignments
        ],
        "dependencies": [
            {
                "task_id": str(row["depends_on_task_id"]),
                "status": row["status"],
                "title": row["title"],
            }
            for row in dependencies
        ],
        "boards": [
            {"board_id": str(row["board_id"]), "column": row["column_name"]}
            for row in boards
        ],
        "dispatch_eligibility": {
            "eligible": eligibility.eligible,
            "reasons": list(eligibility.reasons),
            "snapshot": _snapshot_evidence(snapshot),
            "evaluated_at": now.isoformat(),
        },
        "audit": [
            {
                "audit_seq": row["audit_seq"],
                "action": row["action"],
                "actor_principal_id": str(row["actor_principal_id"]),
                "detail": repository.jsonb_dict(row["detail"]),
                "created_at": row["created_at"].isoformat(),
            }
            for row in audit
        ],
    }


async def task_dispatch_eligibility(
    connection: asyncpg.Connection,
    context: ScopeContext,
    task_id: uuid.UUID,
) -> dict[str, Any]:
    scope_id = context.selected.scope_id
    row = await connection.fetchrow(
        f"SELECT {TASK_COLUMNS} FROM cortex_coord.tasks AS t "
        "WHERE t.scope_id = $1 AND t.task_id = $2",
        scope_id,
        task_id,
    )
    if row is None:
        raise problem("task_not_found")
    now = await repository.transaction_now(connection)
    snapshot = await load_dispatch_snapshot(connection, scope_id, dict(row), now)
    eligibility = evaluate_dispatch(snapshot)
    return {
        "task_id": str(task_id),
        "scope_id": str(scope_id),
        "eligible": eligibility.eligible,
        "reasons": list(eligibility.reasons),
        "snapshot": _snapshot_evidence(snapshot),
        "evaluated_at": now.isoformat(),
    }


async def list_tasks(
    connection: asyncpg.Connection,
    context: ScopeContext,
    filters: dict[str, Any],
) -> dict[str, Any]:
    status = filters.get("status")
    if status is not None and status not in (
        "open",
        "dispatched",
        "completed",
        "cancelled",
    ):
        raise problem("invalid_filter")
    epic_id = filters.get("epic_id")
    wave_id = filters.get("wave_id")
    parsed_ids: tuple[uuid.UUID | None, uuid.UUID | None] = (None, None)
    try:
        parsed_ids = (
            uuid.UUID(epic_id) if epic_id else None,
            uuid.UUID(wave_id) if wave_id else None,
        )
    except ValueError as exc:
        raise problem("invalid_filter") from exc
    try:
        limit = int(filters.get("limit", 25))
    except (TypeError, ValueError) as exc:
        raise problem("invalid_filter") from exc
    if not 1 <= limit <= 100:
        raise problem("invalid_filter")
    cursor = filters.get("cursor")
    cursor_at: Any = None
    cursor_id: Any = None
    if cursor:
        cursor_at, cursor_id = repository.decode_list_cursor(str(cursor))

    rows = await connection.fetch(
        """
        SELECT t.scope_id, t.task_id, t.title, t.status, t.epic_id, t.wave_id,
               t.created_at
          FROM cortex_coord.tasks AS t
         WHERE ($1::text IS NULL OR t.status = $1)
           AND ($2::uuid IS NULL OR t.epic_id = $2)
           AND ($3::uuid IS NULL OR t.wave_id = $3)
           AND ($4::timestamptz IS NULL
                OR (t.created_at, t.task_id) < ($4::timestamptz, $5::uuid))
         ORDER BY t.created_at DESC, t.task_id DESC
         LIMIT $6
        """,
        status,
        parsed_ids[0],
        parsed_ids[1],
        cursor_at,
        cursor_id,
        limit,
    )
    items = [
        {
            "scope_id": str(row["scope_id"]),
            "task_id": str(row["task_id"]),
            "title": row["title"],
            "status": row["status"],
            "epic_id": str(row["epic_id"]) if row["epic_id"] else None,
            "wave_id": str(row["wave_id"]) if row["wave_id"] else None,
            "created_at": row["created_at"].isoformat(),
        }
        for row in rows
    ]
    next_cursor = (
        repository.encode_list_cursor(rows[-1]["created_at"], rows[-1]["task_id"])
        if len(rows) == limit
        else None
    )
    return {
        "items": items,
        "filters": {"status": status, "epic_id": epic_id, "wave_id": wave_id},
        "limit": limit,
        "next_cursor": next_cursor,
    }
