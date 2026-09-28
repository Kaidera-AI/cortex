"""Retrieval operation registry (worker-API contract).

``OPERATIONS`` is the single metadata definition the integrator mounts
generically: dotted operation ids, HTTP method/path under ``/v1``, operation
kind, pydantic request model, handler and usage guidance ("when to use /
when not"). Handler signatures follow the integration contract exactly:

* ``scoped_write``: ``async (connection, context, idempotency_key, payload,
  path_params) -> (status, data, replayed)`` — a per-command idempotency
  receipt is stored in the same transaction as the effects.
* ``scoped_read``: ``async (connection, context, payload, path_params) ->
  dict`` — bounded reads that explain coverage and degradation.

Extraction never runs in API requests: ``graph.extract`` and
``code.publish-index`` durably enqueue ``graph.memory.extract`` /
``graph.code.extract`` jobs for the leased ``graph`` worker role and return
``pending_processing`` receipts.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

import asyncpg

from ..receipts import begin_command, commit_receipt, request_digest
from ..store import ApiProblem, ScopeContext
from . import code_graph, memory_graph
from .code_graph import INTERPRETATION, _graph_state
from .jobs import KIND_CODE_EXTRACT, KIND_MEMORY_EXTRACT
from .models import (
    CodeAnnotateRequest,
    CodeAssessChangeRequest,
    CodeBlastRadiusRequest,
    CodeCallersRequest,
    CodeHotspotsRequest,
    CodeImpactRequest,
    CodePublishIndexRequest,
    GraphExploreRequest,
    GraphExtractRequest,
    GraphRetractRequest,
    InspectHitRequest,
    MemorySearchRequest,
    RebuildProjectionsRequest,
)
from .planner import RetrievalPlanner

#: Default planner: rerank disabled, no embedder configured — the vector
#: stage then reports ``vectors_not_configured`` honestly instead of mixing
#: spaces. Provider wiring (query embedder over the processing embedding
#: port, optional reranker) is a deployment concern and replaces this
#: instance without touching the operations below.
DEFAULT_PLANNER = RetrievalPlanner()


def _read_scope_ids(context: ScopeContext) -> list[uuid.UUID]:
    return [scope.scope_id for scope in context.read_scopes]


async def _resolve_repository(
    connection: asyncpg.Connection, context: ScopeContext, repository_key: str
) -> dict[str, Any]:
    repository = await code_graph.resolve_repository(
        connection, scope_id=context.selected.scope_id, repository_key=repository_key
    )
    if repository is None:
        raise ApiProblem(
            404,
            "repository_not_found",
            "The code repository is unavailable in the selected scope.",
        )
    return repository


async def _enqueue_graph_job(
    connection: asyncpg.Connection,
    context: ScopeContext,
    *,
    kind: str,
    payload: dict[str, Any],
    dedupe_key: str,
) -> dict[str, Any]:
    """Durably enqueue inside the caller's command transaction (one commit
    for command + work intent). Typed 429/503 — never a false acceptance."""
    try:
        from ..processing.queue import JobIntent, enqueue_jobs
    except ImportError as exc:
        raise ApiProblem(
            503,
            "processing_unavailable",
            "The durable processing queue is not available in this build; "
            "graph extraction cannot be accepted.",
            retryable=True,
        ) from exc
    intent = JobIntent(
        job_kind=kind,
        scope_id=context.selected.scope_id,
        # Graph kinds pin their identity in payload["pin"]; the queue
        # contract requires the content columns to stay NULL for them.
        content_id=None,
        source_revision=None,
        profile_id=None,
        payload=payload,
        dedupe_key=dedupe_key,
        required_role="graph",
        requested_by_principal=context.principal.principal_id,
    )
    result = await enqueue_jobs(connection, context=context, intents=[intent])
    for deferred in getattr(result, "deferred", ()):
        if deferred.reason == "budget_exhausted":
            raise ApiProblem(
                429,
                "queue_budget_exhausted",
                "The processing budget is exhausted; retry later or lower "
                "the request rate.",
                retryable=True,
            )
        raise ApiProblem(
            429,
            "queue_admission_full",
            "The processing queue is not admitting new work right now.",
            retryable=True,
        )
    enqueued = result.enqueued[0]
    return {
        "job_id": str(enqueued.job_id),
        "job_created": bool(enqueued.created),
        "job_status": str(enqueued.status),
        "dedupe_key": enqueued.dedupe_key,
    }


# ---------------------------------------------------------------------------
# memory.search / memory.inspect-hit
# ---------------------------------------------------------------------------


async def memory_search(
    connection: asyncpg.Connection,
    context: ScopeContext,
    payload: MemorySearchRequest,
    path_params: dict,
) -> dict[str, Any]:
    return await DEFAULT_PLANNER.search(connection, context, payload)


async def memory_inspect_hit(
    connection: asyncpg.Connection,
    context: ScopeContext,
    payload: InspectHitRequest,
    path_params: dict,
) -> dict[str, Any]:
    return await DEFAULT_PLANNER.inspect_hit(connection, context, payload)


# ---------------------------------------------------------------------------
# graph.explore
# ---------------------------------------------------------------------------


async def graph_explore(
    connection: asyncpg.Connection,
    context: ScopeContext,
    payload: GraphExploreRequest,
    path_params: dict,
) -> dict[str, Any]:
    if payload.kind == "memory":
        return await memory_graph.explore_memory(
            connection,
            scope_ids=_read_scope_ids(context),
            entity_ids=payload.entity_ids or (),
            content_id=payload.content_id,
            relations=payload.relations or (),
            direction=payload.direction,
            hops=payload.hops,
            include_retracted=payload.include_retracted,
            limit=payload.limit,
        )
    repository = await _resolve_repository(
        connection, context, payload.repository_key or ""
    )
    resolved = await code_graph.resolve_generation(
        connection,
        scope_id=context.selected.scope_id,
        repository_id=repository["repository_id"],
        head_commit=payload.head_commit,
    )
    if resolved.generation is None:
        return {
            "kind": "code",
            "graph": _graph_state(None, "unavailable", payload.head_commit),
            "nodes": [],
            "edges": [],
            "unresolved_symbols": list(payload.symbol_keys or []),
            "degraded": ["code_graph_not_built"],
            "interpretation": INTERPRETATION,
        }
    return await code_graph.explore_code(
        connection,
        scope_id=context.selected.scope_id,
        repository_id=repository["repository_id"],
        generation_row=resolved.generation,
        graph_status=resolved.graph_status,
        symbol_keys=payload.symbol_keys or (),
        edge_kinds=payload.edge_kinds or (),
        direction=payload.direction,
        hops=payload.hops,
        limit=payload.limit,
        head_commit=payload.head_commit,
    )


# ---------------------------------------------------------------------------
# graph maintenance commands (enqueue / retract / rebuild)
# ---------------------------------------------------------------------------


async def graph_extract(
    connection: asyncpg.Connection,
    context: ScopeContext,
    idempotency_key: str,
    payload: GraphExtractRequest,
    path_params: dict,
) -> tuple[int, dict[str, Any], bool]:
    operation = "graph.extract"
    digest = request_digest(
        {
            "operation": operation,
            "scope_id": str(context.selected.scope_id),
            "content_id": str(payload.content_id),
            "revision": payload.revision,
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

    revision = payload.revision
    if revision is None:
        revision = await connection.fetchval(
            """
            SELECT max(latest.revision)
              FROM cortex_core.content_revisions AS latest
             WHERE latest.scope_id = $1
               AND latest.content_id = $2
            """,
            context.selected.scope_id,
            payload.content_id,
        )
        if revision is None:
            raise ApiProblem(
                404,
                "content_not_found",
                "The content is unavailable in the selected scope.",
            )
    job = await _enqueue_graph_job(
        connection,
        context,
        kind=KIND_MEMORY_EXTRACT,
        payload={
            "pin": {
                "content_id": str(payload.content_id),
                "revision": revision,
            },
            "extractor_profile": memory_graph.RuleExtractor.profile,
        },
        dedupe_key=(
            f"{KIND_MEMORY_EXTRACT}:{context.selected.scope_id}:"
            f"{payload.content_id}:{revision}:"
            f"{memory_graph.RuleExtractor.profile}"
        ),
    )
    receipt = {
        "state": "pending_processing",
        "operation": operation,
        "scope_id": str(context.selected.scope_id),
        "content_id": str(payload.content_id),
        "revision": revision,
        **job,
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


async def graph_retract_source(
    connection: asyncpg.Connection,
    context: ScopeContext,
    idempotency_key: str,
    payload: GraphRetractRequest,
    path_params: dict,
) -> tuple[int, dict[str, Any], bool]:
    operation = "graph.retract-source"
    digest = request_digest(
        {
            "operation": operation,
            "scope_id": str(context.selected.scope_id),
            "content_id": str(payload.content_id),
            "reason": payload.reason,
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
    stats = await memory_graph.retract_source(
        connection,
        scope_id=context.selected.scope_id,
        content_id=payload.content_id,
        reason=payload.reason,
    )
    receipt = {
        "state": "committed",
        "operation": operation,
        "scope_id": str(context.selected.scope_id),
        "content_id": str(payload.content_id),
        "reason": payload.reason,
        **stats,
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


async def memory_rebuild_projections(
    connection: asyncpg.Connection,
    context: ScopeContext,
    idempotency_key: str,
    payload: RebuildProjectionsRequest | None,
    path_params: dict,
) -> tuple[int, dict[str, Any], bool]:
    operation = "memory.rebuild-projections"
    digest = request_digest(
        {
            "operation": operation,
            "scope_id": str(context.selected.scope_id),
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
    document_count = await connection.fetchval(
        "SELECT cortex_retrieval.rebuild_trigram_projection($1)",
        context.selected.scope_id,
    )
    receipt = {
        "state": "committed",
        "operation": operation,
        "scope_id": str(context.selected.scope_id),
        "trigram_document_count": int(document_count or 0),
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
# code graph commands
# ---------------------------------------------------------------------------


async def code_publish_index(
    connection: asyncpg.Connection,
    context: ScopeContext,
    idempotency_key: str,
    payload: CodePublishIndexRequest,
    path_params: dict,
) -> tuple[int, dict[str, Any], bool]:
    operation = "code.publish-index"
    canonical_files = sorted(
        [
            item.path,
            hashlib.sha256(item.source.encode("utf-8")).hexdigest(),
        ]
        for item in payload.files
    )
    snapshot_id = hashlib.sha256(
        json.dumps(canonical_files, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:32]
    digest = request_digest(
        {
            "operation": operation,
            "scope_id": str(context.selected.scope_id),
            "repository_key": payload.repository_key,
            "commit_sha": payload.commit_sha,
            "snapshot_id": snapshot_id,
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
    job = await _enqueue_graph_job(
        connection,
        context,
        kind=KIND_CODE_EXTRACT,
        payload={
            "pin": {
                "repository_key": payload.repository_key,
                "commit_sha": payload.commit_sha,
                "snapshot_id": snapshot_id,
            },
            "files": [
                {"path": item.path, "source": item.source} for item in payload.files
            ],
        },
        dedupe_key=f"{KIND_CODE_EXTRACT}:{context.selected.scope_id}:{digest.hex()}",
    )
    receipt = {
        "state": "pending_processing",
        "operation": operation,
        "scope_id": str(context.selected.scope_id),
        "repository_key": payload.repository_key,
        "commit_sha": payload.commit_sha,
        "snapshot_id": snapshot_id,
        "file_count": len(payload.files),
        **job,
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


async def code_annotate(
    connection: asyncpg.Connection,
    context: ScopeContext,
    idempotency_key: str,
    payload: CodeAnnotateRequest,
    path_params: dict,
) -> tuple[int, dict[str, Any], bool]:
    operation = "code.annotate"
    digest = request_digest(
        {
            "operation": operation,
            "scope_id": str(context.selected.scope_id),
            "repository_key": payload.repository_key,
            "symbol_key": payload.symbol_key,
            "annotation": payload.annotation,
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
    repository = await _resolve_repository(connection, context, payload.repository_key)
    annotation_id = uuid.uuid4()
    created_at = await connection.fetchval(
        """
        INSERT INTO cortex_retrieval.code_annotations
            (scope_id, annotation_id, repository_id, symbol_key, annotation,
             author_principal_id)
        VALUES ($1, $2, $3, $4, $5, $6)
        RETURNING created_at
        """,
        context.selected.scope_id,
        annotation_id,
        repository["repository_id"],
        payload.symbol_key,
        payload.annotation,
        context.principal.principal_id,
    )
    receipt = {
        "state": "committed",
        "operation": operation,
        "scope_id": str(context.selected.scope_id),
        "annotation_id": str(annotation_id),
        "repository_key": payload.repository_key,
        "symbol_key": payload.symbol_key,
        "annotation": payload.annotation,
        "author_principal_id": str(context.principal.principal_id),
        "created_at": created_at.isoformat(),
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


# ---------------------------------------------------------------------------
# code graph reads
# ---------------------------------------------------------------------------


async def code_callers(
    connection: asyncpg.Connection,
    context: ScopeContext,
    payload: CodeCallersRequest,
    path_params: dict,
) -> dict[str, Any]:
    repository = await _resolve_repository(connection, context, payload.repository_key)
    resolved = await code_graph.resolve_generation(
        connection,
        scope_id=context.selected.scope_id,
        repository_id=repository["repository_id"],
        head_commit=payload.head_commit,
    )
    if resolved.generation is None:
        return {
            "repository_key": payload.repository_key,
            "symbol_key": payload.symbol_key,
            "graph": _graph_state(None, "unavailable", payload.head_commit),
            "callers": [],
            "unresolved_symbols": [payload.symbol_key],
            "degraded": ["code_graph_not_built"],
            "interpretation": INTERPRETATION,
        }
    result = await code_graph.callers_of_symbol(
        connection,
        scope_id=context.selected.scope_id,
        repository_id=repository["repository_id"],
        generation=resolved.generation["generation"],
        symbol_key=payload.symbol_key,
        limit=payload.limit,
        head_commit=payload.head_commit,
    )
    result["repository_key"] = payload.repository_key
    result["symbol_key"] = payload.symbol_key
    return result


async def _impact_inputs(
    connection: asyncpg.Connection,
    context: ScopeContext,
    payload: CodeImpactRequest | CodeBlastRadiusRequest,
):
    repository = await _resolve_repository(connection, context, payload.repository_key)
    resolved = await code_graph.resolve_generation(
        connection,
        scope_id=context.selected.scope_id,
        repository_id=repository["repository_id"],
        head_commit=payload.head_commit,
    )
    if resolved.generation is None:
        return repository, resolved, [], [], list(payload.changed_files or []), list(
            payload.changed_symbols or []
        )
    seeds = await code_graph.resolve_change_seeds(
        connection,
        scope_id=context.selected.scope_id,
        repository_id=repository["repository_id"],
        generation=resolved.generation["generation"],
        changed_files=payload.changed_files or [],
        changed_symbols=payload.changed_symbols or [],
    )
    return repository, resolved, *seeds


async def code_impact(
    connection: asyncpg.Connection,
    context: ScopeContext,
    payload: CodeImpactRequest,
    path_params: dict,
) -> dict[str, Any]:
    repository, resolved, seed_ids, seed_keys, unresolved_files, unresolved_symbols = (
        await _impact_inputs(connection, context, payload)
    )
    result = await code_graph.assess_impact(
        connection,
        scope_id=context.selected.scope_id,
        repository_id=repository["repository_id"],
        generation_row=resolved.generation,
        graph_status=resolved.graph_status,
        seed_symbol_ids=seed_ids,
        seed_symbol_keys=seed_keys,
        unresolved_files=unresolved_files,
        unresolved_symbols=unresolved_symbols,
        max_hops=payload.max_hops,
        limit=payload.limit,
        head_commit=payload.head_commit,
    )
    result["repository_key"] = payload.repository_key
    result["changed_files"] = list(payload.changed_files or [])
    result["changed_symbols"] = list(payload.changed_symbols or [])
    return result


async def code_blast_radius(
    connection: asyncpg.Connection,
    context: ScopeContext,
    payload: CodeBlastRadiusRequest,
    path_params: dict,
) -> dict[str, Any]:
    repository, resolved, seed_ids, seed_keys, unresolved_files, unresolved_symbols = (
        await _impact_inputs(connection, context, payload)
    )
    result = await code_graph.blast_radius(
        connection,
        scope_id=context.selected.scope_id,
        repository_id=repository["repository_id"],
        generation_row=resolved.generation,
        graph_status=resolved.graph_status,
        seed_symbol_ids=seed_ids,
        seed_symbol_keys=seed_keys,
        unresolved_files=unresolved_files,
        unresolved_symbols=unresolved_symbols,
        max_hops=payload.max_hops,
        limit=payload.limit,
        head_commit=payload.head_commit,
    )
    result["repository_key"] = payload.repository_key
    result["changed_files"] = list(payload.changed_files or [])
    result["changed_symbols"] = list(payload.changed_symbols or [])
    return result


async def code_hotspots(
    connection: asyncpg.Connection,
    context: ScopeContext,
    payload: CodeHotspotsRequest,
    path_params: dict,
) -> dict[str, Any]:
    repository = await _resolve_repository(connection, context, payload.repository_key)
    resolved = await code_graph.resolve_generation(
        connection,
        scope_id=context.selected.scope_id,
        repository_id=repository["repository_id"],
        head_commit=payload.head_commit,
    )
    if resolved.generation is None:
        return {
            "repository_key": payload.repository_key,
            "graph": _graph_state(None, "unavailable", payload.head_commit),
            "hotspots": [],
            "degraded": ["code_graph_not_built"],
            "interpretation": INTERPRETATION,
        }
    result = await code_graph.hotspots(
        connection,
        scope_id=context.selected.scope_id,
        repository_id=repository["repository_id"],
        generation=resolved.generation["generation"],
        limit=payload.limit,
        head_commit=payload.head_commit,
        graph_status=resolved.graph_status,
        generation_row=resolved.generation,
    )
    result["repository_key"] = payload.repository_key
    result["degraded"] = code_graph._degraded_for(resolved.graph_status)
    return result


# ---------------------------------------------------------------------------
# code.assess-change (scoped write: persists the cached assessment)
# ---------------------------------------------------------------------------


async def code_assess_change(
    connection: asyncpg.Connection,
    context: ScopeContext,
    idempotency_key: str,
    payload: CodeAssessChangeRequest,
    path_params: dict,
) -> tuple[int, dict[str, Any], bool]:
    operation = "code.assess-change"
    digest = request_digest(
        {
            "operation": operation,
            "scope_id": str(context.selected.scope_id),
            "payload": payload.model_dump(mode="json"),
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

    change = code_graph.change_digest(context.selected.scope_id, payload)
    cached = await code_graph.find_assessment(
        connection, scope_id=context.selected.scope_id, digest=change
    )
    if cached is not None:
        receipt = dict(cached)
        receipt["reused"] = True
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

    result = await code_graph.build_assessment(
        connection, context, payload, DEFAULT_PLANNER
    )
    await commit_receipt(
        connection,
        principal_id=context.principal.principal_id,
        operation=operation,
        idempotency_key=idempotency_key,
        digest=digest,
        receipt_kind="committed",
        receipt=result,
        scope_id=context.selected.scope_id,
    )
    return 201, result, False


# ---------------------------------------------------------------------------
# The registry itself
# ---------------------------------------------------------------------------

OPERATIONS: list[dict[str, Any]] = [
    {
        "operation_id": "memory.search",
        "method": "POST",
        "path": "/v1/memory/searches",
        "kind": "scoped_read",
        "request_model": MemorySearchRequest,
        "handler": memory_search,
        "summary": (
            "Hybrid memory search with explained stages, coverage and "
            "degradation."
        ),
        "usage": {
            "when_to_use": (
                "Use for any memory recall: known-item lookups, symbol or "
                "identifier questions, conceptual questions and relationship "
                "questions. The intent field picks bounded stages; the "
                "response explains which stages ran, what was filtered and "
                "why anything degraded."
            ),
            "when_not_to_use": (
                "Do not use it as proof of absence — zero hits are only "
                "meaningful for the reported coverage. Do not treat ranking "
                "scores as factual confidence; inspect the original with "
                "memory.inspect-hit before a load-bearing claim."
            ),
        },
    },
    {
        "operation_id": "memory.inspect-hit",
        "method": "POST",
        "path": "/v1/memory/hits:inspect",
        "kind": "scoped_read",
        "request_model": InspectHitRequest,
        "handler": memory_inspect_hit,
        "summary": "Verify a cited hit against the preserved original revision.",
        "usage": {
            "when_to_use": (
                "Use after search, before relying on a result: returns the "
                "exact original body, the cited span, current lifecycle "
                "status, hash and supporting graph assertions."
            ),
            "when_not_to_use": (
                "Do not use it to browse content you have not been cited; "
                "it requires a hit citation and never widens scope."
            ),
        },
    },
    {
        "operation_id": "graph.explore",
        "method": "POST",
        "path": "/v1/graphs:explore",
        "kind": "scoped_read",
        "request_model": GraphExploreRequest,
        "handler": graph_explore,
        "summary": "Bounded memory-graph or code-graph expansion with evidence.",
        "usage": {
            "when_to_use": (
                "Use kind=memory for thematic relations with source-revision "
                "evidence, kind=code for structural relations inside a named "
                "commit-bound generation. The graph kind is explicit; edge "
                "meanings are never merged."
            ),
            "when_not_to_use": (
                "Do not infer that a missing edge proves independence, and "
                "do not use a stale code generation as current coverage — "
                "the response labels freshness explicitly."
            ),
        },
    },
    {
        "operation_id": "graph.extract",
        "method": "POST",
        "path": "/v1/graphs/memory:extract",
        "kind": "scoped_write",
        "request_model": GraphExtractRequest,
        "handler": graph_extract,
        "summary": "Enqueue durable memory-graph extraction for a content revision.",
        "usage": {
            "when_to_use": (
                "Use after recording or revising content whose relations "
                "should become explorable. Extraction runs in the leased "
                "graph worker; the receipt is pending_processing with the "
                "job identity."
            ),
            "when_not_to_use": (
                "Do not call it inside a search request path or expect "
                "synchronous graph rows; do not use it to modify canonical "
                "content — it only derives projections."
            ),
        },
    },
    {
        "operation_id": "graph.retract-source",
        "method": "POST",
        "path": "/v1/graphs/memory:retract-source",
        "kind": "scoped_write",
        "request_model": GraphRetractRequest,
        "handler": graph_retract_source,
        "summary": (
            "Retract derived graph evidence for an invalidated or superseded "
            "source."
        ),
        "usage": {
            "when_to_use": (
                "Use when a source content item is invalidated, superseded or "
                "deleted: removes exactly that source's evidence edges, "
                "tombstones stranded assertions and preserves assertions "
                "still supported elsewhere and all authored annotations."
            ),
            "when_not_to_use": (
                "Do not use it to delete authored annotations or to rewrite "
                "canonical content; retraction is terminal per assertion."
            ),
        },
    },
    {
        "operation_id": "memory.rebuild-projections",
        "method": "POST",
        "path": "/v1/memory/projections:rebuild",
        "kind": "scoped_write",
        "request_model": RebuildProjectionsRequest,
        "handler": memory_rebuild_projections,
        "summary": (
            "Rebuild the scope's replaceable trigram projection from canonical "
            "revisions."
        ),
        "usage": {
            "when_to_use": (
                "Use as maintenance when search reports "
                "trigram_projection_stale, or after bulk content changes."
            ),
            "when_not_to_use": (
                "Do not call it per search request; it deletes and "
                "repopulates the projection for the whole selected scope."
            ),
        },
    },
    {
        "operation_id": "code.publish-index",
        "method": "POST",
        "path": "/v1/code/index:publish",
        "kind": "scoped_write",
        "request_model": CodePublishIndexRequest,
        "handler": code_publish_index,
        "summary": (
            "Enqueue a commit-bound code-graph generation from a bounded "
            "snapshot."
        ),
        "usage": {
            "when_to_use": (
                "Use to index a repository snapshot (bounded file set, "
                "commit-anchored) so callers/impact/blast-radius queries "
                "have fresh coverage. Extraction runs in the leased graph "
                "worker."
            ),
            "when_not_to_use": (
                "Do not submit uncommitted work without a content-addressed "
                "snapshot identity, oversized trees, or non-Python files "
                "expecting coverage — exclusions are reported, not parsed."
            ),
        },
    },
    {
        "operation_id": "code.annotate",
        "method": "POST",
        "path": "/v1/code/annotations",
        "kind": "scoped_write",
        "request_model": CodeAnnotateRequest,
        "handler": code_annotate,
        "summary": "Attach a preserved authored annotation to a code symbol.",
        "usage": {
            "when_to_use": (
                "Use to record durable human knowledge about a symbol "
                "(review requirements, ownership, hazards). Annotations are "
                "canonical: they survive every index rebuild."
            ),
            "when_not_to_use": (
                "Do not use it for generated or model output pretending to "
                "be authored knowledge; annotations are append-only and "
                "cannot be edited or deleted through the API."
            ),
        },
    },
    {
        "operation_id": "code.callers",
        "method": "POST",
        "path": "/v1/code/callers",
        "kind": "scoped_read",
        "request_model": CodeCallersRequest,
        "handler": code_callers,
        "summary": (
            "Direct structural callers of one symbol in the resolved "
            "generation."
        ),
        "usage": {
            "when_to_use": (
                "Use before changing a symbol to see who calls it, with the "
                "indexed commit and freshness stated on every response."
            ),
            "when_not_to_use": (
                "Do not treat an empty caller list as proof nothing depends "
                "on the symbol: dynamic dispatch and unindexed languages are "
                "declared coverage limits."
            ),
        },
    },
    {
        "operation_id": "code.impact",
        "method": "POST",
        "path": "/v1/code/impact",
        "kind": "scoped_read",
        "request_model": CodeImpactRequest,
        "handler": code_impact,
        "summary": (
            "Bounded transitive reverse-dependency impact of changed "
            "files/symbols."
        ),
        "usage": {
            "when_to_use": (
                "Use with changed files and/or symbols to get direct and "
                "transitive impacts with hop counts, edge evidence and "
                "explicit unresolved inputs."
            ),
            "when_not_to_use": (
                "Do not read zero impacts as safe-to-change on stale, "
                "partial or unavailable coverage; the interpretation field "
                "states the lower-bound semantics."
            ),
        },
    },
    {
        "operation_id": "code.blast-radius",
        "method": "POST",
        "path": "/v1/code/blast-radius",
        "kind": "scoped_read",
        "request_model": CodeBlastRadiusRequest,
        "handler": code_blast_radius,
        "summary": "Impact plus affected files, preserved annotations and aggregates.",
        "usage": {
            "when_to_use": (
                "Use when reviewing or planning a change: everything "
                "code.impact returns plus affected-file aggregates and the "
                "authored annotations attached to impacted symbols."
            ),
            "when_not_to_use": (
                "Do not use it as a permission to change code; it is "
                "evidence, and stale coverage must be re-indexed first."
            ),
        },
    },
    {
        "operation_id": "code.hotspots",
        "method": "POST",
        "path": "/v1/code/hotspots",
        "kind": "scoped_read",
        "request_model": CodeHotspotsRequest,
        "handler": code_hotspots,
        "summary": "Symbols with the highest static fan-in in the resolved generation.",
        "usage": {
            "when_to_use": (
                "Use to find structurally load-bearing symbols before "
                "refactors or reviews."
            ),
            "when_not_to_use": (
                "Do not read fan-in as runtime frequency or business "
                "importance; it counts static call edges only."
            ),
        },
    },
    {
        "operation_id": "code.assess-change",
        "method": "POST",
        "path": "/v1/code/assess-change",
        "kind": "scoped_write",
        "request_model": CodeAssessChangeRequest,
        "handler": code_assess_change,
        "summary": "Composed change-impact assessment bound to a change digest.",
        "usage": {
            "when_to_use": (
                "Use at the start of code-changing tasks, after materially "
                "changing the planned files/symbols, and before returning "
                "work: composes graph coverage, impacts, callers, related "
                "memories, verification candidates and whitelisted next "
                "actions. Identical inputs are deduplicated on the change "
                "digest."
            ),
            "when_not_to_use": (
                "Do not use it for prose-only tasks, and do not treat its "
                "suggestions as authority to execute anything; an updated "
                "diff invalidates reuse and requires a new assessment."
            ),
        },
    },
]
