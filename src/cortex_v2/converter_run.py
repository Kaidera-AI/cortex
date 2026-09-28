"""Accountable conversion from a read-only restored v1 database to isolated v2.

The caller supplies a verified snapshot digest and an explicit versioned mapping
policy. This module never connects to a production service or prints row bodies.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import asyncpg

from .content import content_hash
from .converter import (
    ConversionDecision,
    ConversionPolicy,
    classify_row,
    ensure_ledger_schema,
    target_content_hash,
    validate_snapshot_sha,
)

_RUN_NAMESPACE = uuid.UUID("d43e343c-7f80-4dad-b95d-a54d917a56e7")
_SAFE_IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]*\Z")
_SOURCE_SCHEMAS = ("public", "cortex", "cortex_auth")
_CANONICAL_FIELDS = (
    "id", "project", "project_id", "customer_id", "summary", "content", "body",
    "status", "to_role", "role", "from_agent", "agent_name", "activity_type",
    "artifact_refs", "created_at", "ts", "started_at", "task", "kind",
    "reply_to_handoff_id", "parent_goal_id", "claimed_by", "claimed_at",
    "next_steps", "context", "handoff_id", "session_id", "notes",
    "rationale", "detail", "code_right", "code_wrong", "title",
)
_CANONICAL_TABLES = frozenset({
    "public.handoffs", "cortex.handoffs", "public.decisions", "public.lessons",
    "public.knowledge", "public.agent_diaries", "public.messages",
    "public.agent_sessions", "public.work_products", "cortex.decisions",
    "cortex.lessons", "cortex.knowledge", "cortex.agent_diaries",
    "cortex.messages",
})


@dataclass(frozen=True, slots=True)
class RunResult:
    run_id: uuid.UUID
    source_count: int
    target_count: int
    quarantined_count: int
    skipped_count: int


def _identifier(value: str) -> str:
    if not _SAFE_IDENTIFIER.fullmatch(value):
        raise ValueError("unexpected legacy database identifier")
    return f'"{value}"'


def _source_scope(fields: set[str]) -> str:
    candidates = [
        f"t.{_identifier(name)}::text"
        for name in ("project", "project_id", "customer_id") if name in fields
    ]
    candidates.append("'<unmapped>'")
    return "COALESCE(" + ", ".join(candidates) + ")"


async def _source_tables(connection: asyncpg.Connection) -> list[tuple[str, str]]:
    rows = await connection.fetch(
        """SELECT n.nspname, c.relname
             FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relkind = 'r' AND n.nspname = ANY($1::text[])
            ORDER BY n.nspname, c.relname""",
        list(_SOURCE_SCHEMAS),
    )
    return [(row["nspname"], row["relname"]) for row in rows]


async def _columns(
    connection: asyncpg.Connection, schema: str, table: str
) -> set[str]:
    rows = await connection.fetch(
        """SELECT column_name FROM information_schema.columns
            WHERE table_schema=$1 AND table_name=$2""",
        schema, table,
    )
    return {row["column_name"] for row in rows}


async def _primary_key(
    connection: asyncpg.Connection, schema: str, table: str
) -> tuple[str, ...]:
    relation = f"{_identifier(schema)}.{_identifier(table)}"
    rows = await connection.fetch(
        """SELECT a.attname FROM pg_index i
             JOIN pg_attribute a ON a.attrelid = i.indrelid
                                 AND a.attnum = ANY(i.indkey)
            WHERE i.indrelid=$1::regclass AND i.indisprimary
            ORDER BY a.attnum""",
        relation,
    )
    return tuple(row["attname"] for row in rows)


def _row_query(
    schema: str, table: str, fields: set[str], primary_key: tuple[str, ...],
    *, has_messages: bool,
) -> str:
    namespace = f"{schema}.{table}"
    relation = f"{_identifier(schema)}.{_identifier(table)}"
    if primary_key:
        components = ", ".join(f"t.{_identifier(name)}" for name in primary_key)
        source_key = f"jsonb_build_array({components})::text"
    else:
        # Immutable restored source: physical CTIDs are stable for this run only.
        source_key = "t.ctid::text"
    if schema == "cortex_auth":
        source_key = f"encode(sha256(convert_to({source_key}, 'UTF8')), 'hex')"
    if namespace in _CANONICAL_TABLES:
        entries = [
            f"'{name}', t.{_identifier(name)}"
            for name in _CANONICAL_FIELDS if name in fields
        ]
        if namespace == "public.agent_sessions" and has_messages:
            entries.append(
                "'message_count', (SELECT count(*) FROM public.messages m "
                "WHERE m.session_id=t.id)"
            )
        payload = (
            f"jsonb_build_object({', '.join(entries)})"
            if entries else "NULL::jsonb"
        )
    else:
        payload = "NULL::jsonb"
    return (
        f"SELECT {source_key} AS source_pk, {_source_scope(fields)} AS source_scope, "
        "sha256(convert_to(to_jsonb(t)::text, 'UTF8')) AS source_hash, "
        f"{payload} AS row_data FROM {relation} AS t ORDER BY {source_key}"
    )


async def _catalog_hash(connection: asyncpg.Connection) -> str:
    records = await connection.fetch(
        """SELECT n.nspname, c.relname, a.attname,
                  format_type(a.atttypid, a.atttypmod) AS data_type
             FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
             JOIN pg_attribute a ON a.attrelid=c.oid
            WHERE c.relkind='r' AND n.nspname=ANY($1::text[])
                  AND a.attnum>0 AND NOT a.attisdropped
            ORDER BY n.nspname, c.relname, a.attnum""",
        list(_SOURCE_SCHEMAS),
    )
    return hashlib.sha256(json.dumps(
        [tuple(row) for row in records], separators=(",", ":")
    ).encode()).hexdigest()


async def _target_schema_hash(connection: asyncpg.Connection) -> str:
    rows = await connection.fetch(
        """SELECT migration_id, checksum_sha256
             FROM cortex_core.schema_migrations ORDER BY migration_id"""
    )
    if not rows:
        raise RuntimeError("the target has not run its product migrations")
    return hashlib.sha256(json.dumps(
        [tuple(row) for row in rows], separators=(",", ":")
    ).encode()).hexdigest()


async def ensure_reconciliation_schema(connection: asyncpg.Connection) -> None:
    sql = Path(__file__).with_name("converter_reconciliation_v1.sql").read_bytes()
    checksum = hashlib.sha256(sql).hexdigest()
    async with connection.transaction():
        exists = await connection.fetchval(
            "SELECT to_regclass('cortex_conversion.reconciliation') IS NOT NULL"
        )
        if exists:
            recorded = await connection.fetchval(
                "SELECT sha256 FROM cortex_conversion.schema_versions "
                "WHERE version='reconciliation_v1'"
            )
            if recorded != checksum:
                raise RuntimeError("converter reconciliation checksum mismatch")
            return
        await connection.execute(sql.decode("utf-8"))
        await connection.execute(
            "INSERT INTO cortex_conversion.schema_versions(version, sha256) "
            "VALUES ('reconciliation_v1', $1)",
            checksum,
        )


async def _check_policy_source(
    connection: asyncpg.Connection, policy: ConversionPolicy
) -> None:
    for name, mapping in policy.projects.items():
        evidence = mapping.source_provenance
        if evidence is None:
            continue
        verified = await connection.fetchval(
            """SELECT EXISTS(
                   SELECT 1 FROM public.cortex_projects p
                   JOIN cortex_auth.principals owner
                     ON owner.project_id=p.id
                   JOIN cortex_auth.grants g
                     ON g.principal_id=owner.id
                  WHERE p.id=$1 AND p.project_key=$2
                    AND owner.id=$3 AND owner.installation_id=$4
                    AND owner.disabled_at IS NULL
                    AND g.disabled_at IS NULL
                    AND g.scopes @> ARRAY['instance:admin']::text[]
                )""",
            evidence.project_id, name, evidence.owner_id,
            evidence.installation_id,
        )
        if not verified:
            raise RuntimeError("policy source project ownership is unverified")


async def _check_policy_target(
    connection: asyncpg.Connection, policy: ConversionPolicy
) -> None:
    for mapping in policy.projects.values():
        permitted = await connection.fetchval(
            """SELECT EXISTS(
                   SELECT 1 FROM cortex_auth.principals p
                   JOIN cortex_auth.scope_grants g
                     ON g.principal_id=p.principal_id
                   JOIN cortex_core.scopes s ON s.scope_id=g.scope_id
                  WHERE p.principal_id=$1 AND p.installation_id=$2
                    AND p.status='active' AND g.scope_id=$3
                    AND g.revoked_at IS NULL AND g.can_write
                    AND s.scope_kind='project'
                )""",
            mapping.principal_id, policy.installation_id, mapping.scope_id,
        )
        if not permitted:
            raise RuntimeError(
                "policy principal lacks a target project write grant"
            )


def _original_time(row: Mapping[str, Any] | None) -> datetime | None:
    if row is None:
        return None
    value = row.get("created_at") or row.get("ts") or row.get("started_at")
    if isinstance(value, str):
        try:
            timestamp = datetime.fromisoformat(value)
        except ValueError:
            return None
        if timestamp.tzinfo is not None:
            return timestamp
    return None


async def _declare_writer(
    connection: asyncpg.Connection, decision: ConversionDecision
) -> None:
    assert decision.scope_id is not None and decision.principal_id is not None
    revision = await connection.fetchval(
        "SELECT COALESCE(max(revision),0)::text FROM cortex_auth.writer_policies "
        "WHERE scope_id=$1",
        decision.scope_id,
    )
    await connection.execute(
        "SELECT set_config('cortex.policy_revision',$1,true)", revision
    )
    await connection.execute(
        "SELECT set_config('cortex.principal_id',$1,true)",
        str(decision.principal_id),
    )


async def _write_canonical(
    connection: asyncpg.Connection,
    decision: ConversionDecision,
    namespace: str,
    source_pk: str,
    timestamp: datetime,
    connector_id: uuid.UUID,
) -> None:
    assert decision.target_id is not None and decision.body is not None
    assert decision.scope_id is not None and decision.principal_id is not None
    await _declare_writer(connection, decision)
    if decision.target_relation == "cortex_coord.handoffs":
        assert decision.payload is not None
        await connection.execute(
            """INSERT INTO cortex_coord.handoffs(
                   scope_id, handoff_id, title, brief, addressed_role,
                   status, created_by_principal, created_at, updated_at
               ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$8)""",
            decision.scope_id, decision.target_id,
            decision.payload["title"], decision.body,
            decision.payload["to_role"], decision.status,
            decision.principal_id, timestamp,
        )
        return
    assert decision.content_class is not None and decision.payload is not None
    await connection.execute(
        """INSERT INTO cortex_core.content_items(
               scope_id,content_id,content_class,created_by_principal,created_at
           ) VALUES ($1,$2,$3,$4,$5)""",
        decision.scope_id, decision.target_id,
        decision.content_class, decision.principal_id, timestamp,
    )
    source_key = f"{namespace}:{source_pk}:r1"
    if len(source_key) > 256:
        raise ValueError("source key exceeds v2 bound")
    await connection.execute(
        """INSERT INTO cortex_core.content_revisions(
               scope_id,content_id,revision,payload,body_text,content_hash,
               author_principal_id,authored_at,source_connector_id,source_key,
               source_observed_at
           ) VALUES ($1,$2,1,$3::jsonb,$4,$5,$6,$7,$8,$9,now())""",
        decision.scope_id, decision.target_id,
        json.dumps(decision.payload, sort_keys=True), decision.body,
        target_content_hash(decision), decision.principal_id,
        timestamp, connector_id, source_key,
    )
    await connection.execute(
        """INSERT INTO cortex_core.content_lexical_documents
           (scope_id,content_id,revision,source_text) VALUES ($1,$2,1,$3)""",
        decision.scope_id, decision.target_id, decision.body,
    )
    if decision.content_class in {"decision", "lesson", "knowledge"}:
        await connection.execute(
            """INSERT INTO cortex_core.memory_records(
                   scope_id,record_id,logical_record_id,record_type,
                   revision,author_principal_id,body,created_at
               ) VALUES ($1,$2,$2,$3,1,$4,$5,$6)""",
            decision.scope_id, decision.target_id, decision.content_class,
            decision.principal_id, decision.body, timestamp,
        )
        await connection.execute(
            """INSERT INTO cortex_core.lexical_documents
               (scope_id,record_id,revision,source_text) VALUES ($1,$2,1,$3)""",
            decision.scope_id, decision.target_id, decision.body,
        )


async def _quarantine(
    connection: asyncpg.Connection,
    run_id: uuid.UUID,
    schema: str,
    table: str,
    source_pk: str,
    source_hash: bytes,
    reason: str,
) -> uuid.UUID:
    # Never persist unconvertible body, credential digest or raw source PK.
    safe_ref = hashlib.sha256(f"{schema}.{table}:{source_pk}".encode()).hexdigest()
    return await connection.fetchval(
        """INSERT INTO cortex_core.conversion_quarantine(
               run_id,source_namespace,source_reference,original_payload,
               payload_sha256,reason
           ) VALUES ($1,$2,$3,$4::jsonb,$5,$6) RETURNING quarantine_id""",
        run_id, f"{schema}.{table}", safe_ref,
        json.dumps({"source_reference_sha256": safe_ref}),
        source_hash, reason,
    )


async def _write_row(
    connection: asyncpg.Connection,
    run_id: uuid.UUID,
    policy: ConversionPolicy,
    schema: str,
    table: str,
    source_row: asyncpg.Record,
    connector_id: uuid.UUID,
) -> None:
    namespace = f"{schema}.{table}"
    data = json.loads(source_row["row_data"]) if source_row["row_data"] else {}
    source_pk = source_row["source_pk"]
    source_hash = bytes(source_row["source_hash"])
    decision = classify_row(namespace, data, policy)
    quarantine_id = None
    body_hash = None
    if decision.outcome == "migrated":
        timestamp = _original_time(data)
        if timestamp is None:
            decision = ConversionDecision("quarantined", "missing_source_time")
        elif decision.target_relation == "cortex_core.content_items" and (
            len(f"{namespace}:{source_pk}:r1") > 256
        ):
            decision = ConversionDecision("quarantined", "oversized_source_key")
        else:
            assert decision.body is not None
            body_hash = hashlib.sha256(decision.body.encode()).digest()
            try:
                async with connection.transaction():
                    await _write_canonical(
                        connection, decision, namespace, source_pk,
                        timestamp, connector_id,
                    )
            except asyncpg.UniqueViolationError:
                decision = ConversionDecision(
                    "quarantined", "target_identity_collision"
                )
                body_hash = None
    if decision.outcome == "quarantined":
        assert decision.reason is not None
        quarantine_id = await _quarantine(
            connection, run_id, schema, table, source_pk, source_hash, decision.reason
        )
    await connection.execute(
        """INSERT INTO cortex_conversion.outcomes(
               run_id,source_schema,source_table,source_pk,source_hash,
               source_scope,target_relation,target_scope_id,target_id,
               target_revision,body_sha256,outcome,reason,quarantine_id
           ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)""",
        run_id, schema, table, source_pk, source_hash,
        source_row["source_scope"], decision.target_relation,
        decision.scope_id, decision.target_id,
        1 if decision.outcome == "migrated" and decision.content_class else None,
        body_hash, decision.outcome, decision.reason, quarantine_id,
    )


async def _verified_target(
    connection: asyncpg.Connection, outcome: asyncpg.Record
) -> tuple[bool, bool]:
    scope = outcome["target_scope_id"]
    target_id = outcome["target_id"]
    if outcome["target_relation"] == "cortex_coord.handoffs":
        row = await connection.fetchrow(
            """SELECT sha256(convert_to(brief,'UTF8')) AS body_hash, status
                 FROM cortex_coord.handoffs
                WHERE scope_id=$1 AND handoff_id=$2""",
            scope, target_id,
        )
        return bool(
            row and row["body_hash"] == outcome["body_sha256"]
            and row["status"] == "open"
        ), False
    if outcome["target_relation"] == "cortex_core.content_items":
        row = await connection.fetchrow(
            """SELECT sha256(convert_to(r.body_text,'UTF8')) AS body_hash,
                      i.content_class, r.content_hash, r.payload, r.body_text,
                      (lex.content_id IS NOT NULL) AS has_lexical
                 FROM cortex_core.content_items i
                 JOIN cortex_core.content_revisions r
                   ON (i.scope_id,i.content_id)=(r.scope_id,r.content_id)
                  AND r.revision=1
                 LEFT JOIN cortex_core.content_lexical_documents lex
                   ON (lex.scope_id,lex.content_id,lex.revision)
                    =(r.scope_id,r.content_id,r.revision)
                WHERE i.scope_id=$1 AND i.content_id=$2""",
            scope, target_id,
        )
        if (
            not row
            or row["body_hash"] != outcome["body_sha256"]
            or not row["has_lexical"]
            or row["content_hash"] != content_hash(
                row["content_class"], json.loads(row["payload"]), row["body_text"]
            )
        ):
            return False, False
        if row["content_class"] not in {"decision", "lesson", "knowledge"}:
            return True, False
        memory = await connection.fetchrow(
            """SELECT sha256(convert_to(m.body,'UTF8')) AS body_hash,
                      (lex.record_id IS NOT NULL) AS has_lexical
                 FROM cortex_core.memory_records m
                 LEFT JOIN cortex_core.lexical_documents lex
                   ON (lex.scope_id,lex.record_id,lex.revision)
                    =(m.scope_id,m.record_id,m.revision)
                WHERE m.scope_id=$1 AND m.record_id=$2
                  AND m.revision=1 AND m.record_type=$3""",
            scope, target_id, row["content_class"],
        )
        return bool(memory and memory["has_lexical"] and
                    memory["body_hash"] == outcome["body_sha256"]), True
    raise RuntimeError("unknown converter target relation")


async def _verify_previous_row(
    connection: asyncpg.Connection,
    previous: asyncpg.Record,
    row: asyncpg.Record,
) -> None:
    if (
        previous["source_hash"] != row["source_hash"]
        or previous["source_scope"] != row["source_scope"]
    ):
        raise RuntimeError("source row diverged from recorded converter outcome")
    if previous["outcome"] == "migrated":
        valid, _ = await _verified_target(connection, previous)
        if not valid:
            raise RuntimeError("converted canonical target or projection diverged")
    elif previous["outcome"] == "quarantined":
        quarantine = await connection.fetchrow(
            """SELECT source_namespace, source_reference, original_payload,
                      payload_sha256, reason
                 FROM cortex_core.conversion_quarantine
                WHERE quarantine_id=$1 AND run_id=$2""",
            previous["quarantine_id"], previous["run_id"],
        )
        expected_ref = hashlib.sha256(
            f"{previous['source_schema']}.{previous['source_table']}:"
            f"{previous['source_pk']}".encode()
        ).hexdigest()
        if (
            not quarantine
            or quarantine["source_namespace"] != (
                f"{previous['source_schema']}.{previous['source_table']}"
            )
            or quarantine["source_reference"] != expected_ref
            or json.loads(quarantine["original_payload"]) != {
                "source_reference_sha256": expected_ref
            }
            or quarantine["payload_sha256"] != row["source_hash"]
            or quarantine["reason"] != previous["reason"]
        ):
            raise RuntimeError("converter quarantine reference diverged")


async def _process_batch(
    connection: asyncpg.Connection,
    run_id: uuid.UUID,
    policy: ConversionPolicy,
    schema: str,
    table: str,
    rows: list[asyncpg.Record],
    connector_id: uuid.UUID,
    *,
    complete: bool,
) -> None:
    previous = {
        outcome["source_pk"]: outcome for outcome in await connection.fetch(
            """SELECT * FROM cortex_conversion.outcomes
                WHERE run_id=$1 AND source_schema=$2 AND source_table=$3
                  AND source_pk=ANY($4::text[])""",
            run_id, schema, table, [row["source_pk"] for row in rows],
        )
    }
    for row in rows:
        outcome = previous.get(row["source_pk"])
        if outcome is not None:
            await _verify_previous_row(connection, outcome, row)
        elif complete:
            raise RuntimeError("completed run missing recorded source outcome")
        else:
            await _write_row(
                connection, run_id, policy, schema, table, row, connector_id
            )


async def _completed_result(
    connection: asyncpg.Connection,
    run_id: uuid.UUID,
    source_counts: Mapping[tuple[str, str, str], tuple[int, bytes]],
) -> RunResult:
    stored = await connection.fetch(
        """SELECT source_schema, source_table, source_scope, source_count,
                  source_rowset_sha256
             FROM cortex_conversion.source_counts WHERE run_id=$1""",
        run_id,
    )
    actual = {
        (row["source_schema"], row["source_table"], row["source_scope"]):
        (row["source_count"], bytes(row["source_rowset_sha256"]))
        for row in stored
    }
    if actual != source_counts:
        raise RuntimeError("source rowset diverged from completed converter run")
    source_total = sum(row[0] for row in source_counts.values())
    outcome_total = await connection.fetchval(
        "SELECT count(*) FROM cortex_conversion.outcomes WHERE run_id=$1", run_id
    )
    if outcome_total != source_total:
        raise RuntimeError("completed converter outcome inventory diverged")
    totals = await connection.fetchrow(
        """SELECT coalesce(sum(target_count),0)::bigint AS targets,
                  coalesce(sum(quarantined_count),0)::bigint AS quarantined,
                  coalesce(sum(skipped_count),0)::bigint AS skipped,
                  coalesce(sum(source_count),0)::bigint AS sources
             FROM cortex_conversion.reconciliation WHERE run_id=$1""",
        run_id,
    )
    if totals["sources"] != source_total:
        raise RuntimeError("completed converter reconciliation diverged")
    return RunResult(
        run_id, source_total, totals["targets"],
        totals["quarantined"], totals["skipped"],
    )


async def _reconcile(
    connection: asyncpg.Connection,
    run_id: uuid.UUID,
    source_counts: Mapping[tuple[str, str, str], tuple[int, bytes]],
) -> RunResult:
    classified: dict[tuple[str, str, str], Counter[str]] = defaultdict(Counter)
    async with connection.transaction():
        async for row in connection.cursor(
            "SELECT * FROM cortex_conversion.outcomes WHERE run_id=$1 "
            "ORDER BY source_schema,source_table,source_pk",
            run_id, prefetch=128,
        ):
            key = (row["source_schema"], row["source_table"], row["source_scope"])
            classified[key][row["outcome"]] += 1
            if row["outcome"] == "migrated":
                verified, memory = await _verified_target(connection, row)
                if not verified:
                    raise RuntimeError(
                        "converted canonical target or projection diverged"
                    )
                classified[key]["verified"] += 1
                if memory:
                    classified[key]["memory"] += 1
    if set(classified) - set(source_counts):
        raise RuntimeError("ledger contains a source not present in the snapshot")
    totals: Counter[str] = Counter()
    async with connection.transaction():
        for key, (source_count, rowset_hash) in sorted(source_counts.items()):
            schema, table, source_scope = key
            counts = classified[key]
            if source_count != sum(counts[item] for item in (
                "migrated", "quarantined", "skipped"
            )):
                raise RuntimeError("physical source and classified ledger count differ")
            await connection.execute(
                """INSERT INTO cortex_conversion.source_counts
                   (run_id,source_schema,source_table,source_scope,
                    source_count,source_rowset_sha256)
                   VALUES ($1,$2,$3,$4,$5,$6)""",
                run_id, schema, table, source_scope, source_count, rowset_hash,
            )
            if table == "handoffs" and schema in {"public", "cortex"}:
                target_relation = "cortex_coord.handoffs"
            elif f"{schema}.{table}" in _CANONICAL_TABLES:
                target_relation = "cortex_core.content_items"
            else:
                target_relation = "<none>"
            await connection.execute(
                """INSERT INTO cortex_conversion.reconciliation
                   (run_id,source_schema,source_table,source_scope,
                    target_relation,source_count,target_count,quarantined_count,
                    skipped_count,memory_projection_count,verified_body_hash_count)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)""",
                run_id, schema, table, source_scope, target_relation, source_count,
                counts["migrated"], counts["quarantined"], counts["skipped"],
                counts["memory"], counts["verified"],
            )
            totals.update(counts)
        await connection.execute(
            "UPDATE cortex_conversion.runs SET status='complete', completed_at=now() "
            "WHERE run_id=$1 AND status='running'",
            run_id,
        )
    return RunResult(
        run_id, sum(item[0] for item in source_counts.values()),
        totals["migrated"], totals["quarantined"], totals["skipped"],
    )


async def convert_snapshot(
    source: asyncpg.Connection,
    target: asyncpg.Connection,
    policy: ConversionPolicy,
    *,
    snapshot_sha256: str,
    converter_revision: str,
) -> RunResult:
    """Convert a frozen source snapshot; return counts/IDs only, never bodies."""
    validate_snapshot_sha(snapshot_sha256)
    if not 7 <= len(converter_revision) <= 128:
        raise ValueError("converter revision must identify this build")
    if source is target:
        raise ValueError("source and target connections must be distinct")
    await _check_policy_target(target, policy)
    async with source.transaction(isolation="repeatable_read", readonly=True):
        source_hash = await _catalog_hash(source)
        tables = await _source_tables(source)
        if {f"{schema}.{table}" for schema, table in tables} != set(
            policy.entities
        ):
            raise ValueError("mapping policy missing or extra source entity")
        if ("public", "handoffs") not in tables:
            raise RuntimeError("restored source has no legacy handoffs table")
        await _check_policy_source(source, policy)
        await ensure_ledger_schema(target)
        await ensure_reconciliation_schema(target)
        target_hash = await _target_schema_hash(target)
        run_id = uuid.uuid5(_RUN_NAMESPACE, ":".join((
            snapshot_sha256, source_hash, target_hash, policy.sha256,
            converter_revision, str(policy.installation_id),
        )))
        previous_run = await target.fetchrow(
            "SELECT * FROM cortex_conversion.runs WHERE run_id=$1", run_id
        )
        if previous_run is not None and (
            previous_run["snapshot_sha256"] != snapshot_sha256
            or previous_run["source_schema_hash"] != source_hash
            or previous_run["target_schema_hash"] != target_hash
            or previous_run["converter_revision"] != converter_revision
            or previous_run["policy_sha256"] != policy.sha256
            or previous_run["policy_version"] != policy.version
            or previous_run["installation_id"] != policy.installation_id
        ):
            raise RuntimeError("converter run manifest diverged")
        complete = previous_run is not None and previous_run["status"] == "complete"
        connector_id = uuid.uuid5(
            _RUN_NAMESPACE, f"connector:{policy.installation_id}"
        )
        if previous_run is None:
            async with target.transaction():
                await target.execute(
                    """INSERT INTO cortex_core.source_connectors
                       (connector_id,namespace,connector_kind,installation_id)
                       VALUES ($1,$2,'manual',$3)
                       ON CONFLICT (connector_id) DO NOTHING""",
                    connector_id,
                    f"legacy-converter-{policy.installation_id.hex[:16]}",
                    policy.installation_id,
                )
                await target.execute(
                    """INSERT INTO cortex_conversion.runs
                       (run_id,snapshot_sha256,source_schema_hash,
                        target_schema_hash,converter_revision,policy_sha256,
                        policy_version,installation_id)
                       VALUES ($1,$2,$3,$4,$5,$6,$7,$8)""",
                    run_id, snapshot_sha256, source_hash, target_hash,
                    converter_revision, policy.sha256, policy.version,
                    policy.installation_id,
                )
        elif previous_run["status"] != "running" and not complete:
            raise RuntimeError("converter run has an unknown lifecycle")
        counts: dict[tuple[str, str, str], tuple[int, Any]] = {}
        for schema, table in tables:
            fields = await _columns(source, schema, table)
            primary_key = await _primary_key(source, schema, table)
            table_ref = f"{_identifier(schema)}.{_identifier(table)}"
            seen = 0
            rows: list[asyncpg.Record] = []
            query = _row_query(
                schema, table, fields, primary_key,
                has_messages=("public", "messages") in tables,
            )
            async for row in source.cursor(query, prefetch=128):
                seen += 1
                scope = row["source_scope"]
                key = (schema, table, scope)
                if key not in counts:
                    counts[key] = (0, hashlib.sha256())
                total, digest = counts[key]
                digest.update(json.dumps(
                    [row["source_pk"], bytes(row["source_hash"]).hex()],
                    separators=(",", ":"),
                ).encode())
                counts[key] = (total + 1, digest)
                rows.append(row)
                if len(rows) == 128:
                    if complete:
                        await _process_batch(
                            target, run_id, policy, schema, table,
                            rows, connector_id, complete=True,
                        )
                    else:
                        async with target.transaction():
                            await _process_batch(
                                target, run_id, policy, schema, table,
                                rows, connector_id, complete=False,
                            )
                    rows.clear()
            if rows:
                if complete:
                    await _process_batch(
                        target, run_id, policy, schema, table,
                        rows, connector_id, complete=True,
                    )
                else:
                    async with target.transaction():
                        await _process_batch(
                            target, run_id, policy, schema, table,
                            rows, connector_id, complete=False,
                        )
            actual = await source.fetchval(f"SELECT count(*) FROM {table_ref}")
            if actual != seen:
                raise RuntimeError("legacy source changed during consistent inventory")
            if not seen:
                counts[(schema, table, "<empty>")] = (0, hashlib.sha256())
    final_counts = {
        key: (value[0], value[1].digest()) for key, value in counts.items()
    }
    if complete:
        return await _completed_result(target, run_id, final_counts)
    return await _reconcile(target, run_id, final_counts)
