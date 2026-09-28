"""Durable graph-extraction job handlers for the leased ``graph`` worker role.

Contract (final, per the Processing owner): handlers never see a database
connection — that is the F08 guarantee. A handler receives the pinned
``AttemptContext`` plus narrow ``AttemptServices``, performs bounded pure
extraction (through ``run_blocking``), and returns a typed ``ExecutionReport``
with the extracted payload in ``handler_output`` and counts in ``stats``. The
registered publisher then writes ``cortex_retrieval`` rows inside the fenced
publish transaction, after the epoch/lease/cancellation re-check succeeded, so
a stale attempt can never publish.

Registration: the worker imports this module (env
``CORTEX_V2_WORKER_HANDLER_MODULES``) and calls ``register(JOB_REGISTRY)``.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from typing import Any

from ..processing.contracts import (
    AttemptContext,
    AttemptServices,
    ExecutionReport,
    Outcome,
)
from .code_graph import (
    EXTRACTOR_PROFILE,
    MAX_FILES,
    MAX_FILE_BYTES,
    SnapshotTooLarge,
    extract_snapshot,
    publish_extracted,
)
from .memory_graph import RuleExtractor, publish_extraction
from .models import COMMIT_PATTERN, REPOSITORY_KEY_PATTERN
from .ports import ExtractedAssertion

KIND_MEMORY_EXTRACT = "graph.memory.extract"
KIND_CODE_EXTRACT = "graph.code.extract"
ROLE_GRAPH = "graph"


def _payload(context: AttemptContext) -> dict[str, Any]:
    intent = context.intent if isinstance(context.intent, dict) else {}
    payload = intent.get("payload")
    return payload if isinstance(payload, dict) else {}


# ---------------------------------------------------------------------------
# graph.memory.extract
# ---------------------------------------------------------------------------


async def handle_memory_extract(
    context: AttemptContext, services: AttemptServices
) -> ExecutionReport:
    """Rule-v1 memory-graph extraction over the pinned content revision.

    Graph kinds carry ``content_id=None`` on the job row and pin their
    identity in ``payload["pin"]``; the pinned canonical revision is read
    through the worker's short-transaction ``read_source`` service by
    re-pinning the attempt context — the handler itself never sees a
    connection (F08).
    """
    payload = _payload(context)
    profile = payload.get("extractor_profile") or RuleExtractor.profile
    if profile != RuleExtractor.profile:
        return ExecutionReport(
            outcome=Outcome.CONFIGURATION_MISSING,
            executor=profile,
            detail=(
                f"extractor profile {profile!r} is not installed on this "
                f"worker; available: {RuleExtractor.profile!r}"
            ),
        )
    pinned = _pinned_revision(payload)
    if pinned is None:
        return ExecutionReport(
            outcome=Outcome.INPUT_REJECTED,
            executor=profile,
            detail="payload.pin must name a content revision "
            "(pin.content_id uuid, pin.revision >= 1)",
        )
    content_id, revision = pinned
    source = await services.read_source(
        replace(context, content_id=content_id, source_revision=revision)
    )
    if source is None:
        return ExecutionReport(
            outcome=Outcome.INPUT_CORRUPT,
            executor=profile,
            detail="the pinned source revision is unavailable in this scope",
        )
    extractor = RuleExtractor()
    extracted = await services.run_blocking(extractor.extract, source.body_text)
    for item in extracted:
        if item.span_start < 0 or item.span_end > len(source.body_text):
            return ExecutionReport(
                outcome=Outcome.OUTPUT_INVALID,
                executor=profile,
                detail="extracted span lies outside the source body",
            )
    return ExecutionReport(
        outcome=Outcome.OK,
        executor=profile,
        stats={"assertions": len(extracted)},
        handler_output={
            "content_id": str(content_id),
            "revision": revision,
            "assertions": [
                {
                    "subject_key": item.subject_key,
                    "subject_type": item.subject_type,
                    "relation": item.relation,
                    "object_key": item.object_key,
                    "object_type": item.object_type,
                    "span_start": item.span_start,
                    "span_end": item.span_end,
                }
                for item in extracted
            ],
        },
    )


def _pinned_revision(payload: dict[str, Any]) -> tuple[uuid.UUID, int] | None:
    pin = payload.get("pin")
    if not isinstance(pin, dict):
        return None
    try:
        content_id = uuid.UUID(str(pin.get("content_id")))
    except (TypeError, ValueError):
        return None
    revision = pin.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        return None
    return content_id, revision


async def publish_memory_extract(
    connection, context: AttemptContext, report: ExecutionReport
) -> None:
    """Write the extracted assertions inside the fenced publish transaction."""
    output = report.handler_output
    if not output:
        raise ValueError("memory-graph publisher requires handler_output")
    content_id = uuid.UUID(str(output["content_id"]))
    revision = int(output["revision"])
    extractor = RuleExtractor()
    extracted = tuple(
        ExtractedAssertion(**item) for item in output["assertions"]
    )
    await publish_extraction(
        connection,
        scope_id=context.scope_id,
        content_id=content_id,
        revision=revision,
        body=None,
        extracted=extracted,
        extractor=extractor,
    )


# ---------------------------------------------------------------------------
# graph.code.extract
# ---------------------------------------------------------------------------


def _validate_code_payload(payload: dict[str, Any]) -> str | None:
    pin = payload.get("pin")
    if not isinstance(pin, dict):
        return "payload.pin is required for graph.code.extract"
    repository_key = pin.get("repository_key")
    commit_sha = pin.get("commit_sha")
    if not isinstance(repository_key, str) or not REPOSITORY_KEY_PATTERN.match(
        repository_key
    ):
        return "pin.repository_key is missing or malformed"
    if not isinstance(commit_sha, str) or not COMMIT_PATTERN.match(commit_sha):
        return "pin.commit_sha must be lowercase hex, 7-64 characters"
    files = payload.get("files")
    if not isinstance(files, list) or not files:
        return "payload.files must be a non-empty list"
    if len(files) > MAX_FILES:
        return f"payload.files exceeds the {MAX_FILES} file bound"
    for entry in files:
        if not isinstance(entry, dict):
            return "each payload file must be an object"
        path = entry.get("path")
        source = entry.get("source")
        if not isinstance(path, str) or not path:
            return "each payload file needs a path"
        if path.startswith(("/", "\\")) or ".." in path.replace("\\", "/").split("/"):
            return f"file path {path!r} must be repository-relative"
        if len(path) > 512:
            return f"file path {path!r} exceeds 512 characters"
        if not isinstance(source, str):
            return f"file source for {path!r} must be text"
        if len(source.encode("utf-8")) > MAX_FILE_BYTES:
            return f"file {path!r} exceeds the bounded snapshot file size"
    return None


async def handle_code_extract(
    context: AttemptContext, services: AttemptServices
) -> ExecutionReport:
    """Bounded stdlib-ast extraction of a commit/snapshot-bound payload."""
    payload = _payload(context)
    violation = _validate_code_payload(payload)
    if violation is not None:
        return ExecutionReport(
            outcome=Outcome.INPUT_REJECTED,
            executor=EXTRACTOR_PROFILE,
            detail=violation,
        )
    pin = payload["pin"]
    files = tuple(
        (entry["path"], entry["source"]) for entry in payload["files"]
    )
    try:
        snapshot = await services.run_blocking(extract_snapshot, files)
    except SnapshotTooLarge as exc:
        return ExecutionReport(
            outcome=Outcome.INPUT_REJECTED, executor=EXTRACTOR_PROFILE, detail=str(exc)
        )
    return ExecutionReport(
        outcome=Outcome.OK,
        executor=EXTRACTOR_PROFILE,
        stats={
            "symbols": len(snapshot.symbols),
            "edges": len(snapshot.edges),
            "excluded": len(snapshot.excluded),
        },
        warnings=tuple(
            f"excluded {item['path']}: {item['reason']}" for item in snapshot.excluded
        ),
        handler_output={
            "repository_key": pin["repository_key"],
            "commit_sha": pin["commit_sha"],
            "language_coverage": snapshot.language_coverage,
            "excluded": snapshot.excluded,
            "symbols": [
                {
                    "symbol_key": symbol.symbol_key,
                    "symbol_kind": symbol.symbol_kind,
                    "file_path": symbol.file_path,
                    "span_start_line": symbol.span_start_line,
                    "span_end_line": symbol.span_end_line,
                    "parent_key": symbol.parent_key,
                }
                for symbol in snapshot.symbols
            ],
            "edges": [
                {
                    "caller_key": edge.caller_key,
                    "edge_kind": edge.edge_kind,
                    "callee_key": edge.callee_key,
                    "source_line": edge.source_line,
                    "resolved": edge.resolved,
                }
                for edge in snapshot.edges
            ],
        },
    )


async def publish_code_extract(
    connection, context: AttemptContext, report: ExecutionReport
) -> None:
    """Publish the extracted generation inside the fenced publish transaction."""
    output = report.handler_output
    if not output:
        raise ValueError("code-graph publisher requires handler_output")
    from .code_graph import EdgeRecord, SymbolRecord

    await publish_extracted(
        connection,
        scope_id=context.scope_id,
        repository_key=output["repository_key"],
        commit_sha=output["commit_sha"],
        symbols=tuple(
            SymbolRecord(
                symbol_key=item["symbol_key"],
                symbol_kind=item["symbol_kind"],
                file_path=item["file_path"],
                span_start_line=item["span_start_line"],
                span_end_line=item["span_end_line"],
                parent_key=item["parent_key"],
            )
            for item in output["symbols"]
        ),
        edges=tuple(
            EdgeRecord(
                caller_key=item["caller_key"],
                edge_kind=item["edge_kind"],
                callee_key=item["callee_key"],
                source_line=item["source_line"],
                resolved=item["resolved"],
            )
            for item in output["edges"]
        ),
        excluded=output["excluded"],
        language_coverage=output["language_coverage"],
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register(registry) -> None:
    """Register both graph kinds with the processing job registry.

    ``registry.register_job_handler(kind, role, handler, *, publisher,
    summary, required_intent)`` is the final processing contract; the worker
    calls this function with ``cortex_v2.processing.registry.JOB_REGISTRY``.
    """
    registry.register_job_handler(
        KIND_MEMORY_EXTRACT,
        ROLE_GRAPH,
        handle_memory_extract,
        publisher=publish_memory_extract,
        summary=(
            "Extract source-bound memory-graph assertions (rule-v1) from the "
            "pinned content revision and publish them with evidence spans."
        ),
        required_intent=("pin",),
    )
    registry.register_job_handler(
        KIND_CODE_EXTRACT,
        ROLE_GRAPH,
        handle_code_extract,
        publisher=publish_code_extract,
        summary=(
            "Publish a commit-bound code-graph generation (python-ast-v1) "
            "from the bounded snapshot payload."
        ),
        required_intent=("pin",),
    )
