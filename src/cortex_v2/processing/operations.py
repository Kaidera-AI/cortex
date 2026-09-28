"""Operation metadata for the Processing module.

``OPERATIONS`` is the integration contract: a generic mount reads it and exposes
the same use case over HTTP, CLI and MCP, so discovery, authorization and error
semantics cannot drift between transports (R23, F05). Domain code stays
transport-free; nothing here imports FastAPI.

Handler signatures:

* ``scoped_write``:
  ``async (connection, context, idempotency_key, payload, path_params) -> (status, data, replayed)``
* ``scoped_read``:
  ``async (connection, context, payload, path_params) -> dict``
"""

from __future__ import annotations

from typing import Any

from . import commands
from .models import (
    ActivateGenerationRequest,
    BackfillRequest,
    CancelJobRequest,
    CoverageRequest,
    CreateEmbeddingSpaceRequest,
    ListJobsRequest,
)

OPERATIONS: list[dict[str, Any]] = [
    {
        "operation_id": "processing.backfill",
        "method": "POST",
        "path": "/v1/processing/backfill",
        "kind": "scoped_write",
        "request_model": BackfillRequest,
        "handler": commands.backfill_jobs,
        "summary": (
            "Durably enqueue a bounded batch of intended revisions for chunking "
            "and selective embedding into one space generation."
        ),
        "usage": {
            "use_when": [
                "a new embedding space or generation needs a corpus built beside the active one",
                "coverage reports revisions the profile intends but no vector exists for",
                "a provider outage ended and queued enrichment should resume",
            ],
            "avoid_when": [
                "the goal is one interactive query embedding — that is a retrieval concern",
                "the space has no active or building generation",
                "the profile's coverage policy does not intend these content classes",
            ],
            "effects": (
                "Committed durable jobs (never in-process tasks), one budget "
                "reservation per accepted job, and an idempotency receipt. Projections "
                "appear later, when a worker leases and completes each job."
            ),
            "receipt": "pending_processing (202)",
            "idempotency": (
                "Same key and payload replays the stored receipt. Identical work "
                "intents collapse on (scope, kind, dedupe key), so resuming with the "
                "returned cursor never duplicates a job or a projection row."
            ),
            "prerequisites": [
                "an active processing profile whose chunking policy matches the space",
                "an embedding space with an active generation, or generation='new'",
            ],
            "limits": "at most 200 revisions scanned per call; queue admission and budget bounds may defer the rest",
            "errors": {
                "404 profile_not_found": "pin an active profile",
                "404 space_not_found": "pin an existing space",
                "409 no_active_generation": "build or activate a generation, or pass generation='new'",
                "409 chunking_policy_mismatch": "the profile and space disagree on chunking; use a matching pair",
                "429 budget_exhausted": "capacity is reserved; retry later or lower the batch limit",
                "422 invalid_cursor": "restart from the first page",
            },
        },
    },
    {
        "operation_id": "processing.jobs.cancel",
        "method": "POST",
        "path": "/v1/processing/jobs/{job_id}:cancel",
        "kind": "scoped_write",
        "request_model": CancelJobRequest,
        "handler": commands.cancel_job,
        "summary": "Cancel a durable job, or fence a running attempt out of publishing.",
        "usage": {
            "use_when": [
                "work was enqueued against the wrong space, generation or profile",
                "a backfill should stop before consuming more provider budget",
            ],
            "avoid_when": [
                "the job already reached a terminal state — nothing is cancellable",
                "the intent is to retry later: cancel is not pause",
            ],
            "effects": (
                "A queued or blocked job becomes cancelled immediately. A leased job "
                "records the cancellation request; its attempt is fenced at the next "
                "heartbeat or publish, so no further effect can be written."
            ),
            "receipt": "committed when cancelled now, pending_processing when a lease holder must observe it",
            "idempotency": "Same key and payload replays the stored receipt.",
            "errors": {
                "404 job_not_found": "the job is not in the selected scope",
                "409 job_terminal": "the job already succeeded, failed, was cancelled or was quarantined",
            },
        },
    },
    {
        "operation_id": "processing.embedding-spaces.create",
        "method": "POST",
        "path": "/v1/processing/embedding-spaces",
        "kind": "scoped_write",
        "authority": "installation_owner",
        "requires_scope": True,
        "request_model": CreateEmbeddingSpaceRequest,
        "handler": commands.create_embedding_space,
        "summary": (
            "Create immutable embedding-space semantics (provider, model identity, "
            "dimensions, normalization, metric, prefixes, chunking policy) with its "
            "first index generation."
        ),
        "usage": {
            "use_when": [
                "a model, tokenizer, dimension, metric or chunking policy changes — that is a new space even when dimensions match",
                "a first semantic index is being introduced for an installation",
            ],
            "avoid_when": [
                "only credentials or an equivalent endpoint changed — the space is unchanged",
                "the intent is to rebuild vectors: begin a new generation of the existing space instead",
            ],
            "effects": (
                "One immutable space row plus generation 1 in state active, and a "
                "privileged-action audit entry. Requires the installation owner."
            ),
            "receipt": "committed (201)",
            "idempotency": "Same key and payload replays the stored receipt.",
            "errors": {
                "403 installation_owner_required": "ask an installation owner",
                "409 space_name_exists": "choose another name or reuse the space",
                "422 invalid_space_definition": "the definition violates space invariants",
            },
            "advisory": (
                "provider_family_known=false means no configured provider role serves "
                "that family yet; execution reports a typed space_mismatch until one does."
            ),
        },
    },
    {
        "operation_id": "processing.embedding-spaces.activate-generation",
        "method": "POST",
        "path": "/v1/processing/embedding-spaces/{space_id}/generations/{generation_id}:activate",
        "kind": "scoped_write",
        "authority": "installation_owner",
        "requires_scope": True,
        "request_model": ActivateGenerationRequest,
        "handler": commands.activate_generation,
        "summary": "Atomically switch retrieval routing to one built generation.",
        "usage": {
            "use_when": [
                "a rebuild beside the active generation passed coverage, recall and latency evaluation",
                "rolling back to a retained previous generation",
            ],
            "avoid_when": [
                "the generation is still building and coverage is unknown",
                "the intent is to start a rebuild — use backfill with generation='new'",
            ],
            "effects": (
                "The previous active generation is retired and the space pointer moves "
                "in one transaction; retrieval reads only the newly active generation."
            ),
            "receipt": "committed (200)",
            "idempotency": "Same key and payload replays the stored receipt.",
            "errors": {
                "403 installation_owner_required": "ask an installation owner",
                "404 generation_not_found": "the generation is not in this space",
                "409 generation_state_conflict": "pass expected_state to make the intent explicit",
            },
        },
    },
    {
        "operation_id": "processing.jobs.status",
        "method": "GET",
        "path": "/v1/processing/jobs/{job_id}",
        "kind": "scoped_read",
        "request_model": None,
        "handler": commands.job_status,
        "summary": (
            "One durable job with its attempts, lease, fencing epoch, budget "
            "reservations, quarantine entries and the projections that actually exist."
        ),
        "usage": {
            "use_when": [
                "a receipt said pending_processing and the caller needs the current state",
                "diagnosing why a revision is not retrievable yet",
            ],
            "avoid_when": ["the question is corpus-wide — use processing.coverage"],
            "effects": "none",
            "honesty": (
                "published.chunks/vectors count projection rows that exist; a stored "
                "artifact is not proof that content is searchable in the active generation."
            ),
            "errors": {"404 job_not_found": "the job is not visible in the selected scope"},
        },
    },
    {
        "operation_id": "processing.jobs.list",
        "method": "POST",
        "path": "/v1/processing/jobs:list",
        "kind": "scoped_read",
        "request_model": ListJobsRequest,
        "handler": commands.list_jobs,
        "summary": (
            "Keyset-paginated jobs across the caller's readable scopes, with queue "
            "depth, queue age and work waiting for an executor role that is not live."
        ),
        "usage": {
            "use_when": [
                "triaging blocked, quarantined or repeatedly failing work",
                "checking whether an optional doc/media executor is actually running",
            ],
            "avoid_when": ["a single job id is already known"],
            "effects": "none",
            "pagination": "cursor is '<created_at>:<job_id>'; ordering is created_at DESC, job_id DESC",
            "errors": {"422 invalid_cursor": "restart from the first page"},
        },
    },
    {
        "operation_id": "processing.coverage",
        "method": "POST",
        "path": "/v1/processing/coverage",
        "kind": "scoped_read",
        "request_model": CoverageRequest,
        "handler": commands.coverage_report,
        "summary": (
            "Intended coverage for one profile, space and generation: intended, "
            "chunked and embedded revisions, gap reasons, job states by error code, "
            "open quarantine and typed degraded[] explanations."
        ),
        "usage": {
            "use_when": [
                "deciding whether a rebuilt generation is complete enough to activate",
                "answering 'why is this content not retrievable?' with evidence",
            ],
            "avoid_when": [
                "the question is one job's state — use processing.jobs.status",
                "no profile is pinned: coverage without an intended policy is meaningless",
            ],
            "effects": "none",
            "measurement": (
                "Intended = current revisions matching the profile's coverage policy "
                "(content classes and minimum text length). Excluded low-value content "
                "is not a gap; that is the point of selective enrichment."
            ),
            "errors": {"404 profile_not_found": "pin an active profile", "404 space_not_found": "pin an existing space"},
        },
    },
    {
        "operation_id": "processing.embedding-spaces.list",
        "method": "GET",
        "path": "/v1/processing/embedding-spaces",
        "kind": "scoped_read",
        "request_model": None,
        "handler": commands.list_embedding_spaces,
        "summary": "Embedding spaces with their immutable semantics and generation history.",
        "usage": {
            "use_when": [
                "choosing the space and generation to query or rebuild",
                "verifying that a model change produced a new space rather than mutating one",
            ],
            "avoid_when": ["the caller needs candidate vectors — that is retrieval's vector stage"],
            "effects": "none",
        },
    },
    {
        "operation_id": "processing.profiles.list",
        "method": "GET",
        "path": "/v1/processing/profiles",
        "kind": "scoped_read",
        "request_model": None,
        "handler": commands.list_processing_profiles,
        "summary": (
            "Immutable processing profiles: extractor, chunking policy, embedder "
            "selection, optional transforms and the intended-coverage selection."
        ),
        "usage": {
            "use_when": ["pinning a profile for backfill or a single revision job"],
            "avoid_when": ["the intent is to change processing: profiles are immutable, add a version"],
            "effects": "none",
            "note": "built-in profiles have installation_id=null and are available to every installation",
        },
    },
    {
        "operation_id": "processing.doc.formats",
        "method": "GET",
        "path": "/v1/processing/doc/formats",
        "kind": "scoped_read",
        "request_model": None,
        "handler": commands.doc_formats,
        "summary": (
            "The tested document-format table of the Document Processor: formats, "
            "executor roles, media types, extensions, parser identity, provenance "
            "kinds, dependencies, per-format limits, plus which executor roles are "
            "live right now."
        ),
        "usage": {
            "use_when": [
                "deciding whether a file type can be processed before uploading it",
                "explaining why a job is blocked on executor_not_activated or ocr_needed",
            ],
            "avoid_when": ["the question is about one job — use processing.jobs.status"],
            "effects": "none",
            "honesty": (
                "formats_waiting_for_executor lists formats whose executor role has no "
                "fresh worker heartbeat; unsupported formats are reported as "
                "unsupported rather than parsed into replacement-character text."
            ),
        },
    },
]


__all__ = ["OPERATIONS"]
