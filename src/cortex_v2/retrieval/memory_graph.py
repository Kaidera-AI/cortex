"""Memory graph use cases (R16/F09).

Assertions are source-bound: every extracted relation carries normalized
evidence edges to exact canonical content revisions with character spans.
Publication deduplicates onto the surviving active assertion (salvage),
retraction tombstones only assertions whose evidence is fully gone, and
authored annotations are never modified or deleted by projection work.

The extractor port is deterministic rule-based extraction today; a
model-backed extractor plugs in later behind the same ``ExtractionPort``.
"""

from __future__ import annotations

import re
import uuid
from typing import Any, Sequence

import asyncpg

from .ports import ExtractedAssertion, ExtractionPort

RELATION_RULES: tuple[tuple[str, str], ...] = (
    ("depends on", "depends_on"),
    ("owns", "owns"),
    ("maintains", "maintains"),
    ("supersedes", "supersedes"),
    ("references", "references"),
    ("implements", "implements"),
    ("blocks", "blocks"),
    ("relates to", "relates_to"),
)

_ENTITY = r"[A-Za-z0-9_@./+-]{1,96}"
_BOUNDARY = r"(?<![A-Za-z0-9_@./+-])"
_TRAIL_PUNCTUATION = ".,;:!?)"


def _entity_type(key: str) -> str:
    if "@" in key:
        return "person"
    if re.fullmatch(r"[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+)+", key):
        return "module"
    return "concept"


class RuleExtractor:
    """Deterministic, documented rule-v1 extraction.

    Limits are explicit: single-token entities (no spaces), the eight
    relations above, case-insensitive matching, casefolded entity keys,
    trailing sentence punctuation stripped from spans. An absent assertion is
    not proof of an absent relationship.
    """

    profile = "rule-v1"

    def __init__(self) -> None:
        self._patterns = tuple(
            (
                re.compile(
                    rf"{_BOUNDARY}({_ENTITY})\s+"
                    rf"{re.escape(phrase)}\s+"
                    rf"({_ENTITY})(?![A-Za-z0-9_@./+-])",
                    re.IGNORECASE,
                ),
                relation,
            )
            for phrase, relation in RELATION_RULES
        )

    def extract(self, body: str) -> tuple[ExtractedAssertion, ...]:
        found: list[ExtractedAssertion] = []
        seen: set[tuple[str, str, str]] = set()
        for pattern, relation in self._patterns:
            for match in pattern.finditer(body):
                subject_raw, object_raw = match.group(1), match.group(2)
                span_start = match.start(1)
                span_end = match.end(2)
                while subject_raw and subject_raw[-1] in _TRAIL_PUNCTUATION:
                    subject_raw = subject_raw[:-1]
                while object_raw and object_raw[-1] in _TRAIL_PUNCTUATION:
                    object_raw = object_raw[:-1]
                    span_end -= 1
                subject_key = subject_raw.casefold()
                object_key = object_raw.casefold()
                if not subject_key or not object_key or subject_key == object_key:
                    continue
                triple = (subject_key, relation, object_key)
                if triple in seen:
                    continue
                seen.add(triple)
                found.append(
                    ExtractedAssertion(
                        subject_key=subject_key,
                        subject_type=_entity_type(subject_key),
                        relation=relation,
                        object_key=object_key,
                        object_type=_entity_type(object_key),
                        span_start=span_start,
                        span_end=span_end,
                    )
                )
        found.sort(key=lambda item: (item.span_start, item.relation, item.subject_key))
        return tuple(found)


async def _retract_stranded(
    connection: asyncpg.Connection,
    *,
    scope_id: uuid.UUID,
    affected_assertion_ids: Sequence[uuid.UUID],
    reason: str,
) -> dict[str, int]:
    """Tombstone extracted assertions that lost all evidence; salvage the
    rest; never touch authored assertions."""
    if not affected_assertion_ids:
        return {"retracted": 0, "salvaged": 0, "preserved_authored": 0}
    rows = await connection.fetch(
        """
        SELECT stranded.assertion_id, stranded.origin,
               (
                   SELECT count(*)
                     FROM cortex_retrieval.graph_assertion_evidence AS remaining
                    WHERE remaining.scope_id = stranded.scope_id
                      AND remaining.assertion_id = stranded.assertion_id
               ) AS remaining_evidence
          FROM cortex_retrieval.graph_assertions AS stranded
         WHERE stranded.scope_id = $1
           AND stranded.assertion_id = ANY($2::uuid[])
           AND stranded.status = 'active'
        """,
        scope_id,
        list(affected_assertion_ids),
    )
    retracted = [
        row["assertion_id"]
        for row in rows
        if row["origin"] == "extracted" and row["remaining_evidence"] == 0
    ]
    salvaged = sum(
        1
        for row in rows
        if row["origin"] == "extracted" and row["remaining_evidence"] > 0
    )
    preserved_authored = sum(1 for row in rows if row["origin"] == "authored")
    if retracted:
        await connection.execute(
            """
            UPDATE cortex_retrieval.graph_assertions
               SET status = 'retracted',
                   retracted_at = now(),
                   retraction_reason = $3
             WHERE scope_id = $1
               AND assertion_id = ANY($2::uuid[])
               AND status = 'active'
            """,
            scope_id,
            retracted,
            reason,
        )
    return {
        "retracted": len(retracted),
        "salvaged": salvaged,
        "preserved_authored": preserved_authored,
    }


async def retract_source(
    connection: asyncpg.Connection,
    *,
    scope_id: uuid.UUID,
    content_id: uuid.UUID,
    reason: str,
) -> dict[str, Any]:
    """Retract derived evidence for an invalidated/superseded/deleted source.

    Removes exactly that source's evidence edges; assertions supported
    elsewhere survive (salvage); assertions left without evidence become
    terminal tombstones; authored assertions are preserved untouched.
    """
    removed = await connection.fetch(
        """
        DELETE FROM cortex_retrieval.graph_assertion_evidence AS prior_evidence
         WHERE prior_evidence.scope_id = $1
           AND prior_evidence.content_id = $2
        RETURNING prior_evidence.assertion_id
        """,
        scope_id,
        content_id,
    )
    affected = list({row["assertion_id"] for row in removed})
    outcome = await _retract_stranded(
        connection, scope_id=scope_id, affected_assertion_ids=affected, reason=reason
    )
    return {"evidence_removed": len(removed), **outcome}


async def publish_extraction(
    connection: asyncpg.Connection,
    *,
    scope_id: uuid.UUID,
    content_id: uuid.UUID,
    revision: int,
    body: str | None,
    extracted: Sequence[ExtractedAssertion],
    extractor: ExtractionPort,
) -> dict[str, Any]:
    """Publish one extraction run for a pinned source revision.

    Re-extracting the same revision first rebuilds: that revision's prior
    evidence edges are removed and stranded assertions tombstoned with reason
    ``rebuild``. New assertions deduplicate onto the active triple; an
    authored assertion always wins over an extracted duplicate.
    """
    if body is not None:
        for item in extracted:
            if item.span_start < 0 or item.span_end > len(body):
                raise ValueError("extracted span lies outside the source body")

    prior_runs = await connection.fetch(
        """
        SELECT run_id, assertion_count
          FROM cortex_retrieval.extraction_runs AS prior
         WHERE prior.scope_id = $1
           AND prior.content_id = $2
           AND prior.source_revision = $3
        """,
        scope_id,
        content_id,
        revision,
    )
    removed = await connection.fetch(
        """
        DELETE FROM cortex_retrieval.graph_assertion_evidence AS prior_evidence
         WHERE prior_evidence.scope_id = $1
           AND prior_evidence.content_id = $2
           AND prior_evidence.source_revision = $3
        RETURNING prior_evidence.assertion_id
        """,
        scope_id,
        content_id,
        revision,
    )
    affected = list({row["assertion_id"] for row in removed})
    rebuild = await _retract_stranded(
        connection, scope_id=scope_id, affected_assertion_ids=affected, reason="rebuild"
    )

    run_id = uuid.uuid4()
    run_row = await connection.fetchrow(
        """
        INSERT INTO cortex_retrieval.extraction_runs
            (scope_id, run_id, content_id, source_revision, extractor_profile,
             assertion_count)
        VALUES ($1, $2, $3, $4, $5, $6)
        RETURNING run_id
        """,
        scope_id,
        run_id,
        content_id,
        revision,
        extractor.profile,
        len(extracted),
    )
    run_id = run_row["run_id"]

    entity_ids: dict[tuple[str, str], uuid.UUID] = {}
    pending: list[tuple[str, str, uuid.UUID]] = []
    for item in extracted:
        for key, kind in ((item.subject_key, item.subject_type),
                          (item.object_key, item.object_type)):
            if (key, kind) not in entity_ids:
                entity_ids[(key, kind)] = uuid.uuid4()
                pending.append((key, kind, entity_ids[(key, kind)]))
    if pending:
        await connection.execute(
            """
            INSERT INTO cortex_retrieval.graph_entities
                (scope_id, entity_id, entity_key, entity_type)
            SELECT $1, candidate.entity_id, candidate.entity_key, candidate.entity_type
              FROM unnest(
                       $2::uuid[], $3::text[], $4::text[]
                   ) AS candidate(entity_id, entity_key, entity_type)
            ON CONFLICT (scope_id, entity_key, entity_type) DO NOTHING
            """,
            scope_id,
            [entry[2] for entry in pending],
            [entry[0] for entry in pending],
            [entry[1] for entry in pending],
        )
        resolved = await connection.fetch(
            """
            SELECT lookup.entity_key, lookup.entity_type, lookup.entity_id
              FROM cortex_retrieval.graph_entities AS lookup
             WHERE lookup.scope_id = $1
               AND lookup.entity_key = ANY($2::text[])
               AND lookup.entity_type = ANY($3::text[])
            """,
            scope_id,
            [entry[0] for entry in pending],
            [entry[1] for entry in pending],
        )
        entity_ids = {
            (row["entity_key"], row["entity_type"]): row["entity_id"]
            for row in resolved
        }

    stats = {
        "assertions_published": 0,
        "evidence_added": 0,
        "deduped_extracted": 0,
        "deduped_authored": 0,
        "retracted_prior": rebuild["retracted"],
        "salvaged_prior": rebuild["salvaged"],
        "preserved_authored_prior": rebuild["preserved_authored"],
        "evidence_removed_prior": len(removed),
        "prior_runs": len(prior_runs),
        "run_id": str(run_id),
    }
    for item in extracted:
        subject_id = entity_ids.get((item.subject_key, item.subject_type))
        object_id = entity_ids.get((item.object_key, item.object_type))
        if subject_id is None or object_id is None:
            continue
        assertion_id = uuid.uuid4()
        inserted = await connection.fetchrow(
            """
            INSERT INTO cortex_retrieval.graph_assertions
                (scope_id, assertion_id, subject_entity_id, relation,
                 object_entity_id, origin, extraction_run_id)
            VALUES ($1, $2, $3, $4, $5, 'extracted', $6)
            ON CONFLICT (scope_id, subject_entity_id, relation, object_entity_id)
                WHERE status = 'active'
            DO NOTHING
            RETURNING assertion_id
            """,
            scope_id,
            assertion_id,
            subject_id,
            item.relation,
            object_id,
            run_id,
        )
        if inserted is not None:
            target_id = inserted["assertion_id"]
            stats["assertions_published"] += 1
        else:
            existing = await connection.fetchrow(
                """
                SELECT existing.assertion_id, existing.origin
                  FROM cortex_retrieval.graph_assertions AS existing
                 WHERE existing.scope_id = $1
                   AND existing.subject_entity_id = $2
                   AND existing.relation = $3
                   AND existing.object_entity_id = $4
                   AND existing.status = 'active'
                """,
                scope_id,
                subject_id,
                item.relation,
                object_id,
            )
            if existing is None or existing["origin"] == "authored":
                stats["deduped_authored"] += 1
                continue
            target_id = existing["assertion_id"]
            stats["deduped_extracted"] += 1
        max_seq = await connection.fetchval(
            """
            SELECT max(evidence.evidence_seq) AS max_seq
              FROM cortex_retrieval.graph_assertion_evidence AS evidence
             WHERE evidence.scope_id = $1
               AND evidence.assertion_id = $2
            """,
            scope_id,
            target_id,
        )
        added = await connection.fetchrow(
            """
            INSERT INTO cortex_retrieval.graph_assertion_evidence
                (scope_id, assertion_id, evidence_seq, content_id,
                 source_revision, span_start, span_end)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            ON CONFLICT (scope_id, assertion_id, content_id, source_revision,
                         span_start, span_end)
            DO NOTHING
            RETURNING evidence_seq
            """,
            scope_id,
            target_id,
            (max_seq or 0) + 1,
            content_id,
            revision,
            item.span_start,
            item.span_end,
        )
        if added is not None:
            stats["evidence_added"] += 1
    return stats


async def assertions_for_revision(
    connection: asyncpg.Connection,
    *,
    scope_ids: Sequence[uuid.UUID],
    content_id: uuid.UUID,
    revision: int,
    include_retracted: bool = False,
    limit: int = 32,
) -> list[dict[str, Any]]:
    rows = await connection.fetch(
        """
        SELECT assertion.assertion_id, assertion.relation, assertion.origin,
               assertion.status, assertion.retraction_reason,
               subject.entity_key AS subject_key,
               subject.entity_type AS subject_type,
               object.entity_key AS object_key,
               object.entity_type AS object_type,
               evidence.span_start, evidence.span_end
          FROM cortex_retrieval.graph_assertions AS assertion
          JOIN cortex_retrieval.graph_assertion_evidence AS evidence
            ON evidence.scope_id = assertion.scope_id
           AND evidence.assertion_id = assertion.assertion_id
          JOIN cortex_retrieval.graph_entities AS subject
            ON subject.scope_id = assertion.scope_id
           AND subject.entity_id = assertion.subject_entity_id
          JOIN cortex_retrieval.graph_entities AS object
            ON object.scope_id = assertion.scope_id
           AND object.entity_id = assertion.object_entity_id
         WHERE assertion.scope_id = ANY($1::uuid[])
           AND evidence.content_id = $2
           AND evidence.source_revision = $3
           AND ($4 OR assertion.status = 'active')
         ORDER BY assertion.assertion_id, evidence.evidence_seq
         LIMIT $5
        """,
        list(scope_ids),
        content_id,
        revision,
        include_retracted,
        limit,
    )
    return [
        {
            "assertion_id": str(row["assertion_id"]),
            "relation": row["relation"],
            "origin": row["origin"],
            "status": row["status"],
            "retraction_reason": row["retraction_reason"],
            "subject_key": row["subject_key"],
            "subject_type": row["subject_type"],
            "object_key": row["object_key"],
            "object_type": row["object_type"],
            "span_start": row["span_start"],
            "span_end": row["span_end"],
        }
        for row in rows
    ]


async def explore_memory(
    connection: asyncpg.Connection,
    *,
    scope_ids: Sequence[uuid.UUID],
    entity_ids: Sequence[uuid.UUID] = (),
    content_id: uuid.UUID | None = None,
    relations: Sequence[str] = (),
    direction: str = "both",
    hops: int = 1,
    include_retracted: bool = False,
    limit: int = 50,
) -> dict[str, Any]:
    """Bounded memory-graph expansion with evidence citations per edge."""
    seeds = list(dict.fromkeys(entity_ids))
    if content_id is not None:
        seeded = await connection.fetch(
            """
            SELECT DISTINCT assertion.subject_entity_id AS entity_id
              FROM cortex_retrieval.graph_assertions AS assertion
              JOIN cortex_retrieval.graph_assertion_evidence AS evidence
                ON evidence.scope_id = assertion.scope_id
               AND evidence.assertion_id = assertion.assertion_id
             WHERE assertion.scope_id = ANY($1::uuid[])
               AND evidence.content_id = $2
               AND assertion.status = 'active'
            UNION
            SELECT DISTINCT assertion.object_entity_id AS entity_id
              FROM cortex_retrieval.graph_assertions AS assertion
              JOIN cortex_retrieval.graph_assertion_evidence AS evidence
                ON evidence.scope_id = assertion.scope_id
               AND evidence.assertion_id = assertion.assertion_id
             WHERE assertion.scope_id = ANY($1::uuid[])
               AND evidence.content_id = $2
               AND assertion.status = 'active'
            """,
            list(scope_ids),
            content_id,
        )
        for row in seeded:
            if row["entity_id"] not in seeds:
                seeds.append(row["entity_id"])

    nodes: dict[uuid.UUID, dict[str, Any]] = {}
    edges: dict[uuid.UUID, dict[str, Any]] = {}
    frontier = seeds
    hops_executed = 0
    for _ in range(max(1, hops)):
        if not frontier or len(edges) >= limit:
            break
        hops_executed += 1
        if direction == "out":
            predicate = "assertion.subject_entity_id = ANY($2::uuid[])"
        elif direction == "in":
            predicate = "assertion.object_entity_id = ANY($2::uuid[])"
        else:
            predicate = (
                "assertion.subject_entity_id = ANY($2::uuid[]) "
                "OR assertion.object_entity_id = ANY($2::uuid[])"
            )
        rows = await connection.fetch(
            f"""
            SELECT assertion.assertion_id, assertion.subject_entity_id,
                   assertion.object_entity_id, assertion.relation,
                   assertion.origin, assertion.status,
                   assertion.retraction_reason,
                   subject.entity_key AS subject_key,
                   subject.entity_type AS subject_type,
                   object.entity_key AS object_key,
                   object.entity_type AS object_type
              FROM cortex_retrieval.graph_assertions AS assertion
              JOIN cortex_retrieval.graph_entities AS subject
                ON subject.scope_id = assertion.scope_id
               AND subject.entity_id = assertion.subject_entity_id
              JOIN cortex_retrieval.graph_entities AS object
                ON object.scope_id = assertion.scope_id
               AND object.entity_id = assertion.object_entity_id
             WHERE assertion.scope_id = ANY($1::uuid[])
               AND ({predicate})
               AND (cardinality($3::text[]) = 0
                    OR assertion.relation = ANY($3::text[]))
               AND ($4 OR assertion.status = 'active')
             ORDER BY assertion.assertion_id
             LIMIT $5
            """,
            list(scope_ids),
            frontier,
            list(relations),
            include_retracted,
            limit,
        )
        next_frontier: list[uuid.UUID] = []
        for row in rows:
            nodes[row["subject_entity_id"]] = {
                "entity_id": str(row["subject_entity_id"]),
                "entity_key": row["subject_key"],
                "entity_type": row["subject_type"],
            }
            nodes[row["object_entity_id"]] = {
                "entity_id": str(row["object_entity_id"]),
                "entity_key": row["object_key"],
                "entity_type": row["object_type"],
            }
            if row["assertion_id"] not in edges:
                edges[row["assertion_id"]] = {
                    "assertion_id": str(row["assertion_id"]),
                    "subject_entity_id": str(row["subject_entity_id"]),
                    "subject_key": row["subject_key"],
                    "relation": row["relation"],
                    "object_entity_id": str(row["object_entity_id"]),
                    "object_key": row["object_key"],
                    "origin": row["origin"],
                    "status": row["status"],
                    "retraction_reason": row["retraction_reason"],
                    "evidence": [],
                }
            for entity_id in (row["subject_entity_id"], row["object_entity_id"]):
                if entity_id not in frontier and entity_id not in next_frontier:
                    next_frontier.append(entity_id)
        if rows:
            evidence_rows = await connection.fetch(
                """
                SELECT evidence.assertion_id, evidence.scope_id,
                       evidence.content_id, evidence.source_revision,
                       evidence.span_start, evidence.span_end
                  FROM cortex_retrieval.graph_assertion_evidence AS evidence
                 WHERE evidence.scope_id = ANY($1::uuid[])
                   AND evidence.assertion_id = ANY($2::uuid[])
                 ORDER BY evidence.assertion_id, evidence.evidence_seq
                """,
                list(scope_ids),
                list(edges),
            )
            for row in evidence_rows:
                edge = edges.get(row["assertion_id"])
                if edge is not None:
                    edge["evidence"].append(
                        {
                            "scope_id": str(row["scope_id"]),
                            "content_id": str(row["content_id"]),
                            "revision": row["source_revision"],
                            "span_start": row["span_start"],
                            "span_end": row["span_end"],
                        }
                    )
        frontier = next_frontier
    return {
        "kind": "memory",
        "nodes": sorted(nodes.values(), key=lambda node: node["entity_key"]),
        "edges": sorted(edges.values(), key=lambda edge: edge["assertion_id"]),
        "hops_executed": hops_executed,
        "coverage": {
            "note": (
                "Memory edges are extracted or authored assertions with their "
                "supporting source revisions. An absent edge is not proof of "
                "absence."
            ),
            "include_retracted": include_retracted,
        },
        "degraded": [],
    }
