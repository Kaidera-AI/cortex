"""Durable job queue: intents, leases, fencing epochs, budgets and dispositions.

Invariants this module exists to guarantee (F06, N02, N03):

* Accepted required work is a committed row, never a process task. ``enqueue_jobs``
  runs inside the caller's command transaction, so "preserve the original and
  enqueue the work" is one commit.
* Claiming uses ``FOR UPDATE SKIP LOCKED`` in a short transaction and always
  advances the fencing epoch; the connection is released before execution.
* Publication re-checks epoch, lease owner, lease expiry, cancellation, job
  status and the pinned source revision in the same transaction as the effect,
  so a stale attempt can never publish.
* Every terminal state releases its budget reservations; every projection write
  is idempotent on a canonical output key, so at-least-once execution does not
  duplicate rows.

No transaction is held across model, network or filesystem work: the claim,
heartbeat, publish and requeue statements are separate short transactions.
"""

from __future__ import annotations

import json
import random
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable, Mapping, Sequence

import asyncpg

from ..store import ApiProblem, ScopeContext
from . import repository
from .contracts import (
    EXECUTOR_CORE,
    EXECUTOR_GRAPH,
    EXECUTOR_ROLES,
    JOB_KINDS,
    Outcome,
    disposition_for,
    DISPOSITION_BLOCK,
    DISPOSITION_QUARANTINE,
    DISPOSITION_RETRY,
    DISPOSITION_SUCCEED,
)
from .formats import detect_format, required_role_for
from .keys import job_dedupe_key

BUDGET_PROVIDER_CALLS = "provider_calls"
BUDGET_CONCURRENCY = "concurrency"
BUDGET_EMBEDDING_TOKENS = "embedding_tokens"
BUDGET_BYTES_IN_FLIGHT = "bytes_in_flight"

#: SQLSTATE raised by the budget admission trigger.
SQLSTATE_CONFIGURATION_LIMIT = "53400"
SQLSTATE_CHECK_VIOLATION = "23514"
SQLSTATE_UNIQUE_VIOLATION = "23505"
SQLSTATE_OBJECT_NOT_IN_PREREQUISITE_STATE = "55000"
SQLSTATE_FOREIGN_KEY_VIOLATION = "23503"

#: Bound on durable queued work per scope. Beyond it, new intents are deferred
#: with a typed reason instead of growing the queue without limit (N04).
DEFAULT_QUEUE_ADMISSION_LIMIT = 5_000

#: A worker whose heartbeat is older than this is not counted as serving its
#: executor roles, so queued work waiting for it stays visible (R12).
WORKER_HEARTBEAT_STALE_SECONDS = 90

CONTENT_PINNED_KINDS = frozenset(
    {
        "doc.extract",
        "embed.chunks",
        "transform.distill",
        "transform.compact",
    }
)
GRAPH_KINDS = frozenset({"graph.code.extract", "graph.memory.extract"})

TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled", "quarantined"})

FAILURE_MESSAGES = {
    "unsupported_job_kind": "That job kind is not part of the versioned contract.",
    "job_intent_invalid": "The job intent does not pin what its kind requires.",
    "job_not_found": "The job is unavailable in the selected scope.",
    "job_terminal": "The job already reached a terminal state.",
    "invalid_cursor": "The pagination cursor is not valid for this listing.",
    "scope_write_denied": "Write access is not granted for this scope.",
    "profile_not_found": "The processing profile is unavailable.",
    "space_not_found": "The embedding space is unavailable.",
    "budget_exhausted": "Processing capacity is reserved; retry later.",
    "queue_admission_full": "The durable queue for this scope is at its bound.",
}


def _problem(code: str, status: int = 422, retryable: bool = False) -> ApiProblem:
    return ApiProblem(status, code, FAILURE_MESSAGES[code], retryable=retryable)


@dataclass(frozen=True, slots=True)
class JobIntent:
    """One unit of durable work intent.

    ``content_id``/``source_revision``/``profile_id`` are required for the
    content-processing kinds and must all be None for graph kinds, which pin
    their own identity in ``payload["pin"]``.
    """

    job_kind: str
    payload: dict[str, Any] = field(default_factory=dict)
    scope_id: uuid.UUID | None = None
    content_id: uuid.UUID | None = None
    source_revision: int | None = None
    profile_id: uuid.UUID | None = None
    space_id: uuid.UUID | None = None
    generation_id: uuid.UUID | None = None
    batch_id: uuid.UUID | None = None
    dedupe_key: str | None = None
    required_role: str | None = None
    priority: int = 100
    available_at: datetime | None = None
    max_attempts: int = 5
    is_interactive: bool = False
    budget: Mapping[str, int] | None = None
    requested_by_principal: uuid.UUID | None = None

    def default_dedupe_key(self, profile_identity: str, scope_id: uuid.UUID) -> str:
        """Stable accepted-work key derived from the resolved scope and pins."""
        if self.dedupe_key:
            return self.dedupe_key
        return job_dedupe_key(
            job_kind=self.job_kind,
            scope_id=scope_id,
            content_id=self.content_id or uuid.UUID(int=0),
            revision=self.source_revision or 0,
            profile_identity=profile_identity,
            space_id=self.space_id,
            generation_id=self.generation_id,
        )

    def validate(self) -> None:
        if self.job_kind not in JOB_KINDS:
            raise _problem("unsupported_job_kind")
        if (self.content_id is None) != (self.source_revision is None):
            raise _problem("job_intent_invalid")
        if self.job_kind in CONTENT_PINNED_KINDS:
            if self.content_id is None or self.profile_id is None:
                raise _problem("job_intent_invalid")
            if self.job_kind != "doc.extract" and "transform" not in self.job_kind:
                if self.space_id is None or self.generation_id is None:
                    raise _problem("job_intent_invalid")
            if self.job_kind == "doc.extract" and self.space_id is None:
                raise _problem("job_intent_invalid")
        if self.job_kind in GRAPH_KINDS:
            pin = self.payload.get("pin")
            if not isinstance(pin, dict) or not pin:
                raise _problem("job_intent_invalid")
            if self.content_id is not None or self.profile_id is not None:
                raise _problem("job_intent_invalid")
            if self.required_role not in (None, EXECUTOR_GRAPH):
                raise _problem("job_intent_invalid")
        if not 1 <= self.max_attempts <= 100:
            raise _problem("job_intent_invalid")
        if not 0 <= self.priority <= 1000:
            raise _problem("job_intent_invalid")


@dataclass(frozen=True, slots=True)
class EnqueuedJob:
    job_id: uuid.UUID
    dedupe_key: str
    created: bool
    status: str


@dataclass(frozen=True, slots=True)
class DeferredJob:
    """Work that was NOT accepted: the caller must report it, never swallow it."""

    dedupe_key: str
    reason: str
    detail: str
    content_id: uuid.UUID | None = None
    source_revision: int | None = None


@dataclass(frozen=True, slots=True)
class EnqueueResult:
    enqueued: tuple[EnqueuedJob, ...] = ()
    deferred: tuple[DeferredJob, ...] = ()

    @property
    def accepted(self) -> int:
        return sum(1 for job in self.enqueued if job.created)

    def as_receipt(self) -> dict[str, Any]:
        return {
            "enqueued": [
                {
                    "job_id": str(job.job_id),
                    "dedupe_key": job.dedupe_key,
                    "created": job.created,
                    "status": job.status,
                }
                for job in self.enqueued
            ],
            "deferred": [
                {
                    "dedupe_key": item.dedupe_key,
                    "reason": item.reason,
                    "detail": item.detail,
                    "content_id": str(item.content_id) if item.content_id else None,
                    "source_revision": item.source_revision,
                }
                for item in self.deferred
            ],
            "accepted": self.accepted,
            "replayed": sum(1 for job in self.enqueued if not job.created),
        }


def resolve_required_role(
    intent: JobIntent, profile: Mapping[str, Any] | None
) -> str:
    """Executor role a job needs, from the profile and any declared format.

    Detection at enqueue time uses only the declared media type and filename;
    execution may reroute once when the real signature says otherwise.
    """
    if intent.job_kind in GRAPH_KINDS:
        # Retrieval's graph extraction pins the canonical graph executor role;
        # only a graph worker advertises that capability.
        return EXECUTOR_GRAPH
    declared = (profile or {}).get("parser", {}).get("executor_role") or EXECUTOR_CORE
    if intent.required_role:
        return intent.required_role
    payload = intent.payload
    media_type = payload.get("media_type") if isinstance(payload, dict) else None
    filename = payload.get("filename") if isinstance(payload, dict) else None
    if isinstance(media_type, str) or isinstance(filename, str):
        detection = detect_format(
            media_type=media_type if isinstance(media_type, str) else None,
            filename=filename if isinstance(filename, str) else None,
        )
        if detection.format_id:
            return required_role_for(detection.format_id)
    return declared


async def enqueue_jobs(
    connection: asyncpg.Connection,
    *,
    context: ScopeContext,
    intents: Sequence[JobIntent],
    reserve_budget: bool = True,
    queue_admission_limit: int = DEFAULT_QUEUE_ADMISSION_LIMIT,
) -> EnqueueResult:
    """Durably enqueue work inside the caller's open transaction.

    This function never commits. Identical intents collapse onto the existing
    job through the ``(scope_id, job_kind, dedupe_key)`` key, so a repeated
    command resumes instead of duplicating work.
    """
    if not intents:
        return EnqueueResult()
    scope_id = context.selected.scope_id
    principal_id = context.principal.principal_id
    installation_id = context.principal.installation_id

    profiles: dict[uuid.UUID, dict[str, Any] | None] = {}
    for intent in intents:
        intent.validate()
        target_scope = intent.scope_id or scope_id
        if target_scope != scope_id:
            raise _problem("scope_write_denied", 403)
        if intent.profile_id is not None and intent.profile_id not in profiles:
            profiles[intent.profile_id] = await repository.get_profile(
                connection, intent.profile_id
            )
            profile = profiles[intent.profile_id]
            if profile is None:
                raise _problem("profile_not_found", 404)
            if profile["status"] != "active":
                raise _problem("profile_not_found", 404)

    queued = int(
        await connection.fetchval(
            """
            SELECT count(*)
              FROM cortex_processing.jobs
             WHERE scope_id = $1 AND status IN ('queued', 'leased', 'blocked')
            """,
            scope_id,
        )
    )

    enqueued: list[EnqueuedJob] = []
    deferred: list[DeferredJob] = []
    for intent in intents:
        profile = profiles.get(intent.profile_id) if intent.profile_id else None
        identity = repository.profile_identity(profile) if profile else "unpinned"
        dedupe_key = intent.default_dedupe_key(identity, scope_id)
        if not 8 <= len(dedupe_key) <= 128:
            raise _problem("job_intent_invalid")
        if queued >= queue_admission_limit:
            deferred.append(
                DeferredJob(
                    dedupe_key=dedupe_key,
                    reason="queue_admission_full",
                    detail=(
                        f"{queued} durable jobs are already waiting in this scope; "
                        f"the bound is {queue_admission_limit}"
                    ),
                    content_id=intent.content_id,
                    source_revision=intent.source_revision,
                )
            )
            continue
        required_role = resolve_required_role(intent, profile)
        job_id = uuid.uuid4()
        budget = dict(intent.budget if intent.budget is not None else {})
        if not intent.is_interactive and not budget:
            budget = {BUDGET_PROVIDER_CALLS: 1}
        intent_payload = {
            "schema_version": 1,
            "job_kind": intent.job_kind,
            "payload": dict(intent.payload),
            "required_role": required_role,
            "profile_identity": identity,
            "is_interactive": intent.is_interactive,
        }
        try:
            # A savepoint per intent: one deferred job must not roll back the
            # command that accepted the rest.
            async with connection.transaction():
                row = await connection.fetchrow(
                    """
                    INSERT INTO cortex_processing.jobs
                        (job_id, scope_id, installation_id, job_kind, dedupe_key,
                         required_role, status, priority, available_at, content_id,
                         source_revision, profile_id, space_id, generation_id,
                         batch_id, intent, max_attempts, requested_by_principal)
                    VALUES ($1, $2, $3, $4, $5, $6, 'queued', $7,
                            COALESCE($8, now()), $9, $10, $11, $12, $13, $14,
                            $15::jsonb, $16, $17)
                    ON CONFLICT (scope_id, job_kind, dedupe_key) DO NOTHING
                    RETURNING job_id, status
                    """,
                    job_id,
                    scope_id,
                    installation_id,
                    intent.job_kind,
                    dedupe_key,
                    required_role,
                    intent.priority,
                    intent.available_at,
                    intent.content_id,
                    intent.source_revision,
                    intent.profile_id,
                    intent.space_id,
                    intent.generation_id,
                    intent.batch_id,
                    json.dumps(intent_payload, ensure_ascii=False, sort_keys=True),
                    intent.max_attempts,
                    intent.requested_by_principal or principal_id,
                )
                if row is None:
                    existing = await connection.fetchrow(
                        """
                        SELECT job_id, status
                          FROM cortex_processing.jobs
                         WHERE scope_id = $1 AND job_kind = $2 AND dedupe_key = $3
                        """,
                        scope_id,
                        intent.job_kind,
                        dedupe_key,
                    )
                    enqueued.append(
                        EnqueuedJob(
                            job_id=existing["job_id"],
                            dedupe_key=dedupe_key,
                            created=False,
                            status=existing["status"],
                        )
                    )
                    continue
                if reserve_budget:
                    for resource, amount in sorted(budget.items()):
                        await connection.execute(
                            """
                            INSERT INTO cortex_processing.budget_reservations
                                (reservation_id, scope_id, job_id, resource, amount,
                                 is_interactive)
                            VALUES ($1, $2, $3, $4, $5, $6)
                            """,
                            uuid.uuid4(),
                            scope_id,
                            row["job_id"],
                            resource,
                            int(amount),
                            intent.is_interactive,
                        )
                await repository.insert_outbox_event(
                    connection,
                    scope_id=scope_id,
                    aggregate_kind="processing_job",
                    aggregate_id=row["job_id"],
                    event_type="processing.job.accepted",
                    aggregate_version=0,
                    payload={
                        "job_kind": intent.job_kind,
                        "required_role": required_role,
                        "dedupe_key": dedupe_key,
                        "batch_id": str(intent.batch_id) if intent.batch_id else None,
                        "content_id": (
                            str(intent.content_id) if intent.content_id else None
                        ),
                        "source_revision": intent.source_revision,
                    },
                )
                enqueued.append(
                    EnqueuedJob(
                        job_id=row["job_id"],
                        dedupe_key=dedupe_key,
                        created=True,
                        status=row["status"],
                    )
                )
                queued += 1
        except asyncpg.PostgresError as exc:
            if exc.sqlstate == SQLSTATE_CONFIGURATION_LIMIT:
                deferred.append(
                    DeferredJob(
                        dedupe_key=dedupe_key,
                        reason="budget_exhausted",
                        detail=(exc.message or "processing budget exhausted")[:512],
                        content_id=intent.content_id,
                        source_revision=intent.source_revision,
                    )
                )
                continue
            if exc.sqlstate == SQLSTATE_CHECK_VIOLATION:
                raise _problem("job_intent_invalid") from exc
            if exc.sqlstate == SQLSTATE_FOREIGN_KEY_VIOLATION:
                raise _problem("job_intent_invalid") from exc
            raise
    return EnqueueResult(enqueued=tuple(enqueued), deferred=tuple(deferred))


async def enqueue_job(
    connection: asyncpg.Connection,
    *,
    context: ScopeContext,
    intent: JobIntent,
    reserve_budget: bool = True,
) -> EnqueuedJob:
    result = await enqueue_jobs(
        connection,
        context=context,
        intents=(intent,),
        reserve_budget=reserve_budget,
    )
    if result.deferred:
        deferred = result.deferred[0]
        raise _problem(deferred.reason, 429, retryable=True)
    return result.enqueued[0]


# ---------------------------------------------------------------------------
# Claiming and leases
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ClaimedJob:
    """A leased attempt. Carries the fencing token every publish must present."""

    job_id: uuid.UUID
    attempt_id: uuid.UUID
    scope_id: uuid.UUID
    installation_id: uuid.UUID
    job_kind: str
    required_role: str
    status: str
    content_id: uuid.UUID | None
    source_revision: int | None
    profile_id: uuid.UUID | None
    space_id: uuid.UUID | None
    generation_id: uuid.UUID | None
    batch_id: uuid.UUID | None
    intent: dict[str, Any]
    fencing_epoch: int
    lease_owner: str
    lease_expires_at: datetime
    attempt_number: int
    priority: int
    max_attempts: int
    rerouted_at: datetime | None

    @property
    def payload(self) -> dict[str, Any]:
        payload = self.intent.get("payload")
        return payload if isinstance(payload, dict) else {}


class ClaimState:
    CLAIMED = "claimed"
    EMPTY = "empty"
    BUDGET_EXHAUSTED = "budget_exhausted"


@dataclass(frozen=True, slots=True)
class ClaimOutcome:
    state: str
    job: ClaimedJob | None = None
    detail: str | None = None


def backoff_delay(
    attempt: int,
    *,
    base_seconds: float = 2.0,
    cap_seconds: float = 900.0,
    jitter_ratio: float = 0.25,
    rng: Callable[[], float] = random.random,
    retry_after_seconds: float | None = None,
) -> float:
    """Bounded exponential backoff with jitter and provider retry guidance."""
    if attempt < 1:
        attempt = 1
    delay = min(cap_seconds, base_seconds * (2.0 ** (attempt - 1)))
    if retry_after_seconds and retry_after_seconds > 0:
        delay = min(cap_seconds, max(delay, float(retry_after_seconds)))
    spread = delay * max(0.0, min(jitter_ratio, 0.9))
    return max(0.0, delay - spread / 2 + spread * rng())


async def claim_job(
    connection: asyncpg.Connection,
    *,
    worker_id: str,
    worker_role: str,
    executor_roles: Sequence[str],
    job_kinds: Sequence[str],
    writable_scopes: Sequence[uuid.UUID],
    lease_seconds: int,
    principal_id: uuid.UUID,
) -> ClaimOutcome:
    """Claim one job with ``FOR UPDATE SKIP LOCKED`` in a short transaction.

    The caller opens the transaction and has already set
    ``cortex.principal_id`` and ``cortex.read_scope_ids``; this function sets
    the transaction-local write scope to the claimed job's scope before it
    updates anything, so the write policy is satisfied by the worker's own
    grant and revocation takes effect immediately.
    """
    if not writable_scopes or not job_kinds:
        return ClaimOutcome(state=ClaimState.EMPTY, detail="no writable scopes or kinds")
    row = None
    for candidate_scope in writable_scopes:
        # A locking read is a write as far as RLS is concerned: PostgreSQL
        # applies the table's UPDATE policy in addition to SELECT for
        # FOR UPDATE, so the transaction-local write scope must already name
        # the scope being claimed. One scope per pass preserves SKIP LOCKED
        # semantics (no wasted attempts, no polling storm) without weakening a
        # policy or adding a "trusted flag" escape hatch. Within a scope the
        # ordering is priority DESC, available_at, job_id; across scopes the
        # caller's writable-scope order decides which scope is served first.
        await connection.execute(
            "SELECT set_config('cortex.write_scope_id', $1, true)",
            str(candidate_scope),
        )
        row = await connection.fetchrow(
            """
            SELECT j.job_id, j.scope_id, j.installation_id, j.job_kind, j.required_role,
                   j.status, j.content_id, j.source_revision, j.profile_id, j.space_id,
                   j.generation_id, j.batch_id, j.intent, j.fencing_epoch,
                   j.attempt_count, j.priority, j.max_attempts, j.rerouted_at
              FROM cortex_processing.jobs AS j
             WHERE ((j.status = 'queued' AND j.available_at <= now())
                    OR (j.status = 'leased' AND j.lease_expires_at < now()
                        AND j.attempt_count < j.max_attempts))
               AND NOT j.cancel_requested
               AND j.required_role = ANY($1::text[])
               AND j.job_kind = ANY($2::text[])
               AND j.scope_id = $3
             ORDER BY j.priority DESC, j.available_at, j.job_id
             LIMIT 1
               FOR UPDATE SKIP LOCKED
            """,
            list(executor_roles),
            list(job_kinds),
            candidate_scope,
        )
        if row is not None:
            break
    if row is None:
        return ClaimOutcome(state=ClaimState.EMPTY)
    scope_id = row["scope_id"]
    attempt_id = uuid.uuid4()
    updated = await connection.fetchrow(
        """
        UPDATE cortex_processing.jobs AS j
           SET status = 'leased',
               fencing_epoch = j.fencing_epoch + 1,
               lease_owner = $2,
               lease_expires_at = now() + make_interval(secs => $3),
               attempt_count = j.attempt_count + 1,
               started_at = COALESCE(j.started_at, now()),
               error_code = NULL,
               error_message = NULL,
               error_retryable = NULL
         WHERE j.job_id = $1
           AND j.status IN ('queued', 'leased')
           AND NOT j.cancel_requested
        RETURNING fencing_epoch, lease_expires_at, attempt_count, status
        """,
        row["job_id"],
        worker_id,
        lease_seconds,
    )
    if updated is None:
        # Another worker won the race between the lock and the update; the
        # transaction rolls back and the next claim picks different work.
        raise LeaseRaceLost()
    await connection.execute(
        """
        INSERT INTO cortex_processing.attempts
            (attempt_id, job_id, scope_id, fencing_epoch, worker_id, worker_role,
             status, claimed_at, heartbeat_at, lease_expires_at)
        VALUES ($1, $2, $3, $4, $5, $6, 'leased', now(), now(), $7)
        """,
        attempt_id,
        row["job_id"],
        scope_id,
        updated["fencing_epoch"],
        worker_id,
        worker_role,
        updated["lease_expires_at"],
    )
    # A reclaimed job may still carry the in-flight reservation of the attempt
    # that died holding the lease. That slot is by definition no longer in
    # flight, so release it before reserving this attempt's - otherwise the job
    # becomes permanently unclaimable, which is the recovery case this queue
    # exists to make safe.
    await connection.execute(
        """
        UPDATE cortex_processing.budget_reservations
           SET released_at = now()
         WHERE job_id = $1
           AND resource = 'concurrency'
           AND released_at IS NULL
        """,
        row["job_id"],
    )
    try:
        await connection.execute(
            """
            INSERT INTO cortex_processing.budget_reservations
                (reservation_id, scope_id, job_id, resource, amount, is_interactive)
            VALUES ($1, $2, $3, 'concurrency', 1, false)
            """,
            uuid.uuid4(),
            scope_id,
            row["job_id"],
        )
    except asyncpg.PostgresError as exc:
        if exc.sqlstate == SQLSTATE_CONFIGURATION_LIMIT:
            raise BudgetExhausted(exc.message or "concurrency budget exhausted") from exc
        raise
    return ClaimOutcome(
        state=ClaimState.CLAIMED,
        job=ClaimedJob(
            job_id=row["job_id"],
            attempt_id=attempt_id,
            scope_id=scope_id,
            installation_id=row["installation_id"],
            job_kind=row["job_kind"],
            required_role=row["required_role"],
            status=updated["status"],
            content_id=row["content_id"],
            source_revision=row["source_revision"],
            profile_id=row["profile_id"],
            space_id=row["space_id"],
            generation_id=row["generation_id"],
            batch_id=row["batch_id"],
            intent=_intent(row["intent"]),
            fencing_epoch=int(updated["fencing_epoch"]),
            lease_owner=worker_id,
            lease_expires_at=updated["lease_expires_at"],
            attempt_number=int(updated["attempt_count"]),
            priority=int(row["priority"]),
            max_attempts=int(row["max_attempts"]),
            rerouted_at=row["rerouted_at"],
        ),
    )


class LeaseRaceLost(Exception):
    """The candidate was taken between locking and updating; roll back."""


class BudgetExhausted(Exception):
    """Concurrency admission refused; roll back the claim and back off."""

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


class LeaseState:
    LIVE = "live"
    CANCELLED = "cancelled"
    LOST = "lost"


async def renew_lease(
    connection: asyncpg.Connection,
    *,
    claim: ClaimedJob,
    lease_seconds: int,
) -> str:
    """Heartbeat. Returns the lease state; a lost lease stops execution."""
    row = await connection.fetchrow(
        """
        UPDATE cortex_processing.jobs
           SET lease_expires_at = now() + make_interval(secs => $4)
         WHERE job_id = $1
           AND fencing_epoch = $2
           AND lease_owner = $3
           AND status = 'leased'
        RETURNING cancel_requested
        """,
        claim.job_id,
        claim.fencing_epoch,
        claim.lease_owner,
        lease_seconds,
    )
    if row is None:
        return LeaseState.LOST
    await connection.execute(
        """
        UPDATE cortex_processing.attempts
           SET heartbeat_at = now(),
               lease_expires_at = now() + make_interval(secs => $2),
               status = CASE WHEN status = 'leased' THEN 'running' ELSE status END
         WHERE attempt_id = $1
        """,
        claim.attempt_id,
        lease_seconds,
    )
    return LeaseState.CANCELLED if row["cancel_requested"] else LeaseState.LIVE


@dataclass(frozen=True, slots=True)
class AttemptDecision:
    """What the queue did with an attempt, for logging and worker accounting."""

    published: bool
    job_status: str
    outcome_code: str
    disposition: str
    detail: str | None = None
    retry_after_seconds: float | None = None
    fenced_reason: str | None = None


PublishCallback = Callable[[asyncpg.Connection], Awaitable[None]]


def _status_for(
    disposition: str, *, attempt_number: int, max_attempts: int
) -> tuple[str, bool]:
    """Map a disposition onto a job status; returns (status, is_terminal)."""
    if disposition == DISPOSITION_SUCCEED:
        return "succeeded", True
    if disposition == DISPOSITION_QUARANTINE:
        return "quarantined", True
    if disposition == DISPOSITION_BLOCK:
        return "blocked", True
    if disposition == DISPOSITION_RETRY:
        if attempt_number >= max_attempts:
            return "failed", True
        return "queued", False
    return "failed", True


def _reroute_target(claim: ClaimedJob, report: Any, disposition: str) -> str | None:
    """Executor role discovered at execution time, when a reroute is still owed.

    Enqueue-time detection uses only the declared media type and filename; the
    real file signature can name a different executor. Rerouting once keeps the
    work durable and claimable by the right worker instead of parking it as
    blocked, and the database refuses a second reroute.
    """
    discovered = getattr(report, "required_role", None)
    if not discovered or discovered == claim.required_role:
        return None
    if discovered not in EXECUTOR_ROLES or claim.rerouted_at is not None:
        return None
    if disposition not in (DISPOSITION_BLOCK, DISPOSITION_RETRY):
        return None
    return str(discovered)


async def finish_attempt(
    connection: asyncpg.Connection,
    *,
    claim: ClaimedJob,
    report: Any,
    publish: PublishCallback | None = None,
    backoff_base_seconds: float = 2.0,
    backoff_cap_seconds: float = 900.0,
) -> AttemptDecision:
    """Finish an attempt: fence, publish, record, release budgets.

    ``publish`` runs only after the fencing predicate matched, inside the same
    transaction, so the projection effect and the state transition commit or
    roll back together. When the fence does not match, nothing is published and
    the attempt is recorded as expired or cancelled. Work whose real executor
    role differs from the enqueue-time prediction is rerouted once instead of
    being parked as blocked.
    """
    outcome = (
        report.outcome if isinstance(report.outcome, Outcome) else Outcome(report.outcome)
    )
    disposition = disposition_for(outcome)
    reroute_to = _reroute_target(claim, report, disposition)
    if reroute_to is not None:
        disposition = DISPOSITION_RETRY
        status, terminal = "queued", False
    else:
        status, terminal = _status_for(
            disposition,
            attempt_number=claim.attempt_number,
            max_attempts=claim.max_attempts,
        )
    retry_after = None
    if status == "queued":
        retry_after = backoff_delay(
            claim.attempt_number,
            base_seconds=backoff_base_seconds,
            cap_seconds=backoff_cap_seconds,
            retry_after_seconds=getattr(report, "retry_after_seconds", None),
        )

    fenced = await connection.fetchrow(
        """
        UPDATE cortex_processing.jobs
           SET status = $2,
               lease_owner = NULL,
               lease_expires_at = NULL,
               error_code = $3,
               error_message = $4,
               error_retryable = $5,
               required_role = COALESCE($6, required_role),
               available_at = CASE
                   WHEN $7::double precision IS NULL THEN available_at
                   ELSE now() + make_interval(secs => $7::double precision)
               END,
               finished_at = CASE WHEN $8 THEN now() ELSE NULL END
         WHERE job_id = $1
           AND fencing_epoch = $9
           AND lease_owner = $10
           AND lease_expires_at > now()
           AND status = 'leased'
           AND NOT cancel_requested
        RETURNING job_id
        """,
        claim.job_id,
        status,
        None if outcome is Outcome.OK else outcome.value,
        None if outcome is Outcome.OK else (getattr(report, "detail", None) or "")[:512] or None,
        None if outcome is Outcome.OK else disposition == DISPOSITION_RETRY,
        reroute_to,
        retry_after,
        terminal,
        claim.fencing_epoch,
        claim.lease_owner,
    )
    if fenced is None:
        reason = await _diagnose_fence_loss(connection, claim=claim)
        await _record_attempt(
            connection,
            claim=claim,
            status="cancelled" if reason == "cancel_requested" else "expired",
            outcome_code=outcome.value,
            detail=f"fence lost: {reason}",
            report=report,
        )
        await release_reservation(connection, job_id=claim.job_id, resource=BUDGET_CONCURRENCY)
        return AttemptDecision(
            published=False,
            job_status="unchanged",
            outcome_code=outcome.value,
            disposition=disposition,
            detail=f"publishing refused: {reason}",
            fenced_reason=reason,
        )

    if publish is not None:
        await publish(connection)

    if disposition == DISPOSITION_QUARANTINE:
        await repository.insert_quarantine(
            connection,
            scope_id=claim.scope_id,
            job_id=claim.job_id,
            content_id=claim.content_id,
            revision=claim.source_revision,
            reason_code=outcome.value,
            reason_detail=getattr(report, "detail", None),
            preserved_payload={
                "job_kind": claim.job_kind,
                "intent": claim.intent,
                "fencing_epoch": claim.fencing_epoch,
                "attempt_number": claim.attempt_number,
                "warnings": list(getattr(report, "warnings", ()) or ()),
                "stats": dict(getattr(report, "stats", {}) or {}),
            },
            executor=getattr(report, "executor", None),
            format_id=getattr(report, "format_id", None),
        )

    await _record_attempt(
        connection,
        claim=claim,
        status=_attempt_status(status),
        outcome_code=outcome.value,
        detail=getattr(report, "detail", None),
        report=report,
    )
    await repository.insert_outbox_event(
        connection,
        scope_id=claim.scope_id,
        aggregate_kind="processing_job",
        aggregate_id=claim.job_id,
        event_type=f"processing.job.{status}",
        aggregate_version=claim.fencing_epoch,
        payload={
            "job_kind": claim.job_kind,
            "outcome": outcome.value,
            "attempt_number": claim.attempt_number,
            "rerouted_to": reroute_to,
            "detail": (getattr(report, "detail", None) or "")[:512] or None,
            "warnings": list(getattr(report, "warnings", ()) or ()),
            "stats": dict(getattr(report, "stats", {}) or {}),
        },
    )
    if terminal:
        await release_all_reservations(connection, job_id=claim.job_id)
    else:
        await release_reservation(
            connection, job_id=claim.job_id, resource=BUDGET_CONCURRENCY
        )
    return AttemptDecision(
        published=True,
        job_status=status,
        outcome_code=outcome.value,
        disposition=disposition,
        detail=getattr(report, "detail", None),
        retry_after_seconds=retry_after,
    )


def _attempt_status(job_status: str) -> str:
    return {
        "succeeded": "succeeded",
        "queued": "failed",
        "failed": "failed",
        "cancelled": "cancelled",
        "quarantined": "quarantined",
        "blocked": "blocked",
    }.get(job_status, "failed")


async def _diagnose_fence_loss(
    connection: asyncpg.Connection, *, claim: ClaimedJob
) -> str:
    row = await connection.fetchrow(
        """
        SELECT status, fencing_epoch, lease_owner, lease_expires_at, cancel_requested
          FROM cortex_processing.jobs
         WHERE job_id = $1
        """,
        claim.job_id,
    )
    if row is None:
        return "job_missing"
    if row["cancel_requested"]:
        return "cancel_requested"
    if row["fencing_epoch"] != claim.fencing_epoch:
        return "epoch_superseded"
    if row["lease_owner"] != claim.lease_owner:
        return "lease_taken"
    if row["lease_expires_at"] is None or row["status"] != "leased":
        return "lease_released"
    return "lease_expired"


async def _record_attempt(
    connection: asyncpg.Connection,
    *,
    claim: ClaimedJob,
    status: str,
    outcome_code: str,
    detail: str | None,
    report: Any,
) -> None:
    await connection.execute(
        """
        UPDATE cortex_processing.attempts
           SET status = $2,
               finished_at = now(),
               heartbeat_at = now(),
               outcome_code = $3,
               outcome_detail = $4,
               stats = $5::jsonb,
               warnings = $6::text[]
         WHERE attempt_id = $1
           AND status IN ('leased', 'running')
        """,
        claim.attempt_id,
        status,
        outcome_code,
        (detail or "")[:512] or None,
        json.dumps(
            dict(getattr(report, "stats", {}) or {}), ensure_ascii=False, sort_keys=True
        ),
        list(getattr(report, "warnings", ()) or ()),
    )


async def release_reservation(
    connection: asyncpg.Connection, *, job_id: uuid.UUID, resource: str
) -> int:
    result = await connection.execute(
        """
        UPDATE cortex_processing.budget_reservations
           SET released_at = now()
         WHERE job_id = $1
           AND resource = $2
           AND released_at IS NULL
        """,
        job_id,
        resource,
    )
    return int(result.rsplit(" ", 1)[-1])


async def release_all_reservations(
    connection: asyncpg.Connection, *, job_id: uuid.UUID
) -> int:
    result = await connection.execute(
        """
        UPDATE cortex_processing.budget_reservations
           SET released_at = now()
         WHERE job_id = $1
           AND released_at IS NULL
        """,
        job_id,
    )
    return int(result.rsplit(" ", 1)[-1])


async def cancel_job(
    connection: asyncpg.Connection,
    *,
    scope_id: uuid.UUID,
    job_id: uuid.UUID,
    reason: str | None = None,
) -> dict[str, Any]:
    """Request or complete cancellation. Never pretends a running attempt stopped.

    A queued or blocked job is cancelled immediately. A leased job records the
    cancellation request; the fencing check refuses its publish and the worker
    finalizes it, so the receipt is honestly ``pending_processing``.
    """
    row = await connection.fetchrow(
        """
        SELECT job_id, status, cancel_requested, fencing_epoch
          FROM cortex_processing.jobs
         WHERE job_id = $1 AND scope_id = $2
           FOR UPDATE
        """,
        job_id,
        scope_id,
    )
    if row is None:
        raise _problem("job_not_found", 404)
    if row["status"] in TERMINAL_STATUSES:
        raise _problem("job_terminal", 409)
    if row["status"] == "leased":
        if row["cancel_requested"]:
            state = "cancellation_pending"
        else:
            await connection.execute(
                """
                UPDATE cortex_processing.jobs
                   SET cancel_requested = true,
                       error_message = $2
                 WHERE job_id = $1
                """,
                job_id,
                (reason or "")[:512] or None,
            )
            state = "cancellation_requested"
        if state == "cancellation_requested":
            await repository.insert_outbox_event(
                connection,
                scope_id=scope_id,
                aggregate_kind="processing_job",
                aggregate_id=job_id,
                event_type="processing.job.cancellation_requested",
                aggregate_version=int(row["fencing_epoch"]),
                payload={"status": row["status"], "reason": (reason or "")[:512] or None},
            )
        return {
            "job_id": str(job_id),
            "state": state,
            "status": row["status"],
            "detail": (
                "The lease holder is fenced at its next heartbeat or publish; "
                "no further effect can be published by that attempt."
            ),
        }
    await connection.execute(
        """
        UPDATE cortex_processing.jobs
           SET status = 'cancelled',
               cancel_requested = true,
               cancelled_at = now(),
               error_code = 'cancelled',
               error_message = $2
         WHERE job_id = $1
        """,
        job_id,
        (reason or "")[:512] or None,
    )
    await release_all_reservations(connection, job_id=job_id)
    await repository.insert_outbox_event(
        connection,
        scope_id=scope_id,
        aggregate_kind="processing_job",
        aggregate_id=job_id,
        event_type="processing.job.cancelled",
        aggregate_version=int(row["fencing_epoch"]),
        payload={"status": "cancelled", "reason": (reason or "")[:512] or None},
    )
    return {
        "job_id": str(job_id),
        "state": "cancelled",
        "status": "cancelled",
        "detail": "The job was cancelled before any attempt could publish.",
    }


async def finalize_cancelled(
    connection: asyncpg.Connection, *, claim: ClaimedJob, reason: str
) -> AttemptDecision:
    """Worker-side completion of a cancellation it observed."""
    fenced = await connection.fetchrow(
        """
        UPDATE cortex_processing.jobs
           SET status = 'cancelled',
               cancelled_at = now(),
               lease_owner = NULL,
               lease_expires_at = NULL,
               error_code = 'cancelled',
               error_message = $2,
               finished_at = now()
         WHERE job_id = $1
           AND fencing_epoch = $3
           AND lease_owner = $4
           AND status = 'leased'
           AND cancel_requested
        RETURNING job_id
        """,
        claim.job_id,
        reason[:512],
        claim.fencing_epoch,
        claim.lease_owner,
    )
    if fenced is None:
        reason_code = await _diagnose_fence_loss(connection, claim=claim)
        await _record_attempt(
            connection,
            claim=claim,
            status="expired",
            outcome_code=Outcome.CANCELLED.value,
            detail=f"fence lost: {reason_code}",
            report=_EmptyReport(),
        )
        return AttemptDecision(
            published=False,
            job_status="unchanged",
            outcome_code=Outcome.CANCELLED.value,
            disposition="cancelled",
            fenced_reason=reason_code,
        )
    await _record_attempt(
        connection,
        claim=claim,
        status="cancelled",
        outcome_code=Outcome.CANCELLED.value,
        detail=reason,
        report=_EmptyReport(),
    )
    await repository.insert_outbox_event(
        connection,
        scope_id=claim.scope_id,
        aggregate_kind="processing_job",
        aggregate_id=claim.job_id,
        event_type="processing.job.cancelled",
        aggregate_version=claim.fencing_epoch,
        payload={
            "job_kind": claim.job_kind,
            "status": "cancelled",
            "reason": reason[:512],
        },
    )
    await release_all_reservations(connection, job_id=claim.job_id)
    return AttemptDecision(
        published=True,
        job_status="cancelled",
        outcome_code=Outcome.CANCELLED.value,
        disposition="cancelled",
        detail=reason,
    )


@dataclass(frozen=True, slots=True)
class _EmptyReport:
    outcome: Outcome = Outcome.CANCELLED
    stats: dict[str, Any] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    detail: str | None = None


class SourceMoved(Exception):
    """The pinned source revision is no longer current, so no effect may publish.

    Raised inside the publish transaction (which then rolls back) so the
    completion-time source recheck and the effect share one unit of work (§5).
    """

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


async def finalize_source_moved(
    connection: asyncpg.Connection, *, claim: ClaimedJob, reason: str
) -> AttemptDecision:
    """Cancel an attempt whose pinned source was superseded, invalidated or lost.

    The projection is not published: indexing a superseded revision would be
    born stale. The job is cancelled with the typed reason so the operator sees
    why no effect exists.
    """
    fenced = await connection.fetchrow(
        """
        UPDATE cortex_processing.jobs
           SET status = 'cancelled',
               cancelled_at = now(),
               lease_owner = NULL,
               lease_expires_at = NULL,
               error_code = $2,
               error_message = $3,
               error_retryable = false,
               finished_at = now()
         WHERE job_id = $1
           AND fencing_epoch = $4
           AND lease_owner = $5
           AND status = 'leased'
        RETURNING job_id
        """,
        claim.job_id,
        reason,
        "the pinned source revision is no longer current; no projection was published",
        claim.fencing_epoch,
        claim.lease_owner,
    )
    if fenced is None:
        fenced_reason = await _diagnose_fence_loss(connection, claim=claim)
        await _record_attempt(
            connection,
            claim=claim,
            status="expired",
            outcome_code=reason,
            detail=f"fence lost: {fenced_reason}",
            report=_EmptyReport(),
        )
        return AttemptDecision(
            published=False,
            job_status="unchanged",
            outcome_code=reason,
            disposition="cancelled",
            fenced_reason=fenced_reason,
        )
    await _record_attempt(
        connection,
        claim=claim,
        status="cancelled",
        outcome_code=reason,
        detail="source is no longer current",
        report=_EmptyReport(),
    )
    await repository.insert_outbox_event(
        connection,
        scope_id=claim.scope_id,
        aggregate_kind="processing_job",
        aggregate_id=claim.job_id,
        event_type="processing.job.cancelled",
        aggregate_version=claim.fencing_epoch,
        payload={
            "job_kind": claim.job_kind,
            "status": "cancelled",
            "reason": reason,
            "detail": "the pinned source revision is no longer current",
        },
    )
    await release_all_reservations(connection, job_id=claim.job_id)
    return AttemptDecision(
        published=True,
        job_status="cancelled",
        outcome_code=reason,
        disposition="cancelled",
        detail=reason,
    )


async def reap_jobs(
    connection: asyncpg.Connection,
    *,
    scope_id: uuid.UUID,
    limit: int = 100,
) -> dict[str, int]:
    """Make abandoned work visible: fail exhausted leases, cancel requested ones.

    Runs as a bounded maintenance sweep, never as an in-memory timer on work
    that was accepted durably.
    """
    cancelled_rows = await connection.fetch(
        """
        UPDATE cortex_processing.jobs
           SET status = 'cancelled',
               cancelled_at = now(),
               lease_owner = NULL,
               lease_expires_at = NULL,
               error_code = 'cancelled',
               finished_at = now()
         WHERE job_id IN (
               SELECT j.job_id
                 FROM cortex_processing.jobs AS j
                WHERE j.scope_id = $1
                  AND j.cancel_requested
                  AND j.status IN ('queued', 'blocked', 'leased')
                  AND (j.status <> 'leased' OR j.lease_expires_at < now())
                ORDER BY j.created_at
                LIMIT $2
                   FOR UPDATE SKIP LOCKED
         )
        RETURNING job_id, fencing_epoch
        """,
        scope_id,
        limit,
    )
    failed_rows = await connection.fetch(
        """
        UPDATE cortex_processing.jobs
           SET status = 'failed',
               lease_owner = NULL,
               lease_expires_at = NULL,
               error_code = 'lease_expired',
               error_message = 'attempts exhausted after expired leases',
               error_retryable = false,
               finished_at = now()
         WHERE job_id IN (
               SELECT j.job_id
                 FROM cortex_processing.jobs AS j
                WHERE j.scope_id = $1
                  AND j.status = 'leased'
                  AND j.lease_expires_at < now()
                  AND j.attempt_count >= j.max_attempts
                  AND NOT j.cancel_requested
                ORDER BY j.created_at
                LIMIT $2
                   FOR UPDATE SKIP LOCKED
         )
        RETURNING job_id, fencing_epoch
        """,
        scope_id,
        limit,
    )
    for event_type, rows in (
        ("processing.job.cancelled", cancelled_rows),
        ("processing.job.failed", failed_rows),
    ):
        for row in rows:
            await repository.insert_outbox_event(
                connection,
                scope_id=scope_id,
                aggregate_kind="processing_job",
                aggregate_id=row["job_id"],
                event_type=event_type,
                aggregate_version=int(row["fencing_epoch"]),
                payload={"reaped": True},
            )
    released = await connection.execute(
        """
        UPDATE cortex_processing.budget_reservations AS r
           SET released_at = now()
         WHERE r.released_at IS NULL
           AND r.scope_id = $1
           AND NOT EXISTS (
               SELECT 1
                 FROM cortex_processing.jobs AS j
                WHERE j.job_id = r.job_id
                  AND j.status IN ('queued', 'leased', 'blocked')
           )
        """,
        scope_id,
    )
    return {
        "cancelled": len(cancelled_rows),
        "failed": len(failed_rows),
        "reservations_released": int(released.rsplit(" ", 1)[-1]),
    }


def _intent(value: str | dict[str, Any]) -> dict[str, Any]:
    intent = json.loads(value) if isinstance(value, str) else value
    return intent if isinstance(intent, dict) else {}


async def read_job(
    connection: asyncpg.Connection, *, scope_id: uuid.UUID, job_id: uuid.UUID
) -> dict[str, Any] | None:
    row = await connection.fetchrow(
        """
        SELECT j.job_id, j.scope_id, j.job_kind, j.required_role, j.status,
               j.priority, j.available_at, j.content_id, j.source_revision,
               j.profile_id, j.space_id, j.generation_id, j.batch_id, j.intent,
               j.attempt_count, j.max_attempts, j.fencing_epoch, j.lease_owner,
               j.lease_expires_at, j.cancel_requested, j.cancelled_at,
               j.error_code, j.error_message, j.error_retryable,
               j.requested_by_principal, j.created_at, j.updated_at,
               j.started_at, j.finished_at, j.dedupe_key, j.rerouted_at
          FROM cortex_processing.jobs AS j
         WHERE j.job_id = $1 AND j.scope_id = $2
        """,
        job_id,
        scope_id,
    )
    if row is None:
        return None
    attempts = await connection.fetch(
        """
        SELECT attempt_id, fencing_epoch, worker_id, worker_role, status, claimed_at,
               started_at, heartbeat_at, lease_expires_at, finished_at,
               outcome_code, outcome_detail, stats, warnings
          FROM cortex_processing.attempts
         WHERE job_id = $1
         ORDER BY fencing_epoch DESC
         LIMIT 10
        """,
        job_id,
    )
    reservations = await connection.fetch(
        """
        SELECT resource, amount, is_interactive, reserved_at, released_at
          FROM cortex_processing.budget_reservations
         WHERE job_id = $1
         ORDER BY reserved_at, resource
        """,
        job_id,
    )
    quarantine = await connection.fetch(
        """
        SELECT quarantine_id, reason_code, reason_detail, disposition, created_at
          FROM cortex_processing.quarantine_ledger
         WHERE job_id = $1
         ORDER BY created_at DESC
        """,
        job_id,
    )
    return {
        "job_id": str(row["job_id"]),
        "scope_id": str(row["scope_id"]),
        "job_kind": row["job_kind"],
        "required_role": row["required_role"],
        "status": row["status"],
        "priority": row["priority"],
        "available_at": repository._iso(row["available_at"]),
        "content_id": str(row["content_id"]) if row["content_id"] else None,
        "source_revision": row["source_revision"],
        "profile_id": str(row["profile_id"]) if row["profile_id"] else None,
        "space_id": str(row["space_id"]) if row["space_id"] else None,
        "generation_id": str(row["generation_id"]) if row["generation_id"] else None,
        "batch_id": str(row["batch_id"]) if row["batch_id"] else None,
        "dedupe_key": row["dedupe_key"],
        "intent": _intent(row["intent"]),
        "attempt_count": row["attempt_count"],
        "max_attempts": row["max_attempts"],
        "fencing_epoch": row["fencing_epoch"],
        "lease_owner": row["lease_owner"],
        "lease_expires_at": repository._iso(row["lease_expires_at"]),
        "cancel_requested": row["cancel_requested"],
        "cancelled_at": repository._iso(row["cancelled_at"]),
        "rerouted_at": repository._iso(row["rerouted_at"]),
        "error_code": row["error_code"],
        "error_message": row["error_message"],
        "error_retryable": row["error_retryable"],
        "requested_by_principal": str(row["requested_by_principal"]),
        "created_at": repository._iso(row["created_at"]),
        "updated_at": repository._iso(row["updated_at"]),
        "started_at": repository._iso(row["started_at"]),
        "finished_at": repository._iso(row["finished_at"]),
        "attempts": [
            {
                "attempt_id": str(attempt["attempt_id"]),
                "fencing_epoch": attempt["fencing_epoch"],
                "worker_id": attempt["worker_id"],
                "worker_role": attempt["worker_role"],
                "status": attempt["status"],
                "claimed_at": repository._iso(attempt["claimed_at"]),
                "started_at": repository._iso(attempt["started_at"]),
                "heartbeat_at": repository._iso(attempt["heartbeat_at"]),
                "lease_expires_at": repository._iso(attempt["lease_expires_at"]),
                "finished_at": repository._iso(attempt["finished_at"]),
                "outcome_code": attempt["outcome_code"],
                "outcome_detail": attempt["outcome_detail"],
                "stats": _intent(attempt["stats"]),
                "warnings": list(attempt["warnings"]),
            }
            for attempt in attempts
        ],
        "reservations": [
            {
                "resource": reservation["resource"],
                "amount": int(reservation["amount"]),
                "is_interactive": reservation["is_interactive"],
                "reserved_at": repository._iso(reservation["reserved_at"]),
                "released_at": repository._iso(reservation["released_at"]),
            }
            for reservation in reservations
        ],
        "quarantine": [
            {
                "quarantine_id": str(entry["quarantine_id"]),
                "reason_code": entry["reason_code"],
                "reason_detail": entry["reason_detail"],
                "disposition": entry["disposition"],
                "created_at": repository._iso(entry["created_at"]),
            }
            for entry in quarantine
        ],
    }


async def list_jobs(
    connection: asyncpg.Connection,
    *,
    scope_ids: Sequence[uuid.UUID],
    status: str | None = None,
    job_kind: str | None = None,
    required_role: str | None = None,
    batch_id: uuid.UUID | None = None,
    cursor: tuple[datetime, uuid.UUID] | None = None,
    limit: int = 25,
) -> tuple[list[dict[str, Any]], str | None]:
    """Keyset-paginated job listing with deterministic ordering."""
    rows = await connection.fetch(
        """
        SELECT j.job_id, j.scope_id, j.job_kind, j.required_role, j.status,
               j.content_id, j.source_revision, j.space_id, j.generation_id,
               j.attempt_count, j.max_attempts, j.error_code, j.error_retryable,
               j.available_at, j.created_at, j.updated_at, j.finished_at,
               j.cancel_requested, j.lease_owner, j.lease_expires_at
          FROM cortex_processing.jobs AS j
         WHERE j.scope_id = ANY($1::uuid[])
           AND ($2::text IS NULL OR j.status = $2)
           AND ($3::text IS NULL OR j.job_kind = $3)
           AND ($4::text IS NULL OR j.required_role = $4)
           AND ($5::uuid IS NULL OR j.batch_id = $5)
           AND ($6::timestamptz IS NULL
                OR (j.created_at, j.job_id) < ($6::timestamptz, $7::uuid))
         ORDER BY j.created_at DESC, j.job_id DESC
         LIMIT $8
        """,
        list(scope_ids),
        status,
        job_kind,
        required_role,
        batch_id,
        cursor[0] if cursor else None,
        cursor[1] if cursor else None,
        limit,
    )
    jobs = [
        {
            "job_id": str(row["job_id"]),
            "scope_id": str(row["scope_id"]),
            "job_kind": row["job_kind"],
            "required_role": row["required_role"],
            "status": row["status"],
            "content_id": str(row["content_id"]) if row["content_id"] else None,
            "source_revision": row["source_revision"],
            "space_id": str(row["space_id"]) if row["space_id"] else None,
            "generation_id": str(row["generation_id"]) if row["generation_id"] else None,
            "attempt_count": row["attempt_count"],
            "max_attempts": row["max_attempts"],
            "error_code": row["error_code"],
            "error_retryable": row["error_retryable"],
            "cancel_requested": row["cancel_requested"],
            "lease_owner": row["lease_owner"],
            "lease_expires_at": repository._iso(row["lease_expires_at"]),
            "available_at": repository._iso(row["available_at"]),
            "created_at": repository._iso(row["created_at"]),
            "updated_at": repository._iso(row["updated_at"]),
            "finished_at": repository._iso(row["finished_at"]),
        }
        for row in rows
    ]
    next_cursor = None
    if len(jobs) == limit:
        last = jobs[-1]
        next_cursor = f"{last['created_at']}:{last['job_id']}"
    return jobs, next_cursor


async def queue_summary(
    connection: asyncpg.Connection, *, scope_id: uuid.UUID
) -> dict[str, Any]:
    """Queue age and depth by state and executor role (N08 health signal).

    ``waiting_for_executor`` names roles that have queued work but no worker
    heartbeat, which is how a missing optional executor stays visible instead
    of looking like an idle system (R12).
    """
    rows = await connection.fetch(
        """
        SELECT status,
               required_role,
               count(*) AS jobs,
               min(created_at) AS oldest_created_at,
               max(extract(epoch FROM (now() - created_at))) AS oldest_age_seconds
          FROM cortex_processing.jobs
         WHERE scope_id = $1
           AND status IN ('queued', 'leased', 'blocked')
         GROUP BY status, required_role
         ORDER BY status, required_role
        """,
        scope_id,
    )
    by_state = [
        {
            "status": row["status"],
            "required_role": row["required_role"],
            "jobs": int(row["jobs"]),
            "oldest_created_at": repository.iso(row["oldest_created_at"]),
            "oldest_age_seconds": (
                round(float(row["oldest_age_seconds"]), 3)
                if row["oldest_age_seconds"] is not None
                else None
            ),
        }
        for row in rows
    ]
    live_roles = await repository.live_executor_roles(
        connection, max_age_seconds=WORKER_HEARTBEAT_STALE_SECONDS
    )
    waiting_for_executor = {
        entry["required_role"]: entry["jobs"]
        for entry in by_state
        if entry["status"] == "queued" and entry["required_role"] not in live_roles
    }
    return {
        "by_state": by_state,
        "live_executor_roles": sorted(live_roles),
        "waiting_for_executor": waiting_for_executor,
    }


def parse_cursor(value: str | None) -> tuple[datetime, uuid.UUID] | None:
    """Decode a keyset cursor to an aware timestamp and a UUID."""
    if value is None:
        return None
    created_at, separator, job_id = value.rpartition(":")
    if not separator or not created_at:
        raise _problem("invalid_cursor")
    try:
        timestamp = datetime.fromisoformat(created_at)
        if timestamp.utcoffset() is None:
            raise ValueError("cursor timestamp requires a timezone")
        return timestamp, uuid.UUID(job_id)
    except ValueError as exc:
        raise _problem("invalid_cursor") from exc
