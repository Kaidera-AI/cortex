"""Deterministic identity and effect keys for the Processing module.

Execution is at-least-once, so every projection row needs a stable output key
that makes a re-executed attempt idempotent instead of duplicative (N03, F06).
All keys are derived from canonical inputs — scope, content revision, space,
chunking policy identity and ordinal — never from attempt identity, clock or
worker id.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import UUID, uuid5

#: Stable namespace for every Processing-derived UUID5. Versioned by value:
#: changing it would silently re-key existing projections, so it is fixed.
PROCESSING_NAMESPACE = UUID("6a1f0c6e-6f2b-5a4d-9c31-2f8b7a4d1e05")


def _digest(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


def text_sha256(text: str) -> bytes:
    return hashlib.sha256(text.encode("utf-8")).digest()


def payload_sha256(payload: dict[str, Any]) -> bytes:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).digest()


def chunk_key(
    *,
    scope_id: UUID,
    content_id: UUID,
    revision: int,
    space_id: UUID,
    ordinal: int,
    policy_identity: str,
) -> UUID:
    """Stable chunk identity for one revision inside one space's chunk policy.

    The same revision re-chunked by the same policy yields the same IDs, so a
    retried attempt upserts onto its own rows instead of appending duplicates.
    """
    return uuid5(
        PROCESSING_NAMESPACE,
        _digest(
            "chunk",
            str(scope_id),
            str(content_id),
            str(revision),
            str(space_id),
            str(ordinal),
            policy_identity,
        ),
    )


def job_dedupe_key(
    *,
    job_kind: str,
    scope_id: UUID,
    content_id: UUID,
    revision: int,
    profile_identity: str,
    space_id: UUID | None = None,
    generation_id: UUID | None = None,
) -> str:
    """Unique accepted-work key: one durable job per identical intent."""
    return _digest(
        job_kind,
        str(scope_id),
        str(content_id),
        str(revision),
        profile_identity,
        str(space_id) if space_id else "-",
        str(generation_id) if generation_id else "-",
    )


def batch_key(*parts: str) -> str:
    return _digest("batch", *parts)[:32]


def distillation_key(
    *,
    scope_id: UUID,
    content_id: UUID,
    revision: int,
    transform_identity: str,
) -> UUID:
    """Stable derived-transform identity; originals are never touched."""
    return uuid5(
        PROCESSING_NAMESPACE,
        _digest(
            "distillation",
            str(scope_id),
            str(content_id),
            str(revision),
            transform_identity,
        ),
    )
