"""Asyncpg repositories for the Processing projection plane.

Every function expects a connection that is already inside a transaction with
transaction-local Cortex context set (``cortex.principal_id``,
``cortex.read_scope_ids``, ``cortex.write_scope_id``) — the same convention as
W1's ``store.py``/``content.py``. Row level security is therefore the second
boundary on every statement here; nothing in this module bypasses it and no
statement reaches into another module's private columns.

Vectors cross the wire as pgvector text literals with an explicit
``::public.vector`` cast. The column type has no fixed dimension, so the only
dimension check is the recorded space dimension, enforced by the database
trigger and re-checked here before the round trip (F07).
"""

from __future__ import annotations

import json
import math
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

import asyncpg

from .contracts import (
    SpaceContract,
    StoredChunk,
)
from .keys import payload_sha256

MAX_VECTOR_LITERAL_VALUES = 16_000


class ProjectionError(RuntimeError):
    """A projection write was refused before it reached the database."""


def format_vector(values: Sequence[float]) -> str:
    """Render a pgvector literal, rejecting non-finite components.

    pgvector itself refuses NaN and infinities; checking first keeps the typed
    outcome in our vocabulary instead of an opaque database error.
    """
    if not values:
        raise ProjectionError("an embedding must have at least one dimension")
    if len(values) > MAX_VECTOR_LITERAL_VALUES:
        raise ProjectionError("an embedding exceeds the supported dimension count")
    parts: list[str] = []
    for value in values:
        number = float(value)
        if not math.isfinite(number):
            raise ProjectionError("an embedding component must be finite")
        parts.append(repr(number))
    return f"[{','.join(parts)}]"


def _json(value: str | dict[str, Any] | list[Any] | None) -> Any:
    if value is None:
        return None
    return json.loads(value) if isinstance(value, str) else value


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def iso(value: datetime | None) -> str | None:
    """Public timestamp rendering shared by the queue and command read models."""
    return _iso(value)


def _texts(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    return tuple(str(item) for item in value)


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------


async def list_profiles(
    connection: asyncpg.Connection,
    *,
    installation_id: uuid.UUID,
    include_retired: bool = False,
) -> list[dict[str, Any]]:
    rows = await connection.fetch(
        """
        SELECT profile_id, installation_id, profile_key, profile_version,
               parser, chunking, embedder, transforms, coverage, status, created_at
          FROM cortex_processing.processing_profiles
         WHERE (installation_id IS NULL OR installation_id = $1)
           AND ($2 OR status = 'active')
         ORDER BY profile_key, profile_version DESC
        """,
        installation_id,
        include_retired,
    )
    return [_profile_row(row) for row in rows]


async def get_profile(
    connection: asyncpg.Connection, profile_id: uuid.UUID
) -> dict[str, Any] | None:
    row = await connection.fetchrow(
        """
        SELECT profile_id, installation_id, profile_key, profile_version,
               parser, chunking, embedder, transforms, coverage, status, created_at
          FROM cortex_processing.processing_profiles
         WHERE profile_id = $1
        """,
        profile_id,
    )
    return _profile_row(row) if row else None


def _profile_row(row: Any) -> dict[str, Any]:
    return {
        "profile_id": str(row["profile_id"]),
        "installation_id": str(row["installation_id"]) if row["installation_id"] else None,
        "builtin": row["installation_id"] is None,
        "profile_key": row["profile_key"],
        "profile_version": row["profile_version"],
        "profile_identity": f"{row['profile_key']}@{row['profile_version']}",
        "parser": _json(row["parser"]),
        "chunking": _json(row["chunking"]),
        "embedder": _json(row["embedder"]),
        "transforms": _json(row["transforms"]),
        "coverage": _json(row["coverage"]),
        "status": row["status"],
        "created_at": _iso(row["created_at"]),
    }


def profile_identity(profile: dict[str, Any]) -> str:
    return profile["profile_identity"]


def chunking_identity(chunking: dict[str, Any] | None) -> str | None:
    """``<policy_id>@<version>`` of a chunking policy block, or None."""
    if not isinstance(chunking, dict):
        return None
    policy_id = chunking.get("policy_id")
    version = chunking.get("version")
    if policy_id is None or version is None:
        return None
    return f"{policy_id}@{version}"


# ---------------------------------------------------------------------------
# Embedding spaces and index generations
# ---------------------------------------------------------------------------


async def list_spaces(connection: asyncpg.Connection) -> list[dict[str, Any]]:
    rows = await connection.fetch(
        """
        SELECT s.space_id, s.space_name, s.provider, s.model_id, s.model_revision,
               s.dimensions, s.normalization, s.metric, s.query_prefix,
               s.document_prefix, s.chunking, s.active_generation_id, s.created_at,
               g.generation AS active_generation, g.state AS active_state
          FROM cortex_processing.embedding_spaces AS s
          LEFT JOIN cortex_processing.index_generations AS g
                 ON g.generation_id = s.active_generation_id
         ORDER BY s.space_name
        """
    )
    return [_space_row(row) for row in rows]


async def get_space(
    connection: asyncpg.Connection, space_id: uuid.UUID
) -> dict[str, Any] | None:
    row = await connection.fetchrow(
        """
        SELECT s.space_id, s.space_name, s.provider, s.model_id, s.model_revision,
               s.dimensions, s.normalization, s.metric, s.query_prefix,
               s.document_prefix, s.chunking, s.active_generation_id, s.created_at,
               g.generation AS active_generation, g.state AS active_state
          FROM cortex_processing.embedding_spaces AS s
          LEFT JOIN cortex_processing.index_generations AS g
                 ON g.generation_id = s.active_generation_id
         WHERE s.space_id = $1
        """,
        space_id,
    )
    return _space_row(row) if row else None


def _space_row(row: Any) -> dict[str, Any]:
    return {
        "space_id": str(row["space_id"]),
        "space_name": row["space_name"],
        "provider": row["provider"],
        "model_id": row["model_id"],
        "model_revision": row["model_revision"],
        "dimensions": row["dimensions"],
        "normalization": row["normalization"],
        "metric": row["metric"],
        "query_prefix": row["query_prefix"],
        "document_prefix": row["document_prefix"],
        "chunking": _json(row["chunking"]),
        "active_generation_id": (
            str(row["active_generation_id"]) if row["active_generation_id"] else None
        ),
        "active_generation": row["active_generation"],
        "active_state": row["active_state"],
        "created_at": _iso(row["created_at"]),
    }


async def create_space(
    connection: asyncpg.Connection,
    *,
    principal_id: uuid.UUID,
    space_name: str,
    provider: str,
    model_id: str,
    model_revision: str | None,
    dimensions: int,
    normalization: str,
    metric: str,
    query_prefix: str | None,
    document_prefix: str | None,
    chunking: dict[str, Any],
) -> dict[str, Any]:
    row = await connection.fetchrow(
        """
        SELECT space_id, generation_id, generation
          FROM cortex_processing.create_embedding_space(
              $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::jsonb
          )
        """,
        principal_id,
        space_name,
        provider,
        model_id,
        model_revision,
        dimensions,
        normalization,
        metric,
        query_prefix,
        document_prefix,
        json.dumps(chunking, ensure_ascii=False, sort_keys=True),
    )
    return {
        "space_id": str(row["space_id"]),
        "generation_id": str(row["generation_id"]),
        "generation": row["generation"],
        "generation_state": "active",
    }


async def list_generations(
    connection: asyncpg.Connection, space_id: uuid.UUID
) -> list[dict[str, Any]]:
    rows = await connection.fetch(
        """
        SELECT generation_id, generation, state, created_at, built_at, activated_at,
               retired_at, failure_code, profile_id
          FROM cortex_processing.index_generations
         WHERE space_id = $1
         ORDER BY generation DESC
        """,
        space_id,
    )
    return [
        {
            "generation_id": str(row["generation_id"]),
            "generation": row["generation"],
            "state": row["state"],
            "created_at": _iso(row["created_at"]),
            "built_at": _iso(row["built_at"]),
            "activated_at": _iso(row["activated_at"]),
            "retired_at": _iso(row["retired_at"]),
            "failure_code": row["failure_code"],
            "profile_id": str(row["profile_id"]) if row["profile_id"] else None,
        }
        for row in rows
    ]


async def begin_generation(
    connection: asyncpg.Connection,
    *,
    principal_id: uuid.UUID,
    space_id: uuid.UUID,
    profile_id: uuid.UUID | None,
) -> dict[str, Any]:
    row = await connection.fetchrow(
        """
        SELECT generation_id, generation
          FROM cortex_processing.begin_index_generation($1, $2, $3)
        """,
        principal_id,
        space_id,
        profile_id,
    )
    return {
        "generation_id": str(row["generation_id"]),
        "generation": row["generation"],
        "state": "building",
    }


async def activate_generation(
    connection: asyncpg.Connection,
    *,
    principal_id: uuid.UUID,
    space_id: uuid.UUID,
    generation_id: uuid.UUID,
) -> dict[str, Any]:
    row = await connection.fetchrow(
        """
        SELECT space_id, generation_id, generation, state
          FROM cortex_processing.activate_index_generation($1, $2, $3)
        """,
        principal_id,
        space_id,
        generation_id,
    )
    return {
        "space_id": str(row["space_id"]),
        "generation_id": str(row["generation_id"]),
        "generation": row["generation"],
        "state": row["state"],
    }


async def mark_generation(
    connection: asyncpg.Connection,
    *,
    generation_id: uuid.UUID,
    state: str,
    failure_code: str | None = None,
) -> bool:
    """Move a building generation to ``built`` or ``failed`` (worker only)."""
    result = await connection.execute(
        """
        UPDATE cortex_processing.index_generations
           SET state = $2,
               failure_code = $3
         WHERE generation_id = $1
           AND state = 'building'
        """,
        generation_id,
        state,
        failure_code,
    )
    return result.endswith("1")


async def read_space_contract(
    connection: asyncpg.Connection,
    space_id: uuid.UUID,
    *,
    generation_id: uuid.UUID | None = None,
) -> tuple[SpaceContract, uuid.UUID | None, str | None] | None:
    """Load immutable space semantics plus the generation being written.

    Returns ``(contract, generation_id, generation_state)``. When no explicit
    generation is given the active one is used.
    """
    if generation_id is not None:
        row = await connection.fetchrow(
            """
            SELECT s.space_id, s.space_name, s.provider, s.model_id, s.model_revision,
                   s.dimensions, s.normalization, s.metric, s.query_prefix,
                   s.document_prefix, s.chunking, g.generation_id, g.state
              FROM cortex_processing.embedding_spaces AS s
              JOIN cortex_processing.index_generations AS g ON g.space_id = s.space_id
             WHERE s.space_id = $1
               AND g.generation_id = $2
            """,
            space_id,
            generation_id,
        )
    else:
        row = await connection.fetchrow(
            """
            SELECT s.space_id, s.space_name, s.provider, s.model_id, s.model_revision,
                   s.dimensions, s.normalization, s.metric, s.query_prefix,
                   s.document_prefix, s.chunking, g.generation_id, g.state
              FROM cortex_processing.embedding_spaces AS s
              LEFT JOIN cortex_processing.index_generations AS g
                     ON g.generation_id = s.active_generation_id
             WHERE s.space_id = $1
            """,
            space_id,
        )
    if row is None:
        return None
    contract = SpaceContract(
        space_id=row["space_id"],
        space_name=row["space_name"],
        provider=row["provider"],
        model_id=row["model_id"],
        model_revision=row["model_revision"],
        dimensions=row["dimensions"],
        normalization=row["normalization"],
        metric=row["metric"],
        query_prefix=row["query_prefix"],
        document_prefix=row["document_prefix"],
        chunking_identity=chunking_identity(_json(row["chunking"])),
    )
    return contract, row["generation_id"], row["state"]


async def read_space_chunking(
    connection: asyncpg.Connection, space_id: uuid.UUID
) -> dict[str, Any] | None:
    return _json(
        await connection.fetchval(
            "SELECT chunking FROM cortex_processing.embedding_spaces WHERE space_id = $1",
            space_id,
        )
    )


# ---------------------------------------------------------------------------
# Canonical source reads (W1 tables, read-only)
# ---------------------------------------------------------------------------


async def read_source_revision(
    connection: asyncpg.Connection,
    *,
    scope_id: uuid.UUID,
    content_id: uuid.UUID,
    revision: int,
) -> dict[str, Any] | None:
    row = await connection.fetchrow(
        """
        SELECT i.content_class,
               r.body_text,
               r.payload,
               r.content_hash,
               r.authored_at,
               cortex_core.content_current_status(i.scope_id, i.content_id) AS status
          FROM cortex_core.content_items AS i
          JOIN cortex_core.content_revisions AS r
            ON r.scope_id = i.scope_id
           AND r.content_id = i.content_id
         WHERE i.scope_id = $1
           AND i.content_id = $2
           AND r.revision = $3
        """,
        scope_id,
        content_id,
        revision,
    )
    if row is None:
        return None
    payload = _json(row["payload"]) or {}
    return {
        "scope_id": scope_id,
        "content_id": content_id,
        "revision": revision,
        "content_class": row["content_class"],
        "status": row["status"],
        "body_text": row["body_text"],
        "payload": payload,
        "content_hash": bytes(row["content_hash"]),
        "authored_at": _iso(row["authored_at"]),
        "media_type": str(payload.get("media_type") or "text/plain"),
        "filename": (
            str(payload["filename"])
            if isinstance(payload.get("filename"), str)
            else None
        ),
        "location": (
            str(payload["location"]) if isinstance(payload.get("location"), str) else None
        ),
    }


async def current_revision(
    connection: asyncpg.Connection, *, scope_id: uuid.UUID, content_id: uuid.UUID
) -> tuple[int, str] | None:
    """Highest stored revision plus the canonical lifecycle status."""
    row = await connection.fetchrow(
        """
        SELECT max(r.revision) AS revision,
               cortex_core.content_current_status($1, $2) AS status
          FROM cortex_core.content_revisions AS r
         WHERE r.scope_id = $1
           AND r.content_id = $2
        """,
        scope_id,
        content_id,
    )
    if row is None or row["revision"] is None:
        return None
    return int(row["revision"]), row["status"]


async def verify_source_current(
    connection: asyncpg.Connection,
    *,
    scope_id: uuid.UUID,
    content_id: uuid.UUID,
    revision: int,
) -> str | None:
    """Completion-time source recheck required before publishing an effect.

    Returns None when the pinned revision is still the current, live one, and a
    typed reason otherwise. A newer revision means the projection would be born
    stale, so the attempt is cancelled instead of published (§5, F06).
    """
    current = await current_revision(
        connection, scope_id=scope_id, content_id=content_id
    )
    if current is None:
        return "source_missing"
    latest, status = current
    if status == "tombstoned":
        return "source_tombstoned"
    if status == "invalidated":
        return "source_invalidated"
    if latest != revision:
        return "source_superseded"
    return None


async def intended_revisions(
    connection: asyncpg.Connection,
    *,
    scope_id: uuid.UUID,
    content_classes: Sequence[str],
    min_text_length: int,
    cursor: tuple[str, uuid.UUID] | None = None,
    limit: int,
) -> list[dict[str, Any]]:
    """Current revisions the profile's coverage policy intends to process (R13).

    Ordered by a stable keyset (``created_at``, ``content_id``) so a bounded
    backfill batch can resume without rescanning or duplicating work.
    """
    rows = await connection.fetch(
        """
        SELECT i.content_id,
               latest.revision,
               i.content_class,
               i.created_at,
               length(r.body_text) AS text_length,
               r.payload,
               cortex_core.content_current_status(i.scope_id, i.content_id) AS status
          FROM cortex_core.content_items AS i
          JOIN LATERAL (
               SELECT max(r.revision) AS revision
                 FROM cortex_core.content_revisions AS r
                WHERE r.scope_id = i.scope_id
                  AND r.content_id = i.content_id
          ) AS latest ON true
          JOIN cortex_core.content_revisions AS r
            ON r.scope_id = i.scope_id
           AND r.content_id = i.content_id
           AND r.revision = latest.revision
         WHERE i.scope_id = $1
           AND i.content_class = ANY($2::text[])
           AND length(r.body_text) >= $3
           AND cortex_core.content_current_status(i.scope_id, i.content_id) = 'current'
           AND ($4::timestamptz IS NULL OR (i.created_at, i.content_id) > ($4::timestamptz, $5::uuid))
         ORDER BY i.created_at, i.content_id
         LIMIT $6
        """,
        scope_id,
        list(content_classes),
        min_text_length,
        cursor[0] if cursor else None,
        cursor[1] if cursor else None,
        limit,
    )
    return [
        {
            "content_id": row["content_id"],
            "revision": int(row["revision"]),
            "content_class": row["content_class"],
            "created_at": _iso(row["created_at"]),
            "text_length": int(row["text_length"]),
            "payload": _json(row["payload"]) or {},
        }
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Chunk revisions and vectors
# ---------------------------------------------------------------------------


async def insert_chunks(
    connection: asyncpg.Connection, rows: Iterable[dict[str, Any]]
) -> int:
    """Insert chunk revisions idempotently; returns the number of new rows.

    The primary key is the canonical projection key, so a re-executed attempt
    upserts onto its own rows instead of duplicating them (N03).
    """
    inserted = 0
    for row in rows:
        result = await connection.execute(
            """
            INSERT INTO cortex_processing.chunk_revisions
                (scope_id, content_id, revision, space_id, chunk_ordinal, chunk_id,
                 chunking_policy, text_sha256, chunk_text, span, block_spans,
                 block_kinds, heading_path, format_id, executor, job_id,
                 truncated, warnings)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb, $11::jsonb,
                    $12::text[], $13::text[], $14, $15, $16, $17, $18::text[])
            ON CONFLICT DO NOTHING
            """,
            row["scope_id"],
            row["content_id"],
            row["revision"],
            row["space_id"],
            row["ordinal"],
            row["chunk_id"],
            row["chunking_policy"],
            row["text_sha256"],
            row["text"],
            json.dumps(row["span"], ensure_ascii=False, sort_keys=True),
            json.dumps(row["block_spans"], ensure_ascii=False),
            list(row["block_kinds"]),
            list(row["heading_path"]),
            row.get("format_id"),
            row.get("executor"),
            row.get("job_id"),
            bool(row.get("truncated", False)),
            list(row.get("warnings", ())),
        )
        inserted += int(result.rsplit(" ", 1)[-1])
    return inserted


async def read_chunks(
    connection: asyncpg.Connection,
    *,
    scope_id: uuid.UUID,
    content_id: uuid.UUID,
    revision: int,
    space_id: uuid.UUID,
    limit: int = 10_000,
) -> tuple[StoredChunk, ...]:
    rows = await connection.fetch(
        """
        SELECT chunk_id, chunk_ordinal, chunk_text, span, block_kinds, text_sha256
          FROM cortex_processing.chunk_revisions
         WHERE scope_id = $1
           AND content_id = $2
           AND revision = $3
           AND space_id = $4
         ORDER BY chunk_ordinal
         LIMIT $5
        """,
        scope_id,
        content_id,
        revision,
        space_id,
        limit,
    )
    return tuple(
        StoredChunk(
            chunk_id=row["chunk_id"],
            ordinal=int(row["chunk_ordinal"]),
            text=row["chunk_text"],
            span=_json(row["span"]) or {},
            block_kinds=_texts(row["block_kinds"]),
            text_sha256=bytes(row["text_sha256"]),
        )
        for row in rows
    )


async def count_chunks(
    connection: asyncpg.Connection,
    *,
    scope_id: uuid.UUID,
    space_id: uuid.UUID | None = None,
) -> int:
    if space_id is None:
        return int(
            await connection.fetchval(
                "SELECT count(*) FROM cortex_processing.chunk_revisions WHERE scope_id = $1",
                scope_id,
            )
        )
    return int(
        await connection.fetchval(
            """
            SELECT count(*)
              FROM cortex_processing.chunk_revisions
             WHERE scope_id = $1 AND space_id = $2
            """,
            scope_id,
            space_id,
        )
    )


async def insert_vectors(
    connection: asyncpg.Connection, rows: Iterable[dict[str, Any]]
) -> int:
    """Insert vectors for one generation idempotently on ``(chunk_id, generation_id)``."""
    inserted = 0
    for row in rows:
        result = await connection.execute(
            """
            INSERT INTO cortex_processing.chunk_vectors
                (chunk_id, generation_id, scope_id, space_id, content_id, revision,
                 embedding, provider, model_id, model_revision, normalization,
                 job_id, attempt_epoch)
            VALUES ($1, $2, $3, $4, $5, $6, $7::public.vector, $8, $9, $10, $11, $12, $13)
            ON CONFLICT (chunk_id, generation_id) DO NOTHING
            """,
            row["chunk_id"],
            row["generation_id"],
            row["scope_id"],
            row["space_id"],
            row["content_id"],
            row["revision"],
            format_vector(row["embedding"]),
            row["provider"],
            row["model_id"],
            row["model_revision"],
            row["normalization"],
            row.get("job_id"),
            row["attempt_epoch"],
        )
        inserted += int(result.rsplit(" ", 1)[-1])
    return inserted


async def count_vectors(
    connection: asyncpg.Connection,
    *,
    scope_id: uuid.UUID,
    generation_id: uuid.UUID,
) -> int:
    return int(
        await connection.fetchval(
            """
            SELECT count(*)
              FROM cortex_processing.chunk_vectors
             WHERE scope_id = $1 AND generation_id = $2
            """,
            scope_id,
            generation_id,
        )
    )


async def embedded_revisions(
    connection: asyncpg.Connection,
    *,
    scope_id: uuid.UUID,
    generation_id: uuid.UUID,
    content_ids: Sequence[uuid.UUID],
) -> set[uuid.UUID]:
    """Which of these content ids already have a vector in this generation."""
    if not content_ids:
        return set()
    rows = await connection.fetch(
        """
        SELECT DISTINCT content_id
          FROM cortex_processing.chunk_vectors
         WHERE scope_id = $1
           AND generation_id = $2
           AND content_id = ANY($3::uuid[])
        """,
        scope_id,
        generation_id,
        list(content_ids),
    )
    return {row["content_id"] for row in rows}


async def chunked_revisions(
    connection: asyncpg.Connection,
    *,
    scope_id: uuid.UUID,
    space_id: uuid.UUID,
    content_ids: Sequence[uuid.UUID],
) -> set[tuple[uuid.UUID, int]]:
    if not content_ids:
        return set()
    rows = await connection.fetch(
        """
        SELECT DISTINCT content_id, revision
          FROM cortex_processing.chunk_revisions
         WHERE scope_id = $1
           AND space_id = $2
           AND content_id = ANY($3::uuid[])
        """,
        scope_id,
        space_id,
        list(content_ids),
    )
    return {(row["content_id"], int(row["revision"])) for row in rows}


async def insert_distillation(
    connection: asyncpg.Connection, row: dict[str, Any]
) -> bool:
    result = await connection.execute(
        """
        INSERT INTO cortex_processing.distillations
            (distillation_id, scope_id, content_id, revision, transform_kind,
             transform_identity, profile_id, output_text, output_payload,
             commitments, coverage, job_id)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10::text[], $11::jsonb, $12)
        ON CONFLICT (scope_id, content_id, revision, transform_identity) DO NOTHING
        """,
        row["distillation_id"],
        row["scope_id"],
        row["content_id"],
        row["revision"],
        row["transform_kind"],
        row["transform_identity"],
        row["profile_id"],
        row["output_text"],
        json.dumps(row.get("output_payload") or {}, ensure_ascii=False, sort_keys=True),
        list(row.get("commitments") or ()),
        json.dumps(row.get("coverage") or {}, ensure_ascii=False, sort_keys=True),
        row.get("job_id"),
    )
    return result.endswith("1")


# ---------------------------------------------------------------------------
# Quarantine ledger
# ---------------------------------------------------------------------------


async def insert_quarantine(
    connection: asyncpg.Connection,
    *,
    scope_id: uuid.UUID,
    job_id: uuid.UUID | None,
    content_id: uuid.UUID | None,
    revision: int | None,
    reason_code: str,
    reason_detail: str | None,
    preserved_payload: dict[str, Any],
    executor: str | None,
    format_id: str | None,
) -> uuid.UUID:
    quarantine_id = uuid.uuid4()
    await connection.execute(
        """
        INSERT INTO cortex_processing.quarantine_ledger
            (quarantine_id, scope_id, job_id, content_id, revision, reason_code,
             reason_detail, preserved_payload, payload_sha256, executor, format_id)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9, $10, $11)
        ON CONFLICT (scope_id, job_id, reason_code) DO NOTHING
        """,
        quarantine_id,
        scope_id,
        job_id,
        content_id,
        revision,
        reason_code,
        (reason_detail or "")[:512] or None,
        json.dumps(preserved_payload, ensure_ascii=False, sort_keys=True),
        payload_sha256(preserved_payload),
        executor,
        format_id,
    )
    return quarantine_id


async def count_open_quarantine(
    connection: asyncpg.Connection, *, scope_id: uuid.UUID
) -> int:
    """Quarantine entries still awaiting an operator disposition."""
    return int(
        await connection.fetchval(
            """
            SELECT count(*)
              FROM cortex_processing.quarantine_ledger
             WHERE scope_id = $1
               AND disposition = 'review'
            """,
            scope_id,
        )
    )


async def list_quarantine(
    connection: asyncpg.Connection,
    *,
    scope_id: uuid.UUID,
    disposition: str = "review",
    limit: int = 50,
) -> list[dict[str, Any]]:
    rows = await connection.fetch(
        """
        SELECT quarantine_id, job_id, content_id, revision, reason_code,
               reason_detail, executor, format_id, disposition, created_at,
               resolved_at, octet_length(preserved_payload::text) AS payload_bytes
          FROM cortex_processing.quarantine_ledger
         WHERE scope_id = $1
           AND disposition = $2
         ORDER BY created_at DESC, quarantine_id
         LIMIT $3
        """,
        scope_id,
        disposition,
        limit,
    )
    return [
        {
            "quarantine_id": str(row["quarantine_id"]),
            "job_id": str(row["job_id"]) if row["job_id"] else None,
            "content_id": str(row["content_id"]) if row["content_id"] else None,
            "revision": row["revision"],
            "reason_code": row["reason_code"],
            "reason_detail": row["reason_detail"],
            "executor": row["executor"],
            "format_id": row["format_id"],
            "disposition": row["disposition"],
            "created_at": _iso(row["created_at"]),
            "resolved_at": _iso(row["resolved_at"]),
            "payload_bytes": int(row["payload_bytes"]),
        }
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Transactional outbox (feed source: cortex_processing.outbox_events)
# ---------------------------------------------------------------------------


async def insert_outbox_event(
    connection: asyncpg.Connection,
    *,
    scope_id: uuid.UUID,
    aggregate_kind: str,
    aggregate_id: uuid.UUID,
    event_type: str,
    aggregate_version: int,
    payload: dict[str, Any] | None = None,
) -> uuid.UUID:
    """Record one durable change fact beside the state change it describes.

    ``aggregate_version`` is monotonic per aggregate (a job's fencing epoch, a
    generation number) because the feed dispatcher orders per-aggregate versions
    and never invents a global causal order.
    """
    event_id = uuid.uuid4()
    body = dict(payload or {})
    body["aggregate_version"] = int(aggregate_version)
    body.setdefault("schema_version", 1)
    await connection.execute(
        """
        INSERT INTO cortex_processing.outbox_events
            (event_id, scope_id, aggregate_kind, aggregate_id, event_type, payload)
        VALUES ($1, $2, $3, $4, $5, $6::jsonb)
        """,
        event_id,
        scope_id,
        aggregate_kind,
        aggregate_id,
        event_type,
        json.dumps(body, ensure_ascii=False, sort_keys=True, default=str),
    )
    return event_id


# ---------------------------------------------------------------------------
# Worker registry (heartbeats make a missing executor role observable)
# ---------------------------------------------------------------------------


async def register_worker(
    connection: asyncpg.Connection,
    *,
    worker_id: str,
    installation_id: uuid.UUID,
    principal_id: uuid.UUID,
    worker_role: str,
    executor_roles: Sequence[str],
    handler_kinds: Sequence[str],
    package_version: str,
) -> None:
    await connection.execute(
        """
        INSERT INTO cortex_processing.workers
            (worker_id, installation_id, principal_id, worker_role, executor_roles,
             handler_kinds, package_version, started_at, last_heartbeat_at)
        VALUES ($1, $2, $3, $4, $5::text[], $6::text[], $7, now(), now())
        ON CONFLICT (worker_id) DO UPDATE
           SET executor_roles = EXCLUDED.executor_roles,
               handler_kinds = EXCLUDED.handler_kinds,
               package_version = EXCLUDED.package_version,
               last_heartbeat_at = now(),
               stopped_at = NULL
        """,
        worker_id,
        installation_id,
        principal_id,
        worker_role,
        list(executor_roles),
        list(handler_kinds),
        package_version,
    )


async def heartbeat_worker(
    connection: asyncpg.Connection,
    *,
    worker_id: str,
    stats: dict[str, int] | None = None,
) -> bool:
    result = await connection.execute(
        """
        UPDATE cortex_processing.workers
           SET last_heartbeat_at = now(),
               stats = $2::jsonb
         WHERE worker_id = $1
        """,
        worker_id,
        json.dumps(stats or {}, ensure_ascii=False, sort_keys=True),
    )
    return result.endswith("1")


async def stop_worker(
    connection: asyncpg.Connection,
    *,
    worker_id: str,
    stats: dict[str, Any] | None = None,
) -> None:
    """Deregister a worker, recording its final counters."""
    await connection.execute(
        """
        UPDATE cortex_processing.workers
           SET stopped_at = now(),
               last_heartbeat_at = now(),
               stats = COALESCE($2::jsonb, stats)
         WHERE worker_id = $1
        """,
        worker_id,
        None if stats is None else json.dumps(stats, ensure_ascii=False, sort_keys=True),
    )


async def live_workers(
    connection: asyncpg.Connection, *, max_age_seconds: int
) -> list[dict[str, Any]]:
    rows = await connection.fetch(
        """
        SELECT worker_id, worker_role, executor_roles, handler_kinds,
               package_version, started_at, last_heartbeat_at, stats
          FROM cortex_processing.workers
         WHERE stopped_at IS NULL
           AND last_heartbeat_at > now() - make_interval(secs => $1)
         ORDER BY worker_role, worker_id
        """,
        max_age_seconds,
    )
    return [
        {
            "worker_id": row["worker_id"],
            "worker_role": row["worker_role"],
            "executor_roles": _texts(row["executor_roles"]),
            "handler_kinds": _texts(row["handler_kinds"]),
            "package_version": row["package_version"],
            "started_at": _iso(row["started_at"]),
            "last_heartbeat_at": _iso(row["last_heartbeat_at"]),
            "stats": _json(row["stats"]) or {},
        }
        for row in rows
    ]


async def live_executor_roles(
    connection: asyncpg.Connection, *, max_age_seconds: int
) -> set[str]:
    rows = await connection.fetch(
        """
        SELECT DISTINCT unnest(executor_roles) AS role
          FROM cortex_processing.workers
         WHERE stopped_at IS NULL
           AND last_heartbeat_at > now() - make_interval(secs => $1)
        """,
        max_age_seconds,
    )
    return {row["role"] for row in rows}


# ---------------------------------------------------------------------------
# Provider role and local inference configuration (R24/R25)
# ---------------------------------------------------------------------------


async def read_provider_roles(connection: asyncpg.Connection) -> dict[str, dict[str, Any]]:
    rows = await connection.fetch(
        """
        SELECT role, revision, provider_kind, base_url, model_id, credential_ref,
               egress_allowlist, timeouts, capabilities, enabled
          FROM cortex_processing.active_provider_role('embedding')
         UNION ALL
        SELECT role, revision, provider_kind, base_url, model_id, credential_ref,
               egress_allowlist, timeouts, capabilities, enabled
          FROM cortex_processing.active_provider_role('rerank')
         UNION ALL
        SELECT role, revision, provider_kind, base_url, model_id, credential_ref,
               egress_allowlist, timeouts, capabilities, enabled
          FROM cortex_processing.active_provider_role('analysis')
        """
    )
    return {
        row["role"]: {
            "revision": int(row["revision"]),
            "provider_kind": row["provider_kind"],
            "base_url": row["base_url"],
            "model_id": row["model_id"],
            "credential_ref": row["credential_ref"],
            "egress_allowlist": _texts(row["egress_allowlist"]),
            "timeouts": _json(row["timeouts"]) or {},
            "capabilities": _json(row["capabilities"]) or {},
            "enabled": bool(row["enabled"]),
        }
        for row in rows
    }


async def read_local_inference_policy(
    connection: asyncpg.Connection,
) -> dict[str, Any] | None:
    row = await connection.fetchrow(
        """
        SELECT revision, routing_policy, max_resident_models, concurrency_cpu,
               concurrency_gpu, batch_limit, idle_unload_seconds, offline
          FROM cortex_processing.active_local_inference_policy()
        """
    )
    if row is None:
        return None
    return {
        "revision": int(row["revision"]),
        "routing_policy": row["routing_policy"],
        "max_resident_models": int(row["max_resident_models"]),
        "concurrency_cpu": int(row["concurrency_cpu"]),
        "concurrency_gpu": int(row["concurrency_gpu"]),
        "batch_limit": int(row["batch_limit"]),
        "idle_unload_seconds": int(row["idle_unload_seconds"]),
        "offline": bool(row["offline"]),
    }


async def read_installed_local_models(
    connection: asyncpg.Connection, *, role: str = "embedding"
) -> tuple[dict[str, Any], ...]:
    rows = await connection.fetch(
        """
        SELECT model_id, role, digest, license, platform, dimensions, status,
               installed_at
          FROM cortex_processing.local_model_manifests
         WHERE role = $1
           AND status IN ('installed', 'activated')
         ORDER BY model_id
        """,
        role,
    )
    return tuple(
        {
            "model_id": row["model_id"],
            "role": row["role"],
            "digest": row["digest"],
            "license": row["license"],
            "platform": row["platform"],
            "dimensions": row["dimensions"],
            "status": row["status"],
            "installed_at": _iso(row["installed_at"]),
        }
        for row in rows
    )


# ---------------------------------------------------------------------------
# Retrieval contract helpers (the SQL functions themselves are the contract)
# ---------------------------------------------------------------------------

VECTOR_CANDIDATES_SQL = """
SELECT content_id, revision, chunk_id, distance
  FROM cortex_processing.vector_candidates($1, $2::public.vector, $3)
"""

SPACE_STATE_SQL = "SELECT * FROM cortex_processing.space_state($1)"


async def vector_candidates(
    connection: asyncpg.Connection,
    *,
    space_id: uuid.UUID,
    query: Sequence[float],
    limit: int,
) -> list[dict[str, Any]]:
    """In-process use of the published retrieval contract (R14 dependency)."""
    rows = await connection.fetch(
        VECTOR_CANDIDATES_SQL, space_id, format_vector(query), limit
    )
    return [
        {
            "content_id": str(row["content_id"]),
            "revision": int(row["revision"]),
            "chunk_id": str(row["chunk_id"]),
            "distance": float(row["distance"]),
        }
        for row in rows
    ]


async def space_state(
    connection: asyncpg.Connection, space_id: uuid.UUID
) -> dict[str, Any] | None:
    row = await connection.fetchrow(SPACE_STATE_SQL, space_id)
    if row is None:
        return None
    return {
        "space_id": str(row["space_id"]),
        "dimensions": int(row["dimensions"]),
        "metric": row["metric"],
        "normalization": row["normalization"],
        "provider": row["provider"],
        "model_id": row["model_id"],
        "model_revision": row["model_revision"],
        "active_generation_id": (
            str(row["active_generation_id"]) if row["active_generation_id"] else None
        ),
        "active_generation_number": row["active_generation_number"],
        "state": row["state"],
    }
