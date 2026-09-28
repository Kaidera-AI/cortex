"""Connector ingestion pipeline with atomic replacement (R11).

One command = one transaction: parse (pure, strict) → ingest new content
through the frozen W1 ``content.ingest_content`` use case → supersede or
invalidate the replaced generation through ``content.change_status`` →
record the connector ledger → store the typed receipt. A failure anywhere
rolls the whole replacement back; a failed parse quarantines the original
payload instead of losing or half-applying it. Repeating an upload with the
same idempotency key replays the stored receipt without duplicating
messages.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Sequence

import asyncpg

from ..content import change_status, ingest_content
from ..models import IngestContentRequest
from ..receipts import begin_command, commit_receipt, request_digest
from ..store import ApiProblem, ScopeContext
from ..interface.context import declare_policy_revision
from .models import (
    IngestDiaryRequest,
    IngestLocalStateRequest,
    IngestSaveChatRequest,
    IngestTranscriptRequest,
)
from .parsers import MAX_ITEM_BYTES, TranscriptParseError, parse_transcript

QUARANTINE_PAYLOAD_LIMIT = 1_000_000


@dataclass(frozen=True, slots=True)
class IngestItem:
    item_role: str
    content_class: str
    body: str
    payload: dict[str, Any]
    observed_at: datetime | None


def _enforce_item_limit(body: str, payload: dict[str, Any], line: int) -> int:
    encoded_bytes = len(body.encode("utf-8")) + len(json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8"))
    if encoded_bytes > MAX_ITEM_BYTES:
        raise TranscriptParseError([{
            "line": line,
            "code": "message_too_large",
            "detail": f"message body and payload exceed {MAX_ITEM_BYTES} bytes",
        }])
    return encoded_bytes


def plan_replacement(
    old_items: Sequence[tuple[str, Any]], new_items: Sequence[tuple[str, Any]]
) -> dict[str, Any]:
    """Pair old/new content by ordinal: supersede with successor where a
    replacement exists, invalidate leftovers, and mark growth as create-only.
    """
    supersede = [
        {"old": old_items[index][0], "successor": new_items[index][0]}
        for index in range(min(len(old_items), len(new_items)))
    ]
    invalidate = [
        {"old": old_items[index][0]}
        for index in range(len(new_items), len(old_items))
    ]
    create_only = [
        new_items[index][0] for index in range(len(old_items), len(new_items))
    ]
    return {"supersede": supersede, "invalidate": invalidate,
            "create_only": create_only}


def _coerce(model: type, payload: Any) -> Any:
    return payload if isinstance(payload, model) else model.model_validate(payload)


async def _next_generation(
    connection: asyncpg.Connection, context: ScopeContext,
    connector_namespace: str, source_key: str,
) -> tuple[int, dict[str, Any] | None]:
    row = await connection.fetchrow(
        """
        SELECT run_id, generation FROM cortex_context.ingest_runs
         WHERE scope_id = $1 AND connector_namespace = $2 AND source_key = $3
         ORDER BY generation DESC LIMIT 1
        """,
        context.selected.scope_id,
        connector_namespace,
        source_key,
    )
    generation = int(row["generation"]) + 1 if row else 1
    previous = None
    if row is not None:
        previous_row = await connection.fetchrow(
            """
            SELECT run_id, generation FROM cortex_context.ingest_runs
             WHERE scope_id = $1 AND connector_namespace = $2
               AND source_key = $3 AND status = 'committed'
             ORDER BY generation DESC LIMIT 1
            """,
            context.selected.scope_id,
            connector_namespace,
            source_key,
        )
        previous = dict(previous_row) if previous_row else None
    return generation, previous


async def _previous_contents(
    connection: asyncpg.Connection, context: ScopeContext,
    previous_run_id: uuid.UUID,
) -> list[dict[str, Any]]:
    rows = await connection.fetch(
        """
        SELECT content_id, item_role, ordinal
          FROM cortex_context.ingest_run_contents
         WHERE scope_id = $1 AND run_id = $2
         ORDER BY ordinal
        """,
        context.selected.scope_id,
        previous_run_id,
    )
    return [dict(row) for row in rows]


def _derived_key(prefix: str, *parts: Any) -> str:
    material = ":".join(str(part) for part in parts)
    return f"{prefix}:{hashlib.sha256(material.encode()).hexdigest()[:48]}"


async def _quarantine(
    connection: asyncpg.Connection,
    context: ScopeContext,
    *,
    operation: str,
    idempotency_key: str,
    digest: bytes,
    connector_namespace: str,
    source_key: str,
    kind: str,
    generation: int,
    failures: list[dict[str, Any]],
    warnings: Sequence[str],
    original: Any,
    policy_revision: int,
) -> tuple[int, dict[str, Any], bool]:
    run_id = uuid.uuid4()
    serialized = json.dumps(original, ensure_ascii=False, sort_keys=True)
    encoded = serialized.encode("utf-8")
    if len(encoded) > QUARANTINE_PAYLOAD_LIMIT:
        serialized = json.dumps(
            {
                "excerpt": encoded[:QUARANTINE_PAYLOAD_LIMIT // 8].decode(
                    "utf-8", errors="ignore"
                ),
                "original_length": len(encoded),
                "sha256": hashlib.sha256(encoded).hexdigest(),
                "truncated": True,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    await connection.execute(
        """
        INSERT INTO cortex_context.ingest_runs
            (scope_id, run_id, connector_namespace, source_key, ingest_kind,
             generation, item_count, parse_warnings, quarantine_payload,
             quarantine_reason, status, created_by_principal)
        VALUES ($1, $2, $3, $4, $5, $6, 0, $7::jsonb, $8::jsonb, $9::jsonb,
                'quarantined', $10)
        """,
        context.selected.scope_id,
        run_id,
        connector_namespace,
        source_key,
        kind,
        generation,
        json.dumps(list(warnings)),
        serialized,
        json.dumps({"failures": failures}),
        context.principal.principal_id,
    )
    receipt = {
        "state": "quarantined",
        "error_code": (
            "state_too_large_quarantined"
            if kind == "local_state"
            else "transcript_parse_failed_quarantined"
        ),
        "operation": operation,
        "run_id": str(run_id),
        "generation": generation,
        "item_count": 0,
        "content_ids": [],
        "failures": failures,
        "warnings": list(warnings),
        "policy_revision": policy_revision,
        "scope_id": str(context.selected.scope_id),
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
    return 422, receipt, False


async def _run_pipeline(
    connection: asyncpg.Connection,
    context: ScopeContext,
    *,
    kind: str,
    operation: str,
    idempotency_key: str,
    digest: bytes,
    connector_namespace: str,
    source_key: str,
    items: Sequence[IngestItem],
    warnings: Sequence[str],
    default_observed_at: datetime | None,
    header_item: IngestItem | None,
) -> tuple[int, dict[str, Any], bool]:
    previous_replay = await begin_command(
        connection,
        principal_id=context.principal.principal_id,
        operation=operation,
        idempotency_key=idempotency_key,
        digest=digest,
        scope_id=context.selected.scope_id,
    )
    previous, replayed = previous_replay
    if replayed and previous is not None:
        return 200, previous, True

    all_items = ([header_item] if header_item else []) + list(items)
    try:
        for ordinal, item in enumerate(all_items):
            _enforce_item_limit(item.body, item.payload, ordinal)
    except TranscriptParseError as exc:
        code = "state_too_large" if kind == "local_state" else "message_too_large"
        raise ApiProblem(
            422, code, f"Ingest item exceeds {MAX_ITEM_BYTES} bytes."
        ) from exc

    policy_revision, _ = await declare_policy_revision(connection, context)
    generation, previous_run = await _next_generation(
        connection, context, connector_namespace, source_key
    )

    created: list[tuple[str, str, uuid.UUID]] = []  # (item_role, ordinal, id)
    for ordinal, item in enumerate(all_items):
        item_key = f"{source_key}@g{generation}#i{ordinal:04d}"
        request = IngestContentRequest(
            connector_namespace=connector_namespace,
            source_key=item_key,
            content_class=item.content_class,
            payload=item.payload,
            body=item.body,
            source_observed_at=item.observed_at or default_observed_at,
        )
        _status, receipt, _replayed = await ingest_content(
            connection, context, request
        )
        created.append(
            (item.item_role, str(ordinal), uuid.UUID(receipt["content_id"]))
        )

    if previous_run is not None:
        old_contents = await _previous_contents(
            connection, context, previous_run["run_id"]
        )
        old_headers = [
            (str(row["content_id"]), row["ordinal"])
            for row in old_contents if row["item_role"] == "header"
        ]
        old_messages = [
            (str(row["content_id"]), row["ordinal"])
            for row in old_contents if row["item_role"] != "header"
        ]
        new_headers = [
            (str(content_id), ordinal)
            for role, ordinal, content_id in created if role == "header"
        ]
        new_messages = [
            (str(content_id), ordinal)
            for role, ordinal, content_id in created if role != "header"
        ]
        header_plan = plan_replacement(old_headers, new_headers)
        message_plan = plan_replacement(old_messages, new_messages)
        for plan in (header_plan, message_plan):
            for pair in plan["supersede"]:
                await change_status(
                    connection, context, uuid.UUID(pair["old"]), "supersede",
                    f"replaced by {kind} ingest generation {generation}",
                    uuid.UUID(pair["successor"]),
                    _derived_key("supersede", context.selected.scope_id,
                                 source_key, generation, pair["old"]),
                )
            for entry in plan["invalidate"]:
                await change_status(
                    connection, context, uuid.UUID(entry["old"]), "invalidate",
                    f"replaced by {kind} ingest generation {generation}",
                    None,
                    _derived_key("invalidate", context.selected.scope_id,
                                 source_key, generation, entry["old"]),
                )

    run_id = uuid.uuid4()
    replaced_run_id = previous_run["run_id"] if previous_run else None
    await connection.execute(
        """
        INSERT INTO cortex_context.ingest_runs
            (scope_id, run_id, connector_namespace, source_key, ingest_kind,
             generation, item_count, parse_warnings, replaced_run_id, status,
             created_by_principal)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9, 'committed', $10)
        """,
        context.selected.scope_id,
        run_id,
        connector_namespace,
        source_key,
        kind,
        generation,
        len(created),
        json.dumps(list(warnings)),
        replaced_run_id,
        context.principal.principal_id,
    )
    for role, ordinal, content_id in created:
        await connection.execute(
            """
            INSERT INTO cortex_context.ingest_run_contents
                (scope_id, run_id, content_id, item_role, ordinal)
            VALUES ($1, $2, $3, $4, $5)
            """,
            context.selected.scope_id,
            run_id,
            content_id,
            role,
            int(ordinal),
        )

    receipt = {
        "state": "committed",
        "operation": operation,
        "run_id": str(run_id),
        "generation": generation,
        "item_count": len(created),
        "content_ids": [str(content_id) for _r, _o, content_id in created],
        "warnings": list(warnings),
        "replaced_run_id": str(replaced_run_id) if replaced_run_id else None,
        "policy_revision": policy_revision,
        "scope_id": str(context.selected.scope_id),
        "connector_namespace": connector_namespace,
        "source_key": source_key,
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


async def ingest_transcript(
    connection: asyncpg.Connection,
    context: ScopeContext,
    kind: str,
    idempotency_key: str,
    payload: Any,
) -> tuple[int, dict[str, Any], bool]:
    request = _coerce(IngestTranscriptRequest, payload)
    operation = f"ingest.{kind}"
    digest = request_digest(
        {
            "operation": operation,
            "scope_id": str(context.selected.scope_id),
            "connector_namespace": request.connector_namespace,
            "source_key": request.source_key,
            "transcript_format": request.transcript_format,
            "transcript_sha256": _sha(request.transcript),
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
        return (422 if previous.get("state") == "quarantined" else 200), previous, True

    try:
        outcome = parse_transcript(
            request.transcript_format, request.transcript
        )
        messages: list[IngestItem] = []
        for item in outcome.items:
            canonical_payload = {
                "role": item.role,
                "text": item.text,
                "source_line": item.source_line,
                "origin": {
                    "kind": kind,
                    "transcript_format": request.transcript_format,
                },
                **({"parts": list(item.parts)} if item.parts else {}),
            }
            _enforce_item_limit(
                item.text, canonical_payload, item.source_line
            )
            messages.append(IngestItem(
                item_role="message",
                content_class="message",
                body=item.text,
                payload=canonical_payload,
                observed_at=item.observed_at,
            ))
    except TranscriptParseError as exc:
        policy_revision, _ = await declare_policy_revision(connection,
                                                           context)
        generation, _previous = await _next_generation(
            connection, context, request.connector_namespace,
            request.source_key,
        )
        return await _quarantine(
            connection, context,
            operation=operation,
            idempotency_key=idempotency_key,
            digest=digest,
            connector_namespace=request.connector_namespace,
            source_key=request.source_key,
            kind=kind,
            generation=generation,
            failures=exc.failures,
            warnings=[],
            original={"transcript": request.transcript,
                      "transcript_format": request.transcript_format},
            policy_revision=policy_revision,
        )

    header = None
    if kind == "session":
        header_body = json.dumps(
            {
                "source_key": request.source_key,
                "transcript_format": request.transcript_format,
                "message_count": len(messages),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        header = IngestItem(
            item_role="header",
            content_class="session",
            body=header_body,
            payload={
                "source": request.connector_namespace,
                "message_count": len(messages),
                "origin": {
                    "kind": kind,
                    "transcript_format": request.transcript_format,
                },
            },
            observed_at=request.source_observed_at,
        )
    return await _run_pipeline(
        connection, context,
        kind=kind,
        operation=operation,
        idempotency_key=idempotency_key,
        digest=digest,
        connector_namespace=request.connector_namespace,
        source_key=request.source_key,
        items=messages,
        warnings=outcome.warnings,
        default_observed_at=request.source_observed_at,
        header_item=header,
    )


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def ingest_local_state(
    connection: asyncpg.Connection,
    context: ScopeContext,
    idempotency_key: str,
    payload: Any,
    path_params: dict[str, Any],
) -> tuple[int, dict[str, Any], bool]:
    request = _coerce(IngestLocalStateRequest, payload)
    operation = "ingest.local_state"
    body = json.dumps(request.capture, ensure_ascii=False, sort_keys=True)
    digest = request_digest(
        {
            "operation": operation,
            "scope_id": str(context.selected.scope_id),
            "connector_namespace": request.connector_namespace,
            "source_key": request.source_key,
            "capture_sha256": _sha(body),
        }
    )
    if len(body.encode("utf-8")) > MAX_ITEM_BYTES:
        previous, replayed = await begin_command(
            connection,
            principal_id=context.principal.principal_id,
            operation=operation,
            idempotency_key=idempotency_key,
            digest=digest,
            scope_id=context.selected.scope_id,
        )
        if replayed and previous is not None:
            return (422 if previous.get("state") == "quarantined" else 200), previous, True
        policy_revision, _ = await declare_policy_revision(connection,
                                                           context)
        generation, _previous = await _next_generation(
            connection, context, request.connector_namespace,
            request.source_key,
        )
        return await _quarantine(
            connection, context,
            operation=operation,
            idempotency_key=idempotency_key,
            digest=digest,
            connector_namespace=request.connector_namespace,
            source_key=request.source_key,
            kind="local_state",
            generation=generation,
            failures=[{"line": 0, "code": "state_too_large",
                       "detail": f"capture exceeds {MAX_ITEM_BYTES} bytes"}],
            warnings=[],
            original=request.capture,
            policy_revision=policy_revision,
        )
    item = IngestItem(
        item_role="state",
        content_class="knowledge",
        body=body,
        payload={"content": body, "origin": {"kind": "local_state"}},
        observed_at=request.captured_at,
    )
    return await _run_pipeline(
        connection, context,
        kind="local_state",
        operation=operation,
        idempotency_key=idempotency_key,
        digest=digest,
        connector_namespace=request.connector_namespace,
        source_key=request.source_key,
        items=[item],
        warnings=(),
        default_observed_at=request.captured_at,
        header_item=None,
    )


async def ingest_diary(
    connection: asyncpg.Connection,
    context: ScopeContext,
    idempotency_key: str,
    payload: Any,
    path_params: dict[str, Any],
) -> tuple[int, dict[str, Any], bool]:
    request = _coerce(IngestDiaryRequest, payload)
    operation = "ingest.diary"
    digest = request_digest(
        {
            "operation": operation,
            "scope_id": str(context.selected.scope_id),
            "connector_namespace": request.connector_namespace,
            "source_key": request.source_key,
            "entries_sha256": _sha(
                json.dumps(
                    [entry.model_dump(mode="json") for entry in request.entries],
                    sort_keys=True,
                )
            ),
        }
    )
    items = [
        IngestItem(
            item_role="entry",
            content_class="diary",
            body=entry.text,
            payload={
                "entry": entry.text,
                "origin": {"kind": "diary"},
                "entry_date": entry.entry_date.isoformat(),
            },
            observed_at=None,
        )
        for entry in request.entries
    ]
    return await _run_pipeline(
        connection, context,
        kind="diary",
        operation=operation,
        idempotency_key=idempotency_key,
        digest=digest,
        connector_namespace=request.connector_namespace,
        source_key=request.source_key,
        items=items,
        warnings=(),
        default_observed_at=None,
        header_item=None,
    )


async def ingest_save_chat(
    connection: asyncpg.Connection,
    context: ScopeContext,
    idempotency_key: str,
    payload: Any,
    path_params: dict[str, Any],
) -> tuple[int, dict[str, Any], bool]:
    request = _coerce(IngestSaveChatRequest, payload)
    operation = "ingest.save_chat"
    digest = request_digest(
        {
            "operation": operation,
            "scope_id": str(context.selected.scope_id),
            "connector_namespace": request.connector_namespace,
            "source_key": request.source_key,
            "title": request.title,
            "messages_sha256": _sha(
                json.dumps(
                    [m.model_dump(mode="json") for m in request.messages],
                    sort_keys=True,
                )
            ),
        }
    )
    header_body = json.dumps(
        {"title": request.title, "message_count": len(request.messages)},
        ensure_ascii=False,
        sort_keys=True,
    )
    if len(header_body.encode("utf-8")) > MAX_ITEM_BYTES:
        raise ApiProblem(
            422, "state_too_large",
            f"Chat metadata exceeds {MAX_ITEM_BYTES} bytes.",
        )
    header = IngestItem(
        item_role="header",
        content_class="session",
        body=header_body,
        payload={
            "source": request.connector_namespace,
            "message_count": len(request.messages),
            "origin": {"kind": "save_chat"},
            "title": request.title,
        },
        observed_at=None,
    )
    messages = [
        IngestItem(
            item_role="message",
            content_class="message",
            body=message.text,
            payload={
                "role": message.role,
                "text": message.text,
                "origin": {"kind": "save_chat"},
            },
            observed_at=message.observed_at,
        )
        for message in request.messages
    ]
    return await _run_pipeline(
        connection, context,
        kind="save_chat",
        operation=operation,
        idempotency_key=idempotency_key,
        digest=digest,
        connector_namespace=request.connector_namespace,
        source_key=request.source_key,
        items=messages,
        warnings=(),
        default_observed_at=None,
        header_item=header,
    )


async def get_ingest_run(
    connection: asyncpg.Connection,
    context: ScopeContext,
    payload: Any,
    path_params: dict[str, Any],
) -> dict[str, Any]:
    run_id = uuid.UUID(str(path_params["run_id"]))
    row = await connection.fetchrow(
        """
        SELECT run_id, connector_namespace, source_key, ingest_kind,
               generation, item_count, parse_warnings, status,
               replaced_run_id, created_at
          FROM cortex_context.ingest_runs
         WHERE scope_id = $1 AND run_id = $2
        """,
        context.selected.scope_id,
        run_id,
    )
    if row is None:
        raise ApiProblem(
            404, "ingest_run_not_found",
            "The ingest run is unavailable in the selected scope.",
        )
    contents = await connection.fetch(
        """
        SELECT content_id, item_role, ordinal
          FROM cortex_context.ingest_run_contents
         WHERE scope_id = $1 AND run_id = $2
         ORDER BY ordinal
        """,
        context.selected.scope_id,
        run_id,
    )
    warnings = row["parse_warnings"]
    if isinstance(warnings, str):
        warnings = json.loads(warnings)
    return {
        "run_id": str(row["run_id"]),
        "connector_namespace": row["connector_namespace"],
        "source_key": row["source_key"],
        "ingest_kind": row["ingest_kind"],
        "generation": row["generation"],
        "item_count": row["item_count"],
        "parse_warnings": list(warnings or ()),
        "status": row["status"],
        "replaced_run_id": (
            str(row["replaced_run_id"]) if row["replaced_run_id"] else None
        ),
        "created_at": row["created_at"].isoformat(),
        "contents": [
            {"content_id": str(item["content_id"]),
             "item_role": item["item_role"], "ordinal": item["ordinal"]}
            for item in contents
        ],
    }

