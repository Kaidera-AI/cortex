"""OPERATIONS published by the ingestion connector module (R11)."""

from __future__ import annotations

from typing import Any

from .connectors import (
    get_ingest_run,
    ingest_diary,
    ingest_local_state,
    ingest_save_chat,
    ingest_transcript,
)
from .models import (
    IngestDiaryRequest,
    IngestLocalStateRequest,
    IngestSaveChatRequest,
    IngestTranscriptRequest,
)

_ATOMICITY = (
    "One command is one transaction: a failure never leaves half a replaced "
    "session, and a failed parse quarantines the original payload with typed "
    "failures instead of losing or half-applying it."
)
_REPLACEMENT = (
    "Re-ingesting the same connector namespace + source key creates the next "
    "generation, supersedes or invalidates the replaced items and links the "
    "runs; repeating the same command with the same idempotency key replays "
    "the stored receipt without duplicating messages."
)


async def _ingest_session(connection, context, idempotency_key, payload,
                          path_params):
    return await ingest_transcript(
        connection, context, "session", idempotency_key, payload
    )


async def _ingest_message(connection, context, idempotency_key, payload,
                          path_params):
    return await ingest_transcript(
        connection, context, "message", idempotency_key, payload
    )


def _transcript_card(kind: str, purpose: str, use_when: str) -> dict[str, Any]:
    return {
        "purpose": purpose,
        "use_when": use_when,
        "avoid_when": "The material is authored knowledge; record it as "
        "memory or content instead.",
        "effects": "writes memory through the W1 content use cases",
        "prerequisites": (
            f"a registered connector namespace ({kind} ingest)",
            "write access to the scope",
        ),
        "cost_class": "moderate",
        "freshness": "committed",
        "evidence_contract": _ATOMICITY + " " + _REPLACEMENT,
        "failure_codes": (
            "transcript_parse_failed_quarantined",
            "message_too_large",
            "connector_not_found",
            "source_key_conflict",
            "writer_policy_denied",
            "idempotency_key_reused",
        ),
        "recovery_actions": (
            "split an over-limit capture after a plain 422",
            "fix a failed parse and re-ingest under the same source key",
            "inspect ingest.run only when a quarantined receipt exists",
        ),
        "examples": (),
        "counterexamples": (
            "Re-uploading a corrected transcript under a fresh source key "
            "and leaving the stale generation current.",
        ),
    }


OPERATIONS: list[dict[str, Any]] = [
    {
        "operation_id": "ingest.session",
        "method": "POST",
        "path": "/v1/ingest/sessions",
        "kind": "scoped_write",
        "request_model": IngestTranscriptRequest,
        "handler": _ingest_session,
        "summary": "Ingest a Claude/Codex/Beat session transcript with atomic "
        "replacement.",
        "usage": _transcript_card(
            "session",
            "Preserve a full session transcript as canonical content with "
            "connector provenance and generation-based replacement.",
            "Uploading or re-uploading a worker session transcript.",
        ),
    },
    {
        "operation_id": "ingest.message",
        "method": "POST",
        "path": "/v1/ingest/messages",
        "kind": "scoped_write",
        "request_model": IngestTranscriptRequest,
        "handler": _ingest_message,
        "summary": "Ingest a transcript as standalone messages (no session "
        "header).",
        "usage": _transcript_card(
            "message",
            "Preserve message-level material without a session envelope.",
            "Uploading message streams that are not a single session.",
        ),
    },
    {
        "operation_id": "ingest.local_state",
        "method": "POST",
        "path": "/v1/ingest/local-state",
        "kind": "scoped_write",
        "request_model": IngestLocalStateRequest,
        "handler": ingest_local_state,
        "summary": "Capture local state as knowledge content with atomic "
        "replacement.",
        "usage": {
            "purpose": "Preserve a local-state capture with its connector "
            "identity; re-capture supersedes the previous generation.",
            "use_when": "A worker's local state snapshot must become "
            "canonical memory.",
            "avoid_when": "The state is ephemeral process data nobody will "
            "read.",
            "effects": "writes memory through the W1 content use cases",
            "prerequisites": ("a registered connector namespace",
                              "write access to the scope"),
            "cost_class": "cheap",
            "freshness": "committed",
            "evidence_contract": _ATOMICITY + " " + _REPLACEMENT,
            "failure_codes": ("state_too_large",
                              "state_too_large_quarantined",
                              "connector_not_found",
                              "idempotency_key_reused"),
            "recovery_actions": (
                "split the capture after a plain size refusal",
                "inspect ingest.run only when a quarantined receipt exists",
            ),
            "examples": (),
            "counterexamples": (),
        },
    },
    {
        "operation_id": "ingest.diary",
        "method": "POST",
        "path": "/v1/ingest/diaries",
        "kind": "scoped_write",
        "request_model": IngestDiaryRequest,
        "handler": ingest_diary,
        "summary": "Ingest diary entries with atomic replacement.",
        "usage": {
            "purpose": "Preserve dated diary entries as canonical diary "
            "content.",
            "use_when": "Syncing a diary capture into Cortex.",
            "avoid_when": "The entry is a decision/lesson; record it as "
            "memory for retrieval.",
            "effects": "writes memory through the W1 content use cases",
            "prerequisites": ("a registered connector namespace",
                              "write access to the scope"),
            "cost_class": "cheap",
            "freshness": "committed",
            "evidence_contract": _ATOMICITY + " " + _REPLACEMENT,
            "failure_codes": ("message_too_large", "connector_not_found",
                              "idempotency_key_reused"),
            "recovery_actions": (
                "split an over-limit diary entry after a plain 422",
            ),
            "examples": (),
            "counterexamples": (),
        },
    },
    {
        "operation_id": "ingest.save_chat",
        "method": "POST",
        "path": "/v1/ingest/save-chats",
        "kind": "scoped_write",
        "request_model": IngestSaveChatRequest,
        "handler": ingest_save_chat,
        "summary": "Save a chat (title + messages) with atomic replacement.",
        "usage": {
            "purpose": "Preserve an explicitly saved chat as session content "
            "plus message items.",
            "use_when": "A user or worker explicitly saves a conversation.",
            "avoid_when": "Transcript ingestion was not opted in; saving "
            "requires consent.",
            "effects": "writes memory through the W1 content use cases",
            "prerequisites": ("a registered connector namespace",
                              "write access to the scope",
                              "explicit save/consent action"),
            "cost_class": "moderate",
            "freshness": "committed",
            "evidence_contract": _ATOMICITY + " " + _REPLACEMENT,
            "failure_codes": ("state_too_large", "message_too_large",
                              "connector_not_found", "idempotency_key_reused"),
            "recovery_actions": ("split the chat after a plain size refusal",),
            "examples": (),
            "counterexamples": (
                "Silently ingesting every live session without an explicit "
                "save/consent.",
            ),
        },
    },
    {
        "operation_id": "ingest.run",
        "method": "GET",
        "path": "/v1/ingest/runs/{run_id}",
        "kind": "scoped_read",
        "request_model": None,
        "handler": get_ingest_run,
        "summary": "Inspect one connector ingest run and its contents.",
        "usage": {
            "purpose": "Read the connector ledger: generation, item count, "
            "parse warnings, quarantine state and content links.",
            "use_when": "Verifying an ingestion receipt or investigating a "
            "quarantine.",
            "avoid_when": "You need the content itself; use content.inspect.",
            "effects": "read-only",
            "prerequisites": ("a run in the selected scope",),
            "cost_class": "cheap",
            "freshness": "live",
            "evidence_contract": "Ledger rows are append-only and cite exact "
            "content ids.",
            "failure_codes": ("ingest_run_not_found",),
            "recovery_actions": (),
            "examples": (),
            "counterexamples": (),
        },
    },
]
