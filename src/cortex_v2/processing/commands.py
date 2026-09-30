"""Processing use cases exposed to transports.

Each function has the signature the generic operation mount expects, so HTTP,
CLI and MCP all reach the same use case with the same authority checks,
idempotency semantics and typed errors. Writes store a canonical request digest
and a typed receipt in the same transaction as their effects; reads never mutate
and never widen the caller's scope.
"""

from __future__ import annotations

import uuid
from typing import Any, TypeVar

import asyncpg

from ..receipts import begin_command, commit_receipt, request_digest
from ..store import ApiProblem, ScopeContext
from . import coverage, formats, parsers, queue, repository
from .contracts import EXECUTOR_CORE
from .formats import detect_format, required_role_for
from .models import (
    ActivateGenerationRequest,
    BackfillRequest,
    CancelJobRequest,
    CoverageRequest,
    CreateEmbeddingSpaceRequest,
    ListJobsRequest,
)

Model = TypeVar("Model")

PROVIDER_FAMILIES = ("hash-local", "openai-compatible", "ollama")

FAILURE_MESSAGES = {
    "profile_not_found": "The processing profile is unavailable or retired.",
    "space_not_found": "The embedding space is unavailable.",
    "generation_not_found": "The index generation is unavailable for this space.",
    "no_active_generation": (
        "The space has no active generation; build one or activate a built generation."
    ),
    "generation_state_conflict": "The generation is not in the expected state.",
    "chunking_policy_mismatch": (
        "The profile's chunking policy does not match the space's; incompatible "
        "chunk policies never share a space."
    ),
    "space_name_exists": "An embedding space with that name already exists.",
    "invalid_space_definition": "The embedding space definition was rejected.",
    "installation_owner_required": "Only an installation owner may change space routing.",
    "owner_authority_required": "This operation requires installation owner authority.",
    "job_not_found": "The job is unavailable in the selected scope.",
    "job_terminal": "The job already reached a terminal state.",
    "budget_exhausted": "Processing capacity is reserved; retry later.",
    "queue_admission_full": "The durable queue for this scope is at its bound.",
    "unsupported_job_kind": "That job kind is not part of the versioned contract.",
    "job_intent_invalid": "The job intent does not pin what its kind requires.",
    "storage_conflict": "The database rejected this processing command.",
}

#: SQLSTATE -> (status, code). Mirrors W1's translation tables.
FAILURE_SQLSTATES: dict[str, tuple[int, str]] = {
    "23514": (422, "invalid_space_definition"),
    "23505": (409, "space_name_exists"),
    "23503": (409, "storage_conflict"),
    "22023": (404, "generation_not_found"),
    "28000": (403, "installation_owner_required"),
    "55000": (409, "storage_conflict"),
    "53400": (429, "budget_exhausted"),
}


def problem(code: str, status: int = 422, retryable: bool = False) -> ApiProblem:
    return ApiProblem(status, code, FAILURE_MESSAGES[code], retryable=retryable)


def translate(
    exc: asyncpg.PostgresError, *, owner_required: bool = False
) -> ApiProblem | None:
    if (
        owner_required
        and exc.sqlstate == "42501"
        and exc.message == "operation requires installation owner authority"
    ):
        return problem("owner_authority_required", 403)
    entry = FAILURE_SQLSTATES.get(exc.sqlstate or "")
    if entry is None:
        return None
    return problem(entry[1], entry[0], retryable=entry[0] == 429)


def _coerce(payload: Any, model: type[Model]) -> Model:
    """Accept either a validated model or a raw mapping from the transport."""
    if isinstance(payload, model):
        return payload
    if payload is None:
        return model()
    if isinstance(payload, dict):
        return model.model_validate(payload)
    raise problem("job_intent_invalid")


def _path_uuid(path_params: dict[str, Any] | None, key: str) -> uuid.UUID:
    value = (path_params or {}).get(key)
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise problem("job_not_found", 404) from exc


def _provider_family_known(provider: str) -> bool:
    try:
        from .embedding import PROVIDER_FAMILIES as configured
    except ImportError:
        configured = PROVIDER_FAMILIES
    return provider in tuple(configured)


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


async def backfill_jobs(
    connection: asyncpg.Connection,
    context: ScopeContext,
    idempotency_key: str,
    payload: Any,
    path_params: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any], bool]:
    """Enqueue a bounded, resumable batch of intended revisions (R13)."""
    request = _coerce(payload, BackfillRequest)
    operation = "processing.backfill"
    digest = request_digest(
        {
            "operation": operation,
            "scope_id": str(context.selected.scope_id),
            "profile_id": str(request.profile_id),
            "space_id": str(request.space_id),
            "generation": request.generation,
            "stage": request.stage,
            "then_embed": request.then_embed,
            "limit": request.limit,
            "priority": request.priority,
            "cursor": request.cursor,
        }
    )
    previous, replayed = await begin_command(
        connection,
        principal_id=context.principal.principal_id,
        operation=operation,
        idempotency_key=idempotency_key,
        digest=digest,
        scope_id=context.selected.scope_id,
    )
    if replayed and previous is not None:
        return 200, previous, True

    profile = await repository.get_profile(connection, request.profile_id)
    if profile is None or profile["status"] != "active":
        raise problem("profile_not_found", 404)
    space = await repository.get_space(connection, request.space_id)
    if space is None:
        raise problem("space_not_found", 404)
    profile_chunking = repository.chunking_identity(profile["chunking"])
    space_chunking = repository.chunking_identity(space["chunking"])
    if profile_chunking and space_chunking and profile_chunking != space_chunking:
        raise problem("chunking_policy_mismatch", 409)

    generation_id, generation_number, generation_state = await _resolve_generation(
        connection, context, space, request.generation, request.profile_id
    )

    policy = profile["coverage"]
    content_classes = [str(item) for item in policy.get("content_classes") or ()]
    min_text_length = int(policy.get("min_text_length") or 0)
    cursor = queue.parse_cursor(request.cursor)
    revisions = await repository.intended_revisions(
        connection,
        scope_id=context.selected.scope_id,
        content_classes=content_classes,
        min_text_length=min_text_length,
        cursor=cursor,
        limit=request.limit,
    )
    batch_id = uuid.uuid4()
    intents = [
        _backfill_intent(
            revision,
            request=request,
            profile=profile,
            space_id=request.space_id,
            generation_id=generation_id,
            batch_id=batch_id,
        )
        for revision in revisions
    ]
    result: queue.EnqueueResult | None = None
    try:
        result = await queue.enqueue_jobs(
            connection,
            context=context,
            intents=intents,
            reserve_budget=request.reserve_budget,
        )
    except asyncpg.PostgresError as exc:
        translated = translate(exc)
        if translated is not None:
            raise translated from exc
        raise

    next_cursor = None
    if revisions and len(revisions) == request.limit:
        last = revisions[-1]
        next_cursor = f"{last['created_at']}:{last['content_id']}"
    receipt = {
        "state": "pending_processing",
        "operation": operation,
        "batch_id": str(batch_id),
        "profile_identity": profile["profile_identity"],
        "space_id": str(request.space_id),
        "generation_id": str(generation_id),
        "generation": generation_number,
        "generation_state": generation_state,
        "scanned": len(revisions),
        "accepted": result.accepted if result else 0,
        "already_durable": (
            sum(1 for job in result.enqueued if not job.created) if result else 0
        ),
        "deferred": [
            {
                "dedupe_key": item.dedupe_key,
                "reason": item.reason,
                "detail": item.detail,
                "content_id": str(item.content_id) if item.content_id else None,
                "source_revision": item.source_revision,
            }
            for item in (result.deferred if result else ())
        ],
        "jobs": [
            {
                "job_id": str(job.job_id),
                "created": job.created,
                "status": job.status,
            }
            for job in (result.enqueued if result else ())
        ],
        "next_cursor": next_cursor,
        "resume_hint": (
            "Repeat this command with next_cursor to continue; already-enqueued "
            "revisions collapse onto their existing durable job."
            if next_cursor
            else "The intended selection is exhausted for this cursor window."
        ),
    }
    await commit_receipt(
        connection,
        principal_id=context.principal.principal_id,
        operation=operation,
        idempotency_key=idempotency_key,
        digest=digest,
        receipt_kind="pending_processing",
        receipt=receipt,
        scope_id=context.selected.scope_id,
    )
    return 202, receipt, False


async def _resolve_generation(
    connection: asyncpg.Connection,
    context: ScopeContext,
    space: dict[str, Any],
    selection: str,
    profile_id: uuid.UUID,
) -> tuple[uuid.UUID, int, str]:
    if selection == "new":
        try:
            created = await repository.begin_generation(
                connection,
                principal_id=context.principal.principal_id,
                space_id=uuid.UUID(space["space_id"]),
                profile_id=profile_id,
            )
        except asyncpg.PostgresError as exc:
            translated = translate(exc)
            if translated is not None:
                raise translated from exc
            raise
        return (
            uuid.UUID(created["generation_id"]),
            int(created["generation"]),
            created["generation_state"],
        )
    if not space["active_generation_id"]:
        raise problem("no_active_generation", 409)
    return (
        uuid.UUID(space["active_generation_id"]),
        int(space["active_generation"] or 0),
        str(space["active_state"] or "active"),
    )


def _backfill_intent(
    revision: dict[str, Any],
    *,
    request: BackfillRequest,
    profile: dict[str, Any],
    space_id: uuid.UUID,
    generation_id: uuid.UUID,
    batch_id: uuid.UUID,
) -> queue.JobIntent:
    """Pick the stage whose executor role can actually serve this revision."""
    payload = revision["payload"]
    media_type = payload.get("media_type") if isinstance(payload, dict) else None
    filename = payload.get("filename") if isinstance(payload, dict) else None
    detection = detect_format(
        media_type=media_type if isinstance(media_type, str) else None,
        filename=filename if isinstance(filename, str) else None,
    )
    required_role = required_role_for(detection.format_id)
    stage = request.stage
    if stage == "auto":
        stage = (
            "embed.chunks"
            if required_role == EXECUTOR_CORE and detection.format_id
            else "doc.extract"
        )
    if stage == "embed.chunks":
        required_role = EXECUTOR_CORE
    intent_payload: dict[str, Any] = {
        "media_type": media_type if isinstance(media_type, str) else None,
        "filename": filename if isinstance(filename, str) else None,
        "location": payload.get("location") if isinstance(payload, dict) else None,
        "detected_format": detection.format_id,
        "detected_by": detection.detected_by,
        "batch_id": str(batch_id),
        "profile_identity": profile["profile_identity"],
    }
    if stage == "doc.extract":
        intent_payload["then_embed"] = request.then_embed
        intent_payload["priority"] = request.priority
    return queue.JobIntent(
        job_kind=stage,
        # scope_id stays implicit: the intent is enqueued in the selected write scope.
        content_id=revision["content_id"],
        source_revision=int(revision["revision"]),
        profile_id=request.profile_id,
        space_id=space_id,
        generation_id=generation_id,
        batch_id=batch_id,
        payload=intent_payload,
        required_role=required_role,
        priority=request.priority,
    )


async def cancel_job(
    connection: asyncpg.Connection,
    context: ScopeContext,
    idempotency_key: str,
    payload: Any,
    path_params: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any], bool]:
    """Cancel or request cancellation of one durable job."""
    request = _coerce(payload, CancelJobRequest)
    job_id = _path_uuid(path_params, "job_id")
    operation = "processing.jobs.cancel"
    digest = request_digest(
        {
            "operation": operation,
            "scope_id": str(context.selected.scope_id),
            "job_id": str(job_id),
            "reason": request.reason,
        }
    )
    previous, replayed = await begin_command(
        connection,
        principal_id=context.principal.principal_id,
        operation=operation,
        idempotency_key=idempotency_key,
        digest=digest,
        scope_id=context.selected.scope_id,
    )
    if replayed and previous is not None:
        return 200, previous, True
    try:
        result = await queue.cancel_job(
            connection,
            scope_id=context.selected.scope_id,
            job_id=job_id,
            reason=request.reason,
        )
    except asyncpg.PostgresError as exc:
        translated = translate(exc)
        if translated is not None:
            raise translated from exc
        raise
    receipt = {
        "state": (
            "committed" if result["state"] == "cancelled" else "pending_processing"
        ),
        "operation": operation,
        **result,
    }
    await commit_receipt(
        connection,
        principal_id=context.principal.principal_id,
        operation=operation,
        idempotency_key=idempotency_key,
        digest=digest,
        receipt_kind=receipt["state"],
        receipt=receipt,
        scope_id=context.selected.scope_id,
    )
    return 200, receipt, False


async def create_embedding_space(
    connection: asyncpg.Connection,
    context: ScopeContext,
    idempotency_key: str,
    payload: Any,
    path_params: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any], bool]:
    """Create immutable space semantics plus its first generation."""
    request = _coerce(payload, CreateEmbeddingSpaceRequest)
    operation = "processing.embedding-spaces.create"
    digest = request_digest(
        {
            "operation": operation,
            "space_name": request.space_name,
            "provider": request.provider,
            "model_id": request.model_id,
            "model_revision": request.model_revision,
            "dimensions": request.dimensions,
            "normalization": request.normalization,
            "metric": request.metric,
            "query_prefix": request.query_prefix,
            "document_prefix": request.document_prefix,
            "chunking": request.chunking_block(),
        }
    )
    previous, replayed = await begin_command(
        connection,
        principal_id=context.principal.principal_id,
        operation=operation,
        idempotency_key=idempotency_key,
        digest=digest,
        scope_id=context.selected.scope_id,
    )
    if replayed and previous is not None:
        return 200, previous, True
    try:
        created = await repository.create_space(
            connection,
            principal_id=context.principal.principal_id,
            space_name=request.space_name,
            provider=request.provider,
            model_id=request.model_id,
            model_revision=request.model_revision,
            dimensions=request.dimensions,
            normalization=request.normalization,
            metric=request.metric,
            query_prefix=request.query_prefix,
            document_prefix=request.document_prefix,
            chunking=request.chunking_block(),
        )
    except asyncpg.PostgresError as exc:
        translated = translate(exc, owner_required=True)
        if translated is not None:
            raise translated from exc
        raise
    await repository.insert_outbox_event(
        connection,
        scope_id=context.selected.scope_id,
        aggregate_kind="embedding_space",
        aggregate_id=uuid.UUID(created["space_id"]),
        event_type="processing.space.created",
        aggregate_version=1,
        payload={
            "space_name": request.space_name,
            "provider": request.provider,
            "model_id": request.model_id,
            "dimensions": request.dimensions,
            "metric": request.metric,
            "normalization": request.normalization,
            "generation_id": created["generation_id"],
        },
    )
    receipt = {
        "state": "committed",
        "operation": operation,
        **created,
        "space_name": request.space_name,
        "provider": request.provider,
        "provider_family_known": _provider_family_known(request.provider),
        "model_id": request.model_id,
        "model_revision": request.model_revision,
        "dimensions": request.dimensions,
        "normalization": request.normalization,
        "metric": request.metric,
        "chunking": request.chunking_block(),
        "immutable": True,
        "guidance": (
            "A model, dimension, metric, normalization or chunking change is a new "
            "space; vectors of different spaces are never comparable."
        ),
    }
    await commit_receipt(
        connection,
        principal_id=context.principal.principal_id,
        operation=operation,
        idempotency_key=idempotency_key,
        digest=digest,
        receipt_kind="committed",
        receipt=receipt,
        scope_id=context.selected.scope_id,
    )
    return 201, receipt, False


async def activate_generation(
    connection: asyncpg.Connection,
    context: ScopeContext,
    idempotency_key: str,
    payload: Any,
    path_params: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any], bool]:
    """Atomically switch retrieval routing to one built generation."""
    request = _coerce(payload, ActivateGenerationRequest)
    space_id = _path_uuid(path_params, "space_id")
    generation_id = _path_uuid(path_params, "generation_id")
    operation = "processing.embedding-spaces.activate-generation"
    digest = request_digest(
        {
            "operation": operation,
            "space_id": str(space_id),
            "generation_id": str(generation_id),
            "expected_state": request.expected_state,
        }
    )
    previous, replayed = await begin_command(
        connection,
        principal_id=context.principal.principal_id,
        operation=operation,
        idempotency_key=idempotency_key,
        digest=digest,
        scope_id=context.selected.scope_id,
    )
    if replayed and previous is not None:
        return 200, previous, True
    generations = await repository.list_generations(connection, space_id)
    current = next(
        (item for item in generations if item["generation_id"] == str(generation_id)),
        None,
    )
    if current is None:
        raise problem("generation_not_found", 404)
    if request.expected_state and current["state"] != request.expected_state:
        raise problem("generation_state_conflict", 409)
    try:
        activated = await repository.activate_generation(
            connection,
            principal_id=context.principal.principal_id,
            space_id=space_id,
            generation_id=generation_id,
        )
    except asyncpg.PostgresError as exc:
        translated = translate(exc, owner_required=True)
        if translated is not None:
            raise translated from exc
        raise
    await repository.insert_outbox_event(
        connection,
        scope_id=context.selected.scope_id,
        aggregate_kind="index_generation",
        aggregate_id=generation_id,
        event_type="processing.generation.activated",
        aggregate_version=int(activated["generation"]),
        payload={
            "space_id": str(space_id),
            "generation": activated["generation"],
            "previous_state": current["state"],
        },
    )
    receipt = {
        "state": "committed",
        "operation": operation,
        **activated,
        "previous_state": current["state"],
        "routing": "retrieval now reads this generation only",
    }
    await commit_receipt(
        connection,
        principal_id=context.principal.principal_id,
        operation=operation,
        idempotency_key=idempotency_key,
        digest=digest,
        receipt_kind="committed",
        receipt=receipt,
        scope_id=context.selected.scope_id,
    )
    return 200, receipt, False


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


async def job_status(
    connection: asyncpg.Connection,
    context: ScopeContext,
    payload: Any,
    path_params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One job with its attempts, reservations and quarantine entries."""
    job_id = _path_uuid(path_params, "job_id")
    job = await queue.read_job(
        connection, scope_id=context.selected.scope_id, job_id=job_id
    )
    if job is None:
        raise problem("job_not_found", 404)
    job["published"] = await _published_effects(
        connection, context=context, job=job
    )
    return job


async def _published_effects(
    connection: asyncpg.Connection, *, context: ScopeContext, job: dict[str, Any]
) -> dict[str, Any]:
    """What actually exists for this job: a stored artifact is not proof of search."""
    effects: dict[str, Any] = {"chunks": 0, "vectors": 0, "distillations": 0}
    if not job.get("content_id"):
        return effects
    content_id = uuid.UUID(job["content_id"])
    revision = int(job["source_revision"])
    if job.get("space_id"):
        effects["chunks"] = int(
            await connection.fetchval(
                """
                SELECT count(*)
                  FROM cortex_processing.chunk_revisions
                 WHERE scope_id = $1 AND content_id = $2 AND revision = $3
                   AND space_id = $4
                """,
                context.selected.scope_id,
                content_id,
                revision,
                uuid.UUID(job["space_id"]),
            )
        )
    if job.get("generation_id"):
        effects["vectors"] = int(
            await connection.fetchval(
                """
                SELECT count(*)
                  FROM cortex_processing.chunk_vectors
                 WHERE scope_id = $1 AND content_id = $2 AND revision = $3
                   AND generation_id = $4
                """,
                context.selected.scope_id,
                content_id,
                revision,
                uuid.UUID(job["generation_id"]),
            )
        )
    effects["distillations"] = int(
        await connection.fetchval(
            """
            SELECT count(*)
              FROM cortex_processing.distillations
             WHERE scope_id = $1 AND content_id = $2 AND revision = $3
            """,
            context.selected.scope_id,
            content_id,
            revision,
        )
    )
    return effects


async def list_jobs(
    connection: asyncpg.Connection,
    context: ScopeContext,
    payload: Any,
    path_params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Keyset-paginated jobs across the caller's readable scopes."""
    request = _coerce(payload, ListJobsRequest)
    cursor = queue.parse_cursor(request.cursor)
    scope_ids = [scope.scope_id for scope in context.read_scopes]
    jobs, next_cursor = await queue.list_jobs(
        connection,
        scope_ids=scope_ids,
        status=request.status,
        job_kind=request.job_kind,
        required_role=request.required_role,
        batch_id=request.batch_id,
        cursor=cursor,
        limit=request.limit,
    )
    response: dict[str, Any] = {
        "jobs": jobs,
        "limit": request.limit,
        "next_cursor": next_cursor,
        "read_scopes": [scope.alias for scope in context.read_scopes],
    }
    if request.include_summary:
        response["queue"] = await queue.queue_summary(
            connection, scope_id=context.selected.scope_id
        )
    return response


async def coverage_report(
    connection: asyncpg.Connection,
    context: ScopeContext,
    payload: Any,
    path_params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Intended coverage for one profile, space and generation."""
    request = _coerce(payload, CoverageRequest)
    profile = await repository.get_profile(connection, request.profile_id)
    if profile is None or profile["status"] != "active":
        raise problem("profile_not_found", 404)
    if request.space_id is not None:
        space = await repository.get_space(connection, request.space_id)
        if space is None:
            raise problem("space_not_found", 404)
    return await coverage.measure(
        connection,
        scope_id=context.selected.scope_id,
        profile=profile,
        space_id=request.space_id,
        generation_id=request.generation_id,
    )


async def list_embedding_spaces(
    connection: asyncpg.Connection,
    context: ScopeContext,
    payload: Any,
    path_params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    spaces = await repository.list_spaces(connection)
    detailed: list[dict[str, Any]] = []
    for space in spaces:
        generations = await repository.list_generations(
            connection, uuid.UUID(space["space_id"])
        )
        detailed.append({**space, "generations": generations})
    return {
        "spaces": detailed,
        "guidance": (
            "Spaces are immutable model semantics. Vectors of different spaces are "
            "never comparable, and only the active generation is retrievable."
        ),
    }


async def list_processing_profiles(
    connection: asyncpg.Connection,
    context: ScopeContext,
    payload: Any,
    path_params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    profiles = await repository.list_profiles(
        connection, installation_id=context.principal.installation_id
    )
    return {
        "profiles": profiles,
        "guidance": (
            "A profile pins extractor, chunker, embedder selection, optional "
            "transforms and the intended-coverage selection. Transforms are "
            "disabled unless a versioned profile enables them."
        ),
    }


async def doc_formats(
    connection: asyncpg.Connection,
    context: ScopeContext,
    payload: Any,
    path_params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Discovery: the tested format table, limits and which roles are live now."""
    table = formats.discovery_table()
    workers = await repository.live_workers(
        connection, max_age_seconds=queue.WORKER_HEARTBEAT_STALE_SECONDS
    )
    live_roles = {role for worker in workers for role in worker["executor_roles"]}
    available: list[str] = []
    waiting: list[str] = []
    for entry in table["formats"]:
        format_id = str(entry["format_id"])
        required = required_role_for(format_id)
        if not entry["executor_roles"]:
            continue
        if required in live_roles:
            available.append(format_id)
        else:
            waiting.append(format_id)
    return {
        **table,
        "formats_available_now": sorted(available),
        "formats_waiting_for_executor": sorted(waiting),
        "live_executor_roles": sorted(live_roles),
        "workers": [
            {
                "worker_id": worker["worker_id"],
                "worker_role": worker["worker_role"],
                "executor_roles": list(worker["executor_roles"]),
                "handler_kinds": list(worker["handler_kinds"]),
                "package_version": worker["package_version"],
                "last_heartbeat_at": worker["last_heartbeat_at"],
            }
            for worker in workers
        ],
        "api_image_adapters": parsers.registry_report(),
    }


__all__ = [
    "activate_generation",
    "backfill_jobs",
    "cancel_job",
    "coverage_report",
    "create_embedding_space",
    "doc_formats",
    "job_status",
    "list_embedding_spaces",
    "list_jobs",
    "list_processing_profiles",
    "problem",
    "translate",
]
