"""Built-in job handlers and their fenced publishers.

A handler executes with no database transaction open and returns a typed
:class:`~cortex_v2.processing.contracts.ExecutionReport`; its publisher runs
inside the fenced publish transaction, after the queue has re-checked epoch,
lease owner, lease expiry, cancellation and — for content work — that the pinned
source revision is still the current one. That ordering is what makes "a stale
attempt can never publish" true instead of hoped for (F06).

Kinds implemented here:

``doc.extract``       parse one revision into chunk revisions with provenance
``embed.chunks``      embed published (or freshly parsed) chunks into a generation
``transform.distill`` optional R18 extractive distillation, disabled by default
``transform.compact`` optional R18 near-duplicate compaction, disabled by default

None of them writes canonical content: Processing derives projections and never
edits original memory based on model output.
"""

from __future__ import annotations

import math
import time
import uuid
from dataclasses import dataclass, replace
from typing import Any, Sequence

import asyncpg

from ..store import Principal, Scope, ScopeContext
from . import chunking, distillation, keys, queue, repository
from .contracts import (
    EXECUTOR_CORE,
    JOB_DOC_EXTRACT,
    JOB_EMBED_CHUNKS,
    JOB_TRANSFORM_COMPACT,
    JOB_TRANSFORM_DISTILL,
    ROLE_DOC,
    ROLE_EMBED,
    AttemptContext,
    AttemptServices,
    Chunk,
    ChunkingPolicy,
    ExecutionReport,
    Outcome,
    ParseResult,
    SourceRef,
    SpaceContract,
    StoredChunk,
)
from .registry import JobRegistry

#: Batch pause guard: an attempt that runs past its deadline stops cleanly and
#: is retried later instead of holding a lease it can no longer renew.
DETAIL_DEADLINE = "attempt deadline exceeded before the next batch"


def _report(outcome: Outcome, **kwargs: Any) -> ExecutionReport:
    return ExecutionReport(outcome=outcome, **kwargs)


def _remaining(context: AttemptContext) -> float:
    return max(0.0, context.deadline - time.monotonic())


def _from_parse(parsed: ParseResult, outcome: Outcome, **extra: Any) -> ExecutionReport:
    """Carry a parse result's provenance into a report; ``extra`` wins.

    Callers refine ``detail`` (for example when the pinned profile rejects the
    detected format), so the overrides are applied after the parsed defaults
    instead of colliding with them.
    """
    fields: dict[str, Any] = {
        "executor": parsed.executor,
        "format_id": parsed.format_id,
        "detected_by": parsed.detected_by,
        "required_role": parsed.required_role,
        "detail": parsed.detail,
        "warnings": parsed.warnings,
        "truncated": parsed.truncated,
        "stats": dict(parsed.stats),
    }
    fields.update(extra)
    return _report(outcome, **fields)


async def _load_source(context: AttemptContext, services: AttemptServices) -> SourceRef:
    source = await services.read_source(context)
    if source is None:
        raise queue.SourceMoved("source_missing")
    return source


async def _with_bytes(
    source: SourceRef, services: AttemptServices
) -> tuple[SourceRef, ExecutionReport | None]:
    """Attach referenced original bytes when the revision points at a blob.

    A missing blob resolver is not an error here: text formats parse from the
    preserved ``body_text``, and a binary format produces its own typed
    ``source_bytes_unavailable`` outcome inside the dispatcher. Integrity
    failures and quota breaches are reported immediately.
    """
    location = source.payload.get("location")
    if not isinstance(location, str) or not location:
        return source, None
    result = await services.read_bytes(source)
    if result.ok and result.data is not None:
        return (
            replace(source, body_bytes=result.data, size_bytes=len(result.data)),
            None,
        )
    if result.outcome is Outcome.SOURCE_BYTES_UNAVAILABLE:
        return source, None
    return source, _report(result.outcome, detail=result.detail)


def _allowed_formats(profile: dict[str, Any] | None) -> tuple[str, ...]:
    if not profile:
        return ("*",)
    formats = profile.get("parser", {}).get("formats")
    if not isinstance(formats, list) or not formats:
        return ("*",)
    return tuple(str(item) for item in formats)


def _transform_spec(
    profile: dict[str, Any] | None, kind: str, requested: dict[str, Any]
) -> tuple[distillation.TransformSpec | None, str | None]:
    """Resolve the pinned transform from the immutable profile (R18)."""
    if not profile:
        return None, "the job does not pin a profile"
    declared_name = requested.get("name")
    for entry in profile.get("transforms") or ():
        if not isinstance(entry, dict) or entry.get("kind") != kind:
            continue
        if declared_name and entry.get("name") != declared_name:
            continue
        try:
            return distillation.spec_from_json(entry), None
        except ValueError as exc:
            return None, f"the profile transform is invalid: {exc}"
    return None, f"the pinned profile declares no {kind} transform"


async def _verify_source(connection: asyncpg.Connection, context: AttemptContext) -> None:
    """Completion-time source recheck, inside the publish transaction (§5)."""
    if context.content_id is None or context.source_revision is None:
        return
    reason = await repository.verify_source_current(
        connection,
        scope_id=context.scope_id,
        content_id=context.content_id,
        revision=context.source_revision,
    )
    if reason is not None:
        raise queue.SourceMoved(reason)


def _worker_scope_context(context: AttemptContext) -> ScopeContext:
    """Write context for chained work intents published by this worker."""
    scope = Scope(
        alias="",
        scope_id=context.scope_id,
        kind="project",
        can_read=True,
        can_write=True,
        can_publish=True,
    )
    return ScopeContext(
        Principal(context.principal_id, context.installation_id), scope, (scope,)
    )


# ---------------------------------------------------------------------------
# doc.extract
# ---------------------------------------------------------------------------


async def doc_extract(
    context: AttemptContext, services: AttemptServices
) -> ExecutionReport:
    """Parse one canonical revision into chunk revisions with exact provenance."""
    if context.space_id is None:
        return _report(
            Outcome.CONFIGURATION_MISSING,
            detail="doc.extract requires a pinned embedding space",
        )
    policy = await services.read_chunking_policy(context.space_id)
    if policy is None:
        return _report(
            Outcome.SPACE_MISMATCH, detail="the pinned embedding space is unavailable"
        )
    profile = (
        await services.read_profile(context.profile_id) if context.profile_id else None
    )
    source = await _load_source(context, services)
    source, failure = await _with_bytes(source, services)
    if failure is not None:
        return failure
    head = source.body_bytes[:64] if source.body_bytes else None
    parsed = await services.parse(source, head=head)
    if not parsed.ok:
        return _from_parse(parsed, parsed.outcome)
    if parsed.format_id not in _allowed_formats(profile):
        return _from_parse(
            parsed,
            Outcome.UNSUPPORTED_FORMAT,
            detail=(
                f"the pinned profile accepts {', '.join(_allowed_formats(profile))}, "
                f"not {parsed.format_id}"
            ),
        )
    if not parsed.blocks:
        return _from_parse(parsed, Outcome.INPUT_EMPTY)
    chunks = await _chunk(services, parsed.blocks, policy)
    if isinstance(chunks, ExecutionReport):
        return chunks
    return _from_parse(
        parsed,
        Outcome.OK,
        chunks=chunks,
        stats={
            **dict(parsed.stats),
            "blocks": len(parsed.blocks),
            "chunks": len(chunks),
            "source_chars": len(source.body_text),
        },
    )


async def _chunk(
    services: AttemptServices, blocks: Sequence[Any], policy: ChunkingPolicy
) -> tuple[Chunk, ...] | ExecutionReport:
    try:
        return await services.run_blocking(chunking.chunk_blocks, blocks, policy)
    except ValueError as exc:
        return _report(
            Outcome.CONFIGURATION_MISSING, detail=f"the chunking policy is invalid: {exc}"
        )


async def publish_chunk_revisions(
    connection: asyncpg.Connection, context: AttemptContext, report: ExecutionReport
) -> None:
    """Write chunk revisions and chain the embedding intent durably."""
    await _verify_source(connection, context)
    if not report.chunks or context.space_id is None:
        return
    space_chunking = await repository.read_space_chunking(connection, context.space_id)
    if not space_chunking:
        raise queue.SourceMoved("space_missing")
    policy_identity = f"{space_chunking['policy_id']}@{space_chunking['version']}"
    rows = [
        {
            "scope_id": context.scope_id,
            "content_id": context.content_id,
            "revision": context.source_revision,
            "space_id": context.space_id,
            "ordinal": chunk.ordinal,
            "chunk_id": keys.chunk_key(
                scope_id=context.scope_id,
                content_id=context.content_id,
                revision=context.source_revision,
                space_id=context.space_id,
                ordinal=chunk.ordinal,
                policy_identity=policy_identity,
            ),
            "chunking_policy": policy_identity,
            "text_sha256": chunk.text_sha256,
            "text": chunk.text,
            "span": chunk.span.as_dict(),
            "block_spans": [span.as_dict() for span in chunk.block_spans],
            "block_kinds": list(chunk.block_kinds),
            "heading_path": list(chunk.heading_path),
            "format_id": report.format_id,
            "executor": report.executor,
            "job_id": context.job_id,
            "truncated": report.truncated,
            "warnings": list(report.warnings),
        }
        for chunk in report.chunks
    ]
    await repository.insert_chunks(connection, rows)

    payload = context.intent.get("payload") if isinstance(context.intent, dict) else None
    payload = payload if isinstance(payload, dict) else {}
    if payload.get("then_embed") and context.generation_id is not None:
        await queue.enqueue_jobs(
            connection,
            context=_worker_scope_context(context),
            intents=(
                queue.JobIntent(
                    job_kind=JOB_EMBED_CHUNKS,
                    scope_id=context.scope_id,
                    content_id=context.content_id,
                    source_revision=context.source_revision,
                    profile_id=context.profile_id,
                    space_id=context.space_id,
                    generation_id=context.generation_id,
                    batch_id=_intent_uuid(context, "batch_id"),
                    required_role=EXECUTOR_CORE,
                    priority=int(payload.get("priority", 100)),
                    payload={
                        "origin_job_id": str(context.job_id),
                        "origin_kind": context.job_kind,
                        "profile_identity": context.intent.get("profile_identity"),
                    },
                ),
            ),
        )


def _intent_uuid(context: AttemptContext, key: str) -> uuid.UUID | None:
    value = context.intent.get(key) if isinstance(context.intent, dict) else None
    if isinstance(value, str):
        try:
            return uuid.UUID(value)
        except ValueError:
            return None
    return value if isinstance(value, uuid.UUID) else None


# ---------------------------------------------------------------------------
# embed.chunks
# ---------------------------------------------------------------------------


async def embed_chunks(
    context: AttemptContext, services: AttemptServices
) -> ExecutionReport:
    """Embed the chunks of one revision into one index generation."""
    if context.space_id is None or context.generation_id is None:
        return _report(
            Outcome.CONFIGURATION_MISSING,
            detail="embed.chunks requires a pinned space and index generation",
        )
    loaded = await services.read_space_generation(
        context.space_id, context.generation_id
    )
    if loaded is None:
        return _report(
            Outcome.SPACE_MISMATCH,
            detail="the pinned space or generation is unavailable",
        )
    space, generation_id, generation_state = loaded
    if generation_state in ("retired", "failed"):
        return _report(
            Outcome.SPACE_MISMATCH,
            detail=f"generation {generation_state}; a retired generation accepts no vectors",
        )
    profile = (
        await services.read_profile(context.profile_id) if context.profile_id else None
    )
    mismatch = _chunking_mismatch(profile, space)
    if mismatch is not None:
        return _report(Outcome.SPACE_MISMATCH, detail=mismatch)

    inputs = await _chunks_to_embed(context, services, space)
    if inputs.failure is not None:
        return inputs.failure
    chunks, chunk_ids, texts, provenance = (
        inputs.chunks,
        inputs.chunk_ids,
        inputs.texts,
        inputs.provenance,
    )
    if not texts:
        return _report(
            Outcome.INPUT_EMPTY,
            detail="the revision produced no chunks to embed",
            stats={"chunks": 0},
        )

    embedder, reason = services.embedder_for(space)
    if embedder is None:
        return _report(
            Outcome.CONFIGURATION_MISSING,
            detail=f"no embedding provider is selectable: {reason}",
        )
    capabilities = embedder.capabilities()
    if capabilities.dimensions and space.dimensions not in capabilities.dimensions:
        return _report(
            Outcome.SPACE_MISMATCH,
            detail=(
                f"{capabilities.provider_id} does not serve {space.dimensions} dimensions"
            ),
        )
    vectors, outcome, detail, warnings, usage, model_revision = await _embed_batches(
        context,
        embedder,
        space,
        texts,
        max_batch=max(1, capabilities.max_batch),
        max_input_chars=capabilities.max_input_chars,
    )
    if outcome is not Outcome.OK:
        return _report(
            outcome,
            detail=detail,
            warnings=tuple(warnings),
            usage=dict(usage),
            model_revision=model_revision,
        )
    validated = _validate_vectors(space, chunk_ids, vectors)
    if validated is not None:
        return _report(
            validated[0],
            detail=validated[1],
            warnings=tuple(warnings),
            usage=dict(usage),
        )
    normalized, normalization_warnings = _normalize(space, vectors)
    return _report(
        Outcome.OK,
        chunks=chunks,
        vectors=normalized,
        vector_chunk_ids=tuple(chunk_ids),
        executor=provenance.executor,
        format_id=provenance.format_id,
        detected_by=provenance.detected_by,
        model_revision=model_revision,
        warnings=tuple((*warnings, *normalization_warnings)),
        usage=dict(usage),
        stats={
            "chunks": len(chunk_ids),
            "vectors": len(normalized),
            "dimensions": space.dimensions,
            "batches": math.ceil(len(texts) / max(1, capabilities.max_batch)),
        },
    )


def _chunking_mismatch(profile: dict[str, Any] | None, space: SpaceContract) -> str | None:
    """A profile and a space must agree on the chunking policy identity.

    Two chunk policies can coexist while a rebuild runs, but never inside one
    space: the space pins the policy its chunk ids were derived from (F07).
    """
    if not profile:
        return None
    profile_identity = repository.chunking_identity(profile.get("chunking"))
    if profile_identity is None or space.chunking_identity is None:
        return None
    if profile_identity != space.chunking_identity:
        return (
            f"profile chunking {profile_identity} does not match space chunking "
            f"{space.chunking_identity}; incompatible chunk policies never share a space"
        )
    return None


@dataclass(frozen=True, slots=True)
class _ChunkInputs:
    """Chunks to embed, their stable ids, and where their provenance came from."""

    chunks: tuple[Chunk, ...]
    chunk_ids: tuple[uuid.UUID, ...]
    texts: tuple[str, ...]
    provenance: ExecutionReport
    failure: ExecutionReport | None = None


def _failed(report: ExecutionReport) -> _ChunkInputs:
    return _ChunkInputs((), (), (), report, report)


async def _chunks_to_embed(
    context: AttemptContext, services: AttemptServices, space: SpaceContract
) -> _ChunkInputs:
    """Published chunks when they exist; otherwise parse and chunk in-attempt."""
    stored: tuple[StoredChunk, ...] = await services.read_chunks(context, space.space_id)
    if stored:
        return _ChunkInputs(
            chunks=(),
            chunk_ids=tuple(chunk.chunk_id for chunk in stored),
            texts=tuple(chunk.text for chunk in stored),
            provenance=_report(
                Outcome.OK,
                stats={
                    "chunks": len(stored),
                    "reused_published_chunks": len(stored),
                },
            ),
        )
    policy = await services.read_chunking_policy(space.space_id)
    if policy is None:
        return _failed(
            _report(
                Outcome.SPACE_MISMATCH, detail="the space records no chunking policy"
            )
        )
    source = await _load_source(context, services)
    source, failure = await _with_bytes(source, services)
    if failure is not None:
        return _failed(failure)
    head = source.body_bytes[:64] if source.body_bytes else None
    parsed = await services.parse(source, head=head)
    if not parsed.ok:
        return _failed(_from_parse(parsed, parsed.outcome))
    if not parsed.blocks:
        return _failed(_from_parse(parsed, Outcome.INPUT_EMPTY))
    chunks = await _chunk(services, parsed.blocks, policy)
    if isinstance(chunks, ExecutionReport):
        return _failed(chunks)
    policy_identity = policy.identity
    chunk_ids = tuple(
        keys.chunk_key(
            scope_id=context.scope_id,
            content_id=context.content_id,
            revision=context.source_revision,
            space_id=space.space_id,
            ordinal=chunk.ordinal,
            policy_identity=policy_identity,
        )
        for chunk in chunks
    )
    return _ChunkInputs(
        chunks=chunks,
        chunk_ids=chunk_ids,
        texts=tuple(chunk.text for chunk in chunks),
        provenance=_report(
            Outcome.OK,
            executor=parsed.executor,
            format_id=parsed.format_id,
            detected_by=parsed.detected_by,
            warnings=parsed.warnings,
            truncated=parsed.truncated,
            stats=dict(parsed.stats),
        ),
    )


async def _embed_batches(
    context: AttemptContext,
    embedder: Any,
    space: SpaceContract,
    texts: Sequence[str],
    *,
    max_batch: int,
    max_input_chars: int,
) -> tuple[list[tuple[float, ...]], Outcome, str | None, list[str], dict[str, int], str | None]:
    """Bounded batch loop: no retry inside an attempt, deadline honoured."""
    vectors: list[tuple[float, ...]] = []
    warnings: list[str] = []
    usage: dict[str, int] = {}
    model_revision: str | None = None
    if max_input_chars > 0:
        over = sum(1 for text in texts if len(text) > max_input_chars)
        if over:
            warnings.append(f"{over} chunks exceed the provider input bound")
    for start in range(0, len(texts), max_batch):
        if _remaining(context) <= 0:
            return (
                vectors,
                Outcome.PROVIDER_TIMEOUT,
                DETAIL_DEADLINE,
                warnings,
                usage,
                model_revision,
            )
        batch = tuple(texts[start : start + max_batch])
        deadline = min(context.deadline, time.monotonic() + max(1.0, _remaining(context)))
        outcome = await embedder.embed_documents(space, batch, deadline=deadline)
        for key, value in outcome.usage.items():
            usage[key] = usage.get(key, 0) + int(value)
        model_revision = model_revision or outcome.model_revision
        warnings.extend(outcome.degraded)
        if not outcome.ok:
            return (
                vectors,
                outcome.outcome,
                outcome.detail,
                warnings,
                usage,
                model_revision,
            )
        if len(outcome.vectors) != len(batch):
            return (
                vectors,
                Outcome.OUTPUT_INVALID,
                (
                    f"provider returned {len(outcome.vectors)} vectors for "
                    f"{len(batch)} inputs; order and count must be preserved"
                ),
                warnings,
                usage,
                model_revision,
            )
        vectors.extend(tuple(float(value) for value in item) for item in outcome.vectors)
    return vectors, Outcome.OK, None, warnings, usage, model_revision


def _validate_vectors(
    space: SpaceContract, chunk_ids: Sequence[uuid.UUID], vectors: Sequence[Sequence[float]]
) -> tuple[Outcome, str] | None:
    if len(vectors) != len(chunk_ids):
        return (
            Outcome.OUTPUT_INVALID,
            f"{len(vectors)} vectors for {len(chunk_ids)} chunks",
        )
    for index, vector in enumerate(vectors):
        if len(vector) != space.dimensions:
            return (
                Outcome.PROVIDER_REJECTED,
                (
                    f"provider returned {len(vector)} dimensions for a "
                    f"{space.dimensions}-dimensional space; vectors are never "
                    "truncated or padded to fit"
                ),
            )
        if not all(math.isfinite(value) for value in vector):
            return (
                Outcome.OUTPUT_INVALID,
                f"vector {index} contains a non-finite component",
            )
    return None


def _normalize(
    space: SpaceContract, vectors: Sequence[Sequence[float]]
) -> tuple[tuple[tuple[float, ...], ...], tuple[str, ...]]:
    """Honour the space's declared normalization; report when it was applied."""
    if space.normalization != "l2":
        return tuple(tuple(float(value) for value in vector) for vector in vectors), ()
    normalized: list[tuple[float, ...]] = []
    adjusted = 0
    for vector in vectors:
        norm = math.sqrt(sum(float(value) ** 2 for value in vector))
        if norm == 0.0:
            normalized.append(tuple(float(value) for value in vector))
            adjusted += 1
            continue
        if abs(norm - 1.0) > 1e-6:
            adjusted += 1
            normalized.append(tuple(float(value) / norm for value in vector))
        else:
            normalized.append(tuple(float(value) for value in vector))
    warnings = ("l2_normalized",) if adjusted else ()
    return tuple(normalized), warnings


async def publish_vectors(
    connection: asyncpg.Connection, context: AttemptContext, report: ExecutionReport
) -> None:
    """Write chunk revisions (when this attempt produced them) and their vectors."""
    await _verify_source(connection, context)
    if context.space_id is None or context.generation_id is None:
        return
    loaded = await repository.read_space_contract(
        connection, context.space_id, generation_id=context.generation_id
    )
    if loaded is None:
        raise queue.SourceMoved("space_missing")
    space = loaded[0]
    if report.chunks:
        space_chunking = await repository.read_space_chunking(connection, context.space_id)
        policy_identity = (
            f"{space_chunking['policy_id']}@{space_chunking['version']}"
            if space_chunking
            else ""
        )
        await repository.insert_chunks(
            connection,
            [
                {
                    "scope_id": context.scope_id,
                    "content_id": context.content_id,
                    "revision": context.source_revision,
                    "space_id": context.space_id,
                    "ordinal": chunk.ordinal,
                    "chunk_id": keys.chunk_key(
                        scope_id=context.scope_id,
                        content_id=context.content_id,
                        revision=context.source_revision,
                        space_id=context.space_id,
                        ordinal=chunk.ordinal,
                        policy_identity=policy_identity,
                    ),
                    "chunking_policy": policy_identity,
                    "text_sha256": chunk.text_sha256,
                    "text": chunk.text,
                    "span": chunk.span.as_dict(),
                    "block_spans": [span.as_dict() for span in chunk.block_spans],
                    "block_kinds": list(chunk.block_kinds),
                    "heading_path": list(chunk.heading_path),
                    "format_id": report.format_id,
                    "executor": report.executor,
                    "job_id": context.job_id,
                    "truncated": report.truncated,
                    "warnings": list(report.warnings),
                }
                for chunk in report.chunks
            ],
        )
    if not report.vectors or not report.vector_chunk_ids:
        return
    await repository.insert_vectors(
        connection,
        [
            {
                "chunk_id": chunk_id,
                "generation_id": context.generation_id,
                "scope_id": context.scope_id,
                "space_id": context.space_id,
                "content_id": context.content_id,
                "revision": context.source_revision,
                "embedding": vector,
                "provider": space.provider,
                "model_id": space.model_id,
                "model_revision": report.model_revision or space.model_revision,
                "normalization": space.normalization,
                "job_id": context.job_id,
                "attempt_epoch": context.fencing_epoch,
            }
            for chunk_id, vector in zip(report.vector_chunk_ids, report.vectors)
        ],
    )


# ---------------------------------------------------------------------------
# transform.distill / transform.compact (R18)
# ---------------------------------------------------------------------------


async def transform_distill(
    context: AttemptContext, services: AttemptServices
) -> ExecutionReport:
    return await _transform(context, services, "distill")


async def transform_compact(
    context: AttemptContext, services: AttemptServices
) -> ExecutionReport:
    return await _transform(context, services, "compact")


async def _transform(
    context: AttemptContext, services: AttemptServices, kind: str
) -> ExecutionReport:
    profile = (
        await services.read_profile(context.profile_id) if context.profile_id else None
    )
    requested = context.intent.get("payload") if isinstance(context.intent, dict) else {}
    requested = requested if isinstance(requested, dict) else {}
    transform = requested.get("transform") if isinstance(requested.get("transform"), dict) else {}
    spec, reason = _transform_spec(profile, kind, transform)
    if spec is None:
        return _report(Outcome.CONFIGURATION_MISSING, detail=reason)
    if not spec.enabled:
        # R18: distillation and compaction stay off until an operator enables a
        # versioned profile and its quality gates have passed.
        return _report(
            Outcome.CONFIGURATION_MISSING,
            detail=f"transform_disabled: {spec.identity} is not enabled in its profile",
        )
    source = await _load_source(context, services)
    parsed = await services.parse(source)
    if not parsed.ok:
        return _from_parse(parsed, parsed.outcome)
    result = await services.run_blocking(
        distillation.distill if kind == "distill" else distillation.compact,
        source.body_text,
        parsed.blocks,
        spec,
    )
    if result.outcome is not Outcome.OK:
        return _report(
            result.outcome,
            detail=result.detail if hasattr(result, "detail") else None,
            warnings=tuple(getattr(result, "warnings", ()) or ()),
            stats=dict(getattr(result, "stats", {}) or {}),
        )
    return _report(
        Outcome.OK,
        distillation={
            "transform": kind,
            "transform_identity": spec.identity,
            "output_text": result.output_text,
            "output_payload": dict(result.output_payload or {}),
            "commitments": list(result.commitments or ()),
            "coverage": dict(result.coverage or {}),
            "lost_commitments": list(result.lost_commitments or ()),
        },
        executor=parsed.executor,
        format_id=parsed.format_id,
        detected_by=parsed.detected_by,
        warnings=tuple(getattr(result, "warnings", ()) or ()),
        stats=dict(getattr(result, "stats", {}) or {}),
    )


async def publish_distillation(
    connection: asyncpg.Connection, context: AttemptContext, report: ExecutionReport
) -> None:
    """Persist derived text as a new row; the original revision is untouched."""
    await _verify_source(connection, context)
    payload = report.distillation
    if not payload or context.content_id is None or context.profile_id is None:
        return
    output_text = str(payload.get("output_text") or "")
    if not output_text:
        return
    await repository.insert_distillation(
        connection,
        {
            "distillation_id": keys.distillation_key(
                scope_id=context.scope_id,
                content_id=context.content_id,
                revision=context.source_revision or 0,
                transform_identity=str(payload.get("transform_identity") or "transform@0"),
            ),
            "scope_id": context.scope_id,
            "content_id": context.content_id,
            "revision": context.source_revision,
            "transform_kind": str(payload.get("transform") or "distill"),
            "transform_identity": str(payload.get("transform_identity") or "transform@0"),
            "profile_id": context.profile_id,
            "output_text": output_text,
            "output_payload": dict(payload.get("output_payload") or {}),
            "commitments": list(payload.get("commitments") or ()),
            "coverage": dict(payload.get("coverage") or {}),
            "job_id": context.job_id,
        },
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register_builtin_handlers(registry: JobRegistry) -> None:
    """Register the kinds this package implements. Idempotent per process."""
    registry.register_job_handler(
        JOB_DOC_EXTRACT,
        ROLE_DOC,
        doc_extract,
        publisher=publish_chunk_revisions,
        summary=(
            "Parse one canonical content revision into chunk revisions with exact "
            "provenance spans. Use it for document formats (PDF, Office, HTML, "
            "EPUB, mail, YAML, XML, RTF) and for any revision whose chunks should "
            "exist before embedding. Do not use it to change canonical content: it "
            "only writes derived chunks."
        ),
    )
    registry.register_job_handler(
        JOB_EMBED_CHUNKS,
        ROLE_EMBED,
        embed_chunks,
        publisher=publish_vectors,
        summary=(
            "Embed the chunks of one pinned content revision into one index "
            "generation of one embedding space. Use it for selective embedding and "
            "backfill. Do not use it across spaces or generations: vectors never mix "
            "dimensions or model semantics."
        ),
    )
    registry.register_job_handler(
        JOB_TRANSFORM_DISTILL,
        ROLE_EMBED,
        transform_distill,
        publisher=publish_distillation,
        summary=(
            "Optional extractive distillation of one revision into a new derived "
            "row. Disabled unless a versioned profile enables it; the original is "
            "never altered and a lost commitment refuses publication."
        ),
        required_intent=("transform",),
    )
    registry.register_job_handler(
        JOB_TRANSFORM_COMPACT,
        ROLE_EMBED,
        transform_compact,
        publisher=publish_distillation,
        summary=(
            "Optional near-duplicate compaction of one revision into a new derived "
            "row. Disabled unless a versioned profile enables it; the original is "
            "never altered."
        ),
        required_intent=("transform",),
    )


__all__ = [
    "doc_extract",
    "embed_chunks",
    "publish_chunk_revisions",
    "publish_distillation",
    "publish_vectors",
    "register_builtin_handlers",
    "transform_compact",
    "transform_distill",
]
