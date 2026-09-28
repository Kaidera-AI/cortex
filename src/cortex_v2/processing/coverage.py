"""Intended-coverage measurement for one scope, profile and generation (R13).

Coverage is measured against what the pinned profile *intends* to process, not
against how many vectors happen to exist. A corpus that deliberately excludes
low-value bulk messages is fully covered; a real gap is reported with its
reason, its queue state and the executor roles it is waiting for. Nothing here
claims a projection is searchable — only that rows exist in one generation.
"""

from __future__ import annotations

import uuid
from typing import Any

import asyncpg

from . import queue, repository


async def measure(
    connection: asyncpg.Connection,
    *,
    scope_id: uuid.UUID,
    profile: dict[str, Any],
    space_id: uuid.UUID | None,
    generation_id: uuid.UUID | None,
) -> dict[str, Any]:
    """One bounded coverage report; every number states its filter."""
    coverage_policy = profile.get("coverage") or {}
    content_classes = [str(item) for item in coverage_policy.get("content_classes") or ()]
    min_text_length = int(coverage_policy.get("min_text_length") or 0)

    projection = await _projection_coverage(
        connection,
        scope_id=scope_id,
        content_classes=content_classes,
        min_text_length=min_text_length,
        space_id=space_id,
        generation_id=generation_id,
    )
    generation = await _generation_state(connection, space_id, generation_id)
    states = await _job_states(connection, scope_id=scope_id, space_id=space_id)
    quarantine_open = await repository.count_open_quarantine(
        connection, scope_id=scope_id
    )
    live_roles = await repository.live_executor_roles(
        connection, max_age_seconds=queue.WORKER_HEARTBEAT_STALE_SECONDS
    )
    waiting = {
        role: jobs
        for role, jobs in states["queued_by_role"].items()
        if role not in live_roles
    }
    degraded = _degraded(projection, generation, states, quarantine_open, waiting)
    intended = projection["intended"]
    embedded = projection["embedded"]
    return {
        "scope_id": str(scope_id),
        "profile": {
            "profile_id": profile.get("profile_id"),
            "profile_identity": profile.get("profile_identity"),
            "content_classes": content_classes,
            "min_text_length": min_text_length,
        },
        "space_id": str(space_id) if space_id else None,
        "generation": generation,
        "intended_revisions": intended,
        "chunked_revisions": projection["chunked"],
        "embedded_revisions": embedded,
        "gap": {
            "revisions": max(0, intended - embedded),
            "missing_chunks": projection["missing_chunks"],
            "missing_vectors": projection["missing_vectors"],
            "empty_sources": projection["empty_sources"],
        },
        "coverage_ratio": (round(embedded / intended, 6) if intended else None),
        "jobs": states["by_status"],
        "jobs_by_error_code": states["by_error_code"],
        "waiting_for_executor": waiting,
        "live_executor_roles": sorted(live_roles),
        "quarantine_open": quarantine_open,
        "degraded": degraded,
        "window": {
            "filter": "current revisions matching the profile coverage policy",
            "space_scope": "one space and one generation" if generation_id else "all generations",
        },
    }


async def _projection_coverage(
    connection: asyncpg.Connection,
    *,
    scope_id: uuid.UUID,
    content_classes: list[str],
    min_text_length: int,
    space_id: uuid.UUID | None,
    generation_id: uuid.UUID | None,
) -> dict[str, int]:
    if not content_classes:
        return {
            "intended": 0,
            "chunked": 0,
            "embedded": 0,
            "missing_chunks": 0,
            "missing_vectors": 0,
            "empty_sources": 0,
        }
    row = await connection.fetchrow(
        """
        WITH intended AS (
            SELECT i.content_id,
                   latest.revision,
                   length(r.body_text) AS text_length
              FROM cortex_core.content_items AS i
              JOIN LATERAL (
                   SELECT max(cr.revision) AS revision
                     FROM cortex_core.content_revisions AS cr
                    WHERE cr.scope_id = i.scope_id
                      AND cr.content_id = i.content_id
              ) AS latest ON true
              JOIN cortex_core.content_revisions AS r
                ON r.scope_id = i.scope_id
               AND r.content_id = i.content_id
               AND r.revision = latest.revision
             WHERE i.scope_id = $1
               AND i.content_class = ANY($2::text[])
               AND length(r.body_text) >= $3
               AND cortex_core.content_current_status(i.scope_id, i.content_id) = 'current'
        )
        SELECT count(*) AS intended,
               count(*) FILTER (WHERE c.content_id IS NOT NULL) AS chunked,
               count(*) FILTER (WHERE v.content_id IS NOT NULL) AS embedded,
               count(*) FILTER (WHERE c.content_id IS NULL) AS missing_chunks,
               count(*) FILTER (
                   WHERE c.content_id IS NOT NULL AND v.content_id IS NULL
               ) AS missing_vectors,
               count(*) FILTER (
                   WHERE c.content_id IS NOT NULL AND c.chunks = 0
               ) AS empty_sources
          FROM intended AS i
          LEFT JOIN LATERAL (
               SELECT c2.content_id, count(*) AS chunks
                 FROM cortex_processing.chunk_revisions AS c2
                WHERE c2.scope_id = $1
                  AND c2.content_id = i.content_id
                  AND c2.revision = i.revision
                  AND ($4::uuid IS NULL OR c2.space_id = $4)
                GROUP BY c2.content_id
          ) AS c ON true
          LEFT JOIN LATERAL (
               SELECT v2.content_id
                 FROM cortex_processing.chunk_vectors AS v2
                WHERE v2.scope_id = $1
                  AND v2.content_id = i.content_id
                  AND v2.revision = i.revision
                  AND ($5::uuid IS NULL OR v2.generation_id = $5)
                LIMIT 1
          ) AS v ON true
        """,
        scope_id,
        content_classes,
        min_text_length,
        space_id,
        generation_id,
    )
    if row is None:
        return {
            "intended": 0,
            "chunked": 0,
            "embedded": 0,
            "missing_chunks": 0,
            "missing_vectors": 0,
            "empty_sources": 0,
        }
    return {
        "intended": int(row["intended"]),
        "chunked": int(row["chunked"]),
        "embedded": int(row["embedded"]),
        "missing_chunks": int(row["missing_chunks"]),
        "missing_vectors": int(row["missing_vectors"]),
        "empty_sources": int(row["empty_sources"]),
    }


async def _generation_state(
    connection: asyncpg.Connection,
    space_id: uuid.UUID | None,
    generation_id: uuid.UUID | None,
) -> dict[str, Any] | None:
    if space_id is None:
        return None
    return await repository.space_state(connection, space_id)


async def _job_states(
    connection: asyncpg.Connection,
    *,
    scope_id: uuid.UUID,
    space_id: uuid.UUID | None,
) -> dict[str, Any]:
    rows = await connection.fetch(
        """
        SELECT status, count(*) AS jobs
          FROM cortex_processing.jobs
         WHERE scope_id = $1
           AND ($2::uuid IS NULL OR space_id = $2)
         GROUP BY status
         ORDER BY status
        """,
        scope_id,
        space_id,
    )
    errors = await connection.fetch(
        """
        SELECT error_code, count(*) AS jobs
          FROM cortex_processing.jobs
         WHERE scope_id = $1
           AND error_code IS NOT NULL
           AND ($2::uuid IS NULL OR space_id = $2)
         GROUP BY error_code
         ORDER BY jobs DESC, error_code
         LIMIT 25
        """,
        scope_id,
        space_id,
    )
    queued = await connection.fetch(
        """
        SELECT required_role, count(*) AS jobs
          FROM cortex_processing.jobs
         WHERE scope_id = $1
           AND status = 'queued'
           AND ($2::uuid IS NULL OR space_id = $2)
         GROUP BY required_role
        """,
        scope_id,
        space_id,
    )
    return {
        "by_status": {row["status"]: int(row["jobs"]) for row in rows},
        "by_error_code": {row["error_code"]: int(row["jobs"]) for row in errors},
        "queued_by_role": {row["required_role"]: int(row["jobs"]) for row in queued},
    }


def _degraded(
    projection: dict[str, int],
    generation: dict[str, Any] | None,
    states: dict[str, Any],
    quarantine_open: int,
    waiting: dict[str, int],
) -> list[str]:
    """Typed explanations, so a partial answer states its own limits."""
    degraded: list[str] = []
    if generation is None:
        degraded.append("no_embedding_space_selected")
    else:
        if generation["state"] == "no_active_generation":
            degraded.append("no_active_generation")
        elif generation["state"] != "active":
            degraded.append(f"generation_state_{generation['state']}")
    if projection["missing_chunks"]:
        degraded.append(f"missing_chunks:{projection['missing_chunks']}")
    if projection["missing_vectors"]:
        degraded.append(f"missing_vectors:{projection['missing_vectors']}")
    if projection["empty_sources"]:
        degraded.append(f"empty_sources:{projection['empty_sources']}")
    for status in ("blocked", "quarantined", "failed"):
        count = states["by_status"].get(status, 0)
        if count:
            degraded.append(f"{status}_jobs:{count}")
    if quarantine_open:
        degraded.append(f"quarantine_open:{quarantine_open}")
    for role, jobs in sorted(waiting.items()):
        degraded.append(f"executor_missing:{role}:{jobs}")
    return degraded


__all__ = ["measure"]
