#!/usr/bin/env python3
"""Extract Cortex knowledge graph entities from durable memory rows."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from typing import Any


ALLOWED_ENTITY_TYPES = {
    "agent",
    "service",
    "file",
    "epic",
    "tool",
    "table",
    "concept",
    "endpoint",
    "branch",
    "project",
    "product",
    "model",
}

ALLOWED_RELATIONSHIP_TYPES = {
    "uses",
    "modifies",
    "depends_on",
    "owns",
    "blocks",
    "tests",
    "deploys",
    "documents",
    "implements",
    "references",
    "relates_to",
}

SOURCE_CONFIG = {
    "decisions": {
        "content_sql": "LEFT(summary, 3000)",
        "order_sql": "created_at",
        "active_sql": "invalidated_at IS NULL",
    },
    "lessons": {
        "content_sql": "LEFT(CONCAT_WS(E'\\n\\n', summary, detail, code_right, code_wrong), 3000)",
        "order_sql": "created_at",
        "active_sql": "invalidated_at IS NULL",
    },
    "knowledge": {
        "content_sql": "LEFT(content, 3000)",
        "order_sql": "COALESCE(created_at, updated_at)",
        "active_sql": "TRUE",
    },
}

DEFAULT_ENTITY_MODEL = "google/gemma-4-31b-it:free"
DEFAULT_ENTITY_FALLBACK_MODELS = (
    "minimax/minimax-m2.5:free,"
    "openai/gpt-oss-120b:free,"
    "nvidia/nemotron-3-super-120b-a12b:free"
)


def sql_literal(value: object) -> str:
    if value is None:
        return "NULL"
    return "'" + str(value).replace("'", "''") + "'"


def run_psql(sql: str, *, capture: bool = True) -> str:
    psql = shutil.which("psql")
    if not psql:
        raise RuntimeError("psql not found on PATH")

    env = os.environ.copy()
    env["PGPASSWORD"] = env.get("PG_PASS", "")
    cmd = [
        psql,
        "-h",
        env.get("PG_HOST", "localhost"),
        "-p",
        env.get("PG_PORT", "5499"),
        "-U",
        env.get("PG_USER", "postgres"),
        "-d",
        env.get("PG_DB", "platform_agent_memory"),
        "-v",
        "ON_ERROR_STOP=1",
        "-qAt",
        "-c",
        sql,
    ]
    proc = subprocess.run(cmd, check=False, capture_output=True, text=True, env=env)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "psql failed").strip())
    return proc.stdout if capture else ""


def json_rows(select_sql: str) -> list[dict[str, Any]]:
    sql = (
        "COPY (SELECT replace(encode(convert_to(row_to_json(q)::text, 'UTF8'), 'base64'), E'\\n', '') "
        "FROM (" + select_sql.strip().rstrip(";") + ") q) TO STDOUT"
    )
    output = run_psql(sql)
    return parse_json_lines(output, base64_encoded=True)


def json_rows_direct(sql: str) -> list[dict[str, Any]]:
    output = run_psql(sql.strip().rstrip(";"))
    return parse_json_lines(output)


def parse_json_lines(output: str, *, base64_encoded: bool = False) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in output.splitlines():
        line = line.strip()
        if line:
            if base64_encoded:
                line = base64.b64decode(line).decode("utf-8")
            rows.append(json.loads(line))
    return rows


def exec_sql(sql: str) -> None:
    run_psql(sql, capture=False)


def install_schema() -> None:
    exec_sql(
        """
        CREATE EXTENSION IF NOT EXISTS pg_trgm;
        CREATE EXTENSION IF NOT EXISTS pgcrypto;

        CREATE TABLE IF NOT EXISTS cortex_entities (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            project TEXT NOT NULL,
            name TEXT NOT NULL,
            entity_type TEXT NOT NULL,
            properties JSONB DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        );

        DO $$ BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'cortex_entities_natural_key'
            ) THEN
                ALTER TABLE cortex_entities
                    ADD CONSTRAINT cortex_entities_natural_key UNIQUE (project, name, entity_type);
            END IF;
        END $$;

        CREATE INDEX IF NOT EXISTS idx_cortex_entities_project ON cortex_entities (project);
        CREATE INDEX IF NOT EXISTS idx_cortex_entities_type ON cortex_entities (entity_type);
        CREATE INDEX IF NOT EXISTS idx_cortex_entities_name_trgm
            ON cortex_entities USING GIN (LOWER(name) gin_trgm_ops);
        CREATE INDEX IF NOT EXISTS idx_cortex_entities_description_trgm
            ON cortex_entities USING GIN (LOWER(COALESCE(properties->>'description', '')) gin_trgm_ops);

        CREATE TABLE IF NOT EXISTS cortex_relationships (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            project TEXT NOT NULL,
            source_entity_id UUID NOT NULL REFERENCES cortex_entities(id) ON DELETE CASCADE,
            target_entity_id UUID NOT NULL REFERENCES cortex_entities(id) ON DELETE CASCADE,
            relationship_type TEXT NOT NULL,
            properties JSONB DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ DEFAULT NOW()
        );

        DO $$ BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'cortex_relationships_natural_key'
            ) THEN
                ALTER TABLE cortex_relationships
                    ADD CONSTRAINT cortex_relationships_natural_key
                    UNIQUE (project, source_entity_id, target_entity_id, relationship_type);
            END IF;
        END $$;

        CREATE INDEX IF NOT EXISTS idx_cortex_relationships_project ON cortex_relationships (project);
        CREATE INDEX IF NOT EXISTS idx_cortex_relationships_source ON cortex_relationships (source_entity_id);
        CREATE INDEX IF NOT EXISTS idx_cortex_relationships_target ON cortex_relationships (target_entity_id);
        CREATE INDEX IF NOT EXISTS idx_cortex_relationships_type ON cortex_relationships (relationship_type);
        """
    )


def normalize_name(value: object) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:200]


def normalize_type(value: object, allowed: set[str], fallback: str) -> str:
    text = re.sub(r"[^a-z0-9_]+", "_", str(value or "").lower()).strip("_")
    return text if text in allowed else fallback


def clean_description(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:500]


def fetch_stats(project: str) -> dict[str, Any]:
    project_sql = sql_literal(project)
    source_rows = []
    for table, cfg in SOURCE_CONFIG.items():
        active = cfg["active_sql"]
        rows = json_rows(
            f"""
            SELECT
                {sql_literal(table)} AS source,
                COUNT(*)::int AS total,
                COUNT(*) FILTER (WHERE metadata->>'entities_extracted' = 'true')::int AS processed
            FROM {table}
            WHERE project = {project_sql}
              AND {active}
              AND {cfg['content_sql']} IS NOT NULL
              AND LENGTH({cfg['content_sql']}) > 10
            """
        )
        source_rows.extend(rows)

    entity_rows = json_rows(
        f"""
        SELECT
            COUNT(*)::int AS entity_count,
            (SELECT COUNT(*)::int FROM cortex_relationships WHERE project = {project_sql}) AS relationship_count
        FROM cortex_entities
        WHERE project = {project_sql}
        """
    )
    by_type = json_rows(
        f"""
        SELECT entity_type, COUNT(*)::int AS count
        FROM cortex_entities
        WHERE project = {project_sql}
        GROUP BY entity_type
        ORDER BY COUNT(*) DESC, entity_type
        """
    )
    return {
        "sources": source_rows,
        "graph": entity_rows[0] if entity_rows else {"entity_count": 0, "relationship_count": 0},
        "by_type": by_type,
    }


def print_stats(project: str) -> None:
    stats = fetch_stats(project)
    print("\n## Entity Graph Stats\n")
    graph = stats["graph"]
    print(f"  Entities:      {graph.get('entity_count', 0)}")
    print(f"  Relationships: {graph.get('relationship_count', 0)}")
    print("\n  Source extraction:")
    for row in stats["sources"]:
        total = int(row.get("total") or 0)
        processed = int(row.get("processed") or 0)
        print(f"    {row.get('source'):<10} {processed:>5} / {total:<5}")
    print("\n  By type:")
    for row in stats["by_type"]:
        print(f"    {row.get('entity_type')}: {row.get('count')}")
    print()


def fetch_source_rows(project: str, source: str, limit: int, reprocess: bool) -> list[dict[str, Any]]:
    project_sql = sql_literal(project)
    tables = list(SOURCE_CONFIG) if source == "all" else [source]
    parts = []
    for table in tables:
        cfg = SOURCE_CONFIG[table]
        processed_filter = "TRUE" if reprocess else "COALESCE(metadata->>'entities_extracted', 'false') <> 'true'"
        parts.append(
            f"""
            SELECT
                id::text,
                {sql_literal(table)} AS source_table,
                {cfg['content_sql']} AS content,
                {cfg['order_sql']} AS sort_ts
            FROM {table}
            WHERE project = {project_sql}
              AND {cfg['active_sql']}
              AND {processed_filter}
              AND {cfg['content_sql']} IS NOT NULL
              AND LENGTH({cfg['content_sql']}) > 10
            """
        )
    query = " UNION ALL ".join(parts)
    return json_rows(f"SELECT * FROM ({query}) rows ORDER BY sort_ts DESC LIMIT {int(limit)}")


def call_openrouter(model: str, text: str) -> dict[str, Any]:
    api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY not set")

    prompt = f"""Extract durable Cortex knowledge graph entities and relationships from this memory row.

Return JSON only:
{{
  "entities": [
    {{"name": "...", "type": "agent|service|file|epic|tool|table|concept|endpoint|branch|project|product|model", "description": "one line"}}
  ],
  "relationships": [
    {{"source": "entity_name", "target": "entity_name", "type": "uses|modifies|depends_on|owns|blocks|tests|deploys|documents|implements|references|relates_to", "description": "one line"}}
  ]
}}

Rules:
- Extract concrete project entities, not generic words.
- Prefer stable canonical names for files, tools, services, agents, branches, tables, and concepts.
- Keep descriptions factual and short.
- Relationships must refer to extracted entity names.

Memory row:
{text[:3000]}
"""
    payload = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
        }
    ).encode("utf-8")
    proc = subprocess.run(
        [
            "curl",
            "-sS",
            "--max-time",
            os.environ.get("CORTEX_ENTITY_TIMEOUT_SECONDS", "8"),
            "-X",
            "POST",
            "https://openrouter.ai/api/v1/chat/completions",
            "-H",
            f"Authorization: Bearer {api_key}",
            "-H",
            "Content-Type: application/json",
            "-d",
            payload.decode("utf-8"),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "OpenRouter request failed").strip())
    raw = proc.stdout

    data = json.loads(raw)
    if data.get("error"):
        raise RuntimeError(json.dumps(data["error"]))
    content = data.get("choices", [{}])[0].get("message", {}).get("content", "{}")
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError(f"empty model content from {model}")
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        match = re.search(r"```(?:json)?\s*(.*?)```", content, re.DOTALL)
        if not match:
            return {}
        return json.loads(match.group(1))


def is_retryable_provider_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return (
        '"code": 429' in text
        or "rate-limited" in text
        or "temporarily" in text
        or "provider returned error" in text
        or "operation timed out" in text
        or "timed out" in text
        or "empty model content" in text
    )


def model_candidates(primary: str, fallback_models: str) -> list[str]:
    candidates: list[str] = []
    for model in [primary, *fallback_models.split(",")]:
        model = model.strip()
        if model and model not in candidates:
            candidates.append(model)
    return candidates


def local_fallback_extract(text: str) -> dict[str, Any]:
    entities: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def add_entity(name: str, entity_type: str, description: str) -> None:
        name = normalize_name(name)
        if not name:
            return
        key = (name.lower(), entity_type)
        if key in seen:
            return
        seen.add(key)
        entities.append(
            {
                "name": name,
                "type": entity_type,
                "description": clean_description(description),
            }
        )

    for agent in sorted(
        set(
            match.group(1).lower()
            for match in re.finditer(r"\bagent(?:\s+name)?[:=]\s*([a-z][a-z0-9-]{1,31})\b", text, re.IGNORECASE)
        )
    ):
        add_entity(agent, "agent", "Agent referenced by Cortex memory row")

    for tool in sorted(set(re.findall(r"\bcortex-[a-z0-9-]+\b", text, re.IGNORECASE))):
        add_entity(tool, "tool", "Cortex CLI/tool referenced by memory row")

    for file_ref in sorted(
        set(
            re.findall(
                r"(?:(?:\.{1,2}|~)?/)?[A-Za-z0-9_.@+-]+(?:/[A-Za-z0-9_.@+-]+)+\.(?:py|sh|sql|md|json|ya?ml|ts|tsx|js|jsx|excalidraw\.md)",
                text,
            )
        )
    ):
        add_entity(file_ref[:200], "file", "File path referenced by memory row")

    concept_patterns = {
        "Cortex": r"\bcortex\b",
        "E005_LOCAL_CORTEX_RELIABILITY_SECURITY": r"\bE005_LOCAL_CORTEX_RELIABILITY_SECURITY\b|\bE005\b",
        "Knowledge graph": r"\bknowledge graph\b",
        "Phase A": r"\bphase a\b",
        "OpenRouter": r"\bopenrouter\b",
        "Docker Compose": r"\bdocker compose\b",
        "pgvector": r"\bpgvector\b",
        "pg_trgm": r"\bpg_trgm\b",
    }
    for name, pattern in concept_patterns.items():
        if re.search(pattern, text, re.IGNORECASE):
            add_entity(name, "concept", "Concept referenced by Cortex memory row")

    if not entities:
        digest = hashlib.sha1(text.encode("utf-8", errors="ignore")).hexdigest()[:10]
        add_entity(f"Cortex memory row {digest}", "concept", text[:160])

    relationships = []
    if len(entities) > 1:
        source = entities[0]["name"]
        for target in entities[1:12]:
            relationships.append(
                {
                    "source": source,
                    "target": target["name"],
                    "type": "references",
                    "description": "Local fallback linked entities co-mentioned in one memory row",
                }
            )

    return {"entities": entities[:30], "relationships": relationships[:40]}


def normalize_extraction(data: dict[str, Any]) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    entities: list[dict[str, str]] = []
    for raw in data.get("entities", []) or []:
        name = normalize_name(raw.get("name"))
        if not name:
            continue
        entities.append(
            {
                "name": name,
                "type": normalize_type(raw.get("type"), ALLOWED_ENTITY_TYPES, "concept"),
                "description": clean_description(raw.get("description")),
            }
        )

    relationships: list[dict[str, str]] = []
    for raw in data.get("relationships", []) or []:
        source = normalize_name(raw.get("source"))
        target = normalize_name(raw.get("target"))
        if not source or not target:
            continue
        relationships.append(
            {
                "source": source,
                "target": target,
                "type": normalize_type(raw.get("type"), ALLOWED_RELATIONSHIP_TYPES, "relates_to"),
                "description": clean_description(raw.get("description")),
            }
        )
    return entities[:30], relationships[:40]


def upsert_entity(
    project: str,
    entity: dict[str, str],
    source_table: str,
    source_id: str,
    threshold: float,
) -> dict[str, Any]:
    project_sql = sql_literal(project)
    name_sql = sql_literal(entity["name"])
    type_sql = sql_literal(entity["type"])
    desc_sql = sql_literal(entity["description"])
    source_table_sql = sql_literal(source_table)
    source_id_sql = sql_literal(source_id)
    threshold_sql = f"{float(threshold):.4f}"

    candidate_rows = json_rows(
        f"""
        SELECT id::text, name, entity_type, TRUE AS reused
        FROM cortex_entities
        WHERE project = {project_sql}
          AND entity_type = {type_sql}
          AND similarity(LOWER(name), LOWER({name_sql})) >= {threshold_sql}
        ORDER BY similarity(LOWER(name), LOWER({name_sql})) DESC, updated_at DESC
        LIMIT 1
        """
    )
    if candidate_rows:
        record = candidate_rows[0]
        update_entity_properties(record["id"], desc_sql, source_table_sql, source_id_sql)
        return record

    rows = json_rows_direct(
        f"""
        WITH upserted AS (
            INSERT INTO cortex_entities (project, name, entity_type, properties)
            VALUES (
                {project_sql},
                {name_sql},
                {type_sql},
                jsonb_build_object(
                    'description', {desc_sql},
                    'last_source_table', {source_table_sql},
                    'last_source_ref', {source_id_sql},
                    'source_refs', jsonb_build_array(jsonb_build_object('table', {source_table_sql}, 'id', {source_id_sql}))
                )
            )
            ON CONFLICT (project, name, entity_type) DO UPDATE SET
                properties = COALESCE(cortex_entities.properties, '{{}}'::jsonb) || EXCLUDED.properties,
                updated_at = NOW()
            RETURNING id::text, name, entity_type
        )
        SELECT row_to_json(q)::text
        FROM (
            SELECT id, name, entity_type, FALSE AS reused FROM upserted
        ) q
        """
    )
    if not rows:
        raise RuntimeError(f"entity upsert returned no row for {entity['name']}")
    return rows[0]


def update_entity_properties(record_id: str, desc_sql: str, source_table_sql: str, source_id_sql: str) -> None:
    exec_sql(
        f"""
        UPDATE cortex_entities
        SET
            properties = COALESCE(properties, '{{}}'::jsonb)
                || jsonb_build_object(
                    'description',
                    CASE
                        WHEN COALESCE(properties->>'description', '') = '' THEN {desc_sql}
                        ELSE properties->>'description'
                    END,
                    'last_source_table', {source_table_sql},
                    'last_source_ref', {source_id_sql},
                    'source_refs',
                    COALESCE(properties->'source_refs', '[]'::jsonb)
                        || jsonb_build_array(jsonb_build_object('table', {source_table_sql}, 'id', {source_id_sql}))
                ),
            updated_at = NOW()
        WHERE id = {sql_literal(record_id)}::uuid
        """
    )


def upsert_relationship(
    project: str,
    source: dict[str, Any],
    target: dict[str, Any],
    rel: dict[str, str],
    source_table: str,
    source_id: str,
) -> None:
    exec_sql(
        f"""
        INSERT INTO cortex_relationships (
            project, source_entity_id, target_entity_id, relationship_type, properties
        )
        VALUES (
            {sql_literal(project)},
            {sql_literal(source['id'])}::uuid,
            {sql_literal(target['id'])}::uuid,
            {sql_literal(rel['type'])},
            jsonb_build_object(
                'description', {sql_literal(rel['description'])},
                'source_table', {sql_literal(source_table)},
                'source_ref', {sql_literal(source_id)}
            )
        )
        ON CONFLICT (project, source_entity_id, target_entity_id, relationship_type) DO UPDATE SET
            properties = COALESCE(cortex_relationships.properties, '{{}}'::jsonb) || EXCLUDED.properties
        """
    )


def mark_processed(table: str, row_id: str, entity_count: int, relationship_count: int, model: str) -> None:
    exec_sql(
        f"""
        UPDATE {table}
        SET metadata = COALESCE(metadata, '{{}}'::jsonb) || jsonb_build_object(
            'entities_extracted', TRUE,
            'entities_extracted_at', NOW(),
            'entities_extracted_count', {int(entity_count)},
            'relationships_extracted_count', {int(relationship_count)},
            'entity_extraction_model', {sql_literal(model)}
        )
        WHERE id = {sql_literal(row_id)}::uuid
        """
    )


def process_row(
    project: str,
    row: dict[str, Any],
    models: list[str],
    threshold: float,
    allow_local_fallback: bool,
) -> tuple[int, int]:
    last_error: Exception | None = None
    used_model = models[0]
    data: dict[str, Any] | None = None
    for model in models:
        try:
            data = call_openrouter(model, str(row["content"]))
            used_model = model
            break
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if not is_retryable_provider_error(exc):
                break
            continue
    if data is None:
        if not allow_local_fallback:
            raise last_error or RuntimeError("entity extraction failed")
        data = local_fallback_extract(str(row["content"]))
        used_model = "local-regex-fallback"

    entities, relationships = normalize_extraction(data)

    canonical: dict[str, dict[str, Any]] = {}
    for entity in entities:
        record = upsert_entity(project, entity, row["source_table"], row["id"], threshold)
        canonical[entity["name"].lower()] = record
        canonical[str(record["name"]).lower()] = record

    inserted_relationships = 0
    for rel in relationships:
        source = canonical.get(rel["source"].lower())
        target = canonical.get(rel["target"].lower())
        if not source or not target:
            continue
        upsert_relationship(project, source, target, rel, row["source_table"], row["id"])
        inserted_relationships += 1

    mark_processed(row["source_table"], row["id"], len(entities), inserted_relationships, used_model)
    return len(entities), inserted_relationships


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Extract Cortex knowledge graph entities")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--source", choices=["decisions", "lessons", "knowledge", "all"], default="decisions")
    parser.add_argument("--project", default=os.environ.get("CORTEX_PROJECT", ""))
    parser.add_argument(
        "--model",
        default=os.environ.get(
            "CORTEX_ENTITY_MODEL",
            os.environ.get("CORTEX_V2_ENTITY_MODEL", DEFAULT_ENTITY_MODEL),
        ),
    )
    parser.add_argument(
        "--fallback-models",
        default=os.environ.get("CORTEX_ENTITY_FALLBACK_MODELS", DEFAULT_ENTITY_FALLBACK_MODELS),
        help="comma-separated OpenRouter fallback models used when the primary provider is rate-limited",
    )
    parser.add_argument("--threshold", type=float, default=0.72, help="pg_trgm similarity threshold for fuzzy entity dedup")
    parser.add_argument("--reprocess", action="store_true", help="include rows already marked entities_extracted")
    parser.add_argument("--stats", action="store_true")
    parser.add_argument("--install-schema", action="store_true")
    parser.add_argument("--backfill", action="store_true", help="process unextracted rows across decisions, lessons, and knowledge")
    parser.add_argument("--incremental", action="store_true", help="process the next unextracted rows; alias for the default mode")
    parser.add_argument("--no-local-fallback", action="store_true", help="fail instead of using deterministic local extraction when providers are unavailable")
    args = parser.parse_args()
    if args.backfill:
        args.source = "all"
    return args


def main() -> int:
    parse_args()
    print(
        "ERROR: _cortex_entity_extract.py is retired as a direct database worker; "
        "use cortex-graph-extract / cortex-extract-entities so extraction runs through cortex-api.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
