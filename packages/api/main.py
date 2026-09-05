"""
Cortex API — Local Agent Memory Service
Multi-project support. Auth by agent:project header.
All agent memory operations go through this API.
No direct database access from scripts.
"""

import asyncio
import base64
import collections
import difflib
import hashlib
import hmac
import json
import math
import os
import re
import subprocess
import sys
import time
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Mapping, Optional
from uuid import UUID, uuid4
import zlib

# Memory efficiency transforms (E2+E4)
#
# ALIASED DELIBERATELY. `ingest.compact_text` returns tuple[str, bool, int], but this
# module also defines an unrelated `compact_text(value, *, limit) -> str` display helper
# used by ~67 call sites. That later `def` won for the whole module, so the three E4 call
# sites unpacking three values received a str and raised
# "ValueError: too many values to unpack (expected 3)". It never surfaced only because E4
# is flag-gated off in every deployment - the feature was dead AND would have broken the
# moment anyone set CORTEX_E4_COMPACT=1. Keep these two names distinct.
try:
    from ingest import (
        compact_text as compact_for_storage,
        distill_message,
        is_always_keep,
    )
except ImportError:
    # Fallback for testing environments where the package isn't wired yet.
    compact_for_storage = lambda t: (t, False, 0)
    distill_message = lambda t: [{"content": t, "metadata": {}}]
    is_always_keep = lambda t: True

# Optional zstandard for better cold-tier compression ratio.
try:
    import zstandard as zstd
except ImportError:
    zstd = None

import asyncpg
import httpx
from fastapi import FastAPI, File, Form, Header, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, model_validator

# PersonaPayload contract — cortex.persona.v2 (Phase 1 harness-system foundation).
# Imported here so the boot endpoint can attach a structured persona to responses
# without changing the existing ``boot``/``surface_version`` fields.
try:
    from models.boot import HarnessAdapter, PersonaPayload, SkillManifestEntry
except ModuleNotFoundError:  # pragma: no cover — fallback for alternate working dirs
    import importlib.util as _ilu
    import pathlib as _pl
    _boot_path = _pl.Path(__file__).parent / "models" / "boot.py"
    _spec = _ilu.spec_from_file_location("models.boot", _boot_path)
    _mod = _ilu.module_from_spec(_spec)  # type: ignore[arg-type]
    _spec.loader.exec_module(_mod)  # type: ignore[union-attr]
    HarnessAdapter = _mod.HarnessAdapter
    PersonaPayload = _mod.PersonaPayload
    SkillManifestEntry = _mod.SkillManifestEntry

# Additive read-only SSE bridge (GET /events). Prefer sse-starlette's
# EventSourceResponse (handles keep-alive + disconnect framing); fall back to a
# plain StreamingResponse(text/event-stream) if the dependency is absent so the
# endpoint degrades rather than failing import.
try:
    from sse_starlette.sse import EventSourceResponse
except ModuleNotFoundError:  # pragma: no cover - exercised only without the dep
    EventSourceResponse = None

from observability import (
    ANALYSIS_CALLS,
    CONTENT_TYPE_LATEST,
    EMBEDDING_CALLS,
    REQUEST_DURATION,
    RERANK_CALLS,
    SEARCH_STAGE_DURATION,
    render_metrics,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PG_DSN = os.getenv(
    "CORTEX_PG_DSN",
    "postgresql://postgres:postgres@cortex-pg:5432/platform_agent_memory",
)
# Phase C cutover (handoff 1f5746f2): two-pool architecture
# - PG_DSN_APP: cortex_app non-superuser role; RLS-enforced; used by handlers
#   that have a resolved project scope (via acquire_scoped helper)
# - PG_DSN_ADMIN: postgres superuser role; RLS-bypassed; used by admin/global
#   endpoints (admin/cortex/*, admin/sql/*, /projects, /health, etc.)
# Defaults: if neither override is set, both pools use PG_DSN (back-compat —
# RLS exists but doesn't actually constrain the api). Once cortex_app role
# is provisioned in production, set PG_DSN_APP to enable enforcement.
PG_DSN_APP = os.getenv("CORTEX_PG_DSN_APP", PG_DSN)
PG_DSN_ADMIN = os.getenv("CORTEX_PG_DSN_ADMIN", PG_DSN)

# Database-side deadline for the *app* pool (request handlers only; the admin
# pool keeps running migrations/backfills unbounded). This is the containment
# backstop for incident 912073e2: on marlow-aws a /search averaged 46s while the
# client gave up at 20s, so Postgres kept burning a backend on a result nobody
# would read. When enough of those piled up the postmaster took a SIGPIPE writing
# to a departed client and crash-recovered the whole cluster (362 times in 14h).
# A server-side deadline shorter than any client deadline means the query dies on
# the server first, as a catchable QueryCanceledError, instead of outliving its
# reader. No handler on the app pool has a legitimate 10s query.
APP_STATEMENT_TIMEOUT_MS = int(os.getenv("CORTEX_APP_STATEMENT_TIMEOUT_MS", "15000"))

# Search gets a much tighter budget than the app-wide backstop. Two different jobs:
# the 15s ceiling above exists so nothing on the app pool can ever outlive its
# client, while this one enforces what /search is actually for. Search is
# interactive and fans out over every memory table, so a slow search is a wrong
# search — better to return partial results fast (flagged `degraded`) than a
# complete answer the caller already gave up on. Kept separate so that raising
# search's budget can never quietly raise it for batch paths like the embedding
# backfill, which legitimately need the larger ceiling.
SEARCH_STATEMENT_TIMEOUT_MS = int(os.getenv("CORTEX_SEARCH_STATEMENT_TIMEOUT_MS", "3000"))

# ...and a ceiling on the WHOLE request, because a per-stage budget does not bound
# one. Measured on marlow-aws after the per-stage budget shipped: `execute_search`
# has SEVEN stage_fetch call sites, so 7 x 3000ms plus rerank/embedding overhead
# produced COLD searches of 24.2s and 24.9s while every individual stage was
# obediently inside its 3s limit. Each stage was correct; their sum was not — the
# same seam-shaped failure as everything else in this incident.
#
# A caller does not experience stages, it experiences the request. This is that
# number, and it is the one the acceptance bar (p95 < 2s, hard max < 5s) is written
# against. Stages consult it before starting and clamp their own timeout to whatever
# is left, so the total cannot walk past it however many stages are added later.
SEARCH_TOTAL_BUDGET_MS = int(os.getenv("CORTEX_SEARCH_TOTAL_BUDGET_MS", "4500"))

# Bulkhead: cap concurrent /search requests. Search is the only handler that fans
# out across every memory table, so it is the one that can saturate a 10-connection
# pool and queue work faster than Postgres drains it. Shedding load with a typed 503
# keeps the rest of the API (boot, handoffs, log) responsive under a search storm.
# Marlow's 2-vCPU self-contained host proved that six admitted searches can still
# saturate PostgreSQL even when every individual request is bounded. Keep only two
# searches in flight by default; operators on larger hosts may raise this explicitly.
SEARCH_MAX_CONCURRENCY = int(os.getenv("CORTEX_SEARCH_MAX_CONCURRENCY", "2"))

# ILIKE '%needle%' can only use a GIN trgm index when the needle has at least 3
# non-wildcard characters; below that pg_trgm has no trigram to look up and the
# planner falls back to a sequential scan. Skip the lexical stage instead.
TRGM_MIN_QUERY_LEN = 3

# Ranking and room-filtering read only this prefix of a body column, so their
# per-row cost is capped no matter how long a row grows. Never wrap an indexed
# MATCH predicate in this — the trgm indexes are on the bare columns, and an
# expression the index does not cover falls back to a sequential scan.
# ponytail: 1000 chars is a ranking/filter cap, not a recall cap — the match
# predicate still sees the whole column. Raise it if ordering quality suffers.
TRGM_PREFIX_CHARS = 1000
# A common term can index-match tens of thousands of rows. Sorting similarity
# over that entire set made the otherwise-fast GIN lookup irrelevant (47k
# `handoff` rows took 2.3s to heap-read and rank on Marlow). Materialize a
# bounded candidate set first, then rank it. This is an explicit fuzzy-ranking
# recall cap; exact/BM25/vector stages still contribute independently.
TRGM_CANDIDATE_LIMIT = int(os.getenv("CORTEX_TRGM_CANDIDATE_LIMIT", "100"))
HARNESS_APPDB_DSN_DEFAULT = "postgresql://harness:harness@localhost:5500/harness_app"
HARNESS_APPDB_CONNECT_TIMEOUT = float(os.getenv("HARNESS_APPDB_MIGRATION_TIMEOUT", "1.0"))
VALID_EVENT_BACKENDS = {"postgres"}


def parse_event_backend(value: str | None = None) -> str:
    """Parse the event backend flag once so bad config fails clearly."""
    raw = (value if value is not None else os.getenv("CORTEX_EVENT_BACKEND", "postgres")).strip().lower()
    if raw not in VALID_EVENT_BACKENDS:
        allowed = ", ".join(sorted(VALID_EVENT_BACKENDS))
        raise RuntimeError(
            f"Invalid CORTEX_EVENT_BACKEND={raw!r}; expected one of: {allowed}"
        )
    return raw


CORTEX_EVENT_BACKEND = parse_event_backend()
CORTEX_API_VERSION = "2.3"
CORTEX_SURFACE_VERSION = os.getenv(
    "CORTEX_SURFACE_VERSION",
    "kaidera-os-e006-inc01-2026-06-01",
)
GRAPH_WORKER_URL = os.getenv(
    "CORTEX_GRAPH_WORKER_URL", "http://cortex-graph-worker:9001"
).rstrip("/")
VISION_WORKER_URL = os.getenv(
    "CORTEX_VISION_WORKER_URL", "http://cortex-vision-worker:9002"
).rstrip("/")
AUDIO_WORKER_URL = os.getenv(
    "CORTEX_AUDIO_WORKER_URL", "http://cortex-audio-worker:9003"
).rstrip("/")
PDF_WORKER_URL = os.getenv(
    "CORTEX_PDF_WORKER_URL", "http://cortex-pdf-worker:9004"
).rstrip("/")
EMBED_WORKER_URL = os.getenv(
    "CORTEX_EMBED_WORKER_URL", "http://cortex-embed-worker:9005"
).rstrip("/")
LOCAL_SEARCH_PROVIDER = "local"
LOCAL_EMBED_MODEL = "sentence-transformers/all-mpnet-base-v2"
LOCAL_EMBED_DIMS = 768
LOCAL_RERANK_MODEL = "cross-encoder/ms-marco-MiniLM-L6-v2"
LOCAL_RERANK_MAX_DOCUMENTS = 64
LOCAL_SEARCH_MAX_TEXT_BYTES = 16 * 1024
LOCAL_SEARCH_MAX_BATCH_TEXT_BYTES = 256 * 1024
LOCAL_SEARCH_MAX_RESPONSE_BYTES = 256 * 1024
EMBED_MODEL = os.getenv(
    "CORTEX_EMBED_MODEL", "nvidia/llama-nemotron-embed-vl-1b-v2:free"
)
EMBED_DIMS = int(os.getenv("CORTEX_EMBED_DIMS", "768"))
# E3 vector-precision gate (memory-efficiency). 'float32' (default) = the original
# $1::vector path. 'halfvec' = cast to halfvec(768) so the smaller halfvec ivfflat indexes
# are used (~60% smaller, faster scan, <1% recall delta on normalized embeddings). The
# query cast MUST match the index's cast expression or the index is simply unused (still
# correct, just a seq scan). Flip per-deployment; instant revert by setting float32.
CORTEX_VECTOR_PRECISION = os.getenv("CORTEX_VECTOR_PRECISION", "float32").strip().lower()
_VECTOR_CAST = "$1::halfvec(768)" if CORTEX_VECTOR_PRECISION == "halfvec" else "$1::vector"
# Rerank defaults to the SAME gateway as embeddings. The previous default pair
# (nvidia / nv-rerank-qa-mistral-4b:1) needs an NVIDIA key, which no old install path set —
# and rerank_results() returns None when the key is missing, so the shipped default
# silently disabled reranking on every fresh deployment. The `:free` suffix is part of the
# OpenRouter id; the bare id has no served endpoint.
RERANK_MODEL = os.getenv(
    "CORTEX_RERANK_MODEL", "nv-rerank-qa-mistral-4b:1"
)
EMBED_PROVIDER = os.getenv("CORTEX_EMBED_PROVIDER", "openrouter").strip().lower() or "openrouter"
RERANK_PROVIDER = os.getenv("CORTEX_RERANK_PROVIDER", "nvidia").strip().lower() or "openrouter"
RERANK_ENABLED = os.getenv("CORTEX_RERANK_ENABLED", "true").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}

# Execution analysis LLM config — configurable per environment.
# Default: free models via OpenRouter with automatic fallback.
# Override with CORTEX_ANALYSIS_MODEL for a single model (no fallback).
ANALYSIS_MODEL = os.getenv("CORTEX_ANALYSIS_MODEL", "")
ANALYSIS_PROVIDER = os.getenv(
    "CORTEX_ANALYSIS_PROVIDER", "openrouter"
)
# Fallback chain for free-tier analysis (tried in order if primary returns 429)
ANALYSIS_FALLBACK_MODELS = [
    m.strip() for m in os.getenv(
        "CORTEX_ANALYSIS_FALLBACK_MODELS",
        "nvidia/nemotron-3-super-120b-a12b:free,"
        "google/gemma-4-31b-it:free,"
        "minimax/minimax-m2.5:free,"
        "openai/gpt-oss-120b:free",
    ).split(",") if m.strip()
]
# REN-SEC-01: do NOT bake a guessable default admin token. /admin/* (incl. the
# arbitrary-SQL /admin/sql endpoints on the superuser pool) is gated only by this
# token, so a shipped default left the admin surface open to anyone who read the
# source. An unset token now fails closed (the require-admin check 403s when
# ADMIN_TOKEN is empty) instead of silently accepting the well-known default.
# Configured installs set CORTEX_ADMIN_TOKEN via .env / launchd plist; rotate any
# legacy "cortex-local-admin" value to a generated secret as an operator step.
ADMIN_TOKEN = os.getenv("CORTEX_ADMIN_TOKEN", "")
# Known weak/default tokens that must not be relied on (rotate to a generated
# secret). Exposed for a future startup assertion (REN-ARCH-02) once this module
# gains structured logging; today the empty default already fails closed.
LEGACY_WEAK_ADMIN_TOKENS = frozenset({"cortex-local-admin"})
CORTEX_JWT_SECRET = os.getenv("CORTEX_JWT_SECRET", "")
CORTEX_AUTH_REQUIRE_JWT = os.getenv("CORTEX_AUTH_REQUIRE_JWT", "false").lower() == "true"
LOCAL_STATE_PROJECT = os.getenv("CORTEX_LOCAL_STATE_PROJECT", "_local_state")
SHARED_KNOWLEDGE_PROJECT = os.getenv("CORTEX_SHARED_KNOWLEDGE_PROJECT", "_global")

# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------

pool: asyncpg.Pool | None = None  # Alias for pool_app — back-compat for raw `pool.acquire()` callers (admin/global handlers); cleanly resolves to pool_admin where appropriate.
pool_app: asyncpg.Pool | None = None
pool_admin: asyncpg.Pool | None = None
# REN-ARCH-02: whether the app pool's role actually enforces RLS (i.e. is NOT a
# superuser and does NOT bypass RLS). Computed once at startup; surfaced in
# /health. None until lifespan runs.
RLS_ENFORCED: bool | None = None
EVENT_WAKE_CHANNEL = "cortex_events"
event_condition: asyncio.Condition | None = None
event_listener_task: asyncio.Task | None = None
event_listener_conn: asyncpg.Connection | None = None
event_listener_ready = False
event_listener_last_id: int | None = None
event_listener_error: str | None = None


async def ensure_roles_schema() -> None:
    """Ensure the first-class roles table exists using the admin DB pool.

    Runtime project handlers use ``pool_app``/``cortex_app`` and must not run
    DDL. This bootstrap path owns CREATE/ALTER/RLS/grants so registering a new
    project agent works on fresh or upgraded local Cortex stores.
    """
    assert pool_admin is not None, "Admin DB pool not initialised"
    async with pool_admin.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS roles (
                project TEXT NOT NULL,
                name TEXT NOT NULL,
                default_capabilities JSONB NOT NULL DEFAULT '{}'::jsonb,
                description TEXT,
                is_builtin BOOLEAN NOT NULL DEFAULT FALSE,
                source_file TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (project, name)
            )
            """
        )
        await conn.execute("ALTER TABLE roles OWNER TO postgres")
        await conn.execute("ALTER TABLE roles ENABLE ROW LEVEL SECURITY")
        await conn.execute("DROP POLICY IF EXISTS roles_project_isolation ON roles")
        await conn.execute(
            """
            CREATE POLICY roles_project_isolation ON roles
              USING (
                  project = current_setting('cortex.project', TRUE)
                  OR project = '_global'
              )
              WITH CHECK (
                  project = current_setting('cortex.project', TRUE)
                  OR project = '_global'
              )
            """
        )
        await conn.execute(
            """
            DO $$
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cortex_app') THEN
                    EXECUTE 'GRANT USAGE ON SCHEMA public TO cortex_app';
                    EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE roles TO cortex_app';
                END IF;
            END $$;
            """
        )


def event_backend_uses_postgres() -> bool:
    return CORTEX_EVENT_BACKEND == "postgres"


def ensure_event_condition() -> asyncio.Condition:
    global event_condition
    if event_condition is None:
        event_condition = asyncio.Condition()
    return event_condition


async def notify_event_waiters(event_id: int | None = None) -> None:
    global event_listener_last_id
    if event_id is not None:
        event_listener_last_id = max(event_listener_last_id or 0, event_id)
    condition = ensure_event_condition()
    async with condition:
        condition.notify_all()
    if event_id is not None:
        asyncio.create_task(invalidate_roster_policy_for_event(event_id))


async def invalidate_roster_policy_for_event(event_id: int) -> None:
    """Invalidate roster-policy cache from an ID-only team-event notification."""
    if pool_admin is None:
        return
    try:
        async with pool_admin.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT project, event_type FROM team_events WHERE id = $1",
                event_id,
            )
        if row and row["event_type"] in {"agent_registered", "project_registered"}:
            _invalidate_roster_policy(row["project"])
    except Exception:
        # The NOTIFY payload is ID-only. If resolution fails, clear all rather
        # than risk serving a stale writer set after a registry mutation.
        _invalidate_roster_policy(None)


def event_notification_callback(
    _conn: asyncpg.Connection,
    _pid: int,
    _channel: str,
    payload: str,
) -> None:
    try:
        event_id = int(payload)
    except (TypeError, ValueError):
        event_id = None
    asyncio.create_task(notify_event_waiters(event_id))


async def listen_for_team_events() -> None:
    """Maintain one dedicated LISTEN connection for ID-only event wakeups."""
    global event_listener_conn, event_listener_ready, event_listener_error

    while True:
        conn: asyncpg.Connection | None = None
        try:
            conn = await asyncpg.connect(PG_DSN_ADMIN)
            event_listener_conn = conn
            await conn.add_listener(EVENT_WAKE_CHANNEL, event_notification_callback)
            event_listener_ready = True
            event_listener_error = None
            await asyncio.Future()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            event_listener_ready = False
            event_listener_error = str(exc)
            await asyncio.sleep(2)
        finally:
            if conn is not None:
                with suppress(Exception):
                    await conn.remove_listener(EVENT_WAKE_CHANNEL, event_notification_callback)
                with suppress(Exception):
                    await conn.close()
            if event_listener_conn is conn:
                event_listener_conn = None
            event_listener_ready = False


async def detect_rls_enforced(app_pool: asyncpg.Pool) -> bool | None:
    """Return True if the app pool's role enforces RLS (non-superuser, no BYPASSRLS).

    REN-ARCH-02: the two-pool RLS design only protects project isolation if
    pool_app connects as a NOBYPASSRLS, non-superuser role (cortex_app). If the
    DSN silently falls back to the postgres superuser, RLS no-ops and the
    isolation backstop is gone with no signal. Detect this once at startup,
    surface it in /health, and fail loud when CORTEX_REQUIRE_RLS is set.
    Returns None if the probe itself could not run.
    """
    try:
        async with app_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"
            )
    except Exception:
        return None
    if row is None:
        return None
    return not (row["rolsuper"] or row["rolbypassrls"])


# ---------------------------------------------------------------------------
# Retention enforcement (I24). retention_config was read only to INSERT its own
# defaults and confirm it existed — nothing enforced it, and messages reached
# 1.78M rows / 8.6 GB with the oldest row at 161 days against a 90-day policy.
# This sweep is the enforcement: the same atomic MOVE the cortex-retain script
# proved (INSERT .. ON CONFLICT DO NOTHING RETURNING id; DELETE only what
# actually landed — a conflicting row STAYS in the source, because deleting a
# row the insert skipped was an unrecoverable loss and archive ids are copied
# verbatim with no shared sequence). Batched so the first run against a large
# backlog never holds a mega-transaction; a sweep that hits its batch ceiling
# says so loudly and continues next sweep — no silent caps.
# ---------------------------------------------------------------------------

def _retention_env_int(name: str, default: int, lo: int, hi: int) -> int:
    try:
        val = int(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        val = default
    return max(lo, min(hi, val))


RETENTION_SWEEP_S = _retention_env_int("CORTEX_RETENTION_SWEEP_S", 21600, 300, 86400)
RETENTION_BATCH_ROWS = _retention_env_int("CORTEX_RETENTION_BATCH_ROWS", 5000, 500, 50000)
RETENTION_MAX_BATCHES = _retention_env_int("CORTEX_RETENTION_MAX_BATCHES", 20, 1, 1000)

# Column lists mirror .agents/scripts/cortex-retain (the archive tables have no
# DEFAULTs to rely on; an unlisted column would silently archive NULLs). The
# handoff guard archives only terminal rows whose reply children are themselves
# terminal AND aged — archiving a parent whose thread is still moving orphans
# the conversation.
_RETENTION_TABLES: dict[str, dict[str, str]] = {
    "messages": {
        "dst": "archive_messages",
        "ts_col": "ts",
        "cols": "id, session_id, project, agent_name, role, content, metadata, ts",
    },
    "team_events": {
        "dst": "archive_events",
        "ts_col": "ts",
        "cols": "id, ts, agent_name, event_type, summary, detail, files, project, sprint_id",
    },
    "decisions": {
        "dst": "archive_decisions",
        "ts_col": "created_at",
        "cols": "id, sprint_id, agent_name, summary, rationale, outcome, category, files_affected, tags, project, created_at",
    },
    "lessons": {
        "dst": "archive_lessons",
        "ts_col": "created_at",
        "cols": "id, agent_name, category, summary, detail, code_right, code_wrong, project, created_at",
    },
    "handoffs": {
        "dst": "archive_handoffs",
        "ts_col": "created_at",
        "cols": (
            "id, kind, reply_to_handoff_id, project, from_agent, to_role, priority, "
            "summary, files_changed, acceptance, evidence, completion_report, retry, "
            "escalation, status, created_at, returned_at, completed_at"
        ),
        "extra_where": (
            "\n              AND status IN ('completed','abandoned','failed','archived')"
            "\n              AND NOT EXISTS ("
            "\n                SELECT 1 FROM handoffs child"
            "\n                 WHERE child.reply_to_handoff_id = handoffs.id"
            "\n                   AND (child.status NOT IN ('completed','abandoned','failed','archived')"
            "\n                        OR child.created_at >= now() - interval '{days} days'))"
        ),
    },
}


def retention_move_sql(table: str, days: int, batch: int) -> str:
    """One atomic batched MOVE statement for ``table`` (tier 2 → tier 3).

    ``days``/``batch`` are int-coerced here so the interpolation can never carry
    anything but bounded integers (days comes from retention_config's INTEGER
    column; batch from a clamped env). No project predicate: enforcement is
    store-wide — the archive rows carry their project. Oldest-first so a partial
    sweep always drains the tail before newer rows."""
    spec = _RETENTION_TABLES[table]
    days = int(days)
    batch = int(batch)
    extra = spec.get("extra_where", "").format(days=days)
    return f"""WITH candidates AS (
            SELECT id FROM {table}
            WHERE {spec['ts_col']} < now() - interval '{days} days'{extra}
            ORDER BY {spec['ts_col']}
            LIMIT {batch}
        ), moved AS (
            INSERT INTO {spec['dst']} ({spec['cols']})
            SELECT {spec['cols']} FROM {table}
            WHERE id IN (SELECT id FROM candidates)
            ON CONFLICT DO NOTHING
            RETURNING id
        ), pruned AS (
            DELETE FROM {table} WHERE id IN (SELECT id FROM moved) RETURNING id
        )
        SELECT count(*) FROM pruned"""


async def run_retention_sweep() -> dict[str, dict]:
    """Apply retention_config once, batched; returns per-table outcomes."""
    from observability import RETENTION_ROWS_ARCHIVED

    results: dict[str, dict] = {}
    try:
        async with pool_admin.acquire() as conn:
            config_rows = await conn.fetch(
                "SELECT table_name, tier2_days FROM retention_config"
            )
    except Exception as exc:
        print(f"WARNING: retention: could not read retention_config (skipping): {exc}", file=sys.stderr)
        return results
    for row in config_rows:
        name = str(row["table_name"]).strip()
        days = int(row["tier2_days"] or 0)
        spec = _RETENTION_TABLES.get(name)
        if spec is None:
            print(
                f"WARNING: retention: retention_config names {name!r} but no "
                "enforcement spec exists for it — rows there are NOT being archived",
                file=sys.stderr,
            )
            results[name] = {"archived": 0, "error": "no enforcement spec"}
            continue
        if days <= 0:
            results[name] = {"archived": 0, "disabled": True}
            continue
        moved_total = 0
        batches = 0
        last_moved = 0
        try:
            while batches < RETENTION_MAX_BATCHES:
                async with pool_admin.acquire() as conn:
                    last_moved = int(
                        await conn.fetchval(
                            retention_move_sql(name, days, RETENTION_BATCH_ROWS)
                        )
                        or 0
                    )
                batches += 1
                if last_moved:
                    moved_total += last_moved
                    RETENTION_ROWS_ARCHIVED.labels(table=name).inc(last_moved)
                if last_moved < RETENTION_BATCH_ROWS:
                    break
                await asyncio.sleep(0.25)
        except Exception as exc:
            print(f"WARNING: retention: {name} sweep failed after {moved_total} rows: {exc}",
                  file=sys.stderr)
            results[name] = {"archived": moved_total, "error": str(exc)}
            continue
        more_pending = batches >= RETENTION_MAX_BATCHES and last_moved >= RETENTION_BATCH_ROWS
        results[name] = {"archived": moved_total, "more_pending": more_pending}
        if moved_total or more_pending:
            tail = " (batch ceiling hit — continuing next sweep)" if more_pending else ""
            print(
                f"retention: {name} → {spec['dst']} archived {moved_total} row(s) "
                f"older than {days}d{tail}",
                file=sys.stderr,
            )
    return results


async def enforce_retention_forever() -> None:
    """The background enforcement loop retention_config always claimed existed."""
    while True:
        try:
            await run_retention_sweep()
        except Exception as exc:  # never let the loop die silently
            print(f"WARNING: retention: sweep crashed (retrying next interval): {exc}",
                  file=sys.stderr)
        await asyncio.sleep(RETENTION_SWEEP_S)


def enforce_memory_transform_release_hold() -> None:
    """Fail closed if unfinished E2/E4 write transforms are enabled in v0.2.002."""
    held = [
        name
        for name in ("CORTEX_E2_DISTILL", "CORTEX_E4_COMPACT")
        if os.environ.get(name) == "1"
    ]
    if held:
        raise RuntimeError(
            "v0.2.002 does not support enabling the held memory transform(s): "
            + ", ".join(held)
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    global pool, pool_app, pool_admin, event_listener_task, RLS_ENFORCED
    enforce_memory_transform_release_hold()
    # Two-pool Phase C architecture
    pool_app = await asyncpg.create_pool(
        PG_DSN_APP,
        min_size=2,
        max_size=10,
        # Every app-pool connection carries the deadline, so a handler cannot opt
        # out of it by forgetting to set one. Admin pool is deliberately exempt.
        server_settings={"statement_timeout": str(APP_STATEMENT_TIMEOUT_MS)},
    )
    pool_admin = await asyncpg.create_pool(PG_DSN_ADMIN, min_size=1, max_size=4)
    await ensure_roles_schema()
    await recover_interrupted_graph_build_jobs()
    # REN-ARCH-02: verify the app pool actually enforces RLS; warn loudly (or
    # fail when CORTEX_REQUIRE_RLS is set) if it silently fell back to superuser.
    RLS_ENFORCED = await detect_rls_enforced(pool_app)
    if RLS_ENFORCED is False:
        _msg = (
            "RLS NOT ENFORCED: cortex-api app pool connects as a superuser/BYPASSRLS "
            "role, so Postgres row-level project isolation is inactive. Point "
            "CORTEX_PG_DSN_APP at the non-superuser cortex_app role. (REN-ARCH-02)"
        )
        if os.getenv("CORTEX_REQUIRE_RLS", "").strip().lower() in {"1", "true", "yes"}:
            raise RuntimeError(_msg)
        print(f"WARNING: {_msg}", file=sys.stderr)
    # Back-compat alias: raw `pool.acquire()` callers in admin/global paths
    # land on the admin pool (they were always implicitly admin since they
    # didn't go through acquire_scoped). The 51 acquire_scoped() callers use
    # pool_app via the helper.
    pool = pool_admin
    if event_backend_uses_postgres():
        ensure_event_condition()
        event_listener_task = asyncio.create_task(listen_for_team_events())
    retention_task = asyncio.create_task(enforce_retention_forever())
    try:
        yield
    finally:
        retention_task.cancel()
        with suppress(asyncio.CancelledError):
            await retention_task
        if event_listener_task is not None:
            event_listener_task.cancel()
            with suppress(asyncio.CancelledError):
                await event_listener_task
            event_listener_task = None
        graph_tasks = tuple(_GRAPH_BUILD_TASKS)
        for task in graph_tasks:
            task.cancel()
        if graph_tasks:
            await asyncio.gather(*graph_tasks, return_exceptions=True)
    await pool_app.close()
    await pool_admin.close()


# ---------------------------------------------------------------------------
# Phase C — RLS defense-in-depth: scoped connection acquire
# ---------------------------------------------------------------------------
# acquire_scoped(project) is the canonical pattern for any handler that has a
# resolved project. It SETs cortex.project at session level so PostgreSQL RLS
# policies (created by .agents/data/migrations/2026-05-08-phase-c-rls.sql) can
# enforce project isolation at the row level.
#
# Postgres `postgres` superuser BYPASSES RLS, so today's cortex-api connections
# are unaffected by the policies — but the SET still happens (cheap; harmless)
# so future cortex_app role cutover requires zero handler changes.

@asynccontextmanager
async def acquire_scoped(project: str, *, statement_timeout_ms: int | None = None):
    """Acquire a pooled connection from pool_app with cortex.project set.

    pool_app connects as cortex_app (non-superuser, RLS-enforced) once the
    PG_DSN_APP env override is set. Until then it falls back to PG_DSN
    (postgres superuser, RLS-bypassed) so the migration is back-compat.
    Phase C of handoff 1f5746f2 (Alpha Option-D / RLS defense-in-depth).
    """
    assert pool_app is not None, "App DB pool not initialised"
    async with pool_app.acquire() as conn:
        await conn.execute("SELECT set_config('cortex.project', $1, false)", project)
        if statement_timeout_ms is not None:
            # Session-level, so it covers every statement this handler runs. The
            # RESET below restores the pool default; if that reset cannot run, the
            # connection is destroyed rather than returned carrying a stray budget.
            await conn.execute(f"SET statement_timeout = {int(statement_timeout_ms)}")
        try:
            yield conn
        finally:
            # Recovery-aware release (incident 912073e2). This reset used to be a bare
            # await: when the cluster had just crash-recovered, or the client had
            # disconnected and cancelled us, it raised on a dead socket and that
            # exception REPLACED whatever real error we were unwinding — which is why
            # the incident surfaced as "connection has been released back to the pool"
            # instead of the actual cause. If the reset cannot run, the session scope
            # is unknown, so destroy the connection rather than return a dirty one to
            # the pool; asyncpg opens a clean replacement on next acquire.
            try:
                reset = "SELECT set_config('cortex.project', '', false)"
                if statement_timeout_ms is not None:
                    await conn.execute("RESET statement_timeout")
                await conn.execute(reset)
            # asyncpg.InterfaceError is NOT a PostgresError — the two are disjoint
            # roots. It is what a *client-side* dead connection raises
            # (`_check_open` -> InterfaceError('connection is closed')), which is
            # precisely the post-crash-recovery case this block exists for. Omitting
            # it left the incident's own scenario escaping the guard.
            except (
                asyncpg.PostgresError,
                asyncpg.InterfaceError,
                OSError,
                asyncio.CancelledError,
            ) as exc:
                with suppress(Exception):
                    conn.terminate()
                if isinstance(exc, asyncio.CancelledError):
                    raise


async def emit_team_event(
    conn: asyncpg.Connection,
    *,
    project: str,
    agent_name: str,
    event_type: str,
    summary: str,
    detail: dict[str, Any] | None = None,
    files: list[str] | None = None,
    sprint_id: UUID | str | None = None,
    related_decision_id: UUID | str | None = None,
    notify: bool = True,
    verify: bool = False,
) -> int:
    """Insert one team_events row and optionally wake listeners with its id.

    The insert and ``pg_notify`` run inside one transaction boundary. The
    notification payload is intentionally only the bigint event id; consumers
    fetch scoped row data from Postgres instead of receiving project/content
    fields through LISTEN/NOTIFY.
    """
    if not project or not project.strip():
        raise ValueError("project is required for team event publication")

    detail_json = json.dumps(detail) if detail is not None else None
    sprint_value = str(sprint_id) if sprint_id else None
    decision_value = str(related_decision_id) if related_decision_id else None

    async with conn.transaction():
        event_id = await conn.fetchval(
            """
            INSERT INTO team_events (
                project, agent_name, event_type, summary, detail, files,
                sprint_id, related_decision_id, ts
            )
            VALUES ($1, $2, $3, $4, $5::jsonb, $6::text[], $7::uuid, $8::uuid, NOW())
            RETURNING id
            """,
            project.strip().lower(),
            agent_name,
            event_type,
            summary,
            detail_json,
            files,
            sprint_value,
            decision_value,
        )
        if notify:
            await conn.execute("SELECT pg_notify('cortex_events', $1)", str(event_id))

        if verify:
            await verify_team_event_persisted(
                conn,
                project=project.strip().lower(),
                event_id=int(event_id),
                expected_agent=agent_name,
                expected_event_type=event_type,
                expected_summary=summary,
                expected_files=files,
            )

    return int(event_id)


def write_fingerprint(value: Any) -> str:
    rendered = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def normalize_ingest_mode(value: str | None) -> str:
    mode = (value or "conflict").strip().lower()
    if mode not in {"conflict", "update"}:
        raise HTTPException(400, "on_conflict must be one of: conflict, update")
    return mode


def ingest_conflict_error(kind: str, row_id: str | int, expected: dict[str, Any], actual: dict[str, Any]) -> None:
    changed_fields = [
        field
        for field, expected_value in expected.items()
        if actual.get(field) != expected_value
    ]
    raise HTTPException(
        409,
        {
            "status": "conflict",
            "kind": kind,
            "id": str(row_id),
            "created": False,
            "embedded": False,
            "changed_fields": changed_fields,
            "expected_sha256": write_fingerprint(expected),
            "actual_sha256": write_fingerprint(actual),
        },
    )


def write_fidelity_error(kind: str, row_id: str | int, field: str, expected: Any, actual: Any) -> None:
    raise HTTPException(
        500,
        (
            f"{kind} write fidelity check failed for {row_id} field {field}; "
            f"expected_sha256={write_fingerprint(expected)} actual_sha256={write_fingerprint(actual)}"
        ),
    )


def assert_write_field_exact(kind: str, row_id: str | int, field: str, expected: Any, actual: Any) -> None:
    if expected != actual:
        write_fidelity_error(kind, row_id, field, expected, actual)


def normalize_db_json(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


async def verify_memory_write_persisted(
    conn: asyncpg.Connection,
    *,
    table: str,
    row_id: UUID | str,
    project: str,
    expected_agent: str,
    expected_summary: str,
    expected_category: str | None,
    expected_metadata: dict[str, Any] | None,
) -> None:
    if table not in {"decisions", "lessons"}:
        raise ValueError(f"unsupported memory write table: {table}")
    row = await conn.fetchrow(
        f"""SELECT id::text, project, agent_name, summary, category, metadata
              FROM {table}
             WHERE id = $1 AND project = $2""",
        row_id,
        project,
    )
    if row is None:
        raise HTTPException(500, f"{table[:-1]} write fidelity check failed; inserted row {row_id} was not readable")
    kind = table[:-1]
    assert_write_field_exact(kind, row_id, "project", project, row["project"])
    assert_write_field_exact(kind, row_id, "agent_name", expected_agent, row["agent_name"])
    assert_write_field_exact(kind, row_id, "summary", expected_summary, row["summary"])
    assert_write_field_exact(kind, row_id, "category", expected_category, row["category"])
    assert_write_field_exact(kind, row_id, "metadata", expected_metadata or {}, normalize_db_json(row["metadata"]) or {})


async def verify_team_event_persisted(
    conn: asyncpg.Connection,
    *,
    project: str,
    event_id: int,
    expected_agent: str,
    expected_event_type: str,
    expected_summary: str,
    expected_files: list[str] | None,
) -> None:
    row = await conn.fetchrow(
        """SELECT id, project, agent_name, event_type, summary, files
             FROM team_events
            WHERE id = $1 AND project = $2""",
        event_id,
        project,
    )
    if row is None:
        raise HTTPException(500, f"team_event write fidelity check failed; inserted row {event_id} was not readable")
    assert_write_field_exact("team_event", event_id, "project", project, row["project"])
    assert_write_field_exact("team_event", event_id, "agent_name", expected_agent, row["agent_name"])
    assert_write_field_exact("team_event", event_id, "event_type", expected_event_type, row["event_type"])
    assert_write_field_exact("team_event", event_id, "summary", expected_summary, row["summary"])
    assert_write_field_exact("team_event", event_id, "files", expected_files or [], list(row["files"] or []))


async def verify_handoff_persisted(
    conn: asyncpg.Connection,
    *,
    row_id: UUID | str,
    project: str,
    expected: Any,
    expected_from_agent: str,
    expected_to_agent: str | None,
) -> None:
    row = await conn.fetchrow(
        """SELECT id::text, project, from_agent, from_role, to_role, to_agent,
                  priority, summary, branch, files_changed, verification,
                  next_steps, context, parent_goal_id::text,
                  acceptance, evidence, retry, escalation
             FROM handoffs
            WHERE id = $1 AND project = $2""",
        row_id,
        project,
    )
    if row is None:
        raise HTTPException(500, f"handoff write fidelity check failed; inserted row {row_id} was not readable")
    assert_write_field_exact("handoff", row_id, "project", project, row["project"])
    assert_write_field_exact("handoff", row_id, "from_agent", expected_from_agent, row["from_agent"])
    assert_write_field_exact("handoff", row_id, "from_role", expected.from_role, row["from_role"])
    assert_write_field_exact("handoff", row_id, "to_role", expected.to_role, row["to_role"])
    assert_write_field_exact("handoff", row_id, "to_agent", expected_to_agent, row["to_agent"])
    assert_write_field_exact("handoff", row_id, "priority", expected.priority, row["priority"])
    assert_write_field_exact("handoff", row_id, "summary", expected.summary, row["summary"])
    assert_write_field_exact("handoff", row_id, "branch", expected.branch, row["branch"])
    assert_write_field_exact("handoff", row_id, "files_changed", expected.files_changed or [], list(row["files_changed"] or []))
    assert_write_field_exact("handoff", row_id, "verification", expected.verification, row["verification"])
    assert_write_field_exact("handoff", row_id, "next_steps", expected.next_steps, row["next_steps"])
    assert_write_field_exact("handoff", row_id, "context", expected.context, row["context"])
    assert_write_field_exact("handoff", row_id, "parent_goal_id", expected.parent_goal_id, row["parent_goal_id"])
    assert_write_field_exact("handoff", row_id, "acceptance", handoff_policy(expected.acceptance), normalize_db_json(row["acceptance"]) or {})
    assert_write_field_exact("handoff", row_id, "evidence", handoff_policy(expected.evidence), normalize_db_json(row["evidence"]) or {})
    assert_write_field_exact("handoff", row_id, "retry", handoff_policy(expected.retry), normalize_db_json(row["retry"]) or {})
    assert_write_field_exact("handoff", row_id, "escalation", handoff_policy(expected.escalation), normalize_db_json(row["escalation"]) or {})


OPEN_HANDOFF_STATUSES = ("pending", "claimed", "returned")
HANDOFF_CREATE_DEDUPE_SCHEMA = "cortex.handoff_create_dedupe.v1"
HANDOFF_COMPLETION_REPORT_SCHEMA = "cortex.handoff_completion_report.v1"


def handoff_create_dedupe_fingerprint(
    *,
    project: str,
    from_agent: str,
    body: Any,
    to_agent: str | None,
) -> str:
    """Stable fingerprint for byte-identical open-handoff create requests."""
    return write_fingerprint({
        "schema": HANDOFF_CREATE_DEDUPE_SCHEMA,
        "project": project,
        "from_agent": from_agent,
        "from_role": body.from_role,
        "to_role": body.to_role,
        "to_agent": to_agent,
        "priority": body.priority,
        "summary": body.summary,
        "branch": body.branch,
        "files_changed": body.files_changed,
        "verification": body.verification,
        "next_steps": body.next_steps,
        "context": body.context,
        "parent_goal_id": body.parent_goal_id,
        "acceptance": handoff_policy(body.acceptance),
        "evidence": handoff_policy(body.evidence),
        "retry": handoff_policy(body.retry),
        "escalation": handoff_policy(body.escalation),
    })


def require_jwt_actor_match(request: Request, actor: str, project: str) -> None:
    """Bind audit-bearing agent writes to the authenticated actor when present."""
    claims = getattr(request.state, "jwt_claims", {}) or {}
    if not claims:
        return
    claimed_actor = str(claims.get("agent") or "").strip()
    legacy_actor = str(claims.get("agent_name") or "").strip()
    if not claimed_actor and not legacy_actor:
        raise HTTPException(403, "Bearer token does not identify an agent")
    if claimed_actor and legacy_actor:
        try:
            claimed_base = agent_base_for_project(
                claimed_actor,
                project,
                field_name="bearer agent",
            )
            legacy_base = agent_base_for_project(
                legacy_actor,
                project,
                field_name="bearer agent_name",
            )
        except HTTPException as exc:
            raise HTTPException(403, "Bearer actor is not valid for this project") from exc
        if claimed_base != legacy_base:
            raise HTTPException(403, "Bearer actor claims disagree")
    claimed_actor = claimed_actor or legacy_actor
    try:
        claimed_base = agent_base_for_project(
            claimed_actor,
            project,
            field_name="bearer agent",
        )
    except HTTPException as exc:
        raise HTTPException(403, "Bearer actor is not valid for this project") from exc
    header_base = agent_base_for_project(
        actor,
        project,
        field_name="X-Agent-Name",
    )
    if claimed_base != header_base:
        raise HTTPException(403, "Bearer actor does not match X-Agent-Name")


def require_handoff_claimant(
    handoff: dict[str, Any],
    *,
    actor: str,
    project: str,
) -> str:
    """Require one exact claimed handoff to be owned by the calling actor.

    Terminal non-success transitions are worker actions, not project-wide admin
    actions.  Keeping this check beside the row lock prevents an omitted or
    caller-selected audit header from terminating another worker's claim.
    """
    if handoff.get("status") != "claimed":
        raise HTTPException(409, "Handoff is not in 'claimed' state")
    claimed_by = str(handoff.get("claimed_by") or "").strip()
    if not claimed_by:
        raise HTTPException(409, "Claimed handoff has no claimant identity")
    try:
        claimed_base = agent_base_for_project(
            claimed_by,
            project,
            field_name="handoff claimed_by",
        )
        actor_base = agent_base_for_project(
            actor,
            project,
            field_name="X-Agent-Name",
        )
    except HTTPException as exc:
        raise HTTPException(409, "Handoff claimant identity is invalid") from exc
    if claimed_base != actor_base:
        raise HTTPException(403, "Only the current claimant may terminate this handoff")
    return actor_base


async def handoff_termination_authority(
    request: Request,
    *,
    actor: str,
    project: str,
) -> tuple[str, str | None]:
    """Resolve explicit admin or exact-claimant authority for termination.

    A valid backend-held admin token is the project-wide recovery authority.
    Without it, the caller must provide and (when JWT enforcement is active)
    authenticate the claimant; the locked row performs the final ownership
    comparison before mutation.
    """
    if has_admin_access(request):
        if actor:
            raw_agent = validate_agent_name(actor)
            return await compound_agent(raw_agent, project), None
        return f"cortex-admin@{project}", None

    if not actor:
        raise HTTPException(
            403,
            "Handoff termination requires the claimant or a valid admin token",
        )
    raw_agent = validate_agent_name(actor)
    require_jwt_actor_match(request, raw_agent, project)
    await require_registered_agent_writer(project, raw_agent)
    return await compound_agent(raw_agent, project), raw_agent


def require_supported_completion_approval(parent: dict[str, Any]) -> None:
    """Fail closed on approval policies this single-review receipt cannot satisfy."""
    acceptance = handoff_policy(parent.get("acceptance"))
    policy = acceptance.get("approval_policy")
    if policy is None:
        return
    if isinstance(policy, str):
        policy = {"mode": policy}
    if not isinstance(policy, dict):
        raise HTTPException(409, "Parent approval_policy must be an object")
    mode = compact_text(policy.get("mode") or "assigned_reviewer").lower()
    try:
        minimum = int(policy.get("min_approvers", 1))
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            409,
            "Parent approval_policy.min_approvers is invalid",
        ) from exc
    if mode not in {"assigned_reviewer", "delegator", "single"} or minimum != 1:
        raise HTTPException(
            409,
            "Parent requires a governed CTO or multi-approver workflow; "
            "a single completion handback cannot close it",
        )


async def find_equal_open_handoff(
    conn: asyncpg.Connection,
    *,
    project: str,
    expected: Any,
    expected_from_agent: str,
    expected_to_agent: str | None,
) -> dict[str, Any] | None:
    """Return an equal pending/claimed handoff, if one is already open."""
    row = await conn.fetchrow(
        """SELECT id::text, status,
                  from_agent, from_role, to_role, to_agent, priority, summary,
                  branch, files_changed, verification, next_steps, context,
                  parent_goal_id::text, acceptance, evidence, retry, escalation,
                  created_at::text
             FROM handoffs
            WHERE project = $1
              AND invalidated_at IS NULL
              AND status = ANY($2::text[])
              AND from_agent = $3
              AND from_role IS NOT DISTINCT FROM $4
              AND to_role = $5
              AND to_agent IS NOT DISTINCT FROM $6
              AND priority = $7
              AND summary = $8
              AND branch IS NOT DISTINCT FROM $9
              AND files_changed IS NOT DISTINCT FROM $10::text[]
              AND verification IS NOT DISTINCT FROM $11
              AND next_steps IS NOT DISTINCT FROM $12
              AND context IS NOT DISTINCT FROM $13
              AND parent_goal_id IS NOT DISTINCT FROM $14::text
              AND acceptance = $15::jsonb
              AND evidence = $16::jsonb
              AND retry = $17::jsonb
              AND escalation = $18::jsonb
            ORDER BY created_at ASC, id::text ASC
            LIMIT 1""",
        project,
        list(OPEN_HANDOFF_STATUSES),
        expected_from_agent,
        expected.from_role,
        expected.to_role,
        expected_to_agent,
        expected.priority,
        expected.summary,
        expected.branch,
        expected.files_changed,
        expected.verification,
        expected.next_steps,
        expected.context,
        expected.parent_goal_id,
        handoff_policy_db(expected.acceptance),
        handoff_policy_db(expected.evidence),
        handoff_policy_db(expected.retry),
        handoff_policy_db(expected.escalation),
    )
    return dict(row) if row is not None else None


async def resolve_unique_handoff_for_mutation(
    conn: asyncpg.Connection,
    *,
    project: str,
    handoff_id: str,
) -> dict[str, Any]:
    prefix = (handoff_id or "").strip()
    if not prefix:
        raise HTTPException(400, "handoff id or prefix is required")
    rows = await conn.fetch(
        """SELECT id::text, status, kind, reply_to_handoff_id::text,
                  returned_at::text, completion_report,
                  from_agent, from_role, to_role, to_agent,
                  priority, summary, files_changed, claimed_by, claimed_at::text,
                  COALESCE(retry_count, 0)::int AS retry_count, terminal_reason,
                  acceptance, evidence, retry, escalation
             FROM handoffs
            WHERE project = $1
              AND id::text LIKE $2 || '%'
            ORDER BY id::text
            LIMIT 2""",
        project,
        prefix,
    )
    if not rows:
        raise HTTPException(404, f"Handoff {handoff_id} not found")
    if len(rows) > 1:
        raise HTTPException(
            409,
            f"Handoff prefix {handoff_id} matched multiple rows; use the full UUID",
        )
    return dict(rows[0])


async def locked_handoff_row(
    conn: asyncpg.Connection,
    *,
    project: str,
    handoff_id: str,
) -> dict[str, Any]:
    row = await conn.fetchrow(
        """SELECT id::text, project, status, kind,
                  reply_to_handoff_id::text, returned_at::text,
                  completion_report, from_agent, from_role, to_role, to_agent,
                  priority, summary, branch, files_changed, verification, next_steps,
                  context, parent_goal_id, acceptance, evidence, retry, escalation,
                  claimed_by, claimed_at::text,
                  COALESCE(retry_count, 0)::int AS retry_count,
                  completed_at::text, terminal_reason
             FROM handoffs
            WHERE id = $1::uuid AND project = $2
            FOR UPDATE""",
        handoff_id,
        project,
    )
    if row is None:
        raise HTTPException(404, f"Handoff {handoff_id} not found")
    return dict(row)


def normalized_handoff_completion_report(body: "HandoffReturn") -> dict[str, Any]:
    summary = compact_text(body.summary, limit=4000)
    if not summary:
        raise HTTPException(400, "completion report summary is required")
    report = {
        "schema": HANDOFF_COMPLETION_REPORT_SCHEMA,
        "outcome": body.outcome,
        "summary": summary,
        "decision": body.decision,
        "work_product_id": compact_text(body.work_product_id) or None,
        "tests_run": json_list(body.tests_run),
        "artifacts": normalize_text_list(body.artifacts),
        "risks": normalize_text_list(body.risks),
        "followups": normalize_text_list(body.followups),
        "metadata": json_object(body.metadata),
    }
    return report


def _meaningful(value: Any) -> bool:
    """A checkable evidence leaf: a non-blank string or a number. Containers only
    contribute through their leaves, so `[{}]` and `{"cmd": {}}` are NOT evidence
    (the D3 review probe showed `str({})` stringifies to a non-empty string and
    slipped past a shallow validator)."""
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, bool):
        return True
    if isinstance(value, (int, float)):
        return True
    if isinstance(value, dict):
        return any(_meaningful(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(_meaningful(v) for v in value)
    return False


def _evidence_present(report: dict[str, Any]) -> bool:
    """A checkable fact: a meaningful tests_run entry, a non-empty artifact path, or a
    work product id. `[{}]`, `[""]` and `[{"cmd": {}}]` are not evidence (Ren review,
    D3 rework; validated recursively)."""
    for entry in report.get("tests_run") or []:
        if _meaningful(entry):
            return True
    if any(str(a).strip() for a in (report.get("artifacts") or [])):
        return True
    return bool(str(report.get("work_product_id") or "").strip())


def apply_completion_evidence_floor(report: dict[str, Any], actor_kind: str | None = None) -> dict[str, Any]:
    """Evidence floor for AGENT returns (tasklist D3, 2026-09-02; reworked 2026-09-03,
    strictness rework 2026-09-04).

    Applicability is decided ONLY by WHO returned (the actor's kind in cortex_actors):
    an actor resolved as agent — including an unknown identity, which is treated as an
    agent, never a human (fail closed) — claiming ``completed`` with no checkable
    evidence class is floored to ``partial``; humans and system actors are NEVER
    floored, even when they carry a legacy ``evidence_quality`` stamp. Metadata the
    caller chose to send no longer decides applicability.
    """
    metadata = report.get("metadata") or {}
    harness_driven = actor_kind is None or actor_kind not in {"human", "system"}
    if not harness_driven or report.get("outcome") != "completed":
        return report
    if _evidence_present(report):
        return report
    report["outcome"] = "partial"
    report["metadata"] = {
        **metadata,
        "evidence_floor": "completed_without_evidence_class",
    }
    return report


def completed_handoff_return_receipt(
    handoff: dict[str, Any],
) -> dict[str, Any]:
    """Replay the original terminal meaning of an already-completed return.

    A worker can crash after Cortex commits the return but before its local outbox
    commits.  The retry must therefore preserve accept versus rework semantics,
    not merely report that the handoff is terminal.
    """
    is_handback = handoff.get("kind") == "completion_handback"
    parent_id = handoff.get("reply_to_handoff_id") if is_handback else handoff["id"]
    if not is_handback:
        return {
            "returned": True,
            "accepted": True,
            "status": "completed",
            "deduped": True,
            "handback_id": None,
            "parent_handoff_id": parent_id,
        }

    decision = str(
        json_object(handoff.get("completion_report")).get("decision") or ""
    ).strip().lower()
    if decision == "rework":
        return {
            "returned": True,
            "rework_requested": True,
            "status": "pending",
            "deduped": True,
            "handback_id": handoff["id"],
            "parent_handoff_id": parent_id,
        }
    return {
        "returned": True,
        "accepted": decision == "accept",
        "status": "completed",
        "deduped": True,
        "handback_id": handoff["id"],
        "parent_handoff_id": parent_id,
    }


async def handback_recipient(
    conn: asyncpg.Connection,
    *,
    project: str,
    handoff: dict[str, Any],
    excluding: str = "",
) -> tuple[str, str, bool]:
    """Return a project-local review recipient.

    Cross-project receipts cannot silently bypass the one-shot CTO relay gate.
    They fall back to this project's default lead and retain that routing fact in
    the completion report metadata. ``excluding`` removes one base identity (the
    returning actor) from the candidate path so a completion handback is never
    addressed back to its own worker via the registered-agent route.
    """
    candidate = str(handoff.get("from_agent") or "").strip().lower()
    candidate_role = compact_text(handoff.get("from_role")) or "lead"
    excluding_base = agent_base_name(excluding) if excluding else ""
    try:
        candidate_base = agent_base_for_project(
            candidate,
            project,
            field_name="handoff from_agent",
        )
        if candidate_base in _BLOCKED_AGENT_NAMES:
            raise HTTPException(400, "system sender requires lead fallback")
        if excluding_base and candidate_base == excluding_base:
            raise HTTPException(400, "self-review excluded from candidate route")
        candidate_row = await conn.fetchrow(
            f"""SELECT role
                  FROM agents a
                 WHERE project = $1
                   AND lower(name) = $2
                   AND {visible_agent_sql('a')}
                 LIMIT 1""",
            project,
            candidate_base,
        )
        if candidate_row is not None:
            return (
                await compound_agent(candidate_base, project),
                compact_text(candidate_row.get("role")) or candidate_role,
                False,
            )
    except HTTPException:
        pass

    default_row = await conn.fetchrow(
        "SELECT default_agent FROM cortex_projects WHERE project_key = $1",
        project,
    )
    default_candidate = str((default_row or {}).get("default_agent") or "").strip()
    default_base = ""
    if default_candidate:
        try:
            default_base = agent_base_for_project(
                default_candidate,
                project,
                field_name="project default_agent",
            )
        except HTTPException:
            default_base = ""
    fallback = await conn.fetchrow(
        f"""SELECT lower(name) AS name, role
              FROM agents a
             WHERE project = $1
               AND {visible_agent_sql('a')}
             ORDER BY
               CASE WHEN lower(name) = $2 THEN 0 ELSE 1 END,
               CASE WHEN lower(role) IN ('lead', 'cpo', 'cmo', 'cto', 'pm')
                    THEN 0 ELSE 1 END,
               created_at,
               lower(name)
             LIMIT 1""",
        project,
        default_base,
    )
    if fallback is None:
        raise HTTPException(
            409,
            "No active project reviewer is available for the completion handback",
        )
    fallback_name = str(fallback["name"])
    fallback_role = compact_text(fallback.get("role")) or "lead"
    return await compound_agent(fallback_name, project), fallback_role, True


async def emit_handoff_lifecycle_event(
    conn: asyncpg.Connection,
    *,
    project: str,
    actor: str,
    action: str,
    handoff: dict[str, Any],
    reason: str | None = None,
    files: list[str] | None = None,
) -> int:
    handoff_id = str(handoff.get("id") or "")
    summary = _budget_one_line(handoff.get("summary"), "(no summary)")
    if len(summary) > 140:
        summary = f"{summary[:137]}..."
    detail = {
        "schema": "cortex.handoff_lifecycle.v1",
        "handoff_id": handoff_id,
        "action": action,
        "status": handoff.get("status"),
        "kind": handoff.get("kind") or "task",
        "reply_to_handoff_id": handoff.get("reply_to_handoff_id"),
        "from_agent": handoff.get("from_agent"),
        "from_role": handoff.get("from_role"),
        "to_role": handoff.get("to_role"),
        "to_agent": handoff.get("to_agent"),
        "priority": handoff.get("priority"),
        "claimed_by": handoff.get("claimed_by"),
        "claimed_at": handoff.get("claimed_at"),
        "retry_count": int(handoff.get("retry_count") or 0),
        "terminal_reason": reason if reason is not None else handoff.get("terminal_reason"),
    }
    for policy_key in ("acceptance", "evidence", "retry", "escalation"):
        if policy_key in handoff:
            detail[policy_key] = handoff_policy(handoff.get(policy_key))
    if handoff.get("completion_report"):
        detail["completion_report"] = normalize_db_json(
            handoff.get("completion_report")
        ) or {}
    return await emit_team_event(
        conn,
        project=project,
        agent_name=actor or "system",
        event_type=f"handoff_{action}",
        summary=f"[HANDOFF-{action.upper()}:{handoff_id[:8]}] {summary}",
        detail=detail,
        files=files,
        verify=True,
    )


def parse_team_event_cursor(last_id: str) -> int | None:
    cursor = (last_id or "").strip()
    if not cursor or "-" in cursor:
        return None
    try:
        value = int(cursor)
    except ValueError:
        return None
    return value if value >= 0 else None


def json_field(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True)


def team_event_stream_entry(row: Any) -> dict[str, Any]:
    ts = row["ts"]
    ts_value = ts.isoformat() if hasattr(ts, "isoformat") else str(ts or "")
    fields: dict[str, str] = {
        "type": row["event_type"] or "event",
        "agent": row["agent_name"] or "",
        "summary": row["summary"] or "",
        "project": row["project"] or "",
        "ts": ts_value,
    }
    detail = json_field(row["detail"])
    if detail:
        fields["detail"] = detail
    files = row["files"]
    if files:
        fields["files"] = json.dumps(list(files))
    if row["sprint_id"]:
        fields["sprint_id"] = str(row["sprint_id"])
    if row["related_decision_id"]:
        fields["related_decision_id"] = str(row["related_decision_id"])
    return {"id": str(row["id"]), "fields": fields}


async def max_team_event_id(conn: asyncpg.Connection, project: str) -> int:
    value = await conn.fetchval(
        "SELECT COALESCE(MAX(id), 0)::bigint FROM team_events WHERE project = $1",
        project,
    )
    return int(value or 0)


async def fetch_team_events_after(
    conn: asyncpg.Connection,
    project: str,
    cursor: int,
    count: int,
) -> list[Any]:
    return await conn.fetch(
        """SELECT id, project, agent_name, event_type, summary, detail, files,
                  sprint_id, related_decision_id, ts
             FROM team_events
            WHERE project = $1
              AND id > $2
            ORDER BY id ASC
            LIMIT $3""",
        project,
        cursor,
        count,
    )


async def fetch_recent_team_events(
    conn: asyncpg.Connection,
    project: str,
    count: int,
) -> list[Any]:
    return await conn.fetch(
        """SELECT *
             FROM (
                 SELECT id, project, agent_name, event_type, summary, detail, files,
                        sprint_id, related_decision_id, ts
                   FROM team_events
                  WHERE project = $1
                  ORDER BY id DESC
                  LIMIT $2
             ) recent
            ORDER BY id ASC""",
        project,
        count,
    )


app = FastAPI(title="Cortex API", version=CORTEX_API_VERSION, lifespan=lifespan)


# ---------------------------------------------------------------------------
# Local JWT boundary
# ---------------------------------------------------------------------------

def jwt_b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii"))


def decode_local_jwt(token: str) -> dict[str, Any]:
    try:
        header_raw, payload_raw, signature_raw = token.split(".", 2)
        header = json.loads(jwt_b64url_decode(header_raw))
        payload = json.loads(jwt_b64url_decode(payload_raw))
        signature = jwt_b64url_decode(signature_raw)
    except Exception as exc:
        raise HTTPException(401, "Invalid bearer token") from exc

    if header.get("alg") != "HS256":
        raise HTTPException(401, "Unsupported bearer token algorithm")

    # SEC-03 (audit 2026-09-01): an EMPTY secret means every forged token verifies
    # (HMAC over the empty key is computable by anyone). A presented bearer token
    # can therefore never be accepted while the secret is unset - fail closed.
    if not CORTEX_JWT_SECRET:
        raise HTTPException(401, "Bearer auth is not configured on this deployment")

    signing_input = f"{header_raw}.{payload_raw}".encode("ascii")
    expected = hmac.new(
        CORTEX_JWT_SECRET.encode("utf-8"),
        signing_input,
        hashlib.sha256,
    ).digest()
    if not hmac.compare_digest(signature, expected):
        raise HTTPException(401, "Invalid bearer token signature")

    exp = payload.get("exp")
    if exp is not None and int(exp) < int(time.time()):
        raise HTTPException(401, "Expired bearer token")

    return payload


@app.middleware("http")
async def local_jwt_middleware(request: Request, call_next):
    auth = request.headers.get("authorization", "").strip()
    claims: dict[str, Any] = {}

    if CORTEX_AUTH_REQUIRE_JWT and not CORTEX_JWT_SECRET and request.url.path not in {"/health", "/metrics"}:
        # Enforcement demanded with no secret to enforce against: refusing every
        # request loudly is the only honest posture - fail-open here was universal
        # forgery (SEC-03).
        return JSONResponse(
            status_code=503,
            content={"detail": "CORTEX_AUTH_REQUIRE_JWT is set but CORTEX_JWT_SECRET is empty; refusing all requests until auth is configured"},
        )
    if auth.lower().startswith("bearer "):
        try:
            claims = decode_local_jwt(auth.split(None, 1)[1])
        except HTTPException as exc:
            return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
        request.state.jwt_claims = claims
    elif CORTEX_AUTH_REQUIRE_JWT and request.url.path not in {"/health", "/metrics"}:
        return JSONResponse(status_code=401, content={"detail": "Bearer token required"})

    jwt_project = str(claims.get("project", "")).lower().strip()
    header_project = request.headers.get("x-project", "").lower().strip()
    if jwt_project and header_project and jwt_project != header_project:
        return JSONResponse(status_code=403, content={"detail": "Bearer project does not match X-Project"})

    return await call_next(request)


# ---------------------------------------------------------------------------
# Prometheus middleware + /metrics endpoint
# ---------------------------------------------------------------------------


@app.middleware("http")
async def prometheus_middleware(request: Request, call_next):
    """Track request duration for all endpoints (except /metrics itself)."""
    if request.url.path == "/metrics":
        return await call_next(request)
    method = request.method
    # Normalise path: collapse UUIDs and agent names to reduce cardinality
    path = request.url.path
    start = time.monotonic()
    response = await call_next(request)
    duration = time.monotonic() - start
    REQUEST_DURATION.labels(method=method, endpoint=path).observe(duration)
    return response


@app.get("/metrics")
async def metrics():
    """Prometheus scrape endpoint."""
    return Response(
        content=render_metrics(),
        media_type=CONTENT_TYPE_LATEST,
    )


# ---------------------------------------------------------------------------
# Auth — project-scoped actor identity from headers
# ---------------------------------------------------------------------------


async def compound_agent(agent: str, project: str) -> str:
    """Build the v2 display agent ID: agent@project.

    Clean cutover rejects retired colon-suffixed identity at validation. Writes normalize to
    the project-key display identity so hex is no longer embedded in history.
    """
    return f"{agent_base_for_project(agent, project)}@{project}"


# Agent names that are never valid identities — common false positives from
# session transcript inference.
_BLOCKED_AGENT_NAMES = frozenset({
    "actually", "adding", "an", "and", "are", "auditing", "building", "but",
    "changing", "create", "deploying", "doing", "editing", "fixing", "for",
    "here", "immediately", "implementing", "in", "instead", "invested",
    "is", "it", "looking", "missing", "not", "now", "of", "on", "or",
    "project", "rebuilding", "researching", "reviewing", "running", "sending",
    "still", "system", "team", "the", "there", "this", "to", "tracing",
    "trying", "using", "was", "with", "working", "writing", "you",
})

# Regex: valid runtime agent identity is a lowercase identifier, optionally with
# a project-key display suffix. Runtime validators stay display-identity aware;
# registry writes use validate_registry_agent_name() below so agent@project and
# transient harness ids are never persisted as roster names.
_VALID_AGENT_RE = re.compile(
    r"^(?:claude-subagent-[a-f0-9]{6,20}|[a-z][a-z0-9_-]{1,31})"
    r"(?:@[a-z0-9][a-z0-9-]{1,63})?$"
)
_REGISTRY_AGENT_RE = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")
_EPHEMERAL_AGENT_RE = re.compile(r"^(?:claude-subagent-[a-f0-9]{6,20}|codex-agent)$")
_VALID_PROJECT_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")  # leading digit allowed; keys are identifiers, not Python names
_VALID_ROLE_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")

# E006 Inc04 — Roster-as-Data.
# Membership/applicability moved to the Cortex registry (agents.capabilities.writer_scope
# + cortex_projects.metadata.roster_policy). These are scope vocabulary values,
# not membership.
CORTEX_ROSTER_SCHEMA_VERSION = "1"  # vocabulary version, not membership
WRITER_SCOPES = frozenset(
    {"work", "system-event", "read-only", "handoff-target-only"}
)


def validate_agent_name(agent: str) -> str:
    """Validate and normalise an agent name from a caller-supplied header.

    Rejects obvious non-identifiers (common English words that slip through
    session transcript inference) and names that don't match the identifier
    pattern.  Returns the normalised name on success.
    """
    normalised = agent.lower().strip()
    if ":" in normalised or "????" in normalised:
        raise HTTPException(
            400,
            f"Colon-suffixed Cortex identity '{normalised}' is retired after "
            "Identity v2 clean cutover; use agent@project or the bare agent "
            "inside the active X-Project scope.",
        )
    if normalised in _BLOCKED_AGENT_NAMES:
        raise HTTPException(
            400,
            f"Blocked agent name '{normalised}' — not a valid agent identity",
        )
    if not _VALID_AGENT_RE.match(normalised):
        raise HTTPException(
            400,
            f"Invalid agent name '{normalised}' — must be a lowercase identifier "
            "(letters, digits, hyphens; 2-32 chars), optionally as agent@project. "
            "Colon-suffixed identity is retired after Identity v2 clean cutover.",
        )
    return normalised


def agent_base_name(agent: str) -> str:
    return agent.lower().strip().split("@", 1)[0]


def agent_base_for_project(agent: str, project: str, *, field_name: str = "agent") -> str:
    """Validate an agent identity and return its base name for this project.

    Bare agent names are scoped by X-Project. Display identities are accepted
    only when their suffix matches X-Project; otherwise the caller is mixing
    project scopes and the write must fail before any row is mutated.
    """
    normalised = validate_agent_name(agent)
    scoped_project = validate_project_key(project)
    base, sep, suffix = normalised.partition("@")
    if sep and suffix != scoped_project:
        raise HTTPException(
            400,
            f"{field_name}={normalised!r} belongs to project '{suffix}', "
            f"but X-Project is '{scoped_project}'. Use the active project's "
            "agent id or switch X-Project explicitly.",
        )
    return base


def validate_registry_agent_name(agent: str, project: str, *, field_name: str = "agent") -> str:
    """Validate a persisted roster name and return the bare project-local name.

    Runtime display identities such as ``ren@kaidera-os`` are accepted only when the
    suffix matches the active project, then stripped before storage. Transient
    harness ids and sentence-fragment false positives are rejected at the write
    boundary so they cannot become registry rows.
    """
    base = agent_base_for_project(agent, project, field_name=field_name)
    if base in _BLOCKED_AGENT_NAMES:
        raise HTTPException(
            400,
            f"Blocked agent name '{base}' — not a valid persisted roster identity",
        )
    if _EPHEMERAL_AGENT_RE.match(base):
        raise HTTPException(
            400,
            f"Ephemeral harness id '{base}' cannot be persisted to a project roster",
        )
    if not _REGISTRY_AGENT_RE.fullmatch(base):
        raise HTTPException(
            400,
            f"Invalid agent name '{base}' — persisted roster names must match "
            "[a-z][a-z0-9_-]{1,31}; do not include project suffixes or sentence fragments.",
        )
    return base


def normalize_agent_removal_name(agent: str) -> str:
    """Normalize an admin removal target.

    Removal must be able to deactivate historical bad rows that current creation
    guards now reject (for example sentence fragments or name@project literals).
    The SQL path is parameterized, so the safe boundary here is non-blank,
    bounded text without NUL bytes; project scope still gates the row.
    """
    normalised = str(agent or "").lower().strip()
    if not normalised or "\x00" in normalised or len(normalised) > 128:
        raise HTTPException(400, "Invalid agent removal target")
    return normalised


def agent_display_name(agent: str, project: str) -> str:
    return f"{agent_base_name(agent)}@{project.lower().strip()}"


def identity_base_sql(value_sql: str) -> str:
    """SQL expression for the base agent name from agent@project."""
    return f"lower(split_part(COALESCE({value_sql}, ''), '@', 1))"


def suggest_agent_name(candidate: str, names, cutoff: float = 0.6) -> str | None:
    """Return the closest registered agent name to `candidate`, or None.

    Used for "did you mean?" guidance so a misspelled agent name is corrected
    at the point of use instead of being silently
    adopted as a new phantom identity that gets used until someone notices.
    Compares base names (without any agent@project display suffix) and only suggests a genuinely close
    match, so a deliberately new agent name returns None rather than a wrong
    correction.

    ``cutoff`` defaults to 0.6 so existing callers keep current behaviour; the
    registry resolver passes the project's configured ``suggest_cutoff``.
    """
    base = agent_base_name(candidate)
    pool = sorted({agent_base_name(n) for n in names if n} - {base})
    if not pool:
        return None
    # cutoff 0.6: a single-character transposition on a short name
    # scores ~0.67, so 0.7 would miss the most common typo class; 0.6 still
    # rejects genuinely different names.
    match = difflib.get_close_matches(base, pool, n=1, cutoff=cutoff)
    return match[0] if match else None


# ---------------------------------------------------------------------------
# E006 Inc04 — Roster-as-Data: registry-driven writer boundary
#
# The writer boundary stays CODE (a guard on every mutation path) but its
# *membership* and its very *applicability* become DATA read from the Cortex
# registry over the same Postgres pools the rest of the API uses:
#   - whether a project enforces a roster  -> cortex_projects.metadata.enforce_writer_roster
#   - who may write / receive work          -> agents.capabilities.writer_scope
#   - system-event writers / roles / persona-> cortex_projects.metadata.roster_policy
#
# Default-closed + fail-closed: an enforcing project with an empty roster rejects
# everyone; a registry read error on an enforcing project RAISES, never bypasses.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RosterPolicy:
    project: str
    enforce: bool
    default_writer_scope: str
    work_writers: frozenset[str]
    system_event_writers: frozenset[str]
    read_only: frozenset[str]
    handoff_targets: frozenset[str]
    beat_may_create_handoff: bool
    roles: dict
    suggest_cutoff: float


# Per-project resolved policy cache: project -> (monotonic_ts, RosterPolicy).
_roster_policy_cache: dict[str, tuple[float, RosterPolicy]] = {}
ROSTER_POLICY_TTL = 30.0  # seconds; bounds staleness regardless of invalidation


def _invalidate_roster_policy(project: str | None = None) -> None:
    """Drop a project (or the whole cache) so the next read re-resolves.

    Called synchronously by the mutating endpoints (register_agent /
    register_project / PATCH roster-policy) and by the team-event waiter on an
    agent_registered/project_registered notify so a newly-added writer is
    visible without a restart.
    """
    if project is None:
        _roster_policy_cache.clear()
    else:
        _roster_policy_cache.pop(project, None)


def _resolve_enforce(project: str, meta: dict, rp: dict) -> bool:
    """Resolve the enforcement decision from registry data.

    Precedence (explicit branches — NOT an operator-precedence one-liner):
      1. explicit top-level metadata.enforce_writer_roster (data wins, any project)
      2. explicit roster_policy.enforce_writer_roster (read-convenience mirror)
      3. UNKNOWN -> False (legacy opt-out until the project enables the guard)
    """
    if "enforce_writer_roster" in meta:
        return bool(meta.get("enforce_writer_roster"))
    if "enforce_writer_roster" in rp:
        return bool(rp.get("enforce_writer_roster"))
    return False


async def load_roster_policy(project: str) -> RosterPolicy:
    """Resolve (and TTL-cache) a project's writer-roster policy from the registry.

    Opens its own short-lived reads (admin pool for cortex_projects.metadata,
    RLS-scoped pool for agents/roles) so the 16 guard call-sites keep their
    argument shape — no connection threading. Hot path is a dict lookup.
    """
    now = time.monotonic()
    hit = _roster_policy_cache.get(project)
    if hit and now - hit[0] < ROSTER_POLICY_TTL:
        return hit[1]
    try:
        async with pool_admin.acquire() as admin:
            prow = await admin.fetchrow(
                "SELECT metadata FROM cortex_projects WHERE project_key=$1 "
                "AND COALESCE(status,'active')<>'deleted' LIMIT 1",
                project,
            )
        if prow is None:
            raise HTTPException(
                404,
                f"Project '{project}' is not registered in Cortex; refusing to evaluate writer gate",
            )
        meta = json_object(prow["metadata"]) if prow else {}
        rp = json_object(meta.get("roster_policy"))
        enforce = _resolve_enforce(project, meta, rp)

        # Only the read of agents/roles needs scoping; do it once.
        async with acquire_scoped(project) as conn:
            rows = await conn.fetch(
                "SELECT lower(name) AS n, capabilities->>'writer_scope' AS scope, "
                f"role FROM agents a WHERE project=$1 AND {visible_agent_sql('a')}",
                project,
            )
            role_rows = await conn.fetch(
                "SELECT name, default_capabilities->>'writer_scope' AS scope "
                "FROM roles WHERE project=$1",
                project,
            )
        role_defaults = {r["name"]: r["scope"] for r in role_rows}
        default_scope = rp.get("default_writer_scope") or "work"

        def effective_scope(row) -> str:
            # precedence: agent explicit > role default > project default
            return row["scope"] or role_defaults.get(row["role"]) or default_scope

        work = frozenset(r["n"] for r in rows if effective_scope(r) == "work")
        read_only = frozenset(
            r["n"] for r in rows if effective_scope(r) == "read-only"
        )
        sysw = frozenset(rp.get("system_event_writers", []))

        policy = RosterPolicy(
            project=project,
            enforce=enforce,
            default_writer_scope=default_scope,
            work_writers=work,
            system_event_writers=sysw,
            read_only=read_only,
            handoff_targets=work,
            beat_may_create_handoff=bool(rp.get("beat_may_create_handoff", False)),
            roles=json_object(rp.get("roles")),
            suggest_cutoff=float(rp.get("suggest_cutoff", 0.6)),
        )
    except HTTPException:
        raise
    except Exception:
        # FAIL CLOSED on read error: serve the last good policy if we have one,
        # otherwise refuse to evaluate the gate. Never bypass.
        if hit is not None:
            return hit[1]
        raise HTTPException(
            503,
            "roster policy unavailable; refusing to evaluate writer gate",
        )
    _roster_policy_cache[project] = (now, policy)
    return policy


async def require_registered_agent_writer(
    project: str,
    agent: str,
    *,
    scope: str = "work",
) -> None:
    """Enforce a project's registered-writer boundary on new writes.

    Membership + applicability come from the Cortex registry (load_roster_policy).
    ``scope`` selects the per-route carve-outs that the old kwargs encoded:
      - "work"         : base writer gate only (default)
      - "system-event" : also admit the project's system_event_writers
      - "work-handoff" : also admit project-declared system event writers when allowed
    The 403 text + did-you-mean are preserved.
    """
    agent = agent_base_for_project(agent, project)
    policy = await load_roster_policy(project)
    if not policy.enforce:
        return

    base = agent_base_name(agent)
    allowed = set(policy.work_writers)
    if scope == "system-event":
        allowed |= policy.system_event_writers
    if scope == "work-handoff" and policy.beat_may_create_handoff:
        allowed |= policy.system_event_writers

    if base not in allowed:
        hint = suggest_agent_name(base, allowed, cutoff=policy.suggest_cutoff)
        did_you_mean = f" Did you mean '{hint}'?" if hint else ""
        raise HTTPException(
            403,
            f"Agent '{base}' is not registered to write in {project}.{did_you_mean} "
            f"{project} writers are {_join_writers(policy.work_writers)}, with "
            "narrow beat/system evidence publishing on approved system paths.",
        )


async def require_registered_handoff_target(
    project: str,
    to_agent: str | None,
) -> str | None:
    """Validate a handoff target against the project's registered writers."""
    if not to_agent:
        return None
    target = agent_base_for_project(to_agent, project, field_name="to_agent")
    policy = await load_roster_policy(project)
    if not policy.enforce:
        return target
    base = agent_base_name(target)
    if base not in policy.handoff_targets:
        hint = suggest_agent_name(base, policy.handoff_targets, cutoff=policy.suggest_cutoff)
        did_you_mean = f" Did you mean '{hint}'?" if hint else ""
        raise HTTPException(
            403,
            f"Handoff target '{base}' is not a registered "
            f"{project} agent.{did_you_mean} route {project} work only to "
            f"{_join_writers(policy.handoff_targets, conj='or')}.",
        )
    return target


def _join_writers(writers, conj: str = "and") -> str:
    """Render a writer set for agent-facing error messages."""
    names = sorted(writers)
    if not names:
        return "(none)"
    if len(names) == 1:
        return names[0]
    return f"{', '.join(names[:-1])} {conj} {names[-1]}"


def validate_profile_agent_name(agent: str) -> str:
    """Validate a profile path agent and return the bare agent name."""
    normalised = agent.lower().strip()
    if not _VALID_AGENT_RE.match(normalised):
        raise HTTPException(400, f"Invalid agent name '{normalised}'")
    return agent_base_name(normalised)


def validate_project_key(project: str) -> str:
    normalized = (project or "").lower().strip()
    if not _VALID_PROJECT_KEY_RE.match(normalized):
        raise HTTPException(
            400,
            "Invalid project key — use lowercase letters, digits, and hyphens "
            "(2-64 chars, starting with a letter)",
        )
    return normalized


def validate_role_slug(role: str) -> str:
    normalized = (role or "").lower().strip()
    if not _VALID_ROLE_SLUG_RE.match(normalized):
        raise HTTPException(
            400,
            "Invalid role slug — use lowercase letters, digits, and hyphens",
        )
    return normalized


async def _require_registered_project_on_conn(
    conn: Any,
    project: str,
) -> dict[str, Any]:
    """Read one project through a caller-owned registry connection."""
    row = await conn.fetchrow(
        """SELECT project_key, id::text AS project_id, display_name, default_agent,
                  repo_root, repo_type, status,
                  graph_generation::text AS graph_generation,
                  graph_requires_full_rebuild
             FROM cortex_projects
            WHERE project_key = $1
              AND COALESCE(status, 'active') <> 'deleted'
            LIMIT 1""",
        project,
    )
    if not row:
        raise HTTPException(
            404,
            f"Project '{project}' is not registered in Cortex. "
            "Register it with POST /projects or cortex-init-project before "
            "bootstrap/search/handoff/session ingest.",
        )
    return dict(row)


async def require_registered_project(project: str) -> dict[str, Any]:
    """Require a project to exist in the Cortex project registry.

    The workspace/project registry is the tenant boundary for bootstrap,
    search, handoffs, and session ingest. Unknown X-Project values must fail
    before mutation instead of creating stray scoped rows.
    """
    assert pool_admin is not None, "Admin DB pool not initialised"
    async with pool_admin.acquire() as conn:
        return await _require_registered_project_on_conn(conn, project)


def json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


WORK_PRODUCT_SCHEMA_VERSION = "cortex.work_product.v1"
WORK_PRODUCT_STATUSES = frozenset({"current", "stale", "superseded"})
DEFAULT_WORK_PRODUCT_ACTIVITY = "task-completed"


def compact_text(value: Any, *, limit: int | None = None) -> str:
    text = " ".join(str(value or "").split())
    if limit is not None and len(text) > limit:
        return text[: max(0, limit - 3)] + "..."
    return text


def strip_compound_suffix(value: Any) -> str:
    return agent_base_name(compact_text(value))


def normalize_text_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        raw_items = re.split(r"[\n,]+", value)
    elif isinstance(value, list):
        raw_items = value
    else:
        raw_items = [value]
    out: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        text = compact_text(item)
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def normalize_work_product_status(value: Any) -> str:
    status = compact_text(value or "current").lower()
    if status not in WORK_PRODUCT_STATUSES:
        raise HTTPException(
            400,
            "work product status must be one of: "
            + ", ".join(sorted(WORK_PRODUCT_STATUSES)),
        )
    return status


def normalize_work_product_activity(value: Any) -> str:
    raw = compact_text(value or DEFAULT_WORK_PRODUCT_ACTIVITY).lower()
    slug = re.sub(r"[^a-z0-9_.:-]+", "-", raw).strip("-")
    return slug or DEFAULT_WORK_PRODUCT_ACTIVITY


def work_product_memory_text(data: dict[str, Any]) -> str:
    tests = json_list(data.get("tests_run"))
    rendered_tests: list[str] = []
    for item in tests:
        if isinstance(item, dict):
            command = compact_text(item.get("command"))
            result = compact_text(item.get("result") or item.get("status"))
            notes = compact_text(item.get("notes"))
            rendered_tests.append(" ".join(p for p in [command, result, notes] if p))
        else:
            rendered_tests.append(compact_text(item))

    sections = [
        f"Schema: {WORK_PRODUCT_SCHEMA_VERSION}",
        f"Title: {compact_text(data.get('title'))}",
        f"Activity: {compact_text(data.get('activity_type'))}",
        f"Status: {compact_text(data.get('status'))}",
        f"Summary: {compact_text(data.get('summary'))}",
        f"Behavior: {compact_text(data.get('behavior_summary'))}",
        f"Architecture: {compact_text(data.get('architecture_notes'))}",
        "Files: " + ", ".join(normalize_text_list(data.get("files_changed"))),
        "Symbols: " + ", ".join(normalize_text_list(data.get("symbols_changed"))),
        "Subjects: " + ", ".join(normalize_text_list(data.get("subject_entities"))),
        "Artifacts: " + ", ".join(normalize_text_list(data.get("artifact_refs"))),
        "Tests: " + "; ".join(t for t in rendered_tests if t),
        "Risks: " + "; ".join(normalize_text_list(data.get("risks"))),
        "Followups: " + "; ".join(normalize_text_list(data.get("followups"))),
    ]
    return "\n".join(section for section in sections if section.split(":", 1)[-1].strip())


def work_product_content_hash(data: dict[str, Any]) -> str:
    payload = {
        "schema": WORK_PRODUCT_SCHEMA_VERSION,
        "activity_type": data.get("activity_type"),
        "title": data.get("title"),
        "summary": data.get("summary"),
        "behavior_summary": data.get("behavior_summary"),
        "architecture_notes": data.get("architecture_notes"),
        "files_changed": normalize_text_list(data.get("files_changed")),
        "symbols_changed": normalize_text_list(data.get("symbols_changed")),
        "subject_entities": normalize_text_list(data.get("subject_entities")),
        "artifact_refs": normalize_text_list(data.get("artifact_refs")),
        "tests_run": json_list(data.get("tests_run")),
        "risks": normalize_text_list(data.get("risks")),
        "followups": normalize_text_list(data.get("followups")),
    }
    return write_fingerprint(payload)


def runtime_repo_root() -> Path:
    configured = os.getenv("CORTEX_WORKSPACE_ROOT") or os.getenv("CORTEX_REPO_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    return Path.cwd().resolve()


def current_git_commit_sha(root: Path | None = None) -> str | None:
    base = root or runtime_repo_root()
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(base),
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except Exception:
        return None
    sha = compact_text(proc.stdout)
    if proc.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", sha):
        return sha
    return None


def resolve_workspace_file(path_text: str, root: Path | None = None) -> Path | None:
    text = compact_text(path_text)
    if not text:
        return None
    base = root or runtime_repo_root()
    try:
        candidate = Path(text).expanduser()
        if not candidate.is_absolute():
            candidate = base / candidate
        resolved = candidate.resolve()
        if base in resolved.parents or resolved == base:
            return resolved
        return None
    except (OSError, RuntimeError):
        return None


def sha256_file(path: Path) -> str | None:
    try:
        if not path.is_file():
            return None
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def compute_file_hashes(files: list[str], root: Path | None = None) -> dict[str, str]:
    hashes: dict[str, str] = {}
    base = root or runtime_repo_root()
    for file_ref in normalize_text_list(files):
        path = resolve_workspace_file(file_ref, base)
        if not path:
            continue
        digest = sha256_file(path)
        if digest:
            hashes[file_ref] = digest
    return hashes


def work_product_row_to_dict(row: Any) -> dict[str, Any]:
    data = dict(row)
    for key in (
        "created_at",
        "updated_at",
        "invalidated_at",
        "freshness_checked_at",
        "projected_at",
        "valid_from",
        "valid_to",
    ):
        value = data.get(key)
        if isinstance(value, datetime):
            data[key] = value.isoformat()
    data["files_changed"] = normalize_text_list(data.get("files_changed"))
    data["symbols_changed"] = normalize_text_list(data.get("symbols_changed"))
    data["subject_entities"] = normalize_text_list(data.get("subject_entities"))
    data["artifact_refs"] = normalize_text_list(data.get("artifact_refs"))
    data["risks"] = normalize_text_list(data.get("risks"))
    data["followups"] = normalize_text_list(data.get("followups"))
    data["tests_run"] = json_list(data.get("tests_run"))
    data["file_hashes"] = json_object(data.get("file_hashes"))
    data["symbol_hashes"] = json_object(data.get("symbol_hashes"))
    data["metadata"] = json_object(data.get("metadata"))
    return data


def validate_writer_scope(value: Any, *, default: str = "work") -> str:
    scope = str(value or default).strip().lower()
    if scope not in WRITER_SCOPES:
        raise HTTPException(
            400,
            f"writer_scope must be one of: {', '.join(sorted(WRITER_SCOPES))}",
        )
    return scope


def roster_policy_from_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    return json_object(metadata.get("roster_policy"))


# Holder-bearing role keys NAME agents, so they belong under
# roster_policy.roles and every value must resolve to the project roster. At the
# roster_policy top level no read surface sees them (the marlow defect: a PATCH
# was echoed 200 while /runtime kept reporting pm_lead=None).
_ROLE_HOLDER_KEYS = ("pm_lead", "support_agents", "approved_agents", "role_assignments")


def _deep_merge_dicts(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Deep-merge ``patch`` onto ``base``: nested dicts merge key-wise, anything
    else (lists, scalars, nulls) REPLACES. Neither input is mutated.

    Why: PATCH roster-policy used to REPLACE metadata.roster_policy wholesale,
    so a two-key diagnostic patch wiped approved_agents/role_assignments. Merge
    is the safe default; wholesale replace remains available via ?replace=true.
    """
    merged = dict(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge_dicts(merged[key], value)
        else:
            merged[key] = value
    return merged


def effective_pm_lead(roles: dict[str, Any]) -> str | None:
    """The ONE pm_lead resolver, shared by every read surface (runtime profile,
    writers view, persona/boot-rules context).

    ``roles.pm_lead`` is canonical; ``roles.role_assignments.pm_lead`` is the
    fallback spelling. Unifying here means the surfaces cannot disagree even
    over data written before write-side normalisation existed.
    """
    lead = roles.get("pm_lead")
    if isinstance(lead, str) and lead.strip():
        return lead.strip()
    assigned = json_object(roles.get("role_assignments")).get("pm_lead")
    if isinstance(assigned, str) and assigned.strip():
        return assigned.strip()
    return None


def _normalise_roster_roles(
    policy: dict[str, Any], patch: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Mirror the resolved pm_lead into BOTH stored spellings (roles.pm_lead and
    roles.role_assignments.pm_lead) so the write echo, /runtime, and the
    boot-rules builder report the same lead from the stored blob alone.

    A lead named by the PATCH (either spelling) wins over the merged-in old
    value — otherwise a patch setting only role_assignments.pm_lead would be
    silently overridden by the stale canonical key (the marlow defect again).
    """
    roles = policy.get("roles")
    if not isinstance(roles, dict):
        return policy
    lead: str | None = None
    patch_roles = (patch or {}).get("roles")
    if isinstance(patch_roles, dict):
        lead = effective_pm_lead(patch_roles)
    if not lead:
        lead = effective_pm_lead(roles)
    if not lead:
        return policy
    normalised_roles = dict(roles)
    normalised_roles["pm_lead"] = lead
    assignments = dict(json_object(roles.get("role_assignments")))
    assignments["pm_lead"] = lead
    normalised_roles["role_assignments"] = assignments
    return {**policy, "roles": normalised_roles}


async def _validate_roster_policy_patch(
    conn: asyncpg.Connection, project_key: str, patch: dict[str, Any]
) -> None:
    """Fail-loud write guard for PATCH roster-policy (previously write-only and
    unvalidated: a typo'd or misplaced holder was echoed 200 while every read
    surface kept reporting the old state).

    Both checks run on the PATCH DELTA only, so a stale legacy key elsewhere in
    the stored blob can never block an unrelated patch:

      1. PLACEMENT — holder-bearing keys belong under ``roles`` -> 422.
      2. ROSTER — every holder the patch names must be a registered, visible
         agent on the project -> 422 listing the unknown holders.
    """
    misplaced = [key for key in _ROLE_HOLDER_KEYS if key in patch]
    if misplaced:
        raise HTTPException(
            422,
            f"Roster-policy holder key(s) {misplaced} must live under 'roles' "
            '(e.g. {"roles": {"pm_lead": "<agent>"}}); at the roster_policy top '
            "level no read surface sees them.",
        )
    roles = json_object(patch.get("roles"))
    holders: set[str] = set()
    lead = roles.get("pm_lead")
    if isinstance(lead, str) and lead.strip():
        holders.add(lead)
    for value in json_list(roles.get("support_agents")) + json_list(roles.get("approved_agents")):
        if isinstance(value, str) and value.strip():
            holders.add(value)
    for value in json_object(roles.get("role_assignments")).values():
        if isinstance(value, str) and value.strip():
            holders.add(value)
    if not holders:
        return
    rows = await conn.fetch(
        f"SELECT a.name FROM agents a WHERE a.project = $1 AND {visible_agent_sql('a')}",
        project_key,
    )
    roster = {agent_base_name(row["name"]) for row in rows}
    unknown = sorted({agent_base_name(holder) for holder in holders} - roster)
    if unknown:
        raise HTTPException(
            422,
            f"Roster-policy holder(s) not on the '{project_key}' roster: {unknown}. "
            "Register the agent first (POST /agents) or correct the holder name.",
        )


async def fetch_project_metadata(project: str) -> dict[str, Any]:
    assert pool_admin is not None, "Admin DB pool not initialised"
    async with pool_admin.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT metadata FROM cortex_projects WHERE project_key=$1 "
            "AND COALESCE(status,'active')<>'deleted' LIMIT 1",
            project,
        )
    return json_object(row["metadata"]) if row else {}


def writer_scope_for_agent(
    agent_name: str,
    capabilities: dict[str, Any],
    policy: RosterPolicy | None,
    metadata: dict[str, Any] | None = None,
) -> str:
    base = agent_base_name(agent_name)
    if policy is not None:
        if base in policy.work_writers:
            return "work"
        if base in policy.read_only:
            return "read-only"
        if base in policy.system_event_writers:
            return "system-event"
    explicit = capabilities.get("writer_scope")
    if explicit:
        return validate_writer_scope(explicit)
    rp = roster_policy_from_metadata(metadata or {})
    return validate_writer_scope(rp.get("default_writer_scope"), default="work")


def runtime_agent_id(agent_name: str, project_key: str) -> str:
    agent_name = (agent_name or "").lower().strip()
    return agent_display_name(agent_name, project_key) if agent_name else ""


def display_agent_name(agent: str) -> str:
    return "-".join(part[:1].upper() + part[1:] for part in agent.split("-") if part)


def build_persona_sections(
    *,
    agent: str,
    project: str,
    role: str,
    lane: str,
    not_lane: str,
    reports_to: str,
    runtime_context: dict[str, Any],
    profile_text: str,
    pending_handoffs: list[Any],
    claimed_handoffs: list[Any],
    recent_decisions: list[Any],
) -> dict[str, str]:
    identity = (
        f"You are {agent_display_name(agent, project)}, {role} for {project}. "
        f"Reports/escalates to: {reports_to}."
    )

    rules = [
        "## Operating Rules",
        "- Stay inside the current Cortex project and roster boundary.",
        "- Use API-backed cortex-* commands; do not bypass Cortex with direct database/container writes.",
        "- Claim only work addressed to your agent, role, or an explicit current user/CTO route.",
        "- Complete handoffs only after evidence exists and residual risk is stated.",
    ]
    if runtime_context.get("enforce_writer_roster"):
        rules.append(
            "- Project roster/writer guard is registry-enforced; only registered "
            "writers can own or receive work."
        )
    pm_lead = runtime_context.get("pm_lead")
    support_agents = json_list(runtime_context.get("support_agents"))
    if pm_lead:
        rules.append(f"- PM lead: {pm_lead}.")
    if support_agents:
        rules.append(f"- Support agents: {', '.join(support_agents)}.")
    for gate in json_list(runtime_context.get("hard_gates")):
        rules.append(f"- Hard gate: {gate}.")

    skills = ["## Persona Skills", f"- Lane: {lane}", f"- Not lane: {not_lane}"]
    for skill in runtime_context.get("skills", []):
        skills.append(f"- {skill}")
    if profile_text:
        skills.extend(["", "## Profile Notes", profile_text.strip()])

    state = [
        "## Runtime Skill Context",
        f"- Active epic: {runtime_context.get('active_epic') or '(none)'}",
        f"- Active increment: {runtime_context.get('active_increment') or '(none)'}",
        "- Policy refs:",
        *[f"  - {ref}" for ref in runtime_context.get("policy_refs", [])],
        f"- Approved agents: {', '.join(json_list(runtime_context.get('approved_agents'))) or '(registry default)'}",
        f"- Non-roster rule: {runtime_context.get('non_roster_rule') or '(default project rules)'}",
        "",
        "## Current State",
    ]
    if pending_handoffs:
        state.append("Pending handoffs:")
        state.extend(
            f"- {row['id']} | {row['priority']} | {row['summary']}"
            for row in pending_handoffs
        )
    else:
        state.append("Pending handoffs: none")
    if claimed_handoffs:
        state.append("Claimed by you:")
        state.extend(
            f"- {row['id']} | {row['priority']} | {row['summary']}"
            for row in claimed_handoffs
        )
    else:
        state.append("Claimed by you: none")
    if recent_decisions:
        state.append("Recent decisions:")
        state.extend(
            f"- {row['agent_name']} | {row['summary']}" for row in recent_decisions
        )
    else:
        state.append("Recent decisions: none")

    footer = (
        "## Cortex Architecture Reminder\n"
        "L6 Boot Context, L5 Multimodal Artifacts, L4 Knowledge Graph, "
        "L3 Code Graph, L2 Vector Embeddings, L1 Verbatim Storage."
    )

    return {
        "identity": identity,
        "operating_rules": "\n".join(rules),
        "persona_skills": "\n".join(skills),
        "current_state": "\n".join(state),
        "architecture_footer": footer,
    }


def build_runtime_profile(
    project_row: dict[str, Any],
    root_rows: list[Any],
    agent_rows: list[Any],
    platform_config: dict[str, Any] | None = None,
    roster_policy: RosterPolicy | None = None,
) -> dict[str, Any]:
    """Build the data contract consumed by launchers, Beat, and package setup."""

    project_key = project_row["project_key"]
    metadata = json_object(project_row.get("metadata"))
    platform_config = platform_config or {}

    roots = exact_project_roots(project_row, root_rows)

    agents: list[dict[str, Any]] = []
    for row in agent_rows:
        capabilities = json_object(row.get("capabilities"))
        pane = json_object(capabilities.get("pane"))
        agents.append(
            {
                "name": row["name"],
                "runtime_id": runtime_agent_id(row["name"], project_key),
                "role": row["role"],
                "model": row["model"],
                "capabilities": capabilities,
                "writer_scope": writer_scope_for_agent(
                    row["name"],
                    capabilities,
                    roster_policy,
                    metadata,
                ),
                "pane": pane,
            }
        )

    default_agent = (
        project_row.get("default_agent")
        or metadata.get("default_agent")
        or (agents[0]["name"] if agents else None)
    )
    beat_metadata = json_object(metadata.get("beat"))
    explicit_beat_fields = [
        field
        for field in ("orchestrator_agent", "agent")
        if field in beat_metadata
    ]
    if len(explicit_beat_fields) > 1:
        raise HTTPException(
            409,
            "Persisted metadata.beat must use exactly one Beat identity field; repair the project registry before starting Beat",
        )
    explicit_beat_field = explicit_beat_fields[0] if explicit_beat_fields else None
    if explicit_beat_field is not None:
        raw_beat_agent = beat_metadata.get(explicit_beat_field)
        if not isinstance(raw_beat_agent, str) or not raw_beat_agent.strip():
            raise HTTPException(
                409,
                f"Persisted metadata.beat.{explicit_beat_field} is invalid; repair the project registry before starting Beat",
            )
        try:
            persisted_beat_agent = validate_registry_agent_name(
                raw_beat_agent,
                project_key,
                field_name=f"metadata.beat.{explicit_beat_field}",
            )
        except HTTPException as exc:
            raise HTTPException(
                409,
                f"Persisted metadata.beat.{explicit_beat_field} is invalid; repair the project registry before starting Beat",
            ) from exc
        registered_agent_names = {agent["name"] for agent in agents}
        if persisted_beat_agent not in registered_agent_names:
            raise HTTPException(
                409,
                f"Persisted metadata.beat.{explicit_beat_field}={persisted_beat_agent!r} is absent from the registered agent roster; repair the project registry before starting Beat",
            )
        beat_metadata = {**beat_metadata, explicit_beat_field: persisted_beat_agent}
    orchestrator_agent = (
        beat_metadata.get("orchestrator_agent")
        or beat_metadata.get("agent")
        or next((agent["name"] for agent in agents if agent["role"] == "orchestrator"), None)
        or next((agent["name"] for agent in agents if agent["name"] == "beat"), None)
        or "beat"
    )
    cadence_minutes = int(beat_metadata.get("cadence_minutes") or 25)
    launchd_label = beat_metadata.get("launchd_label") or f"com.cortex.{project_key}.beat"
    api_url = platform_config.get("cortex_api_url") or CORTEX_PLATFORM_DEFAULTS["cortex_api_url"]
    beat_agent_id = runtime_agent_id(
        beat_metadata.get("agent_id") or orchestrator_agent,
        project_key,
    )

    roster_metadata = roster_policy_from_metadata(metadata)
    roster_roles = json_object(roster_metadata.get("roles"))
    approved_agents = json_list(roster_roles.get("approved_agents"))
    if roster_policy is not None:
        approved_agents = approved_agents or sorted(roster_policy.work_writers)
    support_agents = json_list(roster_roles.get("support_agents"))

    return {
        "project_key": project_key,
        "project_id": str(project_row.get("id") or project_row.get("project_id") or ""),
        "display_name": project_row.get("display_name") or project_key,
        "parent_project_key": project_row.get("parent_project_key"),
        "repo_root": project_row.get("repo_root"),
        "repo_type": project_row.get("repo_type"),
        "status": project_row.get("status"),
        "default_agent": default_agent,
        "roots": roots,
        "agents": agents,
        "roster": {
            "enforce": bool(roster_policy.enforce) if roster_policy is not None else bool(metadata.get("enforce_writer_roster", False)),
            "roster_schema_version": roster_metadata.get("roster_schema_version", CORTEX_ROSTER_SCHEMA_VERSION),
            "default_writer_scope": (
                roster_policy.default_writer_scope if roster_policy is not None else roster_metadata.get("default_writer_scope", "work")
            ),
            "pm_lead": effective_pm_lead(roster_roles),
            "support_agents": support_agents,
            "approved_agents": approved_agents,
            "role_assignments": json_object(roster_roles.get("role_assignments")),
            "system_event_writers": (
                sorted(roster_policy.system_event_writers) if roster_policy is not None else json_list(roster_metadata.get("system_event_writers"))
            ),
            "writers": sorted(roster_policy.work_writers) if roster_policy is not None else [],
            "handoff_targets": sorted(roster_policy.handoff_targets) if roster_policy is not None else [],
        },
        "api_url": api_url,
        "beat": {
            "agent": orchestrator_agent,
            "agent_id": beat_agent_id,
            "cadence_minutes": cadence_minutes,
            "start_interval_seconds": cadence_minutes * 60,
            "launchd_label": launchd_label,
            "plist_name": f"{launchd_label}.plist",
            "progress_provider": beat_metadata.get("progress_provider", "none"),
            "progress_file": beat_metadata.get("progress_file"),
            "env": {
                "CORTEX_PROJECT": project_key,
                "CORTEX_API_URL": api_url,
                "BEAT_CORTEX_AGENT": beat_agent_id,
                "CORTEX_WORKSPACE_ROOT": project_row.get("repo_root"),
            },
        },
        "metadata": metadata,
    }


def parse_identity(
    x_agent: str = Header(alias="X-Agent-Name", default=""),
    x_project: str = Header(alias="X-Project", default=""),
) -> tuple[str, str]:
    if not x_agent or not x_project:
        raise HTTPException(400, "X-Agent-Name and X-Project headers required")
    project = validate_project_key(x_project)
    return agent_base_for_project(x_agent, project, field_name="X-Agent-Name"), project


def require_project_scope(x_project: str) -> str:
    if not (x_project or "").strip():
        raise HTTPException(400, "X-Project header required")
    return validate_project_key(x_project)


def normalize_cell(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (dict, list)):
        return value
    return value


def visible_agent_sql(alias: str = "a") -> str:
    return (
        "COALESCE("
        f"{alias}.capabilities->>'visibility', 'active'"
        ") <> 'history-only' AND "
        "("
        f"COALESCE({alias}.capabilities->>'keep_visible', 'false') = 'true' "
        "OR EXISTS ("
        "SELECT 1 FROM agent_profiles ap "
        f"WHERE ap.project = {alias}.project AND ap.agent_name = {alias}.name"
        ")"
        ")"
    )


async def upsert_role_record(
    conn: asyncpg.Connection,
    project: str,
    role: str,
    capabilities: dict[str, Any],
    description: str | None = None,
    is_builtin: bool = False,
    source_file: str | None = None,
) -> None:
    sql = """INSERT INTO roles
                 (project, name, default_capabilities, description, is_builtin,
                  source_file, updated_at)
             VALUES ($1, $2, $3::jsonb, $4, $5, $6, NOW())
             ON CONFLICT (project, name)
             DO UPDATE SET
                 default_capabilities = COALESCE(NULLIF(EXCLUDED.default_capabilities, '{}'::jsonb), roles.default_capabilities),
                 description = COALESCE(EXCLUDED.description, roles.description),
                 is_builtin = roles.is_builtin OR EXCLUDED.is_builtin,
                 source_file = COALESCE(EXCLUDED.source_file, roles.source_file),
                 updated_at = NOW()"""
    args = (
        project,
        role,
        json.dumps(capabilities or {}),
        description,
        is_builtin,
        source_file,
    )
    try:
        await conn.execute(sql, *args)
    except (asyncpg.UndefinedTableError, asyncpg.InsufficientPrivilegeError):
        await ensure_roles_schema()
        try:
            await conn.execute(sql, *args)
        except asyncpg.InsufficientPrivilegeError as retry_exc:
            raise HTTPException(
                503,
                "roles table is not writable by the Cortex app role after "
                "admin schema bootstrap; rerun migrations or restart cortex-api",
            ) from retry_exc
        except asyncpg.UndefinedTableError as retry_exc:
            raise HTTPException(
                503,
                "roles table is unavailable after admin schema bootstrap",
            ) from retry_exc


def validate_table_name(table: str) -> str:
    if not re.fullmatch(r"[a-z_][a-z0-9_]*", table):
        raise HTTPException(400, f"Unsafe table name: {table}")
    return table


def validate_search_hall(hall: str) -> str:
    normalized = (hall or "project").strip().lower()
    if normalized not in {"project", "shared", "all", "local"}:
        raise HTTPException(
            400,
            "Unknown hall. Expected one of: project, shared, all, local",
        )
    return normalized


def has_admin_access(request: Request) -> bool:
    token = request.headers.get("X-Cortex-Admin-Token", "").strip()
    return bool(
        ADMIN_TOKEN
        and token
        and hmac.compare_digest(token.encode("utf-8"), ADMIN_TOKEN.encode("utf-8"))
    )


def require_admin_access(request: Request):
    if not has_admin_access(request):
        raise HTTPException(403, "Admin compatibility endpoints require a valid token")


SCHEMA_MIGRATION_ID_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-[A-Za-z0-9_.-]+\.sql$")

# This immutable migration was applied on the dogfood appliance before review
# found that it batches several blocking DROP INDEX statements.  Keep its bytes
# for existing ledger rows, but never execute it on a fresh deployment; the
# 2026-08-17 concurrent successors replace it safely.
SUPERSEDED_SCHEMA_MIGRATIONS = {
    "2026-08-16-03-drop-degenerate-ivfflat-indexes.sql",
}

# v0.2.001 shipped this roster no-op with one brand name changed in a comment.
# The SQL behavior is byte-for-byte equivalent; accept only that reviewed digest
# while retaining the audited dogfood digest as the canonical source file.
SCHEMA_MIGRATION_CHECKSUM_ALIASES = {
    "2026-06-01-roster-as-data.sql": {
        "26f8e808b9f4b38542ca6a9e79ddad153f78db8855ed2ea3d8ea789e5f0c2da5",
    },
}

# Each destructive successor keeps its SQL to the single CONCURRENTLY statement
# PostgreSQL requires outside a transaction.  The API performs this exact live
# catalog check immediately before that statement, so direct targeting is as
# safe as the installer-ordered path.
IVFFLAT_DROP_HNSW_REPLACEMENTS = {
    "2026-08-17-04-drop-work-products-embedding-ivfflat.sql": (
        "work_products",
        "idx_work_products_embedding_hnsw",
    ),
    "2026-08-17-05-drop-lessons-embedding-ivfflat.sql": (
        "lessons",
        "idx_lessons_embedding_hnsw",
    ),
    "2026-08-17-06-drop-knowledge-embedding-ivfflat.sql": (
        "knowledge",
        "idx_knowledge_embedding_hnsw",
    ),
    "2026-08-17-07-drop-decisions-embedding-ivfflat.sql": (
        "decisions",
        "idx_decisions_embedding_hnsw",
    ),
    "2026-08-17-08-drop-messages-embedding-ivfflat.sql": (
        "messages",
        "idx_messages_embedding_hnsw",
    ),
}

# Only these exact file IDs and byte digests may cross PostgreSQL's transaction
# boundary.  SQL + ledger is one transaction for every other migration.  For these
# online index operations the session advisory lock and exact catalog postcondition
# are the durable commit protocol: a crash after DDL but before the ledger is reconciled
# from the catalog on retry.  Unknown/changing nontransactional bytes remain a HOLD.
REVIEWED_ONLINE_INDEX_MIGRATIONS: dict[str, dict[str, str]] = {
    "2026-08-17-02-archive-messages-raw-session-index.sql": {
        "checksum": "35100735d08bc4655b2a43470e822d3b1dfbe8966a08de991766b246abe1b986",
        "operation": "create",
        "schema": "public",
        "index": "idx_archive_messages_raw_session",
        "table": "archive_messages",
        "definition": (
            "CREATE INDEX idx_archive_messages_raw_session "
            "ON public.archive_messages USING btree (raw_session_id)"
        ),
    },
    "2026-08-17-04-drop-work-products-embedding-ivfflat.sql": {
        "checksum": "69a4e34b842acbf873446494d0f500787a37a419f1bfe41d24fb733728c0b42c",
        "operation": "drop",
        "schema": "public",
        "index": "idx_work_products_embedding",
        "table": "work_products",
        "definition": (
            "CREATE INDEX idx_work_products_embedding ON public.work_products "
            "USING ivfflat (embedding vector_cosine_ops) WITH (lists = 1)"
        ),
    },
    "2026-08-17-05-drop-lessons-embedding-ivfflat.sql": {
        "checksum": "f95bf2e2ba86856ddc29e6560f23b5fb193228e679cba35fdfe1052bf80a8ae8",
        "operation": "drop",
        "schema": "public",
        "index": "idx_lessons_embedding",
        "table": "lessons",
        "definition": (
            "CREATE INDEX idx_lessons_embedding ON public.lessons "
            "USING ivfflat (embedding vector_cosine_ops) WITH (lists = 1)"
        ),
    },
    "2026-08-17-06-drop-knowledge-embedding-ivfflat.sql": {
        "checksum": "4b773002ffeacbc38dbb08f52b87c49f31efb9f7c29dc0a087e93a727969c294",
        "operation": "drop",
        "schema": "public",
        "index": "idx_knowledge_embedding",
        "table": "knowledge",
        "definition": (
            "CREATE INDEX idx_knowledge_embedding ON public.knowledge "
            "USING ivfflat (embedding vector_cosine_ops) WITH (lists = 1)"
        ),
    },
    "2026-08-17-07-drop-decisions-embedding-ivfflat.sql": {
        "checksum": "8c0fb146da0484b458c4954a30f39b1db45e825bde374f16e92b9dbd784acc46",
        "operation": "drop",
        "schema": "public",
        "index": "idx_decisions_embedding",
        "table": "decisions",
        "definition": (
            "CREATE INDEX idx_decisions_embedding ON public.decisions "
            "USING ivfflat (embedding vector_cosine_ops) WITH (lists = 1)"
        ),
    },
    "2026-08-17-08-drop-messages-embedding-ivfflat.sql": {
        "checksum": "53d1c0c7eed7e0d8f8c67a8b0b79c8232520e19de4d318e26a7ca7965fe9b102",
        "operation": "drop",
        "schema": "public",
        "index": "idx_messages_embedding",
        "table": "messages",
        "definition": (
            "CREATE INDEX idx_messages_embedding ON public.messages "
            "USING ivfflat (embedding vector_cosine_ops) WITH (lists = 1)"
        ),
    },
    "2026-08-18-03-messages-project-ts-hot-path-index.sql": {
        "checksum": "526958c5fbcc76e5326844fab5a3ad87b463b6e32160d910e283a930f8e6b6c0",
        "operation": "create",
        "schema": "public",
        "index": "idx_messages_project_ts",
        "table": "messages",
        "definition": (
            "CREATE INDEX idx_messages_project_ts "
            "ON public.messages USING btree (project, ts DESC)"
        ),
    },
}


def configured_schema_migrations_dir() -> Path:
    """Return the mounted/schema-migration directory for the API process.

    Containerized Kaidera OS mounts ``.agents/data/migrations`` at
    ``/app/migrations``. Unit tests and direct source runs fall back to the
    repository's ``.agents/data/migrations`` path.
    """
    configured = Path(os.getenv("CORTEX_MIGRATIONS_DIR", "/app/migrations"))
    if configured.exists():
        return configured
    source_tree = Path(__file__).resolve().parents[1] / "data" / "migrations"
    return source_tree


def schema_migration_files(migration_dir: Path | None = None) -> list[dict[str, str]]:
    root = migration_dir or configured_schema_migrations_dir()
    if not root.exists() or not root.is_dir():
        raise HTTPException(500, f"Schema migration directory is not available: {root}")

    out: list[dict[str, str]] = []
    for path in sorted(root.glob("*.sql")):
        migration_id = path.name
        if not SCHEMA_MIGRATION_ID_RE.match(migration_id):
            raise HTTPException(
                500,
                f"Unsafe schema migration filename: {migration_id}",
            )
        sql = path.read_text(encoding="utf-8")
        out.append(
            {
                "id": migration_id,
                "path": str(path),
                "checksum_sha256": hashlib.sha256(sql.encode("utf-8")).hexdigest(),
                "sql": sql,
            }
        )
    return out


ORDERED_MIGRATION_ID_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}-.+)-\d+-(.+\.sql)$"
)


def schema_migration_ledger_ids(migration_id: str) -> list[str]:
    """Return current and compatible historical ledger IDs for a migration.

    Some migrations are renamed to insert an explicit ordering segment after
    initial dogfood application, for example ``...-1-foundation.sql``. The SQL
    checksum stays unchanged, so an existing ledger row under the pre-ordering
    name should be treated as the same applied migration instead of replaying it.
    """
    ids = [migration_id]
    match = ORDERED_MIGRATION_ID_RE.match(migration_id)
    if match:
        ids.append(f"{match.group(1)}-{match.group(2)}")
    return ids


async def ensure_schema_migrations_table(conn: asyncpg.Connection) -> None:
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS cortex_schema_migrations (
            migration_id TEXT PRIMARY KEY,
            checksum_sha256 TEXT NOT NULL,
            source_path TEXT NOT NULL,
            applied_by TEXT NOT NULL,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            statement_status TEXT,
            surface_version TEXT
        )
        """
    )
    await conn.execute("ALTER TABLE cortex_schema_migrations OWNER TO postgres")
    await conn.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cortex_app') THEN
                EXECUTE 'GRANT SELECT ON TABLE cortex_schema_migrations TO cortex_app';
            END IF;
        END $$;
        """
    )


async def fetch_applied_schema_migrations(conn: asyncpg.Connection) -> dict[str, dict[str, Any]]:
    await ensure_schema_migrations_table(conn)
    rows = await conn.fetch(
        """
        SELECT migration_id, checksum_sha256, source_path, applied_by,
               applied_at, statement_status, surface_version
          FROM cortex_schema_migrations
        """
    )
    return {row["migration_id"]: dict(row) for row in rows}


async def require_ivfflat_drop_replacement(
    conn: asyncpg.Connection,
    migration_id: str,
) -> None:
    expected = IVFFLAT_DROP_HNSW_REPLACEMENTS.get(migration_id)
    if expected is None:
        return
    table_name, index_name = expected
    ready = await conn.fetchrow(
        """
        SELECT TRUE AS ready
          FROM pg_catalog.pg_class AS idx
          JOIN pg_catalog.pg_namespace AS idx_ns
            ON idx_ns.oid = idx.relnamespace
          JOIN pg_catalog.pg_index AS meta
            ON meta.indexrelid = idx.oid
          JOIN pg_catalog.pg_class AS tbl
            ON tbl.oid = meta.indrelid
          JOIN pg_catalog.pg_namespace AS tbl_ns
            ON tbl_ns.oid = tbl.relnamespace
          JOIN pg_catalog.pg_am AS access_method
            ON access_method.oid = idx.relam
          JOIN pg_catalog.pg_attribute AS indexed_column
            ON indexed_column.attrelid = tbl.oid
           AND indexed_column.attnum = meta.indkey[0]
          JOIN pg_catalog.pg_opclass AS operator_class
            ON operator_class.oid = meta.indclass[0]
         WHERE idx_ns.nspname = 'public'
           AND tbl_ns.nspname = 'public'
           AND idx.relname = $1
           AND tbl.relname = $2
           AND access_method.amname = 'hnsw'
           AND meta.indisready
           AND meta.indisvalid
           AND meta.indpred IS NULL
           AND meta.indnatts = 1
           AND indexed_column.attname = 'embedding'
           AND operator_class.opcname = 'vector_cosine_ops'
        """,
        index_name,
        table_name,
    )
    if ready is None:
        raise HTTPException(
            409,
            "Refusing IVFFLAT removal: exact ready HNSW replacement is missing "
            f"for public.{table_name}.embedding",
        )


def canonical_index_definition(value: str) -> str:
    """Canonicalize the reviewed CREATE form and PostgreSQL pg_get_indexdef output."""
    normalized = value.strip().rstrip(";").casefold().replace('"', "")
    normalized = re.sub(r"\bconcurrently\b", "", normalized)
    normalized = re.sub(r"\bif\s+not\s+exists\b", "", normalized)
    normalized = re.sub(r"\busing\s+btree\b", "", normalized)
    normalized = normalized.replace("public.", "")
    return re.sub(r"\s+", "", normalized)


async def online_index_state(
    conn: asyncpg.Connection,
    protocol: dict[str, str],
) -> dict[str, Any] | None:
    row = await conn.fetchrow(
        """
        SELECT table_ns.nspname AS table_schema,
               table_rel.relname AS table_name,
               index_meta.indisvalid AS valid,
               index_meta.indisready AS ready,
               pg_get_indexdef(index_meta.indexrelid) AS definition
          FROM pg_catalog.pg_class AS index_rel
          JOIN pg_catalog.pg_namespace AS index_ns
            ON index_ns.oid = index_rel.relnamespace
          JOIN pg_catalog.pg_index AS index_meta
            ON index_meta.indexrelid = index_rel.oid
          JOIN pg_catalog.pg_class AS table_rel
            ON table_rel.oid = index_meta.indrelid
          JOIN pg_catalog.pg_namespace AS table_ns
            ON table_ns.oid = table_rel.relnamespace
         WHERE index_ns.nspname = $1
           AND index_rel.relname = $2
        """,
        protocol["schema"],
        protocol["index"],
    )
    return dict(row) if row is not None else None


def online_index_identity_matches(
    protocol: dict[str, str],
    state: dict[str, Any],
) -> bool:
    if state.get("table_schema") != protocol["schema"] \
            or state.get("table_name") != protocol["table"]:
        return False
    expected = protocol.get("definition")
    return expected is None or canonical_index_definition(str(state.get("definition") or "")) \
        == canonical_index_definition(expected)


async def record_schema_migration(
    conn: asyncpg.Connection,
    item: dict[str, Any],
    *,
    applied_by: str,
    statement_status: str,
) -> bool:
    """Insert one ledger row under the caller's lock; return False if already exact."""
    async with conn.transaction():
        ledger = await conn.fetchrow(
            """SELECT checksum_sha256
                 FROM cortex_schema_migrations
                WHERE migration_id = $1
                FOR UPDATE""",
            item["id"],
        )
        if ledger is not None:
            if ledger["checksum_sha256"] != item["checksum_sha256"]:
                raise HTTPException(
                    409,
                    f"Schema migration checksum mismatch: {item['id']}",
                )
            return False
        await conn.execute(
            """
            INSERT INTO cortex_schema_migrations (
                migration_id, checksum_sha256, source_path, applied_by,
                statement_status, surface_version
            )
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            item["id"],
            item["checksum_sha256"],
            item["source_path"],
            applied_by,
            statement_status,
            CORTEX_SURFACE_VERSION,
        )
        return True


async def apply_reviewed_online_index_migration(
    conn: asyncpg.Connection,
    item: dict[str, Any],
    source: dict[str, str],
    *,
    applied_by: str,
) -> tuple[str, str, bool]:
    protocol = REVIEWED_ONLINE_INDEX_MIGRATIONS.get(item["id"])
    if protocol is None or protocol["checksum"] != item["checksum_sha256"]:
        raise HTTPException(
            409,
            f"Pending schema migration has no exact reviewed online-index protocol: {item['id']}",
        )
    lock_key = f"cortex-schema-online-index:{item['id']}"
    await conn.fetchval("SELECT pg_advisory_lock(hashtextextended($1, 0))", lock_key)
    unlock_error = False
    try:
        ledger = await conn.fetchrow(
            """SELECT checksum_sha256
                 FROM cortex_schema_migrations
                WHERE migration_id = $1""",
            item["id"],
        )
        if ledger is not None:
            if ledger["checksum_sha256"] != item["checksum_sha256"]:
                raise HTTPException(
                    409,
                    f"Schema migration checksum mismatch: {item['id']}",
                )
            return "skip_applied", "ALREADY APPLIED", False

        state = await online_index_state(conn, protocol)
        reconciled = False
        if protocol["operation"] == "drop":
            await require_ivfflat_drop_replacement(conn, item["id"])
            if state is not None and (
                not online_index_identity_matches(protocol, state)
                or not bool(state.get("valid"))
                or not bool(state.get("ready"))
            ):
                raise HTTPException(
                    409,
                    f"Refusing online index drop with mismatched catalog identity: {item['id']}",
                )
            if state is None:
                statement_status = "POSTCONDITION RECONCILED: INDEX ABSENT"
                reconciled = True
            else:
                statement_status = await conn.execute(source["sql"])
            if await online_index_state(conn, protocol) is not None:
                raise HTTPException(
                    409,
                    f"Online index drop postcondition failed: {item['id']}",
                )
        elif protocol["operation"] == "create":
            if state is not None and not online_index_identity_matches(protocol, state):
                raise HTTPException(
                    409,
                    f"Refusing online index reconciliation with mismatched catalog identity: {item['id']}",
                )
            if state is not None and bool(state.get("valid")) and bool(state.get("ready")):
                statement_status = "POSTCONDITION RECONCILED: INDEX READY"
                reconciled = True
            else:
                if state is not None:
                    # CREATE INDEX CONCURRENTLY may leave its exact index invalid after a
                    # crash. Remove only the reviewed identity before replaying exact bytes.
                    await conn.execute(
                        f'DROP INDEX CONCURRENTLY IF EXISTS "{protocol["schema"]}".'
                        f'"{protocol["index"]}"'
                    )
                    if await online_index_state(conn, protocol) is not None:
                        raise HTTPException(
                            409,
                            f"Invalid online index could not be removed safely: {item['id']}",
                        )
                statement_status = await conn.execute(source["sql"])
                state = await online_index_state(conn, protocol)
                if state is None or not online_index_identity_matches(protocol, state) \
                        or not bool(state.get("valid")) or not bool(state.get("ready")):
                    raise HTTPException(
                        409,
                        f"Online index create postcondition failed: {item['id']}",
                    )
        else:  # Defensive: the committed manifest is deliberately finite.
            raise HTTPException(500, "Invalid reviewed online-index operation")

        inserted = await record_schema_migration(
            conn,
            item,
            applied_by=applied_by,
            statement_status=statement_status,
        )
        return ("reconciled" if reconciled else "applied"), statement_status, inserted
    finally:
        unlocked = await conn.fetchval(
            "SELECT pg_advisory_unlock(hashtextextended($1, 0))",
            lock_key,
        )
        unlock_error = unlocked is not True
        if unlock_error:
            raise RuntimeError("online-index migration advisory lock was not released exactly")


async def schema_migration_plan(
    conn: asyncpg.Connection,
    *,
    migration_dir: Path | None = None,
    target_ids: list[str] | None = None,
) -> dict[str, Any]:
    available = schema_migration_files(migration_dir)
    applied = await fetch_applied_schema_migrations(conn)
    selected = set(target_ids or [])
    available_ids = {item["id"] for item in available}
    missing_targets = sorted(selected - available_ids)
    if missing_targets:
        raise HTTPException(
            404,
            "Schema migration target(s) not found: " + ", ".join(missing_targets),
        )

    items: list[dict[str, Any]] = []
    for item in available:
        if selected and item["id"] not in selected:
            continue
        applied_row = None
        applied_migration_id = None
        for ledger_id in schema_migration_ledger_ids(item["id"]):
            applied_row = applied.get(ledger_id)
            if applied_row is not None:
                applied_migration_id = ledger_id
                break
        if applied_row is None:
            status = (
                "superseded"
                if item["id"] in SUPERSEDED_SCHEMA_MIGRATIONS
                else "pending"
            )
        elif (
            applied_row["checksum_sha256"] != item["checksum_sha256"]
            and applied_row["checksum_sha256"]
            not in SCHEMA_MIGRATION_CHECKSUM_ALIASES.get(item["id"], set())
        ):
            status = "checksum_mismatch"
        else:
            status = "applied"
        items.append(
            {
                "id": item["id"],
                "checksum_sha256": item["checksum_sha256"],
                "source_path": item["path"],
                "status": status,
                "applied_migration_id": applied_migration_id,
                "applied_at": (
                    applied_row["applied_at"].isoformat()
                    if applied_row and applied_row.get("applied_at")
                    else None
                ),
                "surface_version": (
                    applied_row.get("surface_version") if applied_row else None
                ),
            }
        )
    return {
        "source_dir": str(migration_dir or configured_schema_migrations_dir()),
        "surface_version": CORTEX_SURFACE_VERSION,
        "migrations": items,
    }


async def apply_schema_migrations(
    conn: asyncpg.Connection,
    *,
    dry_run: bool = True,
    migration_dir: Path | None = None,
    target_ids: list[str] | None = None,
    max_count: int | None = None,
    applied_by: str = "admin-api",
) -> dict[str, Any]:
    plan = await schema_migration_plan(
        conn,
        migration_dir=migration_dir,
        target_ids=target_ids,
    )
    files_by_id = {item["id"]: item for item in schema_migration_files(migration_dir)}
    pending = [item for item in plan["migrations"] if item["status"] == "pending"]
    mismatched = [item for item in plan["migrations"] if item["status"] == "checksum_mismatch"]
    if mismatched:
        raise HTTPException(
            409,
            "Schema migration checksum mismatch: "
            + ", ".join(item["id"] for item in mismatched),
        )
    if max_count is not None:
        if max_count < 0:
            raise HTTPException(400, "max_count must be >= 0")
        pending = pending[:max_count]

    # The ledger is an atomic commit record, not a best-effort audit row. SQL that cannot
    # run in the same PostgreSQL transaction (or that embeds its own transaction control)
    # needs a separately reviewed postcondition/reconciliation protocol. Existing appliance
    # baselines may ledger such historical files atomically, but forward apply fails before
    # executing any pending file rather than leaving unledgered durable DDL after a crash.
    nontransactional_pattern = re.compile(
        r"(?im)^\s*(?:"
        r"(?:CREATE|DROP|REINDEX)\s+(?:UNIQUE\s+)?INDEX\s+CONCURRENTLY\b|"
        r"REFRESH\s+MATERIALIZED\s+VIEW\s+CONCURRENTLY\b|"
        r"VACUUM\b|"
        r"(?:BEGIN|START\s+TRANSACTION|COMMIT|ROLLBACK|END)\s*;"
        r")"
    )
    nontransactional = {
        item["id"]
        for item in pending
        if nontransactional_pattern.search(str(files_by_id[item["id"]]["sql"]))
    }
    blocked = [
        item["id"]
        for item in pending
        if item["id"] in nontransactional
        and (
            item["id"] not in REVIEWED_ONLINE_INDEX_MIGRATIONS
            or REVIEWED_ONLINE_INDEX_MIGRATIONS[item["id"]]["checksum"]
            != item["checksum_sha256"]
        )
    ]
    if not dry_run and blocked:
        raise HTTPException(
            409,
            "Pending schema migration requires an explicit crash-safe nontransactional "
            "reconciliation protocol: " + ", ".join(blocked),
        )

    results: list[dict[str, Any]] = []
    if dry_run:
        for item in pending:
            results.append({**item, "action": "would_apply"})
        for item in plan["migrations"]:
            if item["status"] == "applied":
                results.append({**item, "action": "skip_applied"})
            elif item["status"] == "superseded":
                results.append({**item, "action": "skip_superseded"})
        return {
            **plan,
            "dry_run": True,
            "applied_count": 0,
            "pending_count": len(pending),
            "results": results,
        }

    applied_count = 0
    for item in pending:
        source = files_by_id[item["id"]]
        if item["id"] in nontransactional:
            action, statement_status, inserted = await apply_reviewed_online_index_migration(
                conn,
                item,
                source,
                applied_by=applied_by,
            )
            if inserted:
                applied_count += 1
            results.append(
                {
                    **item,
                    "status": "applied",
                    "action": action,
                    "statement_status": statement_status,
                    "surface_version": CORTEX_SURFACE_VERSION,
                }
            )
            continue
        skipped_concurrent = False
        async with conn.transaction():
            await conn.fetchval(
                "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                f"cortex-schema-migration:{item['id']}",
            )
            ledger = await conn.fetchrow(
                """SELECT checksum_sha256
                     FROM cortex_schema_migrations
                    WHERE migration_id = $1
                    FOR UPDATE""",
                item["id"],
            )
            if ledger is not None:
                if ledger["checksum_sha256"] != item["checksum_sha256"]:
                    raise HTTPException(
                        409,
                        f"Schema migration checksum mismatch: {item['id']}",
                    )
                skipped_concurrent = True
                statement_status = "ALREADY APPLIED"
            else:
                await require_ivfflat_drop_replacement(conn, item["id"])
                statement_status = await conn.execute(source["sql"])
                await conn.execute(
                    """
                    INSERT INTO cortex_schema_migrations (
                        migration_id, checksum_sha256, source_path, applied_by,
                        statement_status, surface_version
                    )
                    VALUES ($1, $2, $3, $4, $5, $6)
                    """,
                    item["id"],
                    item["checksum_sha256"],
                    item["source_path"],
                    applied_by,
                    statement_status,
                    CORTEX_SURFACE_VERSION,
                )
                applied_count += 1
        results.append(
            {
                **item,
                "status": "applied",
                "action": "skip_applied" if skipped_concurrent else "applied",
                "statement_status": statement_status,
                "surface_version": CORTEX_SURFACE_VERSION,
            }
        )

    for item in plan["migrations"]:
        if item["status"] == "applied":
            results.append({**item, "action": "skip_applied"})
        elif item["status"] == "superseded":
            results.append({**item, "action": "skip_superseded"})

    return {
        **plan,
        "dry_run": False,
        "applied_count": applied_count,
        "pending_count": 0,
        "results": results,
    }


async def find_invalidation_target(conn: asyncpg.Connection, project: str, item_id: str):
    for table in ("decisions", "lessons", "handoffs"):
        row = await conn.fetchrow(
            f"SELECT id::text AS id FROM {table} WHERE project = $1 AND id::text = $2 LIMIT 1",
            project,
            item_id,
        )
        if row:
            return table, row["id"]

    for table in ("decisions", "lessons", "handoffs"):
        row = await conn.fetchrow(
            f"SELECT id::text AS id FROM {table} WHERE project = $1 AND id::text LIKE $2 || '%' LIMIT 1",
            project,
            item_id,
        )
        if row:
            return table, row["id"]

    return None, None


# ---------------------------------------------------------------------------
# Embedding helper — auto-embed on write
# ---------------------------------------------------------------------------


# Query-embedding cache. SEARCH embeds its query through an external provider on every
# call, and measurement on marlow-aws showed that round-trip IS the latency: a search
# matching ZERO rows still cost 1.0-1.7s, an uncontended real search 2.2-4.3s, while the
# SQL stages ran in 19-114ms. Postgres was never the bottleneck, and neither were the
# 161.8MB of unused indexes an earlier theory blamed. Nothing cached this, so three
# identical queries in a row measured 3.18s, 2.05s and 2.45s.
#
# Keyed on (project, provider, model, dims, provider-input hash) so an operator changing
# the embedding model centrally can never be served a vector produced by the old one,
# and one project's cache timing cannot disclose another project's recent searches.
#
# BOUNDED, deliberately: a cache keyed on caller-supplied text grows without limit into
# a memory-exhaustion path -- the same mistake as capping CSV rows but not columns.
# Failures are never cached, so a provider blip cannot pin a null.
# Concurrent callers share one in-flight provider task. Without this map, a burst of
# identical searches all misses before the first response lands and recreates the
# provider-amplification incident the cache was introduced to prevent.
#
# ponytail: OrderedDict LRU, no TTL. The key already carries the only thing that
# invalidates a vector; add a TTL when something other than model config can.
_QueryEmbedKey = tuple[str, str, str, int, bytes]
_QUERY_EMBED_CACHE: "collections.OrderedDict[_QueryEmbedKey, list[float]]" = (
    collections.OrderedDict()
)
_QUERY_EMBED_INFLIGHT: dict[_QueryEmbedKey, "asyncio.Task[list[float] | None]"] = {}
_QUERY_EMBED_CACHE_MAX = 512


def _query_embed_cache_put(key: _QueryEmbedKey, vector: list[float]) -> None:
    _QUERY_EMBED_CACHE[key] = vector
    _QUERY_EMBED_CACHE.move_to_end(key)
    while len(_QUERY_EMBED_CACHE) > _QUERY_EMBED_CACHE_MAX:
        _QUERY_EMBED_CACHE.popitem(last=False)


async def embed_query_cached(query: str, project: str = "") -> list[float] | None:
    """`embed_text` for SEARCH, memoised and single-flight per project/config/query.

    Search only. Ingestion embeds unique content, so caching it would spend memory to
    guarantee misses.
    """
    text = (query or "").strip()
    if len(text) < 10:
        return None
    config = await load_cortex_platform_config_cached()
    provider = str(config.get("embedding_provider") or EMBED_PROVIDER).strip().lower()
    model = str(config.get("embedding_model") or EMBED_MODEL).strip()
    try:
        dims = int(config.get("embedding_dims") or EMBED_DIMS)
    except (TypeError, ValueError):
        dims = EMBED_DIMS
    provider_text = text[:_provider_input_limit(config, "embedding")]
    ck: _QueryEmbedKey = (
        (project or "").strip(),
        provider,
        model,
        dims,
        hashlib.sha256(provider_text.encode("utf-8")).digest(),
    )

    cached = _QUERY_EMBED_CACHE.get(ck)
    if cached is not None:
        _QUERY_EMBED_CACHE.move_to_end(ck)
        return cached

    task = _QUERY_EMBED_INFLIGHT.get(ck)
    if task is None:
        task = asyncio.create_task(_embed_text_with_config(provider_text, config))
        _QUERY_EMBED_INFLIGHT[ck] = task

        def finish(done: "asyncio.Task[list[float] | None]") -> None:
            if _QUERY_EMBED_INFLIGHT.get(ck) is done:
                _QUERY_EMBED_INFLIGHT.pop(ck, None)
            if done.cancelled():
                return
            try:
                vector = done.result()
            except Exception:
                return
            if vector:  # never cache a failure
                _query_embed_cache_put(ck, vector)

        task.add_done_callback(finish)

    # A request deadline may cancel one waiter. Shielding keeps the shared bounded
    # provider call alive for concurrent waiters and lets its successful result warm
    # the cache instead of making every timed-out caller launch a replacement.
    return await asyncio.shield(task)


async def embed_text(text: str) -> list[float] | None:
    if not text or len(text.strip()) < 10:
        return None
    config = await load_cortex_platform_config_cached()
    return await _embed_text_with_config(text, config)


async def _post_local_search_worker(
    path: str,
    payload: dict[str, Any],
    *,
    timeout: float,
) -> dict[str, Any]:
    """Call the internal keyless worker and bound its response before JSON decode."""
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream(
            "POST", f"{EMBED_WORKER_URL}{path}", json=payload
        ) as response:
            response.raise_for_status()
            chunks: list[bytes] = []
            received = 0
            async for chunk in response.aiter_bytes():
                received += len(chunk)
                if received > LOCAL_SEARCH_MAX_RESPONSE_BYTES:
                    raise ValueError(
                        "local-search worker response exceeded its byte limit"
                    )
                chunks.append(chunk)
        data = json.loads(b"".join(chunks))
    if not isinstance(data, dict):
        raise ValueError("local-search worker returned non-object JSON")
    return data


def _truncate_utf8_bytes(text: str, max_bytes: int) -> str:
    """Truncate without splitting a code point; local-worker limits are bytes."""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def _extract_local_embedding(
    data: dict[str, Any],
    *,
    model: str,
    dims: int,
) -> list[float] | None:
    if data.get("model") != model or data.get("dim") != dims:
        return None
    vectors = data.get("vectors")
    if not isinstance(vectors, list) or len(vectors) != 1:
        return None
    raw_vector = vectors[0]
    if not isinstance(raw_vector, list) or len(raw_vector) != dims:
        return None
    vector: list[float] = []
    for value in raw_vector:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        number = float(value)
        if not math.isfinite(number):
            return None
        vector.append(number)
    return vector


def _extract_local_rerank_results(
    data: dict[str, Any],
    *,
    model: str,
    document_count: int,
    top_n: int,
) -> list[dict] | None:
    if data.get("model") != model:
        return None
    rows = data.get("results")
    expected_rows = min(top_n, document_count)
    if not isinstance(rows, list) or len(rows) != expected_rows:
        return None
    out: list[dict] = []
    seen: set[int] = set()
    previous_score = math.inf
    for row in rows:
        if not isinstance(row, dict):
            return None
        index = row.get("index")
        score = row.get("relevance_score")
        if (
            isinstance(index, bool)
            or not isinstance(index, int)
            or index < 0
            or index >= document_count
            or index in seen
            or isinstance(score, bool)
            or not isinstance(score, (int, float))
            or not math.isfinite(float(score))
        ):
            return None
        seen.add(index)
        numeric_score = float(score)
        if numeric_score > previous_score:
            return None
        previous_score = numeric_score
        out.append({"index": index, "relevance_score": numeric_score})
    return out


async def _embed_text_with_config(
    text: str, config: dict[str, Any]
) -> list[float] | None:
    """Embed with one immutable config snapshot.

    The cache key and provider request must use the same snapshot; loading config a
    second time here could store a new-model vector under the old-model cache key.
    """
    provider = str(config.get("embedding_provider") or EMBED_PROVIDER).strip().lower()
    model = str(config.get("embedding_model") or EMBED_MODEL).strip()
    try:
        dims = int(config.get("embedding_dims") or EMBED_DIMS)
    except (TypeError, ValueError):
        dims = EMBED_DIMS
    key: str | None = None
    if provider != LOCAL_SEARCH_PROVIDER:
        key = await resolve_provider_key(provider)
        if not key or not model:
            return None
    elif not model:
        return None
    truncated = text[:_provider_input_limit(config, "embedding")]
    try:
        if provider == LOCAL_SEARCH_PROVIDER:
            if model != LOCAL_EMBED_MODEL or dims != LOCAL_EMBED_DIMS:
                EMBEDDING_CALLS.labels(model=model, status="config_mismatch").inc()
                return None
            truncated = _truncate_utf8_bytes(
                truncated,
                LOCAL_SEARCH_MAX_TEXT_BYTES,
            )
            if not truncated:
                return None
            data = await _post_local_search_worker(
                "/embed",
                {"model": model, "texts": [truncated]},
                timeout=_provider_timeout(config, "embedding"),
            )
            emb = _extract_local_embedding(data, model=model, dims=dims)
            if emb:
                EMBEDDING_CALLS.labels(model=model, status="success").inc()
                return emb
            EMBEDDING_CALLS.labels(model=model, status="invalid_response").inc()
            return None

        if key is None:  # defensive: external providers are resolved above
            return None
        async with httpx.AsyncClient(timeout=_provider_timeout(config, "embedding")) as client:
            resp = await client.post(
                _embedding_endpoint(provider),
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                },
                json={"model": model, "input": truncated, "dimensions": dims},
            )
            data = resp.json()
            emb = _extract_embedding(data)
            if emb and len(emb) == dims:
                EMBEDDING_CALLS.labels(model=model, status="success").inc()
                return emb
            if emb:
                EMBEDDING_CALLS.labels(model=model, status="dimension_mismatch").inc()
                return None
            EMBEDDING_CALLS.labels(model=model, status="empty").inc()
            return None
    except httpx.TimeoutException:
        EMBEDDING_CALLS.labels(model=model, status="timeout").inc()
        return None
    except Exception:
        EMBEDDING_CALLS.labels(model=model, status="error").inc()
        return None


async def rerank_results(
    query: str, documents: list[str], top_n: int = 8
) -> list[dict] | None:
    config = await load_cortex_platform_config_cached()
    if not bool(config.get("rerank_enabled", True)) or len(documents) < 2:
        return None
    provider = str(config.get("rerank_provider") or RERANK_PROVIDER).strip().lower()
    model = str(config.get("rerank_model") or RERANK_MODEL).strip()
    key: str | None = None
    if provider != LOCAL_SEARCH_PROVIDER:
        key = await resolve_provider_key(provider)
        if not key or not model:
            return None
    elif not model:
        return None
    limit = _provider_input_limit(config, "rerank")
    query_text = query[:limit]
    docs = [d[:limit] for d in documents]
    if provider == LOCAL_SEARCH_PROVIDER:
        if model != LOCAL_RERANK_MODEL:
            RERANK_CALLS.labels(model=model, status="config_mismatch").inc()
            return None
        if isinstance(top_n, bool) or not isinstance(top_n, int) or top_n < 1:
            return None
        query_text = _truncate_utf8_bytes(query_text, LOCAL_SEARCH_MAX_TEXT_BYTES)
        docs = [
            _truncate_utf8_bytes(document, LOCAL_SEARCH_MAX_TEXT_BYTES)
            for document in docs[:LOCAL_RERANK_MAX_DOCUMENTS]
        ]
        if (
            not query_text
            or any(not document for document in docs)
            or len(query_text.encode("utf-8"))
            + sum(len(document.encode("utf-8")) for document in docs)
            > LOCAL_SEARCH_MAX_BATCH_TEXT_BYTES
        ):
            return None
        local_top_n = min(top_n, len(docs), LOCAL_RERANK_MAX_DOCUMENTS)
        try:
            data = await _post_local_search_worker(
                "/rerank",
                {
                    "model": model,
                    "query": query_text,
                    "documents": docs,
                    "top_n": local_top_n,
                },
                timeout=_provider_timeout(config, "rerank"),
            )
            results = _extract_local_rerank_results(
                data,
                model=model,
                document_count=len(docs),
                top_n=local_top_n,
            )
            if results:
                RERANK_CALLS.labels(model=model, status="success").inc()
            else:
                RERANK_CALLS.labels(model=model, status="invalid_response").inc()
            return results
        except httpx.TimeoutException:
            RERANK_CALLS.labels(model=model, status="timeout").inc()
            return None
        except Exception:
            RERANK_CALLS.labels(model=model, status="error").inc()
            return None

    if key is None:  # defensive: external providers are resolved above
        return None
    if provider == "nvidia":
        url = "https://ai.api.nvidia.com/v1/retrieval/nvidia/reranking"
        body = {
            "model": model,
            "query": {"text": query_text},
            "passages": [{"text": d} for d in docs],
        }
    elif provider == "cohere":
        url = "https://api.cohere.com/v2/rerank"
        body = {
            "model": model,
            "query": query_text,
            "documents": docs,
            "top_n": top_n,
        }
    elif provider == "openrouter":
        url = "https://openrouter.ai/api/v1/rerank"
        body = {
            "model": model,
            "query": query_text,
            "documents": docs,
            "top_n": top_n,
        }
    else:
        return None
    try:
        async with httpx.AsyncClient(timeout=_provider_timeout(config, "rerank")) as client:
            resp = await client.post(
                url,
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                },
                json=body,
            )
            data = resp.json()
            results = _extract_rerank_results(data)
            if results:
                RERANK_CALLS.labels(model=model, status="success").inc()
            else:
                RERANK_CALLS.labels(model=model, status="empty").inc()
            return results
    except httpx.TimeoutException:
        RERANK_CALLS.labels(model=model, status="timeout").inc()
        return None
    except Exception:
        RERANK_CALLS.labels(model=model, status="error").inc()
        return None


def prioritise_messages(messages: list[dict], budget_chars: int = 32000) -> str:
    """Truncate session transcript by priority for LLM analysis.

    Priority: 0=user requests, 2=errors/tool failures, 3=assistant reasoning,
    5=system/skill injection (skipped).
    """
    prioritised = []
    for msg in messages:
        role = msg.get("role", "agent")
        content = msg.get("content", "")
        if not content or len(content) < 5:
            continue

        if role == "human":
            priority = 0
        elif role == "system":
            priority = 5
        elif "error" in content.lower() or "traceback" in content.lower():
            priority = 2
        else:
            priority = 3

        prioritised.append({"role": role, "content": content, "priority": priority})

    prioritised.sort(key=lambda m: m["priority"])

    result_parts = []
    total = 0
    for msg in prioritised:
        text = f"[{msg['role']}] {msg['content'][:2000]}"
        if total + len(text) > budget_chars:
            break
        result_parts.append(text)
        total += len(text)

    return "\n\n".join(result_parts)


ANALYSIS_PROMPT = (
    "You are analysing an AI agent coding session. Given the conversation below, "
    "produce a JSON object with these fields:\n\n"
    "- task_completed: boolean\n"
    "- quality_score: number 0-10\n"
    "- patterns_used: string[] (known approaches the agent applied)\n"
    "- patterns_failed: string[] (approaches tried but failed)\n"
    "- novel_patterns: string[] (new solutions invented, format: 'short-title: description')\n"
    "- tools_used: array of {\"tool\": string, \"uses\": int, \"successes\": int, \"failures\": int}\n"
    "- summary: string (one sentence)\n\n"
    "Respond with valid JSON only. No markdown fences.\n\n"
    "Conversation:\n"
)


def _capability_role_aliases(capabilities: Any) -> list[str]:
    """Role aliases stored on Cortex agents as data.

    Kaidera OS routing can use project-local role aliases (for example a worker whose
    canonical role is ``graphics`` may also own ``creative-multimedia`` handoffs).
    Claim authorization must see the same aliases or Dispatch can select a worker
    that Cortex refuses to let claim. Keep this parser narrow and data-only: it
    accepts strings/lists from the agent capabilities blob and returns normalized
    role slugs, not arbitrary claim bypasses.
    """
    if isinstance(capabilities, str):
        with suppress(ValueError):
            capabilities = json.loads(capabilities)
    if not isinstance(capabilities, dict):
        return []

    values: list[Any] = [
        capabilities.get("role_aliases"),
        capabilities.get("runtime_role"),
        capabilities.get("kaidera_os_role"),
        capabilities.get("platform_role"),
    ]
    aliases: list[str] = []
    for raw in values:
        parts: list[Any]
        if isinstance(raw, str):
            parts = raw.split(",")
        elif isinstance(raw, list):
            parts = raw
        else:
            parts = []
        for part in parts:
            alias = str(part or "").strip().lower()
            if alias and re.fullmatch(r"[a-z0-9][a-z0-9_-]*", alias):
                aliases.append(alias)
    return aliases


_DEFAULT_AGENT_LEAD_ROLES = frozenset({"lead", "cpo", "co-lead", "pm", "cmo"})


async def resolve_agent_roles(
    conn: asyncpg.Connection, project: str, agent: str
) -> list[str]:
    bare_agent = agent.lower().strip()
    rows = await conn.fetch(
        """SELECT role, capabilities
             FROM agents
            WHERE project = $1
              AND lower(name) = $2
              AND (role IS NOT NULL OR capabilities IS NOT NULL)
           UNION ALL
           SELECT role, NULL::jsonb AS capabilities
             FROM agent_profiles
            WHERE project = $1
              AND lower(agent_name) = $2
              AND role IS NOT NULL""",
        project,
        bare_agent,
    )
    roles: set[str] = set()
    for row in rows:
        role = (row.get("role") or "").strip().lower()
        if role:
            roles.add(role)
        roles.update(_capability_role_aliases(row.get("capabilities")))
    default_row = await conn.fetchrow(
        "SELECT default_agent FROM cortex_projects WHERE project_key = $1",
        project,
    )
    if default_row and agent_base_name(default_row.get("default_agent") or "") == bare_agent:
        roles.update(_DEFAULT_AGENT_LEAD_ROLES)
    return sorted(roles)


async def search_graph(
    conn: asyncpg.Connection, project: str, query: str, room: str | None, limit: int = 6
) -> list[dict]:
    rows = await conn.fetch(
        """WITH seeds AS (
               SELECT id, name, entity_type
                 FROM cortex_entities
                WHERE project = $2
                  AND (
                      name ILIKE '%' || $1 || '%'
                      OR COALESCE(properties->>'description', '') ILIKE '%' || $1 || '%'
                  )
                  AND (
                      $3::text IS NULL
                      OR name ILIKE '%' || $3 || '%'
                      OR entity_type ILIKE '%' || $3 || '%'
                      OR COALESCE(properties->>'description', '') ILIKE '%' || $3 || '%'
                  )
                ORDER BY name
                LIMIT 4
           ),
           edges AS (
               SELECT s.name AS seed_name,
                      s.entity_type AS seed_type,
                      t.name AS related_name,
                      t.entity_type AS related_type,
                      r.relationship_type AS relationship_type,
                      COALESCE(r.properties->>'description', '') AS rel_description
                 FROM seeds s
                 JOIN cortex_relationships r
                   ON r.project = $2
                  AND r.source_entity_id = s.id
                 JOIN cortex_entities t
                   ON t.id = r.target_entity_id
                UNION ALL
               SELECT s.name AS seed_name,
                      s.entity_type AS seed_type,
                      src.name AS related_name,
                      src.entity_type AS related_type,
                      r.relationship_type AS relationship_type,
                      COALESCE(r.properties->>'description', '') AS rel_description
                 FROM seeds s
                 JOIN cortex_relationships r
                   ON r.project = $2
                  AND r.target_entity_id = s.id
                 JOIN cortex_entities src
                   ON src.id = r.source_entity_id
           )
           SELECT seed_name,
                  seed_type,
                  related_name,
                  related_type,
                  relationship_type,
                  rel_description
             FROM edges
            LIMIT $4""",
        query,
        project,
        room,
        limit,
    )

    return [
        {
            "text": (
                f"{row['seed_name']} ({row['seed_type']}) --{row['relationship_type']}--> "
                f"{row['related_name']} ({row['related_type']})"
            ),
            "meta": row["rel_description"] or "knowledge graph",
            "category": row["relationship_type"] or "",
            "source": "graph",
            "tier": "graph",
        }
        for row in rows
    ]


GRAPH_HIGH_TYPES = ["concept", "epic", "service", "project", "product", "work_product"]
GRAPH_LOW_TYPES = ["file", "tool", "endpoint", "table", "branch", "model", "agent"]
GRAPH_ENTITY_TYPES = set(GRAPH_HIGH_TYPES + GRAPH_LOW_TYPES)
GRAPH_RELATIONSHIP_TYPES = {
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
GRAPH_SOURCES = {"decisions", "lessons", "knowledge", "work_products"}
GRAPH_SOURCE_ALIASES = {
    "project_memory": ("knowledge", "work_products"),
    "memory": ("knowledge", "work_products"),
    "all": tuple(sorted(GRAPH_SOURCES)),
}
GRAPH_DEFAULT_SOURCE = "project_memory"
GRAPH_LLM_ENABLED = os.getenv("CORTEX_GRAPH_LLM_ENABLED", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
GRAPH_DOMAIN_SUFFIXES = (
    "agent",
    "application",
    "backend",
    "capability",
    "connector",
    "dashboard",
    "database",
    "dataset",
    "domain",
    "frontend",
    "interface",
    "metric",
    "model",
    "operation",
    "pipeline",
    "platform",
    "portal",
    "process",
    "project",
    "queue",
    "report",
    "schema",
    "service",
    "system",
    "table",
    "view",
    "warehouse",
    "workflow",
    "worksheet",
)
GRAPH_PHRASE_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "how",
    "in",
    "into",
    "is",
    "it",
    "of",
    "on",
    "or",
    "the",
    "this",
    "to",
    "what",
    "when",
    "where",
    "which",
    "who",
    "why",
    "with",
}
GRAPH_PHRASE_BREAKWORDS = {
    "ask",
    "asks",
    "build",
    "builds",
    "collect",
    "collects",
    "contain",
    "contains",
    "create",
    "creates",
    "display",
    "displays",
    "drive",
    "drives",
    "need",
    "needs",
    "show",
    "shows",
    "store",
    "stored",
    "stores",
    "use",
    "uses",
    "using",
}
GRAPH_ACRONYM_DENYLIST = {
    "API",
    "GET",
    "HEAD",
    "HTTP",
    "HTTPS",
    "ID",
    "JSON",
    "KEY",
    "NEW",
    "OFF",
    "OK",
    "ON",
    "OPTIONS",
    "PATCH",
    "POST",
    "PUT",
    "SKIP",
    "TODO",
    "URI",
    "URL",
    "UUID",
}
GRAPH_PHRASE_LEADING_NOISE = GRAPH_PHRASE_BREAKWORDS | {
    "add",
    "adds",
    "allow",
    "allows",
    "change",
    "changes",
    "check",
    "checks",
    "corrupt",
    "corrupts",
    "delete",
    "deletes",
    "disable",
    "disables",
    "do",
    "does",
    "drop",
    "drops",
    "enable",
    "enables",
    "fail",
    "fails",
    "fix",
    "fixes",
    "get",
    "gets",
    "keep",
    "keeps",
    "probably",
    "read",
    "reads",
    "remove",
    "removes",
    "run",
    "runs",
    "should",
    "test",
    "tests",
    "update",
    "updates",
    "write",
    "writes",
}


def graph_source_tables(source: str) -> tuple[str, ...]:
    normalized = (source or GRAPH_DEFAULT_SOURCE).strip().lower().replace("-", "_")
    if normalized in GRAPH_SOURCE_ALIASES:
        return GRAPH_SOURCE_ALIASES[normalized]
    if normalized in GRAPH_SOURCES:
        return (normalized,)
    allowed = ", ".join([*sorted(GRAPH_SOURCES), *sorted(GRAPH_SOURCE_ALIASES)])
    raise HTTPException(400, f"source must be one of: {allowed}")


def graph_source_label(source: str) -> str:
    normalized = (source or GRAPH_DEFAULT_SOURCE).strip().lower().replace("-", "_")
    return normalized or GRAPH_DEFAULT_SOURCE


def graph_normalize_name(value: Any) -> str:
    return compact_text(value, limit=200)


def graph_normalize_type(value: Any, fallback: str = "concept") -> str:
    normalized = re.sub(r"[^a-z0-9_]+", "_", str(value or "").lower()).strip("_")
    return normalized if normalized in GRAPH_ENTITY_TYPES else fallback


def graph_normalize_relationship_type(value: Any) -> str:
    normalized = re.sub(r"[^a-z0-9_]+", "_", str(value or "").lower()).strip("_")
    return normalized if normalized in GRAPH_RELATIONSHIP_TYPES else "relates_to"


def graph_clean_description(value: Any) -> str:
    return compact_text(value, limit=500)


def graph_tokens_for(query: str) -> list[str]:
    tokens = [
        token.lower()
        for token in re.findall(r"[A-Za-z0-9_.:/-]+", query)
        if len(token) > 2
    ]
    return tokens or [query.lower()]


async def ensure_graph_schema(conn: asyncpg.Connection) -> None:
    # Normal request handling uses the least-privilege cortex_app role. The
    # installer/migration path owns schema DDL; a read must not require CREATE on
    # public merely because CREATE TABLE IF NOT EXISTS still checks that privilege.
    ready = await conn.fetchval(
        """
        SELECT to_regclass('public.cortex_entities') IS NOT NULL
           AND to_regclass('public.cortex_relationships') IS NOT NULL
        """
    )
    if ready:
        return
    try:
        await conn.execute(
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
    except asyncpg.InsufficientPrivilegeError as exc:
        raise HTTPException(
            503,
            "graph schema is unavailable to the app role; run Cortex migrations",
        ) from exc


async def ensure_work_products_schema(conn: asyncpg.Connection) -> None:
    exists = await conn.fetchval("SELECT to_regclass('public.work_products') IS NOT NULL")
    if exists:
        missing = await conn.fetch(
            """
            SELECT column_name
              FROM (VALUES
                    ('commit_sha'),
                    ('file_hashes'),
                    ('symbol_hashes'),
                    ('freshness_status'),
                    ('freshness_reason'),
                    ('freshness_checked_at'),
                    ('projection_status'),
                    ('projection_error'),
                    ('projected_at'),
                    ('valid_from'),
                    ('valid_to')) required(column_name)
             WHERE NOT EXISTS (
                    SELECT 1
                      FROM information_schema.columns
                     WHERE table_schema = 'public'
                       AND table_name = 'work_products'
                       AND column_name = required.column_name
             )
            """
        )
        if missing:
            missing_names = ", ".join(row["column_name"] for row in missing)
            raise HTTPException(
                503,
                "work_products schema is missing production columns "
                f"({missing_names}); run Cortex migrations",
            )
        return
    try:
        await conn.execute(
            f"""
            CREATE EXTENSION IF NOT EXISTS pg_trgm;
            CREATE EXTENSION IF NOT EXISTS pgcrypto;
            CREATE EXTENSION IF NOT EXISTS vector;

            CREATE TABLE IF NOT EXISTS work_products (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                project TEXT NOT NULL,
                handoff_id UUID NULL,
                agent_name TEXT,
                activity_type TEXT NOT NULL DEFAULT '{DEFAULT_WORK_PRODUCT_ACTIVITY}',
                status TEXT NOT NULL DEFAULT 'current',
                title TEXT NOT NULL,
                summary TEXT NOT NULL,
                behavior_summary TEXT,
                architecture_notes TEXT,
                files_changed TEXT[] DEFAULT '{{}}'::text[],
                symbols_changed TEXT[] DEFAULT '{{}}'::text[],
                subject_entities TEXT[] DEFAULT '{{}}'::text[],
                artifact_refs TEXT[] DEFAULT '{{}}'::text[],
                tests_run JSONB DEFAULT '[]'::jsonb,
                risks TEXT[] DEFAULT '{{}}'::text[],
                followups TEXT[] DEFAULT '{{}}'::text[],
                approval_status TEXT,
                content_hash TEXT,
                commit_sha TEXT,
                file_hashes JSONB DEFAULT '{{}}'::jsonb,
                symbol_hashes JSONB DEFAULT '{{}}'::jsonb,
                freshness_status TEXT NOT NULL DEFAULT 'unknown',
                freshness_reason TEXT,
                freshness_checked_at TIMESTAMPTZ NULL,
                projection_status TEXT NOT NULL DEFAULT 'pending',
                projection_error TEXT,
                projected_at TIMESTAMPTZ NULL,
                source_event_id BIGINT NULL,
                supersedes_id UUID NULL,
                metadata JSONB DEFAULT '{{}}'::jsonb,
                embedding vector({EMBED_DIMS}),
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW(),
                valid_from TIMESTAMPTZ DEFAULT NOW(),
                valid_to TIMESTAMPTZ NULL,
                invalidated_at TIMESTAMPTZ NULL
            );

            CREATE UNIQUE INDEX IF NOT EXISTS idx_work_products_project_handoff_current
                ON work_products (project, handoff_id)
                WHERE handoff_id IS NOT NULL AND invalidated_at IS NULL;
            CREATE INDEX IF NOT EXISTS idx_work_products_project_status
                ON work_products (project, status, updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_work_products_handoff
                ON work_products (handoff_id);
            CREATE INDEX IF NOT EXISTS idx_work_products_files
                ON work_products USING GIN (files_changed);
            CREATE INDEX IF NOT EXISTS idx_work_products_symbols
                ON work_products USING GIN (symbols_changed);
            CREATE INDEX IF NOT EXISTS idx_work_products_subjects
                ON work_products USING GIN (subject_entities);
            CREATE INDEX IF NOT EXISTS idx_work_products_metadata
                ON work_products USING GIN (metadata);
            CREATE INDEX IF NOT EXISTS idx_work_products_file_hashes
                ON work_products USING GIN (file_hashes);
            CREATE INDEX IF NOT EXISTS idx_work_products_freshness
                ON work_products (project, freshness_status, updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_work_products_projection
                ON work_products (project, projection_status, updated_at DESC);
            -- Vector columns use HNSW, NOT ivfflat. A `lists = 1` ivfflat index is a
            -- single degenerate cluster the planner correctly ignores (0 lifetime scans
            -- measured across all five such indexes on 2026-08-16) while still costing
            -- disk and write amplification on every insert. Switched here so no fresh
            -- deploy can reintroduce one. See migration 2026-08-16-03.
            CREATE INDEX IF NOT EXISTS idx_work_products_embedding_hnsw
                ON work_products USING hnsw (embedding vector_cosine_ops)
                WITH (m = 16, ef_construction = 64);
            CREATE INDEX IF NOT EXISTS idx_work_products_fts
                ON work_products USING GIN (
                    to_tsvector(
                        'english',
                        COALESCE(title, '') || ' ' ||
                        COALESCE(summary, '') || ' ' ||
                        COALESCE(behavior_summary, '') || ' ' ||
                        COALESCE(architecture_notes, '')
                    )
                );
            CREATE INDEX IF NOT EXISTS idx_work_products_title_trgm
                ON work_products USING GIN (LOWER(COALESCE(title, '')) gin_trgm_ops);
            CREATE INDEX IF NOT EXISTS idx_work_products_summary_trgm
                ON work_products USING GIN (LOWER(COALESCE(summary, '')) gin_trgm_ops);
            """
        )
    except asyncpg.InsufficientPrivilegeError as exc:
        raise HTTPException(
            503,
            "work_products schema is unavailable to the app role; run Cortex migrations",
        ) from exc


async def graph_entity_search(
    conn: asyncpg.Connection,
    project: str,
    query: str,
    entity_types: list[str],
    limit: int,
) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        """
        WITH terms(term) AS (
            SELECT unnest($4::text[])
        ),
        scored AS (
            SELECT
                e.id::text AS id,
                e.entity_type,
                e.name,
                COALESCE(e.properties->>'description', '') AS description,
                (
                    GREATEST(
                        similarity(LOWER(e.name), LOWER($5)),
                        COALESCE((SELECT MAX(similarity(LOWER(e.name), term)) FROM terms), 0)
                    )
                    + CASE WHEN LOWER(e.name) LIKE '%' || LOWER($5) || '%' THEN 2 ELSE 0 END
                    + CASE WHEN LOWER(COALESCE(e.properties->>'description', '')) LIKE '%' || LOWER($5) || '%' THEN 1 ELSE 0 END
                    + CASE WHEN EXISTS (
                        SELECT 1 FROM terms
                        WHERE LOWER(e.name) LIKE '%' || term || '%'
                           OR LOWER(COALESCE(e.properties->>'description', '')) LIKE '%' || term || '%'
                    ) THEN 1 ELSE 0 END
                ) AS score
            FROM cortex_entities e
            WHERE e.project = $1
              AND e.entity_type = ANY($2::text[])
              AND (
                  LOWER(e.name) LIKE '%' || LOWER($5) || '%'
                  OR LOWER(COALESCE(e.properties->>'description', '')) LIKE '%' || LOWER($5) || '%'
                  OR EXISTS (
                      SELECT 1 FROM terms
                      WHERE LOWER(e.name) LIKE '%' || term || '%'
                         OR LOWER(COALESCE(e.properties->>'description', '')) LIKE '%' || term || '%'
                         OR similarity(LOWER(e.name), term) >= 0.35
                  )
              )
        )
        SELECT id, entity_type, name, LEFT(description, 140) AS description, score::float AS score
          FROM scored
         ORDER BY score DESC, entity_type, name
         LIMIT $3
        """,
        project,
        entity_types,
        limit,
        graph_tokens_for(query),
        query,
    )
    return [
        {
            "id": row["id"],
            "entity_type": row["entity_type"],
            "name": row["name"],
            "description": row["description"],
            "score": round(float(row["score"] or 0), 3),
        }
        for row in rows
    ]


async def graph_relationship_search(
    conn: asyncpg.Connection, project: str, matched_ids: list[str], limit: int
) -> list[dict[str, Any]]:
    if not matched_ids:
        return []
    rows = await conn.fetch(
        """
        WITH matched(id) AS (
            SELECT unnest($2::uuid[])
        )
        SELECT DISTINCT
            s.name AS source,
            s.entity_type AS source_type,
            r.relationship_type,
            t.name AS target,
            t.entity_type AS target_type,
            LEFT(COALESCE(r.properties->>'description', ''), 120) AS description
        FROM cortex_relationships r
        JOIN cortex_entities s ON r.source_entity_id = s.id
        JOIN cortex_entities t ON r.target_entity_id = t.id
        WHERE r.project = $1
          AND (
              r.source_entity_id IN (SELECT id FROM matched)
              OR r.target_entity_id IN (SELECT id FROM matched)
          )
        ORDER BY source, relationship_type, target
        LIMIT $3
        """,
        project,
        [UUID(row_id) for row_id in matched_ids],
        limit,
    )
    return [dict(row) for row in rows]


async def graph_stats(conn: asyncpg.Connection, project: str) -> dict[str, Any]:
    # Compute each source's total and extraction backlog in the same table pass.
    row = await conn.fetchrow(
        """
        WITH entity_stats AS (
            SELECT COUNT(*)::int AS entity_count
              FROM cortex_entities e
             WHERE e.project = $1
        ),
        relationship_stats AS (
            SELECT COUNT(*)::int AS relationship_count
              FROM cortex_relationships r
             WHERE r.project = $1
        ),
        decision_stats AS (
            SELECT COUNT(*)::int AS decision_count,
                   (COUNT(*) FILTER (
                       WHERE d.summary IS NOT NULL
                         AND LENGTH(d.summary) > 10
                         AND COALESCE(d.metadata->>'entities_extracted', 'false') <> 'true'
                   ))::int AS decision_backlog
              FROM decisions d
             WHERE d.project = $1
               AND d.invalidated_at IS NULL
        ),
        lesson_stats AS (
            SELECT COUNT(*)::int AS lesson_count,
                   (COUNT(*) FILTER (
                       WHERE LENGTH(CONCAT_WS(E'\n\n', l.summary, l.detail, l.code_right, l.code_wrong)) > 10
                         AND COALESCE(l.metadata->>'entities_extracted', 'false') <> 'true'
                   ))::int AS lesson_backlog
              FROM lessons l
             WHERE l.project = $1
               AND l.invalidated_at IS NULL
        ),
        knowledge_stats AS (
            SELECT COUNT(*)::int AS knowledge_count,
                   (COUNT(*) FILTER (
                       WHERE k.content IS NOT NULL
                         AND LENGTH(k.content) > 10
                         AND COALESCE(k.metadata->>'entities_extracted', 'false') <> 'true'
                   ))::int AS knowledge_backlog
              FROM knowledge k
             WHERE k.project = $1
        ),
        work_product_stats AS (
            SELECT COUNT(*)::int AS work_product_count,
                   (COUNT(*) FILTER (
                       WHERE LENGTH(CONCAT_WS(E'\n\n', w.title, w.summary, w.behavior_summary, w.architecture_notes)) > 10
                         AND COALESCE(w.metadata->>'entities_extracted', 'false') <> 'true'
                   ))::int AS work_product_backlog
              FROM work_products w
             WHERE w.project = $1
               AND w.invalidated_at IS NULL
        )
        SELECT
            e.entity_count,
            r.relationship_count,
            d.decision_count,
            l.lesson_count,
            k.knowledge_count,
            w.work_product_count,
            d.decision_backlog,
            l.lesson_backlog,
            k.knowledge_backlog,
            w.work_product_backlog
          FROM entity_stats e
          CROSS JOIN relationship_stats r
          CROSS JOIN decision_stats d
          CROSS JOIN lesson_stats l
          CROSS JOIN knowledge_stats k
          CROSS JOIN work_product_stats w
        """,
        project,
    )
    return {
        "entity_count": row["entity_count"] if row else 0,
        "relationship_count": row["relationship_count"] if row else 0,
        "source_counts": {
            "decisions": row["decision_count"] if row else 0,
            "lessons": row["lesson_count"] if row else 0,
            "knowledge": row["knowledge_count"] if row else 0,
            "work_products": row["work_product_count"] if row else 0,
        },
        "backlog": {
            "decisions": row["decision_backlog"] if row else 0,
            "lessons": row["lesson_backlog"] if row else 0,
            "knowledge": row["knowledge_backlog"] if row else 0,
            "work_products": row["work_product_backlog"] if row else 0,
        },
    }


def _count_rows(rows: list[Any], key: str, count_key: str = "count") -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        name = compact_text(_record_value(row, key) or "unknown")
        counts[name] = counts.get(name, 0) + int(_record_value(row, count_key, 0) or 0)
    return counts


def _projection_status_row(row: Any) -> dict[str, Any]:
    data = {
        "id": str(_record_value(row, "id", "")),
        "title": compact_text(_record_value(row, "title")),
        "projection_status": compact_text(_record_value(row, "projection_status") or "unknown"),
        "projection_error": compact_text(_record_value(row, "projection_error")),
        "freshness_status": compact_text(_record_value(row, "freshness_status") or "unknown"),
        "freshness_reason": compact_text(_record_value(row, "freshness_reason")),
        "updated_at": compact_text(_record_value(row, "updated_at")),
    }
    return data


def _embedding_job_status_row(row: Any) -> dict[str, Any]:
    return {
        "id": str(_record_value(row, "id", "")),
        "table": compact_text(_record_value(row, "table_name")),
        "status": compact_text(_record_value(row, "status") or "unknown"),
        "processed": int(_record_value(row, "processed", 0) or 0),
        "embedded": int(_record_value(row, "embedded", 0) or 0),
        "errors": int(_record_value(row, "errors", 0) or 0),
        "skipped": int(_record_value(row, "skipped", 0) or 0),
        "created_at": compact_text(_record_value(row, "created_at")),
        "updated_at": compact_text(_record_value(row, "updated_at")),
    }


def _graph_build_job_status_row(row: Any) -> dict[str, Any]:
    embed_value = _record_value(row, "embed", True)
    return {
        "id": str(_record_value(row, "id", "")),
        "repo": compact_text(_record_value(row, "repo")),
        "status": compact_text(_record_value(row, "status") or "unknown"),
        "full": bool(_record_value(row, "full", False) or False),
        "embed": bool(embed_value if embed_value is not None else True),
        "error": compact_text(_record_value(row, "error")),
        "created_at": compact_text(_record_value(row, "created_at")),
        "updated_at": compact_text(_record_value(row, "updated_at")),
    }


async def projection_status_snapshot(
    conn: asyncpg.Connection,
    project: str,
    *,
    recent_limit: int = 10,
) -> dict[str, Any]:
    """Read-only operator projection status across Cortex projection surfaces."""

    await ensure_graph_schema(conn)
    await ensure_work_products_schema(conn)
    await ensure_embedding_backfill_jobs_schema()
    await ensure_graph_build_jobs_schema()

    graph = await graph_stats(conn, project)
    graph_backlog = json_object(graph.get("backlog"))

    work_status_rows = await conn.fetch(
        """
        SELECT COALESCE(projection_status, 'unknown') AS projection_status,
               COALESCE(freshness_status, 'unknown') AS freshness_status,
               COUNT(*)::int AS count
          FROM work_products
         WHERE project = $1
           AND invalidated_at IS NULL
         GROUP BY COALESCE(projection_status, 'unknown'),
                  COALESCE(freshness_status, 'unknown')
         ORDER BY count DESC
        """,
        project,
    )
    recent_work_rows = await conn.fetch(
        """
        SELECT id::text AS id, title, projection_status, projection_error,
               freshness_status, freshness_reason, updated_at
          FROM work_products
         WHERE project = $1
           AND invalidated_at IS NULL
           AND (
                COALESCE(projection_status, 'unknown') <> 'projected'
                OR COALESCE(freshness_status, 'unknown') <> 'current'
           )
         ORDER BY updated_at DESC
         LIMIT $2
        """,
        project,
        max(1, min(int(recent_limit or 10), 50)),
    )

    embedding_job_rows = await conn.fetch(
        """
        SELECT status, COUNT(*)::int AS count
          FROM embedding_backfill_jobs
         WHERE project = $1
         GROUP BY status
         ORDER BY status
        """,
        project,
    )
    recent_job_rows = await conn.fetch(
        """
        SELECT id::text AS id, table_name, status, processed, embedded, errors,
               skipped, created_at, updated_at
          FROM embedding_backfill_jobs
         WHERE project = $1
         ORDER BY created_at DESC
         LIMIT $2
        """,
        project,
        max(1, min(int(recent_limit or 10), 50)),
    )
    graph_job_rows = await conn.fetch(
        """
        SELECT status, COUNT(*)::int AS count
          FROM graph_build_jobs
         WHERE project = $1
         GROUP BY status
         ORDER BY status
        """,
        project,
    )
    recent_graph_job_rows = await conn.fetch(
        """
        SELECT id::text AS id, repo, status, full, embed, error, created_at,
               updated_at
          FROM graph_build_jobs
         WHERE project = $1
         ORDER BY created_at DESC
         LIMIT $2
        """,
        project,
        max(1, min(int(recent_limit or 10), 50)),
    )

    return {
        "project": project,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "graph": {
            "entity_count": int(graph.get("entity_count") or 0),
            "relationship_count": int(graph.get("relationship_count") or 0),
            "backlog": graph_backlog,
            "total_backlog": sum(int(v or 0) for v in graph_backlog.values()),
        },
        "work_products": {
            "projection_status": _count_rows(work_status_rows, "projection_status"),
            "freshness_status": _count_rows(work_status_rows, "freshness_status"),
            "attention": [_projection_status_row(row) for row in recent_work_rows],
        },
        "embedding_jobs": {
            "status": _count_rows(embedding_job_rows, "status"),
            "recent": [_embedding_job_status_row(row) for row in recent_job_rows],
        },
        "graph_build_jobs": {
            "status": _count_rows(graph_job_rows, "status"),
            "recent": [_graph_build_job_status_row(row) for row in recent_graph_job_rows],
        },
        "boot": {
            "schema_version": "cortex.boot_context.v1",
            "metadata_path": "persona.metadata.boot_context",
        },
    }


def graph_add_entity(
    entities: list[dict[str, str]],
    seen: set[tuple[str, str]],
    name: Any,
    entity_type: str,
    description: Any,
) -> None:
    normalized_name = graph_normalize_name(name)
    normalized_type = graph_normalize_type(entity_type)
    if not normalized_name:
        return
    key = (normalized_name.lower(), normalized_type)
    if key in seen:
        return
    seen.add(key)
    entities.append(
        {
            "name": normalized_name,
            "type": normalized_type,
            "description": graph_clean_description(description),
        }
    )


def graph_add_relationship(
    relationships: list[dict[str, str]],
    source: Any,
    target: Any,
    relationship_type: str,
    description: Any,
) -> None:
    source_name = graph_normalize_name(source)
    target_name = graph_normalize_name(target)
    if not source_name or not target_name or source_name == target_name:
        return
    rel_type = graph_normalize_relationship_type(relationship_type)
    if any(
        relationship.get("source") == source_name
        and relationship.get("target") == target_name
        and relationship.get("type") == rel_type
        for relationship in relationships
    ):
        return
    relationships.append(
        {
            "source": source_name,
            "target": target_name,
            "type": rel_type,
            "description": graph_clean_description(description),
        }
    )


def graph_domain_entity_type(name: str) -> str:
    lowered = name.lower()
    if lowered.endswith((" service", " api", " backend", " frontend", " portal", " connector")):
        return "service"
    if lowered.endswith((" platform", " application", " system")):
        return "product"
    if lowered.endswith(" project"):
        return "project"
    return "concept"


def graph_trim_phrase(value: str) -> str:
    words = re.findall(r"[A-Za-z][A-Za-z0-9_+-]*", value)
    break_positions = [
        idx
        for idx, word in enumerate(words[:-1])
        if word.lower() in GRAPH_PHRASE_BREAKWORDS or word.lower() in {"and", "or"}
    ]
    if break_positions:
        words = words[break_positions[-1] + 1:]
    while words and words[0].lower() in GRAPH_PHRASE_STOPWORDS:
        words.pop(0)
    while words and words[-1].lower() in GRAPH_PHRASE_STOPWORDS:
        words.pop()
    if not words:
        return ""
    if len(words) > 5:
        words = words[-5:]
    phrase = " ".join(words)
    if len(phrase) < 3:
        return ""
    return phrase


def graph_phrase_is_noise(value: str) -> bool:
    words = re.findall(r"[A-Za-z][A-Za-z0-9_+-]*", value)
    if not words:
        return True
    lowered = [word.lower() for word in words]
    if lowered[0] in GRAPH_PHRASE_LEADING_NOISE:
        return True
    if lowered[0].endswith("'s") or lowered[0].endswith("s'"):
        return True
    if len(words) > 1 and any(word in GRAPH_PHRASE_STOPWORDS for word in lowered[1:-1]):
        return True
    # All-lowercase prose fragments need a strong domain suffix anchor. Title Case,
    # quoted phrases, and acronyms reach this path too, but this branch prevents
    # "should probably update the model" style sentences from becoming concepts.
    if (
        len(words) > 1
        and all(word == word.lower() for word in words)
        and lowered[-1].rstrip("s") not in GRAPH_DOMAIN_SUFFIXES
    ):
        return True
    return False


def graph_extract_domain_phrases(text: str) -> list[str]:
    """Extract project-domain terms from the row text itself.

    This is intentionally dictionary-light and package-agnostic: acronyms,
    Title Case terms, quoted/backticked terms, and noun phrases ending in generic
    product/data/business suffixes. It catches project-specific domain concepts
    only when the project knowledge actually contains them.
    """
    candidates: list[str] = []

    for token in re.findall(r"\b[A-Z][A-Z0-9]{1,9}\b", text):
        if token not in GRAPH_ACRONYM_DENYLIST:
            candidates.append(token)

    for quoted in re.findall(r"[`\"']([^`\"'\n]{3,80})[`\"']", text):
        phrase = graph_trim_phrase(quoted)
        if phrase:
            candidates.append(phrase)

    suffix = "|".join(re.escape(item) for item in GRAPH_DOMAIN_SUFFIXES)
    titled_suffix = re.compile(
        rf"\b([A-Z][A-Za-z0-9_+-]{{2,}}\s+(?:{suffix})s?)\b"
    )
    for phrase in titled_suffix.findall(text):
        trimmed = graph_trim_phrase(phrase)
        if trimmed:
            candidates.append(trimmed)

    title_pattern = r"\b(?:[A-Z][A-Za-z0-9_+-]{2,})(?:\s+[A-Z][A-Za-z0-9_+-]{2,}){1,4}\b"
    for phrase in re.findall(title_pattern, text):
        trimmed = graph_trim_phrase(phrase)
        if (
            trimmed
            and len(trimmed.split()) > 1
            # Historical brand names stay filtered without becoming active identifiers.
            and not re.fullmatch(
                r"(Cortex|Kaidera|Kaidera OS|EnGen(?: OS|AI)?|API|JSON|HTTP|HTTPS)",
                trimmed,
            )
        ):
            candidates.append(trimmed)

    noun_phrase = re.compile(
        rf"\b([A-Za-z][A-Za-z0-9_+-]*(?:\s+[A-Za-z][A-Za-z0-9_+-]*){{0,4}}\s+(?:{suffix})s?)\b",
        re.IGNORECASE,
    )
    for match in noun_phrase.findall(text):
        trimmed = graph_trim_phrase(match)
        if not trimmed:
            continue
        words = trimmed.split()
        if len(words) == 1 and words[0].lower() in GRAPH_DOMAIN_SUFFIXES:
            continue
        if graph_phrase_is_noise(trimmed):
            continue
        if any(word.lower() not in GRAPH_PHRASE_STOPWORDS for word in words):
            candidates.append(trimmed)

    seen: set[str] = set()
    out: list[str] = []
    for candidate in candidates:
        normalized = graph_normalize_name(candidate)
        key = normalized.lower()
        if not normalized or key in seen:
            continue
        # Drop sentence fragments that still look like instructions rather than
        # terms. The extractor is conservative because bad entities pollute the
        # graph more than missing one low-signal phrase.
        if len(normalized.split()) > 1 and graph_phrase_is_noise(normalized):
            continue
        seen.add(key)
        out.append(normalized)
        if len(out) >= 24:
            break
    return out


def deterministic_graph_extract(text: str) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    entities: list[dict[str, str]] = []
    relationships: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    for agent, project in sorted(
        set(re.findall(r"\b([a-z][a-z0-9_-]{1,63})@([a-z][a-z0-9_-]{1,63})\b", text, re.IGNORECASE))
    ):
        identity = f"{agent.lower()}@{project.lower()}"
        graph_add_entity(entities, seen, identity, "agent", "Agent identity referenced by Cortex memory")
        graph_add_entity(entities, seen, project.lower(), "project", "Project referenced by Cortex memory")
        graph_add_relationship(
            relationships,
            identity,
            project.lower(),
            "belongs_to",
            "Agent identity includes project scope",
        )

    for tool in sorted(set(re.findall(r"\bcortex-[a-z0-9-]+\b", text, re.IGNORECASE))):
        graph_add_entity(entities, seen, tool, "tool", "Cortex command or tool")

    file_pattern = (
        r"(?:(?:\.{1,2}|~)?/)?[A-Za-z0-9_.@+-]+"
        r"(?:/[A-Za-z0-9_.@+-]+)+"
        r"\.(?:py|sh|sql|md|json|ya?ml|toml|ts|tsx|js|jsx|css|html|excalidraw\.md)"
    )
    for file_ref in sorted(set(re.findall(file_pattern, text))):
        graph_add_entity(entities, seen, file_ref, "file", "File path referenced by Cortex memory")

    for endpoint in sorted(set(re.findall(r"/[A-Za-z0-9_./{}:-]{3,}", text))):
        endpoint = endpoint.rstrip(".,;)]")
        if "." in endpoint.rsplit("/", 1)[-1]:
            continue
        # Reject path NOISE: a real API route has >=2 segments, none purely
        # numeric, each with an alphabetic core >=2 chars. Drops bare/fragment
        # paths like /views, /hour, /90/365, /need: while keeping /api/chat,
        # /boot/{agent}, /beat/embeddings/backfill.
        segs = [s for s in endpoint.strip("/").split("/") if s]
        if len(segs) < 2:
            continue
        if any(
            s.isdigit() or ":" in s or len(s.strip("{}")) < 2 or not s.strip("{}")[:1].isalpha()
            for s in segs
        ):
            continue
        if all(re.fullmatch(r"[A-Z][A-Za-z0-9_+-]*", s.strip("{}")) for s in segs):
            continue
        graph_add_entity(entities, seen, endpoint[:200], "endpoint", "API endpoint referenced by Cortex memory")

    for table in (
        "work_products",
        "cortex_entities",
        "cortex_relationships",
        "knowledge",
        "messages",
        "handoffs",
        "team_events",
    ):
        if re.search(rf"\b{re.escape(table)}\b", text, re.IGNORECASE):
            graph_add_entity(entities, seen, table, "table", "Database table referenced by Cortex memory")

    concept_patterns = {
        "Work Product Memory": r"\bwork product memory\b|\bwork_products\b",
        "Cortex": r"\bcortex\b",
        "Knowledge graph": r"\bknowledge graph\b|\bgraph memory\b",
        "Vector memory": r"\bvector memory\b|\bembedding",
        "Freshness tracking": r"\bfreshness\b|\bstale\b",
        "Provenance": r"\bprovenance\b|\bsource_event_id\b",
        "pgvector": r"\bpgvector\b",
        "pg_trgm": r"\bpg_trgm\b",
    }
    for name, pattern in concept_patterns.items():
        if re.search(pattern, text, re.IGNORECASE):
            graph_add_entity(entities, seen, name, "concept", "Concept referenced by Cortex memory")

    domain_entities: list[str] = []
    for phrase in graph_extract_domain_phrases(text):
        entity_type = graph_domain_entity_type(phrase)
        graph_add_entity(entities, seen, phrase, entity_type, "Domain term extracted from project knowledge")
        domain_entities.append(phrase)

    # A row with no extractable entities contributes nothing to the graph. The
    # old per-row "Cortex memory row <digest>" fallback was pure noise that the
    # search-centred graph tab could never surface meaningfully.

    if len(domain_entities) > 1:
        source = domain_entities[0]
        for target in domain_entities[1:20]:
            graph_add_relationship(
                relationships,
                source,
                target,
                "relates_to",
                "Domain concepts co-mentioned in one Cortex memory row",
            )

    if len(entities) > 1:
        source = entities[0]["name"]
        for target in entities[1:20]:
            graph_add_relationship(
                relationships,
                source,
                target["name"],
                "references",
                "Entities co-mentioned in one Cortex memory row",
            )

    return entities[:40], relationships[:80]


def graph_json_from_llm_text(text: str) -> dict[str, Any]:
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1]
    if cleaned.endswith("```"):
        cleaned = cleaned.rsplit("```", 1)[0]
    cleaned = cleaned.strip()
    return json.loads(cleaned)


def graph_sanitize_llm_payload(payload: Any) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    if not isinstance(payload, dict):
        return [], []
    entities: list[dict[str, str]] = []
    relationships: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for entity in payload.get("entities") or []:
        if not isinstance(entity, dict):
            continue
        graph_add_entity(
            entities,
            seen,
            entity.get("name"),
            entity.get("type") or graph_domain_entity_type(str(entity.get("name") or "")),
            entity.get("description") or "Domain entity extracted by graph LLM",
        )
        if len(entities) >= 30:
            break

    entity_names = {entity["name"].lower(): entity["name"] for entity in entities}
    for relationship in payload.get("relationships") or []:
        if not isinstance(relationship, dict):
            continue
        source = entity_names.get(graph_normalize_name(relationship.get("source")).lower())
        target = entity_names.get(graph_normalize_name(relationship.get("target")).lower())
        if not source or not target:
            continue
        graph_add_relationship(
            relationships,
            source,
            target,
            relationship.get("type") or "relates_to",
            relationship.get("description") or "Relationship extracted by graph LLM",
        )
        if len(relationships) >= 60:
            break
    return entities, relationships


async def llm_graph_extract(
    text: str,
    *,
    config: dict[str, Any],
    model_override: str | None = None,
) -> tuple[list[dict[str, str]], list[dict[str, str]], str]:
    provider = str(config.get("analysis_provider") or ANALYSIS_PROVIDER or "openrouter").strip().lower()
    model = str(
        model_override
        or config.get("analysis_model")
        or ANALYSIS_MODEL
        or (ANALYSIS_FALLBACK_MODELS[0] if ANALYSIS_FALLBACK_MODELS else "")
    ).strip()
    if not provider or not model:
        return [], [], ""

    prompt = (
        "Extract a compact knowledge graph from this project memory row. "
        "Return ONLY JSON with keys entities and relationships. "
        "entities: [{name,type,description}] where type is one of "
        f"{sorted(GRAPH_ENTITY_TYPES)}. relationships: "
        "[{source,target,type,description}] where source/target match entity names "
        f"and type is one of {sorted(GRAPH_RELATIONSHIP_TYPES)}. "
        "Prefer domain concepts, systems, products, services, datasets, metrics, "
        "dashboards, workflows, roles, and explicit technologies. Ignore filler, "
        "generic row ids, operational chatter, and URL path fragments unless they "
        "are real API endpoints.\n\n"
        f"TEXT:\n{compact_text(text, limit=6000)}"
    )

    try:
        provider_key = await resolve_provider_key(provider)
        if not provider_key:
            return [], [], f"{provider}:{model}:unavailable"
        async with httpx.AsyncClient(timeout=_provider_timeout(config, "analysis")) as client:
            if provider == "anthropic":
                resp = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": provider_key,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json={
                        "model": model,
                        "max_tokens": 900,
                        "messages": [{"role": "user", "content": prompt}],
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                content = "".join(
                    block.get("text", "")
                    for block in data.get("content", [])
                    if isinstance(block, dict) and block.get("type") == "text"
                )
            else:
                if provider == "openai":
                    url = "https://api.openai.com/v1/chat/completions"
                elif provider == "nvidia":
                    url = "https://integrate.api.nvidia.com/v1/chat/completions"
                else:
                    url = "https://openrouter.ai/api/v1/chat/completions"
                resp = await client.post(
                    url,
                    headers={"Authorization": f"Bearer {provider_key}", "Content-Type": "application/json"},
                    json={
                        "model": model,
                        "messages": [{"role": "user", "content": prompt}],
                        "max_tokens": 900,
                        "temperature": 0,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        entities, relationships = graph_sanitize_llm_payload(graph_json_from_llm_text(content))
        return entities, relationships, f"{provider}:{model}" if entities else f"{provider}:{model}:empty"
    except Exception:
        return [], [], f"{provider}:{model}:error"


def merge_graph_payloads(
    base_entities: list[dict[str, str]],
    base_relationships: list[dict[str, str]],
    extra_entities: list[dict[str, str]],
    extra_relationships: list[dict[str, str]],
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    entities: list[dict[str, str]] = []
    relationships: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for entity in [*base_entities, *extra_entities]:
        graph_add_entity(
            entities,
            seen,
            entity.get("name"),
            entity.get("type"),
            entity.get("description"),
        )
    names = {entity["name"].lower() for entity in entities}
    for relationship in [*base_relationships, *extra_relationships]:
        if relationship.get("source", "").lower() not in names:
            continue
        if relationship.get("target", "").lower() not in names:
            continue
        graph_add_relationship(
            relationships,
            relationship.get("source"),
            relationship.get("target"),
            relationship.get("type"),
            relationship.get("description"),
        )
    return entities[:60], relationships[:120]


def work_product_graph_extract(row: dict[str, Any]) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    entities, relationships = deterministic_graph_extract(
        work_product_memory_text(
            {
                "title": row.get("title"),
                "activity_type": row.get("activity_type"),
                "status": row.get("status"),
                "summary": row.get("summary"),
                "behavior_summary": row.get("behavior_summary"),
                "architecture_notes": row.get("architecture_notes"),
                "files_changed": row.get("files_changed"),
                "symbols_changed": row.get("symbols_changed"),
                "subject_entities": row.get("subject_entities"),
                "artifact_refs": row.get("artifact_refs"),
                "tests_run": row.get("tests_run"),
                "risks": row.get("risks"),
                "followups": row.get("followups"),
            }
        )
    )
    seen = {(entity["name"].lower(), entity["type"]) for entity in entities}
    title = graph_normalize_name(row.get("title") or f"Work product {str(row.get('id', ''))[:8]}")
    graph_add_entity(entities, seen, title, "work_product", row.get("summary") or "Cortex work product")

    if row.get("agent_name"):
        agent = strip_compound_suffix(row["agent_name"])
        graph_add_entity(entities, seen, agent, "agent", "Agent that recorded the work product")
        graph_add_relationship(relationships, agent, title, "documents", "Agent recorded this work product")

    for file_ref in normalize_text_list(row.get("files_changed")):
        graph_add_entity(entities, seen, file_ref, "file", "File changed by this work product")
        graph_add_relationship(relationships, title, file_ref, "modifies", "Work product changed this file")

    for symbol in normalize_text_list(row.get("symbols_changed")):
        graph_add_entity(entities, seen, symbol, "concept", "Symbol changed by this work product")
        graph_add_relationship(relationships, title, symbol, "implements", "Work product changed this symbol")

    for subject in normalize_text_list(row.get("subject_entities")):
        graph_add_entity(entities, seen, subject, "concept", "Subject captured by this work product")
        graph_add_relationship(relationships, title, subject, "documents", "Work product documents this subject")

    for artifact in normalize_text_list(row.get("artifact_refs")):
        graph_add_entity(entities, seen, artifact, "concept", "Artifact referenced by this work product")
        graph_add_relationship(relationships, title, artifact, "references", "Work product references this artifact")

    return entities[:60], relationships[:120]


async def upsert_graph_entity(
    conn: asyncpg.Connection,
    *,
    project: str,
    entity: dict[str, str],
    source_table: str,
    source_id: str,
    source_event_id: int | None = None,
) -> dict[str, Any]:
    source_ref = {
        "table": source_table,
        "id": source_id,
    }
    if source_event_id is not None:
        source_ref["event_id"] = source_event_id
    properties = {
        "description": entity.get("description") or "",
        "last_source_table": source_table,
        "last_source_ref": source_id,
        "source_refs": [source_ref],
    }
    row = await conn.fetchrow(
        """
        INSERT INTO cortex_entities (project, name, entity_type, properties)
        VALUES ($1, $2, $3, $4::jsonb)
        ON CONFLICT (project, name, entity_type) DO UPDATE SET
            properties = (
                COALESCE(cortex_entities.properties, '{}'::jsonb)
                || jsonb_build_object(
                    'description',
                    CASE
                        WHEN COALESCE(cortex_entities.properties->>'description', '') = ''
                        THEN EXCLUDED.properties->>'description'
                        ELSE cortex_entities.properties->>'description'
                    END,
                    'last_source_table', EXCLUDED.properties->>'last_source_table',
                    'last_source_ref', EXCLUDED.properties->>'last_source_ref',
                    'source_refs',
                    COALESCE(cortex_entities.properties->'source_refs', '[]'::jsonb)
                        || COALESCE(EXCLUDED.properties->'source_refs', '[]'::jsonb)
                )
            ),
            updated_at = NOW()
        RETURNING id::text AS id, name, entity_type
        """,
        project,
        entity["name"],
        graph_normalize_type(entity.get("type")),
        json.dumps(properties),
    )
    if not row:
        raise HTTPException(500, "graph entity upsert returned no row")
    return dict(row)


async def upsert_graph_relationship(
    conn: asyncpg.Connection,
    *,
    project: str,
    source: dict[str, Any],
    target: dict[str, Any],
    relationship: dict[str, str],
    source_table: str,
    source_id: str,
    source_event_id: int | None = None,
) -> None:
    source_ref = {
        "table": source_table,
        "id": source_id,
    }
    if source_event_id is not None:
        source_ref["event_id"] = source_event_id
    properties = {
        "description": relationship.get("description") or "",
        "last_source_table": source_table,
        "last_source_ref": source_id,
        "source_refs": [source_ref],
    }
    await conn.execute(
        """
        INSERT INTO cortex_relationships (
            project, source_entity_id, target_entity_id, relationship_type, properties
        )
        VALUES ($1, $2::uuid, $3::uuid, $4, $5::jsonb)
        ON CONFLICT (project, source_entity_id, target_entity_id, relationship_type)
        DO UPDATE SET
            properties = (
                COALESCE(cortex_relationships.properties, '{}'::jsonb)
                || jsonb_build_object(
                    'description',
                    CASE
                        WHEN COALESCE(cortex_relationships.properties->>'description', '') = ''
                        THEN EXCLUDED.properties->>'description'
                        ELSE cortex_relationships.properties->>'description'
                    END,
                    'last_source_table', EXCLUDED.properties->>'last_source_table',
                    'last_source_ref', EXCLUDED.properties->>'last_source_ref',
                    'source_refs',
                    COALESCE(cortex_relationships.properties->'source_refs', '[]'::jsonb)
                        || COALESCE(EXCLUDED.properties->'source_refs', '[]'::jsonb)
                )
            )
        """,
        project,
        source["id"],
        target["id"],
        graph_normalize_relationship_type(relationship.get("type")),
        json.dumps(properties),
    )


async def mark_graph_source_processed(
    conn: asyncpg.Connection,
    *,
    table: str,
    row_id: str,
    entity_count: int,
    relationship_count: int,
    model: str,
) -> None:
    if table not in GRAPH_SOURCES:
        raise HTTPException(400, f"unsupported graph source: {table}")
    if table == "work_products":
        await conn.execute(
            """
            UPDATE work_products
               SET projection_status = 'projected',
                   projection_error = NULL,
                   projected_at = NOW(),
                   metadata = COALESCE(metadata, '{}'::jsonb) || jsonb_build_object(
                    'entities_extracted', TRUE,
                    'entities_extracted_at', NOW(),
                    'entities_extracted_count', $2::int,
                    'relationships_extracted_count', $3::int,
                    'entity_extraction_model', $4::text
                   ),
                   updated_at = NOW()
             WHERE id = $1::uuid
            """,
            row_id,
            entity_count,
            relationship_count,
            model,
        )
        return
    await conn.execute(
        f"""
        UPDATE {table}
           SET metadata = COALESCE(metadata, '{{}}'::jsonb) || jsonb_build_object(
                    'entities_extracted', TRUE,
                    'entities_extracted_at', NOW(),
                    'entities_extracted_count', $2::int,
                    'relationships_extracted_count', $3::int,
                    'entity_extraction_model', $4::text
               )
         WHERE id = $1::uuid
        """,
        row_id,
        entity_count,
        relationship_count,
        model,
    )


async def project_graph_row(
    conn: asyncpg.Connection,
    *,
    project: str,
    row: dict[str, Any],
    source_event_id: int | None = None,
    use_llm: bool = False,
    platform_config: dict[str, Any] | None = None,
    model_override: str | None = None,
) -> dict[str, int | str]:
    source_table = str(row["source_table"])
    source_id = str(row["id"])
    extractor_model = "api-deterministic-domain-projector"
    text = str(row.get("content") or "")
    if source_table == "work_products":
        entities, relationships = work_product_graph_extract(row)
    else:
        entities, relationships = deterministic_graph_extract(text)
    if use_llm and text:
        llm_entities, llm_relationships, llm_model = await llm_graph_extract(
            text,
            config=platform_config or {},
            model_override=model_override,
        )
        if llm_entities:
            entities, relationships = merge_graph_payloads(
                entities,
                relationships,
                llm_entities,
                llm_relationships,
            )
            extractor_model = f"{extractor_model}+{llm_model}"
        elif llm_model:
            extractor_model = f"{extractor_model}+{llm_model}"

    canonical: dict[str, dict[str, Any]] = {}
    for entity in entities:
        record = await upsert_graph_entity(
            conn,
            project=project,
            entity=entity,
            source_table=source_table,
            source_id=source_id,
            source_event_id=source_event_id,
        )
        canonical[entity["name"].lower()] = record
        canonical[str(record["name"]).lower()] = record

    inserted_relationships = 0
    for relationship in relationships:
        source = canonical.get(relationship["source"].lower())
        target = canonical.get(relationship["target"].lower())
        if not source or not target:
            continue
        await upsert_graph_relationship(
            conn,
            project=project,
            source=source,
            target=target,
            relationship=relationship,
            source_table=source_table,
            source_id=source_id,
            source_event_id=source_event_id,
        )
        inserted_relationships += 1

    await mark_graph_source_processed(
        conn,
        table=source_table,
        row_id=source_id,
        entity_count=len(entities),
        relationship_count=inserted_relationships,
        model=extractor_model,
    )
    return {
        "source": source_table,
        "id": source_id,
        "entities": len(entities),
        "relationships": inserted_relationships,
        "extractor": extractor_model,
    }


async def fetch_graph_source_rows(
    conn: asyncpg.Connection,
    *,
    project: str,
    source: str,
    limit: int,
    reprocess: bool,
) -> list[dict[str, Any]]:
    tables = graph_source_tables(source)
    processed_filter = "TRUE" if reprocess else "COALESCE(metadata->>'entities_extracted', 'false') <> 'true'"
    parts: list[str] = []
    for table in tables:
        if table == "decisions":
            parts.append(
                f"""
                SELECT id::text,
                       'decisions' AS source_table,
                       LEFT(summary, 3000) AS content,
                       created_at AS sort_ts,
                       NULL::text AS title,
                       NULL::text AS agent_name,
                       NULL::text AS activity_type,
                       NULL::text AS status,
                       NULL::text AS summary,
                       NULL::text AS behavior_summary,
                       NULL::text AS architecture_notes,
                       ARRAY[]::text[] AS files_changed,
                       ARRAY[]::text[] AS symbols_changed,
                       ARRAY[]::text[] AS subject_entities,
                       ARRAY[]::text[] AS artifact_refs,
                       '[]'::jsonb AS tests_run,
                       ARRAY[]::text[] AS risks,
                       ARRAY[]::text[] AS followups,
                       NULL::bigint AS source_event_id
                  FROM decisions
                 WHERE project = $1
                   AND invalidated_at IS NULL
                   AND summary IS NOT NULL
                   AND LENGTH(summary) > 10
                   AND {processed_filter}
                """
            )
        elif table == "lessons":
            parts.append(
                f"""
                SELECT id::text,
                       'lessons' AS source_table,
                       LEFT(CONCAT_WS(E'\n\n', summary, detail, code_right, code_wrong), 3000) AS content,
                       created_at AS sort_ts,
                       NULL::text AS title,
                       NULL::text AS agent_name,
                       NULL::text AS activity_type,
                       NULL::text AS status,
                       summary,
                       NULL::text AS behavior_summary,
                       NULL::text AS architecture_notes,
                       ARRAY[]::text[] AS files_changed,
                       ARRAY[]::text[] AS symbols_changed,
                       ARRAY[]::text[] AS subject_entities,
                       ARRAY[]::text[] AS artifact_refs,
                       '[]'::jsonb AS tests_run,
                       ARRAY[]::text[] AS risks,
                       ARRAY[]::text[] AS followups,
                       NULL::bigint AS source_event_id
                  FROM lessons
                 WHERE project = $1
                   AND invalidated_at IS NULL
                   AND LENGTH(CONCAT_WS(E'\n\n', summary, detail, code_right, code_wrong)) > 10
                   AND {processed_filter}
                """
            )
        elif table == "knowledge":
            parts.append(
                f"""
                SELECT id::text,
                       'knowledge' AS source_table,
                       LEFT(content, 3000) AS content,
                       COALESCE(created_at, updated_at) AS sort_ts,
                       NULL::text AS title,
                       NULL::text AS agent_name,
                       NULL::text AS activity_type,
                       NULL::text AS status,
                       NULL::text AS summary,
                       NULL::text AS behavior_summary,
                       NULL::text AS architecture_notes,
                       ARRAY[]::text[] AS files_changed,
                       ARRAY[]::text[] AS symbols_changed,
                       ARRAY[]::text[] AS subject_entities,
                       ARRAY[]::text[] AS artifact_refs,
                       '[]'::jsonb AS tests_run,
                       ARRAY[]::text[] AS risks,
                       ARRAY[]::text[] AS followups,
                       NULL::bigint AS source_event_id
                  FROM knowledge
                 WHERE project = $1
                   AND content IS NOT NULL
                   AND LENGTH(content) > 10
                   AND {processed_filter}
                """
            )
        elif table == "work_products":
            parts.append(
                f"""
                SELECT id::text,
                       'work_products' AS source_table,
                       LEFT(CONCAT_WS(E'\n\n', title, summary, behavior_summary, architecture_notes), 3000) AS content,
                       updated_at AS sort_ts,
                       title,
                       agent_name,
                       activity_type,
                       status,
                       summary,
                       behavior_summary,
                       architecture_notes,
                       files_changed,
                       symbols_changed,
                       subject_entities,
                       artifact_refs,
                       tests_run,
                       risks,
                       followups,
                       source_event_id
                  FROM work_products
                 WHERE project = $1
                   AND invalidated_at IS NULL
                   AND LENGTH(CONCAT_WS(E'\n\n', title, summary, behavior_summary, architecture_notes)) > 10
                   AND {processed_filter}
                """
            )
    query = " UNION ALL ".join(parts)
    rows = await conn.fetch(
        f"SELECT * FROM ({query}) graph_rows ORDER BY sort_ts DESC LIMIT $2",
        project,
        limit,
    )
    return [dict(row) for row in rows]


async def resolve_unique_work_product(
    conn: asyncpg.Connection,
    *,
    project: str,
    work_product_id: str,
) -> dict[str, Any]:
    prefix = (work_product_id or "").strip()
    if not prefix:
        raise HTTPException(400, "work product id or prefix is required")
    rows = await conn.fetch(
        """SELECT *
             FROM work_products
            WHERE project = $1
              AND id::text LIKE $2 || '%'
            ORDER BY id::text
            LIMIT 2""",
        project,
        prefix,
    )
    if not rows:
        raise HTTPException(404, f"Work product {work_product_id} not found")
    if len(rows) > 1:
        raise HTTPException(
            409,
            f"Work product prefix {work_product_id} matched multiple rows; use the full UUID",
        )
    return work_product_row_to_dict(rows[0])


async def fetch_work_product_briefs(
    conn: asyncpg.Connection,
    project: str,
    *,
    query: str | None = None,
    file: str | None = None,
    symbol: str | None = None,
    handoff_uuid: str | None = None,
    status: str | None = "current",
    limit: int = 5,
) -> list[dict[str, Any]]:
    target = compact_text(query or file or symbol or handoff_uuid or "")
    rows = await conn.fetch(
        """
        WITH candidates AS (
            SELECT
                wp.*,
                (
                    CASE WHEN $3::uuid IS NOT NULL AND wp.handoff_id = $3::uuid THEN 8 ELSE 0 END
                    + CASE WHEN $4::text IS NOT NULL AND EXISTS (
                        SELECT 1 FROM unnest(COALESCE(wp.files_changed, '{}'::text[])) f
                         WHERE f = $4 OR f ILIKE '%' || $4 || '%'
                    ) THEN 4 ELSE 0 END
                    + CASE WHEN $5::text IS NOT NULL AND EXISTS (
                        SELECT 1 FROM unnest(COALESCE(wp.symbols_changed, '{}'::text[])) s
                         WHERE s = $5 OR s ILIKE '%' || $5 || '%'
                    ) THEN 4 ELSE 0 END
                    + CASE WHEN $2::text <> '' AND (
                        wp.title ILIKE '%' || $2 || '%'
                        OR wp.summary ILIKE '%' || $2 || '%'
                        OR COALESCE(wp.behavior_summary, '') ILIKE '%' || $2 || '%'
                        OR COALESCE(wp.architecture_notes, '') ILIKE '%' || $2 || '%'
                        OR EXISTS (
                            SELECT 1 FROM unnest(COALESCE(wp.subject_entities, '{}'::text[])) e
                             WHERE e ILIKE '%' || $2 || '%'
                        )
                        OR EXISTS (
                            SELECT 1 FROM unnest(COALESCE(wp.files_changed, '{}'::text[])) f
                             WHERE f ILIKE '%' || $2 || '%'
                        )
                        OR EXISTS (
                            SELECT 1 FROM unnest(COALESCE(wp.symbols_changed, '{}'::text[])) s
                             WHERE s ILIKE '%' || $2 || '%'
                        )
                    ) THEN 2 ELSE 0 END
                    + GREATEST(
                        similarity(LOWER(COALESCE(wp.title, '')), LOWER($2)),
                        similarity(LOWER(COALESCE(wp.summary, '')), LOWER($2)),
                        similarity(LOWER(COALESCE(wp.behavior_summary, '')), LOWER($2)),
                        similarity(LOWER(COALESCE(wp.architecture_notes, '')), LOWER($2))
                    )
                ) AS score
            FROM work_products wp
            WHERE wp.project = $1
              AND wp.invalidated_at IS NULL
              AND ($6::text IS NULL OR wp.status = $6)
              AND (
                    $2::text = ''
                    OR $3::uuid IS NOT NULL
                    OR $4::text IS NOT NULL
                    OR $5::text IS NOT NULL
                    OR wp.title ILIKE '%' || $2 || '%'
                    OR wp.summary ILIKE '%' || $2 || '%'
                    OR COALESCE(wp.behavior_summary, '') ILIKE '%' || $2 || '%'
                    OR COALESCE(wp.architecture_notes, '') ILIKE '%' || $2 || '%'
                    OR similarity(LOWER(COALESCE(wp.title, '')), LOWER($2)) > 0.1
                    OR similarity(LOWER(COALESCE(wp.summary, '')), LOWER($2)) > 0.1
                  )
        )
        SELECT *
          FROM candidates
         WHERE score > 0 OR $2::text = ''
         ORDER BY CASE status WHEN 'current' THEN 0 WHEN 'stale' THEN 1 ELSE 2 END,
                  score DESC,
                  updated_at DESC
         LIMIT $7
        """,
        project,
        target,
        handoff_uuid,
        file,
        symbol,
        status,
        max(1, min(int(limit or 5), 50)),
    )
    return [work_product_row_to_dict(row) for row in rows]


def _record_value(row: Any, key: str, default: Any = None) -> Any:
    try:
        return row[key]
    except Exception:
        try:
            return row.get(key, default)
        except Exception:
            return default


async def fetch_boot_work_product_briefs(
    conn: asyncpg.Connection,
    project: str,
    *,
    handoff_rows: list[Any],
    query: str | None = None,
    limit: int = 3,
) -> list[dict[str, Any]]:
    handoff_ids: list[UUID] = []
    files: list[str] = []
    summaries: list[str] = []

    for row in handoff_rows:
        raw_id = str(_record_value(row, "id", "") or "").split(":", 1)[0]
        with suppress(ValueError):
            handoff_ids.append(UUID(raw_id))
        files.extend(normalize_text_list(_record_value(row, "files_changed")))
        summary = compact_text(_record_value(row, "summary", ""), limit=120)
        if summary:
            summaries.append(summary)

    files = normalize_text_list(files)[:20]
    target = compact_text(query or " ".join(summaries[:3]), limit=240)
    if not handoff_ids and not files and not target:
        return []

    await ensure_work_products_schema(conn)
    rows = await conn.fetch(
        """
        WITH candidates AS (
            SELECT
                wp.*,
                (
                    CASE WHEN cardinality($2::uuid[]) > 0
                              AND wp.handoff_id = ANY($2::uuid[]) THEN 10 ELSE 0 END
                    + CASE WHEN cardinality($3::text[]) > 0
                              AND EXISTS (
                                  SELECT 1
                                    FROM unnest(COALESCE(wp.files_changed, '{}'::text[])) f
                                    JOIN unnest($3::text[]) scoped_file
                                      ON f = scoped_file
                                      OR f ILIKE '%' || scoped_file || '%'
                                      OR scoped_file ILIKE '%' || f || '%'
                              ) THEN 8 ELSE 0 END
                    + CASE WHEN $4::text <> '' AND (
                        wp.title ILIKE '%' || $4 || '%'
                        OR wp.summary ILIKE '%' || $4 || '%'
                        OR COALESCE(wp.behavior_summary, '') ILIKE '%' || $4 || '%'
                        OR COALESCE(wp.architecture_notes, '') ILIKE '%' || $4 || '%'
                        OR EXISTS (
                            SELECT 1 FROM unnest(COALESCE(wp.subject_entities, '{}'::text[])) e
                             WHERE e ILIKE '%' || $4 || '%'
                        )
                    ) THEN 3 ELSE 0 END
                    + GREATEST(
                        similarity(LOWER(COALESCE(wp.title, '')), LOWER($4)),
                        similarity(LOWER(COALESCE(wp.summary, '')), LOWER($4)),
                        similarity(LOWER(COALESCE(wp.behavior_summary, '')), LOWER($4))
                    )
                ) AS score
            FROM work_products wp
            WHERE wp.project = $1
              AND wp.status = 'current'
              AND wp.invalidated_at IS NULL
        )
        SELECT *
          FROM candidates
         WHERE score > 0
         ORDER BY score DESC, updated_at DESC
         LIMIT $5
        """,
        project,
        handoff_ids,
        files,
        target,
        max(1, min(int(limit or 3), 5)),
    )
    return [work_product_row_to_dict(row) for row in rows]


def build_boot_context_metadata(
    *,
    project: str,
    agent: str,
    project_info: dict[str, Any],
    profile: Any,
    generated_at: str,
    handoffs: list[Any],
    claimed_handoffs: list[Any],
    decisions: list[Any],
    lessons: list[Any],
    quality_decisions: list[Any],
    degraded: list[Any],
    boot_work_products: list[dict[str, Any]],
    topic_recall: dict[str, Any] | None,
) -> dict[str, Any]:
    """Structured provenance for /boot consumers.

    The boot text remains compact for agents, while this metadata gives the
    console/SDK enough evidence to explain where context came from and how fresh
    it is without inspecting SQL or generated prose.
    """

    work_products: list[dict[str, Any]] = []
    projection_counts: dict[str, int] = {}
    freshness_counts: dict[str, int] = {}
    for row in boot_work_products:
        projection_status = compact_text(row.get("projection_status") or "unknown")
        freshness_status = compact_text(row.get("freshness_status") or "unknown")
        projection_counts[projection_status] = projection_counts.get(projection_status, 0) + 1
        freshness_counts[freshness_status] = freshness_counts.get(freshness_status, 0) + 1
        work_products.append(
            {
                "id": str(row.get("id") or ""),
                "title": compact_text(row.get("title"), limit=120),
                "updated_at": compact_text(row.get("updated_at")),
                "freshness_status": freshness_status,
                "freshness_reason": compact_text(row.get("freshness_reason")),
                "freshness_checked_at": compact_text(row.get("freshness_checked_at")),
                "projection_status": projection_status,
                "projection_error": compact_text(row.get("projection_error")),
                "projected_at": compact_text(row.get("projected_at")),
                "files_changed": normalize_text_list(row.get("files_changed"))[:8],
            }
        )

    topic_results = (
        topic_recall.get("results", [])
        if isinstance(topic_recall, dict) and isinstance(topic_recall.get("results"), list)
        else []
    )

    return {
        "schema_version": "cortex.boot_context.v1",
        "project": project,
        "agent": agent,
        "agent_registered": bool(profile),
        "confidence": "high" if profile else "medium-unregistered-agent",
        "generated_at": generated_at,
        "source_boundary": "cortex-api scoped live read; no filesystem fallback",
        "project_registry": {
            "project_key": compact_text(project_info.get("project_key") or project),
            "project_id": compact_text(project_info.get("project_id")),
            "repo_type": compact_text(project_info.get("repo_type")),
            "status": compact_text(project_info.get("status") or "active"),
        },
        "sources": [
            {"section": "identity", "tables": ["agent_profiles", "agents"], "freshness": "live"},
            {"section": "roles", "tables": ["agent_profiles", "agents", "roles"], "freshness": "live"},
            {"section": "handoffs", "tables": ["handoffs"], "freshness": "live pending/claimed"},
            {"section": "decisions", "tables": ["decisions"], "freshness": "created_at >= now() - 7 days"},
            {"section": "lessons", "tables": ["lessons"], "freshness": "created_at >= now() - 14 days"},
            {"section": "quality", "tables": ["decisions", "pattern_metrics"], "freshness": "live"},
            {"section": "work_products", "tables": ["work_products"], "freshness": "current rows only"},
            {"section": "topic_recall", "tables": ["memory tables via execute_search"], "freshness": "query-time"},
        ],
        "counts": {
            "pending_handoffs": len(handoffs),
            "claimed_handoffs": len(claimed_handoffs),
            "decisions": len(decisions),
            "lessons": len(lessons),
            "quality_decisions": len(quality_decisions),
            "degradation_alerts": len(degraded),
            "work_product_briefs": len(work_products),
            "topic_recall_results": len(topic_results),
        },
        "freshness": {
            "handoffs": "live",
            "decisions": "7d window",
            "lessons": "14d window",
            "team_activity": "not included in /boot; use /bootstrap for text activity brief",
            "work_products": freshness_counts,
            "topic_recall": "query-time" if topic_recall else "not-requested",
        },
        "projections": {
            "work_products": projection_counts,
            "embedding_backlog": "operator-visible via /beat/embeddings/backlog",
        },
        "work_products": work_products,
    }


async def search_work_products(
    conn: asyncpg.Connection,
    project: str,
    query: str,
    *,
    room: str | None = None,
    limit: int = 8,
) -> list[dict[str, Any]]:
    rows = await fetch_work_product_briefs(
        conn,
        project,
        query=query,
        file=room,
        symbol=room,
        status="current",
        limit=limit,
    )
    results: list[dict[str, Any]] = []
    for row in rows:
        text = work_product_memory_text(row)
        meta_bits = []
        if row.get("handoff_id"):
            meta_bits.append(f"handoff={row['handoff_id']}")
        files = normalize_text_list(row.get("files_changed"))
        if files:
            meta_bits.append("files=" + ", ".join(files[:4]))
        tests = json_list(row.get("tests_run"))
        if tests:
            meta_bits.append(f"tests={len(tests)}")
        results.append(
            {
                "id": row.get("id"),
                "text": compact_text(text, limit=500),
                "meta": " ".join(meta_bits),
                "category": row.get("activity_type") or "work-product",
                "source": "work_products",
                "score": float(row.get("score") or 0.0) + 2.0,
                "tier": "work-product",
            }
        )
    return results


async def execute_search(
    conn: asyncpg.Connection,
    project: str,
    query: str,
    *,
    search_type: str = "all",
    rerank: bool = True,
    room: str | None = None,
    hall: str = "project",
    graph: bool = False,
    limit: int = 20,
) -> dict[str, Any]:
    hall = validate_search_hall(hall)
    room = room.strip() if isinstance(room, str) and room.strip() else None
    include_project_content = hall in {"project", "all"}
    include_graph = include_project_content and (graph or search_type == "all")
    include_artifacts = include_project_content and search_type in {"all", "artifacts"}

    def skip_table(table: str) -> bool:
        # Non-knowledge tables are project-scoped — skip them when project content is
        # excluded (e.g. hall='shared', which searches only the shared knowledge base).
        # One definition shared by the trigram + pgvector stages (was duplicated inline).
        return table != "knowledge" and not include_project_content

    tables = {
        "knowledge": (
            "content",
            "LEFT(content, 150), source_file, category",
        ),
        "decisions": (
            "summary",
            "LEFT(summary, 150), category, agent_name",
        ),
        "lessons": (
            "summary",
            "LEFT(summary, 150), category, agent_name",
        ),
    }

    if search_type != "all":
        tables = {key: value for key, value in tables.items() if key == search_type}

    results: list[dict[str, Any]] = []
    # Stages that hit the database deadline and returned nothing. A non-empty list
    # means these results are partial — surfaced to the caller as `degraded` rather
    # than silently passed off as a complete answer.
    degraded_stages: list[str] = []
    # The whole-request clock. Monotonic, so it cannot be skewed by a wall-clock
    # adjustment mid-request. Started here rather than per stage precisely because
    # the sum of the stages is the thing that was unbounded.
    deadline = time.monotonic() + (SEARCH_TOTAL_BUDGET_MS / 1000.0)

    async def stage_fetch(
        label: str, sql: str, *args: Any, optional: bool = False
    ) -> list[Any]:
        """Run one stage's query, degrading truthfully if the deadline kills it.

        Every stage in this function needs the same two-branch decision, and
        writing it inline is how the first pass got it wrong in both directions:
        the vector stage had no handler at all (a deadline kill escaped as a 500,
        against the incident's "zero 5xx" bar) while three stages caught bare
        `Exception` and dropped their table while still reporting `degraded=[]`.
        Routing every fetch through one helper makes truthful degradation the
        default that a newly added stage cannot silently opt out of.

        `optional=True` also tolerates a missing relation, which is the only thing
        those bare excepts were ever really for — deployments predating
        search_vector, the L5 artifacts schema, or work_products.

        It ALSO enforces the whole-request budget. A per-stage limit bounds each
        query and says nothing about their sum: seven stages inside a 3s limit
        still cost 24s, which is what marlow actually served. So each stage asks
        how much of the request's budget is left, skips outright when there is
        none, and clamps its own deadline to the remainder when that is tighter
        than the per-stage limit. A stage that never runs is reported `degraded`
        exactly like one the database killed — the caller is told the answer is
        partial either way, which is the whole point of that field.
        """
        remaining_ms = int((deadline - time.monotonic()) * 1000)
        if remaining_ms <= 0:
            # Budget spent. Do not open a query we have already promised not to wait for.
            degraded_stages.append(label)
            return []
        try:
            if remaining_ms < SEARCH_STATEMENT_TIMEOUT_MS:
                # Clamp so the LAST stage cannot overrun the request budget by up to
                # a full per-stage timeout. Safe to set on this connection: it is
                # scoped to the request and `acquire_scoped` resets it on release.
                await conn.execute(f"SET statement_timeout = {remaining_ms}")
            return await conn.fetch(sql, *args)
        except asyncpg.exceptions.QueryCanceledError:
            degraded_stages.append(label)
            return []
        except Exception:
            if optional:
                return []
            raise

    async def within_budget(label: str, operation: Any, fallback: Any) -> Any:
        """Run a NETWORK stage under the same request deadline as the DB stages.

        `stage_fetch` bounded the database and nothing else, which left the two
        provider calls — the query embedding and the reranker — outside the budget
        entirely. Their own ceilings are 15s and 2.5s, so a 4.5s request budget could
        still serve 22s. That is the identical mistake the total budget was added to
        fix, one layer out: bounding the parts and not the whole.

        `asyncio.wait_for` is safe here in a way it would NOT be for `stage_fetch` —
        these are cancellable HTTP calls, not a connection that would be abandoned
        mid-query. Degrading to `fallback` is honest: no embedding means the vector
        stage is skipped, no rerank means results keep their lexical order, and either
        way the caller is told the answer is partial.
        """
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            degraded_stages.append(label)
            return fallback
        try:
            return await asyncio.wait_for(operation(), timeout=remaining)
        except (TimeoutError, asyncio.TimeoutError):
            degraded_stages.append(label)
            return fallback

    def row_value(row: Any, key_or_index: str | int, default: Any = None) -> Any:
        if isinstance(key_or_index, str):
            try:
                return row[key_or_index]
            except Exception:
                return default
        try:
            return row[key_or_index]
        except Exception:
            return default

    # -------------------------------------------------------------------------
    # Stage -1: ID lookup
    # If query looks like a UUID or prefix, try exact lookup.
    # -------------------------------------------------------------------------
    if len(query) >= 8 and all(c in "0123456789abcdef-" for c in query.lower()):
        # messages timestamps live in `ts`, not `created_at`. The old uniform SELECT made
        # every messages probe raise -- and the broad except below swallowed it, so message
        # IDs were silently un-findable by exact-ID search with nothing degraded and nothing
        # logged (public-RC hold 12). The column is mapped per table and a failed probe now
        # reports itself in degraded[] instead of vanishing.
        exact_id_ts_column = {"messages": "ts", "knowledge": "updated_at"}
        for table in ["decisions", "lessons", "knowledge", "messages", "handoffs", "work_products"]:
            ts_column = exact_id_ts_column.get(table, "created_at")
            try:
                id_row = await conn.fetchrow(
                    f"SELECT id::text, project, {ts_column}::text AS created_at FROM {table} WHERE id::text ILIKE $1 LIMIT 1",
                    f"{query}%"
                )
                if id_row:
                    content_col = "content" if table in ["knowledge", "messages"] else "summary"
                    row = await conn.fetchrow(f"SELECT {content_col} FROM {table} WHERE id::text = $1", id_row["id"])
                    results.append({
                        "id": id_row["id"],
                        "text": f"[{table.upper()} MATCH] " + (row[0] if row else ""),
                        "meta": f"project={id_row['project']} created={id_row['created_at']}",
                        "category": "id-match",
                        "source": table,
                        "score": 10.0,
                        "tier": "id",
                    })
                    # Found exact match, can stop or continue.
            except Exception:
                degraded_stages.append(f"exact-id:{table}")

        # FAST-PATH (exact-ID lookup): a hex/UUID query that matched a real row id is the
        # documented cortex-search use case (exact handoff IDs / row IDs). Return now and
        # skip the BM25 + trigram + pgvector + graph + rerank sweep below — that full sweep
        # is what makes /search ~2s and OOM-kills the CLI on large projects. Exact-ID hits
        # are deterministic, so ranking adds nothing.
        if results:
            return {
                "query": query,
                "results": results[:limit],
                "reranked": False,
                "hall": hall,
                "room": room,
                "graph": include_graph,
                "degraded": degraded_stages,
            }

    # -------------------------------------------------------------------------
    # Stage -0.5: current work-product memory.
    # Completed work should be answered from canonical briefs before raw chat,
    # decisions, or full-code rediscovery.
    # -------------------------------------------------------------------------
    if include_project_content and search_type in {"all", "work_products"}:
        _stage_work_products_start = time.monotonic()
        try:
            results.extend(
                await search_work_products(
                    conn,
                    project,
                    query,
                    room=room,
                    limit=min(max(limit, 5), 12),
                )
            )
        except asyncpg.exceptions.QueryCanceledError:
            degraded_stages.append("work_products")
        except Exception:
            # Deployments before the work_products schema should keep search usable.
            pass
        SEARCH_STAGE_DURATION.labels(stage="work_products").observe(
            time.monotonic() - _stage_work_products_start
        )

    # -------------------------------------------------------------------------
    # Stage 0: BM25 full-text search (tsvector/tsquery)
    # Runs across all Cortex tables including captured_patterns and messages.
    # Results are added to the candidate pool before trigram/vector/rerank.
    # -------------------------------------------------------------------------
    _stage_bm25_start = time.monotonic()
    bm25_tables: list[tuple[str, str, str, str, list[Any]]] = [
        # (table_name, content_col, project_filter_sql, room_filter_sql, args_list)
        (
            "knowledge",
            "content",
            f"project IN ($2, '{SHARED_KNOWLEDGE_PROJECT}')" if hall == "all"
            else "project = $2",
            (
                "AND ($3::text IS NULL "
                "OR COALESCE(section, '') ILIKE '%' || $3 || '%' "
                "OR COALESCE(category, '') ILIKE '%' || $3 || '%' "
                "OR COALESCE(source_file, '') ILIKE '%' || $3 || '%' "
                f"OR LEFT(content, {TRGM_PREFIX_CHARS}) ILIKE '%' || $3 || '%')"
            ),
            [
                query,
                SHARED_KNOWLEDGE_PROJECT if hall == "shared" else (
                    LOCAL_STATE_PROJECT if hall not in ("project", "all", "shared") else project
                ),
                room,
            ],
        ),
    ]
    if include_project_content:
        bm25_tables.extend(
            [
                (
                    "decisions",
                    "summary",
                    "project = $2",
                    (
                        "AND ($3::text IS NULL "
                        "OR COALESCE(category, '') ILIKE '%' || $3 || '%' "
                        "OR COALESCE(agent_name, '') ILIKE '%' || $3 || '%' "
                        f"OR LEFT(summary, {TRGM_PREFIX_CHARS}) ILIKE '%' || $3 || '%')"
                    ),
                    [query, project, room],
                ),
                (
                    "lessons",
                    "summary",
                    "project = $2",
                    (
                        "AND ($3::text IS NULL "
                        "OR COALESCE(category, '') ILIKE '%' || $3 || '%' "
                        "OR COALESCE(agent_name, '') ILIKE '%' || $3 || '%' "
                        f"OR LEFT(summary, {TRGM_PREFIX_CHARS}) ILIKE '%' || $3 || '%')"
                    ),
                    [query, project, room],
                ),
                (
                    "captured_patterns",
                    "title",
                    "project = $2",
                    (
                        "AND ($3::text IS NULL "
                        "OR COALESCE(pattern_type, '') ILIKE '%' || $3 || '%' "
                        "OR title ILIKE '%' || $3 || '%' "
                        f"OR COALESCE(LEFT(description, {TRGM_PREFIX_CHARS}), '') ILIKE '%' || $3 || '%')"
                    ),
                    [query, project, room],
                ),
                (
                    "messages",
                    "content",
                    "project = $2",
                    f"AND ($3::text IS NULL OR LEFT(content, {TRGM_PREFIX_CHARS}) ILIKE '%' || $3 || '%')",
                    [query, project, room],
                ),
            ]
        )

    for bm25_table, content_col, pf_sql, room_sql, bm25_args in bm25_tables:
        if bm25_table == "messages":
            # @@ is not leakproof, so the cortex_app RLS barrier prevents the
            # planner from reaching idx_messages_search_vector and turns this
            # one stage into a full-table scan. The fixed SECURITY DEFINER query
            # validates $2 against this connection's cortex.project scope before
            # selecting anything, then fetches content only for the ten top IDs.
            bm25_sql = """SELECT id, display_text AS text, source, score
                             FROM public.cortex_search_messages_bm25($2, $1, $3)"""
        else:
            bm25_sql = f"""SELECT id::text, LEFT({content_col}, 300) AS text,
                       '{bm25_table}' AS source,
                       ts_rank_cd(search_vector, plainto_tsquery('english', $1)) AS score
                  FROM {bm25_table}
                 WHERE search_vector @@ plainto_tsquery('english', $1)
                   AND {pf_sql}
                   {room_sql}
                 ORDER BY score DESC
                 LIMIT 10"""
        bm25_rows = await stage_fetch(
            f"bm25:{bm25_table}",
            bm25_sql,
            *bm25_args,
            # The scoped messages bridge is a required installer migration. The
            # other tables remain optional for deployments predating search_vector.
            optional=bm25_table != "messages",
        )
        for row in bm25_rows:
            results.append(
                {
                    "id": row_value(row, "id"),
                    "text": row_value(row, "text", "") or "",
                    "meta": "",
                    "category": "",
                    "source": row_value(row, "source", bm25_table),
                    "score": float(row_value(row, "score", 0.0) or 0.0),
                    "tier": "bm25",
                }
            )

    SEARCH_STAGE_DURATION.labels(stage="bm25").observe(time.monotonic() - _stage_bm25_start)

    # -------------------------------------------------------------------------
    # Stage 0.5: L5 artifacts lexical/trigram search.
    # Artifacts do not currently have search_vector backfill, so they need an
    # explicit pass over the enrichment fields populated by ingestion.
    # -------------------------------------------------------------------------
    if include_artifacts:
        _stage_artifacts_start = time.monotonic()
        artifact_rows = await stage_fetch(
            "artifacts",
            """
                WITH artifact_candidates AS (
                    SELECT
                        id::text,
                        COALESCE(
                            NULLIF(caption, ''),
                            NULLIF(neighborhood_text, ''),
                            NULLIF(raw_content, ''),
                            NULLIF(section_context, ''),
                            source_file
                        ) AS text,
                        source_file,
                        COALESCE(NULLIF(modality, ''), 'artifact') AS category,
                        GREATEST(
                            similarity(LOWER(COALESCE(caption, '')), LOWER($1)),
                            similarity(LOWER(COALESCE(neighborhood_text, '')), LOWER($1)),
                            similarity(LOWER(COALESCE(raw_content, '')), LOWER($1)),
                            similarity(LOWER(COALESCE(section_context, '')), LOWER($1)),
                            similarity(LOWER(COALESCE(source_file, '')), LOWER($1))
                        ) AS score,
                        updated_at
                    FROM artifacts
                    WHERE project = $2
                      AND (
                          $3::text IS NULL
                          OR COALESCE(source_file, '') ILIKE '%' || $3 || '%'
                          OR COALESCE(modality, '') ILIKE '%' || $3 || '%'
                          OR COALESCE(source_type, '') ILIKE '%' || $3 || '%'
                          OR COALESCE(extraction_method, '') ILIKE '%' || $3 || '%'
                          OR COALESCE(section_context, '') ILIKE '%' || $3 || '%'
                          OR COALESCE(caption, '') ILIKE '%' || $3 || '%'
                          OR COALESCE(neighborhood_text, '') ILIKE '%' || $3 || '%'
                          OR COALESCE(raw_content, '') ILIKE '%' || $3 || '%'
                      )
                      AND (
                          COALESCE(source_file, '') ILIKE '%' || $1 || '%'
                          OR COALESCE(modality, '') ILIKE '%' || $1 || '%'
                          OR COALESCE(source_type, '') ILIKE '%' || $1 || '%'
                          OR COALESCE(extraction_method, '') ILIKE '%' || $1 || '%'
                          OR COALESCE(section_context, '') ILIKE '%' || $1 || '%'
                          OR COALESCE(caption, '') ILIKE '%' || $1 || '%'
                          OR COALESCE(neighborhood_text, '') ILIKE '%' || $1 || '%'
                          OR COALESCE(raw_content, '') ILIKE '%' || $1 || '%'
                          OR similarity(LOWER(COALESCE(caption, '')), LOWER($1)) > 0.1
                          OR similarity(LOWER(COALESCE(neighborhood_text, '')), LOWER($1)) > 0.1
                          OR similarity(LOWER(COALESCE(raw_content, '')), LOWER($1)) > 0.1
                          OR similarity(LOWER(COALESCE(section_context, '')), LOWER($1)) > 0.1
                          OR similarity(LOWER(COALESCE(source_file, '')), LOWER($1)) > 0.1
                      )
                )
                SELECT id, LEFT(text, 300) AS text, source_file, category, score
                  FROM artifact_candidates
                 ORDER BY score DESC, updated_at DESC
                 LIMIT 10
                """,
            query,
            project,
            room,
            optional=True,  # deployments before the L5 artifact schema
        )
        for row in artifact_rows:
            results.append(
                {
                    "id": row_value(row, "id"),
                    "text": row_value(row, "text", "") or "",
                    "meta": row_value(row, "source_file", "") or "",
                    "category": row_value(row, "category", "") or "",
                    "source": "artifacts",
                    "score": float(row_value(row, "score", 0.0) or 0.0),
                    "tier": "artifact",
                }
            )
        SEARCH_STAGE_DURATION.labels(stage="artifacts").observe(
            time.monotonic() - _stage_artifacts_start
        )

    # -------------------------------------------------------------------------
    # Stage 1: Trigram (pg_trgm) — bounded, index-backed substring recall.
    #
    # Incident 912073e2: this stage was 58,743s of the 70,510s /search spent on
    # marlow-aws (~36.7s per execution, every execution over 2.5s). It ran
    #     WHERE similarity(body, $1) > 0.1 OR body ILIKE '%'||$1||'%'
    #     ORDER BY similarity(body, $1) DESC
    # against knowledge.content / decisions.summary / lessons.summary.
    #
    # The trgm indexes were NOT missing — idx_knowledge_content_trgm and friends
    # exist in the deployed schema. The OR poisoned them. `similarity(col,$1) > 0.1`
    # is not an indexable predicate (only LIKE/ILIKE and the `%` operator can use
    # gin_trgm_ops), and a disjunction is only index-servable when EVERY branch is.
    # So one unindexable branch forced a sequential scan that computed trigrams over
    # every row's full body — while the perfectly good index sat unused beside it.
    # The ORDER BY then recomputed similarity across the whole match set before
    # LIMIT 10.
    #
    # Now: the ILIKE branch stands alone on a bare column, so the existing index
    # serves it, and similarity only ranks the handful of rows the index already
    # returned. Substring recall (exact IDs, incident strings) is preserved — that
    # is what ILIKE gave us; the similarity branch only ever added fuzzy matches
    # that BM25 in Stage 0 already covers on these same columns, via its own index.
    # -------------------------------------------------------------------------
    _stage_trigram_start = time.monotonic()
    # ILIKE '%x%' below 3 characters has no trigram to look up, so the planner
    # would fall straight back to the sequential scan this stage exists to avoid.
    trigram_eligible = len(query.strip()) >= TRGM_MIN_QUERY_LEN
    for table, (search_col, display_cols) in tables.items():
        if skip_table(table) or not trigram_eligible:
            continue
        if table == "knowledge":
            candidate_meta_col = "source_file"
            candidate_category_col = "category"
            knowledge_project = project
            if hall == "project":
                project_filter = "AND project = $2"
            elif hall == "shared":
                project_filter = (
                    "AND project = $2"
                    " AND category NOT IN ('claude-todo','claude-plan','claude-memory',"
                    "'claude-indexeddb-conversation','claude-indexeddb-source',"
                    "'claude-indexeddb-account','claude-indexeddb-draft')"
                )
                knowledge_project = SHARED_KNOWLEDGE_PROJECT
            elif hall == "all":
                project_filter = (
                    f"AND project IN ($2, '{SHARED_KNOWLEDGE_PROJECT}')"
                )
            else:
                project_filter = "AND project = $2"
                knowledge_project = LOCAL_STATE_PROJECT
            # The room filter's body-text ILIKE was a second unbounded body scan
            # hiding in this stage. It is AND'd as a group, so its internal ORs
            # never defeated the outer indexed predicate — but it still forced a
            # full-length ILIKE over every row that predicate returned. Bounding it
            # keeps the per-row work proportional to the prefix.
            #
            # ponytail: LEFT(col, N) here IS a recall cap on the ROOM FILTER, not
            # just a ranking cap — a row whose room token appears only past char N
            # now drops out of a room-filtered search. Accepted because room tokens
            # are short labels that appear early, and the dedicated section/
            # category/source_file branches above match them exactly. Raise
            # TRGM_PREFIX_CHARS if a real room lives deeper in body text.
            room_filter = f"""
                AND (
                    $3::text IS NULL
                    OR COALESCE(section, '') ILIKE '%' || $3 || '%'
                    OR COALESCE(category, '') ILIKE '%' || $3 || '%'
                    OR COALESCE(source_file, '') ILIKE '%' || $3 || '%'
                    OR LEFT(content, {TRGM_PREFIX_CHARS}) ILIKE '%' || $3 || '%'
                )
            """
            invalidation = ""
            args = (query, knowledge_project, room)
        else:
            candidate_meta_col = "category"
            candidate_category_col = "agent_name"
            project_filter = "AND project = $2"
            room_filter = f"""
                AND (
                    $3::text IS NULL
                    OR COALESCE(category, '') ILIKE '%' || $3 || '%'
                    OR COALESCE(agent_name, '') ILIKE '%' || $3 || '%'
                    OR LEFT(summary, {TRGM_PREFIX_CHARS}) ILIKE '%' || $3 || '%'
                )
            """
            invalidation = "AND invalidated_at IS NULL"
            args = (query, project, room)

        # The match column stays BARE. gin_trgm_ops indexes in the deployed schema
        # (idx_knowledge_content_trgm, idx_decisions_summary_trgm,
        # idx_lessons_summary_trgm) are on the plain columns, and the planner only
        # uses an expression index on an exact expression match — wrapping this in
        # LEFT()/LOWER() would silently reintroduce the sequential scan.
        # The index can still return a huge match set for common words. Cap that
        # set before applying similarity(): without this CTE, `handoff` matched
        # 47k decision rows in 13ms and then spent 2.3s heap-reading + ranking all
        # of them. Project only the small fields search consumes; SELECT * here
        # also materialized embeddings and erased most of the cap's benefit.
        # A 10x oversample (100 candidates for 10 results) measured ~8ms for the
        # worst common term on Marlow while the 1000-row cap still consumed ~57ms.
        if table == "decisions":
            # RLS is intentionally a security barrier. Because ILIKE is not
            # leakproof, PostgreSQL cannot push it below that barrier and the
            # cortex_app role otherwise seq-scans the full decisions table even
            # though idx_decisions_summary_trgm exists. This SECURITY DEFINER
            # function re-establishes the same session-project boundary inside a
            # fixed, non-dynamic query, allowing the owner plan to use the GIN
            # index without giving cortex_app any direct RLS bypass.
            trigram_rows = await stage_fetch(
                f"trigram:{table}",
                f"""SELECT id, display_text, meta, category, source
                       FROM cortex_search_decisions_trigram(
                           $2, $1, $3, {TRGM_CANDIDATE_LIMIT}
                       )""",
                *args,
            )
        else:
            trigram_rows = await stage_fetch(
                f"trigram:{table}",
                f"""WITH trigram_candidates AS MATERIALIZED (
                    SELECT id::text AS id,
                           LEFT({search_col}, 150) AS display_text,
                           {candidate_meta_col} AS meta,
                           {candidate_category_col} AS category,
                           LEFT({search_col}, {TRGM_PREFIX_CHARS}) AS rank_text
                      FROM {table}
                     WHERE {search_col} ILIKE '%' || $1 || '%'
                       {project_filter}
                       {invalidation}
                       {room_filter}
                     LIMIT {TRGM_CANDIDATE_LIMIT}
                 )
                 SELECT id, display_text, meta, category, '{table}' as source
                   FROM trigram_candidates
                  ORDER BY similarity(rank_text, $1) DESC
                  LIMIT 10""",
                *args,
            )
        for row in trigram_rows:
            results.append(
                {
                    "id": row_value(row, 0),
                    "text": row_value(row, 1, ""),
                    "meta": row_value(row, 2, "") or "",
                    "category": row_value(row, 3, "") or "",
                    "source": row_value(row, 4, table),
                    "tier": "trigram",
                }
            )

    SEARCH_STAGE_DURATION.labels(stage="trigram").observe(time.monotonic() - _stage_trigram_start)

    if search_type not in {"all", "artifacts"} and results and not include_graph:
        deduped_type: list[dict[str, Any]] = []
        seen_type: set[tuple[str, str, str, str]] = set()
        for item in results:
            key = (
                item.get("source", ""),
                item.get("text", ""),
                item.get("meta", ""),
                item.get("category", ""),
            )
            if key in seen_type:
                continue
            seen_type.add(key)
            deduped_type.append(item)
        return {
            "query": query,
            "results": deduped_type[:limit],
            "reranked": False,
            "hall": hall,
            "room": room,
            "graph": include_graph,
            "degraded": degraded_stages,
        }

    _stage_vector_start = time.monotonic()
    # The embedding provider's own ceiling is 15s — more than three times the whole
    # request budget — so it must answer to the deadline, not to itself.
    query_embedding = await within_budget(
        "embedding", lambda: embed_query_cached(query, project), None
    )
    if query_embedding:
        vec_str = "[" + ",".join(str(v) for v in query_embedding) + "]"
        if include_project_content and search_type in {"all", "work_products"}:
            work_vector_rows = await stage_fetch(
                "vector:work_products",
                """SELECT id::text,
                              LEFT(
                                  COALESCE(title, '') || E'\n' ||
                                  COALESCE(summary, '') || E'\n' ||
                                  COALESCE(behavior_summary, '') || E'\n' ||
                                  COALESCE(architecture_notes, ''),
                                  500
                              ) AS text,
                              files_changed,
                              activity_type,
                              ROUND((1 - (embedding <=> $1::vector))::numeric, 3) AS score
                         FROM work_products
                        WHERE project = $2
                          AND status = 'current'
                          AND invalidated_at IS NULL
                          AND embedding IS NOT NULL
                          AND (
                              $3::text IS NULL
                              OR EXISTS (
                                  SELECT 1 FROM unnest(COALESCE(files_changed, '{}'::text[])) f
                                   WHERE f ILIKE '%' || $3 || '%'
                              )
                              OR EXISTS (
                                  SELECT 1 FROM unnest(COALESCE(symbols_changed, '{}'::text[])) s
                                   WHERE s ILIKE '%' || $3 || '%'
                              )
                              OR EXISTS (
                                  SELECT 1 FROM unnest(COALESCE(subject_entities, '{}'::text[])) e
                                   WHERE e ILIKE '%' || $3 || '%'
                              )
                          )
                        ORDER BY embedding <=> $1::vector
                        LIMIT 8""",
                vec_str,
                project,
                room,
                optional=True,  # deployments before the work_products schema
            )
            for row in work_vector_rows:
                files = normalize_text_list(row_value(row, "files_changed"))
                results.append(
                    {
                        "id": row_value(row, "id"),
                        "text": row_value(row, "text", "") or "",
                        "meta": ("files=" + ", ".join(files[:4])) if files else "",
                        "category": row_value(row, "activity_type", "work-product") or "work-product",
                        "source": "work_products (semantic)",
                        "score": float(row_value(row, "score", 0) or 0) + 2.0,
                        "tier": "work-product-vector",
                    }
                )
        if include_artifacts:
            artifact_vector_rows = await stage_fetch(
                "vector:artifacts",
                """SELECT id::text,
                           LEFT(
                               COALESCE(NULLIF(caption, ''), NULLIF(raw_content, ''), source_file),
                               500
                           ) AS text,
                           source_file,
                           COALESCE(modality, 'artifact') AS category,
                           ROUND((1 - (embedding <=> $1::vector))::numeric, 3) AS score
                      FROM artifacts
                     WHERE project = $2
                       AND embedding IS NOT NULL
                       AND (
                           $3::text IS NULL
                           OR COALESCE(section_context, '') ILIKE '%' || $3 || '%'
                           OR COALESCE(source_file, '') ILIKE '%' || $3 || '%'
                           OR COALESCE(raw_content, '') ILIKE '%' || $3 || '%'
                           OR COALESCE(caption, '') ILIKE '%' || $3 || '%'
                       )
                     ORDER BY embedding <=> $1::vector
                     LIMIT 8""",
                vec_str,
                project,
                room,
                optional=True,
            )
            for row in artifact_vector_rows:
                results.append(
                    {
                        "id": row_value(row, "id"),
                        "text": row_value(row, "text", "") or "",
                        "meta": row_value(row, "source_file", "") or "",
                        "category": row_value(row, "category", "artifact") or "artifact",
                        "source": "artifacts (semantic)",
                        "score": float(row_value(row, "score", 0) or 0),
                        "tier": "artifact-vector",
                    }
                )
        for table, (search_col, display_cols) in tables.items():
            if skip_table(table):
                continue
            if table == "knowledge":
                knowledge_project = project
                if hall == "project":
                    project_filter = "AND project = $2"
                elif hall == "shared":
                    project_filter = (
                        "AND project = $2"
                        " AND category NOT IN ('claude-todo','claude-plan','claude-memory',"
                        "'claude-indexeddb-conversation','claude-indexeddb-source',"
                        "'claude-indexeddb-account','claude-indexeddb-draft')"
                    )
                    knowledge_project = SHARED_KNOWLEDGE_PROJECT
                elif hall == "all":
                    project_filter = (
                        f"AND project IN ($2, '{SHARED_KNOWLEDGE_PROJECT}')"
                    )
                else:
                    project_filter = "AND project = $2"
                    knowledge_project = LOCAL_STATE_PROJECT
                room_filter = f"""
                    AND (
                        $3::text IS NULL
                        OR COALESCE(section, '') ILIKE '%' || $3 || '%'
                        OR COALESCE(category, '') ILIKE '%' || $3 || '%'
                        OR COALESCE(source_file, '') ILIKE '%' || $3 || '%'
                        OR LEFT(content, {TRGM_PREFIX_CHARS}) ILIKE '%' || $3 || '%'
                    )
                """
                invalidation = ""
                args = (vec_str, knowledge_project, room)
            else:
                project_filter = "AND project = $2"
                room_filter = f"""
                    AND (
                        $3::text IS NULL
                        OR COALESCE(category, '') ILIKE '%' || $3 || '%'
                        OR COALESCE(agent_name, '') ILIKE '%' || $3 || '%'
                        OR LEFT(summary, {TRGM_PREFIX_CHARS}) ILIKE '%' || $3 || '%'
                    )
                """
                invalidation = "AND invalidated_at IS NULL"
                args = (vec_str, project, room)

            # A filtered HNSW scan under a selective project/room/invalidation
            # post-filter is exactly the shape that blows a 3s budget, so this is
            # reachable rather than theoretical — and before the budget existed it
            # could not fire here at all.
            vector_rows = await stage_fetch(
                f"vector:{table}",
                f"""SELECT id::text, {display_cols},
                           '{table} (semantic)' as source,
                           ROUND((1 - (embedding <=> {_VECTOR_CAST}))::numeric, 3) as score
                      FROM {table}
                     WHERE embedding IS NOT NULL
                       {project_filter}
                       {invalidation}
                       {room_filter}
                     ORDER BY embedding <=> {_VECTOR_CAST}
                     LIMIT 8""",
                *args,
            )
            for row in vector_rows:
                results.append(
                    {
                        "id": row_value(row, 0),
                        "text": row_value(row, 1, ""),
                        "meta": row_value(row, 2, "") or "",
                        "category": row_value(row, 3, "") or "",
                        "source": row_value(row, 4, f"{table} (semantic)"),
                        "score": float(row_value(row, 5, 0) or 0),
                        "tier": "vector",
                    }
                )

    SEARCH_STAGE_DURATION.labels(stage="vector").observe(time.monotonic() - _stage_vector_start)

    if include_graph:
        _stage_graph_start = time.monotonic()
        try:
            # The last unbounded await. It is a DATABASE call, so it gets the
            # skip-if-spent half of the contract and NOT asyncio.wait_for — cancelling
            # a query mid-flight is exactly what stage_fetch avoids.
            if deadline - time.monotonic() > 0:
                results.extend(await search_graph(conn, project, query, room))
            else:
                degraded_stages.append("graph")
        except asyncpg.exceptions.QueryCanceledError:
            degraded_stages.append("graph")
        SEARCH_STAGE_DURATION.labels(stage="graph").observe(time.monotonic() - _stage_graph_start)

    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for item in results:
        key = (
            item.get("source", ""),
            item.get("text", ""),
            item.get("meta", ""),
            item.get("category", ""),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    results = deduped

    # -------------------------------------------------------------------------
    # Quality penalty (applied BEFORE reranking)
    # Batch-fetch quality data for tables that track it, then penalise
    # candidates with a poor quality_score but enough selection history.
    # -------------------------------------------------------------------------
    _quality_tables = ("decisions", "lessons", "knowledge", "captured_patterns")
    quality_data: dict[str, tuple[float | None, int]] = {}
    for _qtable in _quality_tables:
        _table_ids = [
            r["id"]
            for r in results
            if r.get("source", "").split(" ")[0] == _qtable and r.get("id")
        ]
        if not _table_ids:
            continue
        _rows = await stage_fetch(
            f"quality:{_qtable}",
            f"SELECT id::text, quality_score, times_selected FROM {_qtable} "
            f"WHERE id = ANY($1::uuid[])",
            _table_ids,
        )
        for _row in _rows:
            quality_data[_row["id"]] = (
                _row["quality_score"],
                _row["times_selected"] or 0,
            )

    for r in results:
        qd = quality_data.get(r.get("id"))
        if not qd:
            continue
        qs, ts = qd
        if ts >= 3 and qs is not None:
            if qs < 0.2:
                r["score"] = r.get("score", 0) * 0.3
            elif qs < 0.5:
                r["score"] = r.get("score", 0) * 0.6

    _selection_tables = ("decisions", "lessons", "knowledge", "captured_patterns")

    async def _track_selections(final_results: list[dict[str, Any]]) -> None:
        # Selection counters are telemetry, and they run after the answer is
        # already assembled. Under the search budget a deadline here would throw
        # away a complete, correct result set over a statistics UPDATE — so a
        # cancelled counter degrades the request, it never fails it.
        try:
            selections_by_table: dict[str, set[str]] = {}
            for r in final_results:
                source_table = r.get("source", "").split(" ")[0]
                rid = r.get("id")
                if source_table in _selection_tables and rid:
                    selections_by_table.setdefault(source_table, set()).add(rid)
            for source_table, ids in selections_by_table.items():
                await conn.execute(
                    f"UPDATE {source_table} SET times_selected = COALESCE(times_selected, 0) + 1 "
                    f"WHERE id = ANY($1::uuid[])",
                    list(ids),
                )
        except asyncpg.exceptions.QueryCanceledError:
            degraded_stages.append("selection_tracking")

    if rerank and len(results) >= 2:
        _stage_rerank_start = time.monotonic()
        doc_results = [result for result in results if result.get("text")]
        docs = [result["text"] for result in doc_results]
        # Rerank is the last thing that runs and improves ORDER, not correctness, so
        # it is the right thing to drop when the budget is nearly spent.
        reranked = await within_budget(
            "rerank", lambda: rerank_results(query, docs), None
        )
        if reranked:
            rerank_output = []
            for item in reranked:
                index = item.get("index", 0)
                if index >= len(doc_results):
                    continue
                base = doc_results[index]
                rerank_output.append(
                    {
                        **base,
                        "relevance": item.get("relevance_score", 0),
                    }
                )
            rerank_output = rerank_output[:limit]
            await _track_selections(rerank_output)
            SEARCH_STAGE_DURATION.labels(stage="rerank").observe(time.monotonic() - _stage_rerank_start)
            return {
                "query": query,
                "results": rerank_output,
                "reranked": True,
                "hall": hall,
                "room": room,
                "graph": include_graph,
                "degraded": degraded_stages,
            }

    results = results[:limit]
    await _track_selections(results)
    return {
        "query": query,
        "results": results,
        "reranked": False,
        "hall": hall,
        "room": room,
        "graph": include_graph,
        "degraded": degraded_stages,
    }


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class LogRequest(BaseModel):
    event_type: str  # decision, lesson, commit, started, stopped, blocked
    summary: str
    category: Optional[str] = None
    importance: Optional[int] = 5
    files_affected: Optional[list[str]] = None
    metadata: Optional[dict] = None
    supersedes_id: Optional[str] = None
    supersession_summary: Optional[str] = None


def handoff_policy(value: Any) -> dict[str, Any]:
    """Canonical in-memory value for optional handoff policy JSON fields."""
    if value is None:
        return {}
    normalized = normalize_db_json(value)
    if not isinstance(normalized, dict):
        raise ValueError("handoff policy must be a JSON object")
    return dict(normalized)


def handoff_policy_db(value: dict[str, Any] | None) -> str:
    """Stable JSON string for asyncpg JSONB parameters and fingerprinting."""
    return json.dumps(handoff_policy(value), sort_keys=True)


class HandoffCreate(BaseModel):
    from_role: Optional[str] = None
    to_role: str
    to_agent: Optional[str] = None
    priority: Literal["low", "medium", "high", "urgent"] = "medium"
    summary: str
    branch: Optional[str] = None
    files_changed: Optional[list[str]] = None
    verification: Optional[str] = None
    next_steps: Optional[str] = None
    context: Optional[str] = None
    parent_goal_id: Optional[str] = None
    acceptance: dict[str, Any] = Field(default_factory=dict)
    evidence: dict[str, Any] = Field(default_factory=dict)
    retry: dict[str, Any] = Field(default_factory=dict)
    escalation: dict[str, Any] = Field(default_factory=dict)


class HandoffReturn(BaseModel):
    outcome: Literal["completed", "partial", "blocked"] = "completed"
    summary: str
    decision: Optional[Literal["accept", "rework"]] = None
    work_product_id: Optional[str] = None
    tests_run: list[Any] = Field(default_factory=list)
    artifacts: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    followups: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class CrossProjectHandoffCreate(HandoffCreate):
    target_project: str


class HandoffBudgetReserve(BaseModel):
    lane: str = "handoff"
    tick_id: Optional[str] = None
    input_tokens: int = 1200
    max_output_tokens: int = 1200
    reasoning_label: str = "handoff_claim"
    config: Optional[dict[str, Any]] = None
    usage: Optional[dict[str, Any]] = None


class HandoffClaimWithBudget(BaseModel):
    budget: HandoffBudgetReserve = Field(default_factory=HandoffBudgetReserve)


class SkillRegister(BaseModel):
    """Register (or upsert) a skill in the agent_skills registry.

    scope='global' is the shared-skills-repo channel: such rows are stored under
    the sentinel project '*' and reach every project/agent at boot without a
    binding. Any other scope is stored under the caller's X-Project.
    """

    skill_slug: str
    name: Optional[str] = None
    description: Optional[str] = None
    scope: str = "global"
    body_ref: Optional[str] = None
    body_hash: Optional[str] = None
    version: str = "1"
    skill_type: str = "capability"
    permission: Optional[str] = None
    trust_tier: Optional[str] = None
    metadata: Optional[dict[str, Any]] = None


class SkillBind(BaseModel):
    """Bind a skill to a role or a single agent for a project."""

    subject_kind: str = "role"
    subject: str
    project: Optional[str] = None
    binding_type: str = "include"
    priority: int = 50
    version_pin: Optional[str] = None


class WorkProductWrite(BaseModel):
    title: str
    summary: str
    handoff_id: Optional[str] = None
    agent_name: Optional[str] = None
    activity_type: Optional[str] = DEFAULT_WORK_PRODUCT_ACTIVITY
    status: Optional[str] = "current"
    behavior_summary: Optional[str] = None
    architecture_notes: Optional[str] = None
    files_changed: Optional[list[str]] = None
    symbols_changed: Optional[list[str]] = None
    subject_entities: Optional[list[str]] = None
    artifact_refs: Optional[list[str]] = None
    tests_run: Optional[list[Any]] = None
    risks: Optional[list[str]] = None
    followups: Optional[list[str]] = None
    approval_status: Optional[str] = None
    content_hash: Optional[str] = None
    commit_sha: Optional[str] = None
    file_hashes: Optional[dict[str, str]] = None
    supersedes_id: Optional[str] = None
    metadata: Optional[dict[str, Any]] = None


class WorkProductFreshnessRequest(BaseModel):
    work_product_id: Optional[str] = None
    limit: int = 100
    dry_run: bool = True
    current_file_hashes: Optional[dict[str, str]] = None
    treat_missing_as_stale: bool = False


HANDOFF_BUDGET_SCHEMA_VERSION = "cortex.handoff_budget.v1"
DEFAULT_HANDOFF_BUDGET_CONFIG = {
    "max_input_tokens": 12000,
    "max_output_tokens": 4000,
    "max_total_tokens": 16000,
    "max_cost_usd": 0.25,
    "input_cost_per_1k": 0.0008,
    "output_cost_per_1k": 0.004,
    "warn_fraction": 0.8,
    "reset_window_minutes": 60,
}


def _budget_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _budget_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _budget_money(value: float) -> float:
    return round(max(0.0, value), 6)


def _budget_one_line(value: Any, fallback: str = "") -> str:
    text = " ".join(str(value or "").split())
    return text or fallback


def _budget_ratio(used: float, limit: float) -> float:
    if limit <= 0:
        return 1.0 if used > 0 else 0.0
    return round(used / limit, 4)


def handoff_budget_config(value: dict[str, Any] | None) -> dict[str, Any]:
    data = value if isinstance(value, dict) else {}
    defaults = DEFAULT_HANDOFF_BUDGET_CONFIG
    return {
        "max_input_tokens": max(0, _budget_int(data.get("max_input_tokens"), defaults["max_input_tokens"])),
        "max_output_tokens": max(0, _budget_int(data.get("max_output_tokens"), defaults["max_output_tokens"])),
        "max_total_tokens": max(0, _budget_int(data.get("max_total_tokens"), defaults["max_total_tokens"])),
        "max_cost_usd": max(0.0, _budget_float(data.get("max_cost_usd"), defaults["max_cost_usd"])),
        "input_cost_per_1k": max(0.0, _budget_float(data.get("input_cost_per_1k"), defaults["input_cost_per_1k"])),
        "output_cost_per_1k": max(0.0, _budget_float(data.get("output_cost_per_1k"), defaults["output_cost_per_1k"])),
        "warn_fraction": min(1.0, max(0.0, _budget_float(data.get("warn_fraction"), defaults["warn_fraction"]))),
        "reset_window_minutes": max(1, _budget_int(data.get("reset_window_minutes"), defaults["reset_window_minutes"])),
    }


def handoff_budget_usage(value: dict[str, Any] | None) -> dict[str, Any]:
    data = value if isinstance(value, dict) else {}
    input_used = max(0, _budget_int(data.get("input_tokens_used") or data.get("input_tokens"), 0))
    output_used = max(0, _budget_int(data.get("output_tokens_used") or data.get("output_tokens"), 0))
    total_used = max(0, _budget_int(data.get("total_tokens_used"), input_used + output_used))
    return {
        "input_tokens_used": input_used,
        "output_tokens_used": output_used,
        "total_tokens_used": total_used,
        "cost_usd_used": _budget_money(_budget_float(data.get("cost_usd_used") or data.get("cost_usd"), 0.0)),
    }


def combine_handoff_budget_usage(
    seed: dict[str, Any],
    reserved: dict[str, Any],
) -> dict[str, Any]:
    return {
        "input_tokens_used": int(seed["input_tokens_used"]) + int(reserved["input_tokens_used"]),
        "output_tokens_used": int(seed["output_tokens_used"]) + int(reserved["output_tokens_used"]),
        "total_tokens_used": int(seed["total_tokens_used"]) + int(reserved["total_tokens_used"]),
        "cost_usd_used": _budget_money(float(seed["cost_usd_used"]) + float(reserved["cost_usd_used"])),
    }


def handoff_budget_request(value: HandoffBudgetReserve) -> dict[str, Any]:
    return {
        "input_tokens": max(0, _budget_int(value.input_tokens, 0)),
        "max_output_tokens": max(0, _budget_int(value.max_output_tokens, 0)),
        "reasoning_label": _budget_one_line(value.reasoning_label, "handoff_claim"),
    }


def handoff_budget_cost(config: dict[str, Any], input_tokens: int, output_tokens: int) -> float:
    return _budget_money(
        (input_tokens * float(config["input_cost_per_1k"]) / 1000.0)
        + (output_tokens * float(config["output_cost_per_1k"]) / 1000.0)
    )


def handoff_budget_remaining(
    config: dict[str, Any],
    usage: dict[str, Any],
) -> dict[str, Any]:
    return {
        "input_tokens": max(0, int(config["max_input_tokens"]) - int(usage["input_tokens_used"])),
        "output_tokens": max(0, int(config["max_output_tokens"]) - int(usage["output_tokens_used"])),
        "total_tokens": max(0, int(config["max_total_tokens"]) - int(usage["total_tokens_used"])),
        "cost_usd": _budget_money(float(config["max_cost_usd"]) - float(usage["cost_usd_used"])),
    }


def exhausted_handoff_budget(
    *,
    project: str,
    agent: str,
    lane: str,
    handoff_id: str,
    tick_id: str | None,
    config: dict[str, Any],
    usage: dict[str, Any],
    request_record: dict[str, Any],
    remaining_before: dict[str, Any],
    blockers: list[str],
) -> dict[str, Any]:
    reason = "budget exhausted: " + ", ".join(blockers)
    return {
        "schema_version": HANDOFF_BUDGET_SCHEMA_VERSION,
        "project": project,
        "agent": agent,
        "lane": lane,
        "handoff_id": handoff_id,
        "tick_id": tick_id,
        "status": "exhausted",
        "allow_llm": False,
        "reason": reason,
        "config": config,
        "usage": usage,
        "request": request_record,
        "remaining_before": remaining_before,
        "approved": {
            "input_tokens": 0,
            "max_output_tokens": 0,
            "estimated_total_tokens": 0,
            "estimated_cost_usd": 0.0,
            "limits_applied": ["llm_skipped"],
        },
        "remaining_after": remaining_before,
        "utilization_after": {
            "input": _budget_ratio(usage["input_tokens_used"], config["max_input_tokens"]),
            "output": _budget_ratio(usage["output_tokens_used"], config["max_output_tokens"]),
            "total": _budget_ratio(usage["total_tokens_used"], config["max_total_tokens"]),
            "cost": _budget_ratio(usage["cost_usd_used"], config["max_cost_usd"]),
        },
        "recommended_action": "stop_and_consult",
    }


def evaluate_handoff_budget(
    *,
    project: str,
    agent: str,
    lane: str,
    handoff_id: str,
    tick_id: str | None,
    config: dict[str, Any],
    usage: dict[str, Any],
    request_record: dict[str, Any],
) -> dict[str, Any]:
    remaining_before = handoff_budget_remaining(config, usage)
    input_tokens = int(request_record["input_tokens"])
    requested_output = int(request_record["max_output_tokens"])
    input_cost = handoff_budget_cost(config, input_tokens, 0)
    blockers: list[str] = []

    if input_tokens > remaining_before["input_tokens"]:
        blockers.append("input token budget")
    if input_tokens > remaining_before["total_tokens"]:
        blockers.append("total token budget")
    if input_cost > remaining_before["cost_usd"]:
        blockers.append("input cost budget")
    if requested_output <= 0:
        blockers.append("reasoning output request is zero")
    if blockers:
        return exhausted_handoff_budget(
            project=project,
            agent=agent,
            lane=lane,
            handoff_id=handoff_id,
            tick_id=tick_id,
            config=config,
            usage=usage,
            request_record=request_record,
            remaining_before=remaining_before,
            blockers=blockers,
        )

    remaining_cost_after_input = _budget_money(remaining_before["cost_usd"] - input_cost)
    if float(config["output_cost_per_1k"]) <= 0:
        cost_limited_output = 10**18
    else:
        cost_limited_output = max(
            0,
            math.floor(remaining_cost_after_input * 1000.0 / float(config["output_cost_per_1k"])),
        )
    approved_output = min(
        requested_output,
        int(remaining_before["output_tokens"]),
        max(0, int(remaining_before["total_tokens"]) - input_tokens),
        cost_limited_output,
    )
    if approved_output <= 0:
        return exhausted_handoff_budget(
            project=project,
            agent=agent,
            lane=lane,
            handoff_id=handoff_id,
            tick_id=tick_id,
            config=config,
            usage=usage,
            request_record=request_record,
            remaining_before=remaining_before,
            blockers=["reasoning output budget"],
        )

    estimated_cost = handoff_budget_cost(config, input_tokens, approved_output)
    approved_total = input_tokens + approved_output
    used_after = {
        "input_tokens_used": int(usage["input_tokens_used"]) + input_tokens,
        "output_tokens_used": int(usage["output_tokens_used"]) + approved_output,
        "total_tokens_used": int(usage["total_tokens_used"]) + approved_total,
        "cost_usd_used": _budget_money(float(usage["cost_usd_used"]) + estimated_cost),
    }
    remaining_after = handoff_budget_remaining(config, used_after)
    utilization_after = {
        "input": _budget_ratio(used_after["input_tokens_used"], config["max_input_tokens"]),
        "output": _budget_ratio(used_after["output_tokens_used"], config["max_output_tokens"]),
        "total": _budget_ratio(used_after["total_tokens_used"], config["max_total_tokens"]),
        "cost": _budget_ratio(used_after["cost_usd_used"], config["max_cost_usd"]),
    }
    limits_applied = []
    if approved_output < requested_output:
        limits_applied.append("output_clamped")
    status = (
        "near_limit"
        if limits_applied or any(value >= float(config["warn_fraction"]) for value in utilization_after.values())
        else "available"
    )
    return {
        "schema_version": HANDOFF_BUDGET_SCHEMA_VERSION,
        "project": project,
        "agent": agent,
        "lane": lane,
        "handoff_id": handoff_id,
        "tick_id": tick_id,
        "status": status,
        "allow_llm": True,
        "reason": "budget available with deterministic limits" if status == "available" else "budget near limit; use approved output limit",
        "config": config,
        "usage": usage,
        "request": request_record,
        "remaining_before": remaining_before,
        "approved": {
            "input_tokens": input_tokens,
            "max_output_tokens": approved_output,
            "estimated_total_tokens": approved_total,
            "estimated_cost_usd": estimated_cost,
            "limits_applied": limits_applied,
        },
        "remaining_after": remaining_after,
        "utilization_after": utilization_after,
        "recommended_action": "claim_and_execute_with_limit" if limits_applied else "claim_and_execute",
    }


async def insert_handoff_budget_event(
    conn: asyncpg.Connection,
    *,
    project: str,
    agent: str,
    event_type: str,
    summary: str,
    detail: dict[str, Any],
) -> int:
    event_id = await conn.fetchval(
        """
        INSERT INTO team_events (project, agent_name, event_type, summary, detail, ts)
        VALUES ($1, $2, $3, $4, $5::jsonb, NOW())
        RETURNING id
        """,
        project,
        agent,
        event_type,
        summary,
        json.dumps(detail, sort_keys=True),
    )
    if event_backend_uses_postgres():
        await conn.execute("SELECT pg_notify('cortex_events', $1)", str(event_id))
    return int(event_id)


class DiaryWrite(BaseModel):
    summary: str
    outcome: str = "completed"
    importance: int = 5
    commits: Optional[list[str]] = None
    files_modified: Optional[list[str]] = None
    room: Optional[str] = None


class SaveChatRequest(BaseModel):
    """Atomic checkpoint write: agents UPSERT + agent_sessions + messages +
    team_events + knowledge. Replaces cortex-save-chat's direct-SQL silent-fail
    path.
    """
    topic: str
    summary: str


class BeatArchiveStaleRequest(BaseModel):
    older_than_hours: int = 48


class TaskCreate(BaseModel):
    title: str
    assigned_agent: Optional[str] = None
    assigned_role: Optional[str] = None
    priority: int = 50
    description: Optional[str] = None


class TaskUpdate(BaseModel):
    status: str


class InvalidateRequest(BaseModel):
    reason: Optional[str] = None
    superseded_by: Optional[str] = None
    undo: bool = False
    successor_summary: Optional[str] = None


class MemoryWrite(BaseModel):
    section: str
    content: str
    category: str = "operational"
    source: Optional[str] = None


class AgentRegister(BaseModel):
    name: Optional[str] = None
    role: str
    capabilities: Optional[dict] = None
    writer_scope: Optional[str] = None
    role_description: Optional[str] = None
    role_is_builtin: bool = False
    role_source_file: Optional[str] = None


class ProjectRootRegister(BaseModel):
    path: str
    kind: str = "primary"
    metadata: Optional[dict] = None


class ProjectAgentRegister(BaseModel):
    name: str
    role: str = "generalist"
    model: Optional[str] = None
    capabilities: Optional[dict] = None


class ProjectRegister(BaseModel):
    project_key: str
    display_name: Optional[str] = None
    parent_project_key: Optional[str] = None
    repo_root: Optional[str] = None
    repo_type: str = "repo"
    status: str = "active"
    default_agent: Optional[str] = None
    roots: list[ProjectRootRegister] = []
    agents: list[ProjectAgentRegister] = []
    metadata: Optional[dict] = None
    enforce_writer_roster: Optional[bool] = None
    roster_policy: Optional[dict] = None


class ProjectRosterPolicyPatch(BaseModel):
    enforce_writer_roster: Optional[bool] = None
    roster_policy: Optional[dict] = None


class ProjectPatch(BaseModel):
    repo_root: Optional[str] = None


class ProjectTransferTable(BaseModel):
    schema_name: Literal["public", "cortex"]
    table_name: str
    rows: list[dict[str, Any]] = Field(default_factory=list)


class ProjectImportRequest(BaseModel):
    format: Literal["kaidera.cortex-project.v1"]
    source_project: dict[str, Any]
    tables: list[ProjectTransferTable] = Field(default_factory=list)
    allow_existing: bool = False


PROJECT_KEY_RENAME_PROJECT_TABLES: tuple[str, ...] = (
    "agent_diaries",
    "agent_profiles",
    "agent_sessions",
    "agent_skill_bindings",
    "agent_skills",
    "agents",
    "archive_decisions",
    "archive_events",
    "archive_handoffs",
    "archive_lessons",
    "archive_messages",
    "artifact_edges",
    "artifacts",
    "captured_patterns",
    "cortex_audit_log",
    "cortex_entities",
    "cortex_relationships",
    "decisions",
    "embedding_backfill_jobs",
    "epics",
    "execution_analyses",
    "graph_build_jobs",
    "handoffs",
    "knowledge",
    "lessons",
    "messages",
    "pattern_metrics",
    "roles",
    "rules",
    "session_sources",
    "sprints",
    "tasks",
    "team_events",
    "work_products",
)

PROJECT_KEY_RENAME_PROJECT_KEY_TABLES: tuple[str, ...] = (
    "cortex_project_paths",
    "cortex_legacy_identity_archive",
)

CONSOLE_APPDB_PROJECT_TABLE_CONFLICT_KEYS: dict[str, tuple[str, ...] | None] = {
    "agent_settings": ("agent",),
    "project_autonomy": (),
    "project_propose_mode": (),
    "pending_approval": ("handoff_id",),
    "handoff_orchestration": None,
    "run_state": None,
    "usage_events": None,
    "scheduled_jobs": ("id",),
    "mailbox_feeders": ("id",),
}

CONSOLE_APPDB_PROJECT_JSON_COLUMNS: dict[str, tuple[str, ...]] = {
    "scheduled_jobs": ("payload",),
    "mailbox_feeders": ("config", "state"),
}


def sql_identifier(name: str) -> str:
    if not re.fullmatch(r"[a-z_][a-z0-9_]*", name):
        raise HTTPException(500, f"Unsafe SQL identifier: {name}")
    return f'"{name}"'


def affected_count(status: str | None) -> int:
    if not status:
        return 0
    try:
        return int(status.split()[-1])
    except (ValueError, IndexError):
        return 0


async def public_table_has_column(conn: Any, table: str, column: str) -> bool:
    return bool(
        await conn.fetchval(
            """SELECT EXISTS (
                   SELECT 1
                     FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name = $1
                      AND column_name = $2
               )""",
            table,
            column,
        )
    )


async def ensure_project_key_update_cascade(conn: Any) -> list[str]:
    """Ensure project-key FKs cannot block an in-place parent key rename."""
    ensured: list[str] = []
    has_project_key = await public_table_has_column(conn, "cortex_projects", "project_key")
    if not has_project_key:
        return ensured

    if await public_table_has_column(conn, "cortex_project_paths", "project_key"):
        await conn.execute(
            """ALTER TABLE public.cortex_project_paths
                   DROP CONSTRAINT IF EXISTS cortex_project_paths_project_key_fkey"""
        )
        await conn.execute(
            """ALTER TABLE public.cortex_project_paths
                   ADD CONSTRAINT cortex_project_paths_project_key_fkey
                   FOREIGN KEY (project_key)
                   REFERENCES public.cortex_projects(project_key)
                   ON UPDATE CASCADE ON DELETE CASCADE"""
        )
        ensured.append("cortex_project_paths_project_key_fkey")

    if await public_table_has_column(conn, "cortex_projects", "parent_project_key"):
        await conn.execute(
            """ALTER TABLE public.cortex_projects
                   DROP CONSTRAINT IF EXISTS cortex_projects_parent_project_key_fkey"""
        )
        await conn.execute(
            """ALTER TABLE public.cortex_projects
                   ADD CONSTRAINT cortex_projects_parent_project_key_fkey
                   FOREIGN KEY (parent_project_key)
                   REFERENCES public.cortex_projects(project_key)
                   ON UPDATE CASCADE"""
        )
        ensured.append("cortex_projects_parent_project_key_fkey")

    return ensured


def harness_appdb_dsn() -> str:
    """Resolve the separate console app-DB DSN without importing console code."""
    override = os.getenv("HARNESS_APPDB_DSN_HOST", "").strip()
    if override:
        return override
    raw = os.getenv("HARNESS_APPDB_DSN", "").strip()
    return raw or HARNESS_APPDB_DSN_DEFAULT


async def update_console_appdb_project_table(
    conn: Any,
    *,
    table: str,
    conflict_columns: tuple[str, ...] | None,
    old_key: str,
    new_key: str,
) -> dict[str, int]:
    if not await public_table_has_column(conn, table, "project"):
        return {}
    ident = sql_identifier(table)

    if conflict_columns is not None:
        for column in conflict_columns:
            if not await public_table_has_column(conn, table, column):
                return {}
        if conflict_columns:
            match = " AND ".join(
                f"source.{sql_identifier(column)} = target.{sql_identifier(column)}"
                for column in conflict_columns
            )
        else:
            match = "TRUE"
        status = await conn.execute(
            f"""DELETE FROM {ident} target
                  WHERE target.project = $2
                    AND EXISTS (
                        SELECT 1
                          FROM {ident} source
                         WHERE source.project = $1
                           AND {match}
                    )""",
            old_key,
            new_key,
        )
        deleted = affected_count(status)
    else:
        deleted = 0

    status = await conn.execute(
        f"UPDATE {ident} SET project = $2 WHERE project = $1",
        old_key,
        new_key,
    )
    counts = {f"appdb.{table}.project": affected_count(status)}
    if deleted:
        counts[f"appdb.{table}.project_conflicts_deleted"] = deleted
    return counts


async def update_console_appdb_default_project(
    conn: Any,
    *,
    old_key: str,
    new_key: str,
) -> dict[str, int]:
    required_columns = ("key", "value")
    for column in required_columns:
        if not await public_table_has_column(conn, "app_settings", column):
            return {}
    set_clause = "value = to_jsonb($2::text)"
    if await public_table_has_column(conn, "app_settings", "updated_at"):
        set_clause += ", updated_at = NOW()"
    status = await conn.execute(
        f"""UPDATE app_settings
               SET {set_clause}
             WHERE key = 'cortex_default_project'
               AND value = to_jsonb($1::text)""",
        old_key,
        new_key,
    )
    return {"appdb.app_settings.cortex_default_project": affected_count(status)}


async def update_console_appdb_project_json(
    conn: Any,
    *,
    table: str,
    column: str,
    old_key: str,
    new_key: str,
) -> dict[str, int]:
    for required in ("project", column):
        if not await public_table_has_column(conn, table, required):
            return {}
    table_ident = sql_identifier(table)
    column_ident = sql_identifier(column)
    status = await conn.execute(
        f"""UPDATE {table_ident}
               SET {column_ident} = replace({column_ident}::text, $1, $2)::jsonb
             WHERE project = $2
               AND {column_ident}::text LIKE ('%' || $1 || '%')""",
        old_key,
        new_key,
    )
    return {f"appdb.{table}.{column}": affected_count(status)}


async def migrate_console_appdb_project_key(
    *,
    old_key: str,
    new_key: str,
    conn: Any | None = None,
) -> dict[str, Any]:
    """Best-effort migration of console operational state in harness_app."""
    owns_conn = conn is None
    if conn is None:
        try:
            conn = await asyncpg.connect(
                dsn=harness_appdb_dsn(),
                timeout=HARNESS_APPDB_CONNECT_TIMEOUT,
                command_timeout=HARNESS_APPDB_CONNECT_TIMEOUT,
            )
        except Exception as exc:
            return {
                "attempted": True,
                "available": False,
                "counts": {},
                "error": str(exc),
            }

    counts: dict[str, int] = {}
    try:
        async with conn.transaction():
            for table, conflict_columns in CONSOLE_APPDB_PROJECT_TABLE_CONFLICT_KEYS.items():
                counts.update(
                    await update_console_appdb_project_table(
                        conn,
                        table=table,
                        conflict_columns=conflict_columns,
                        old_key=old_key,
                        new_key=new_key,
                    )
                )
            counts.update(
                await update_console_appdb_default_project(
                    conn,
                    old_key=old_key,
                    new_key=new_key,
                )
            )
            for table, columns in CONSOLE_APPDB_PROJECT_JSON_COLUMNS.items():
                for column in columns:
                    counts.update(
                        await update_console_appdb_project_json(
                            conn,
                            table=table,
                            column=column,
                            old_key=old_key,
                            new_key=new_key,
                        )
                    )
        return {
            "attempted": True,
            "available": True,
            "counts": counts,
        }
    except Exception as exc:
        return {
            "attempted": True,
            "available": False,
            "counts": counts,
            "error": str(exc),
        }
    finally:
        if owns_conn and conn is not None:
            with suppress(Exception):
                await conn.close()


async def migrate_project_key(
    conn: Any,
    *,
    old_key: str,
    new_key: str,
    appdb_conn: Any | None = None,
    migrate_appdb: bool = True,
) -> dict[str, Any]:
    """Move a registered project key without creating a split identity.

    This is intentionally explicit: only known public Cortex tables with
    project/project_key columns are touched. The underlying cortex_projects.id
    remains stable, so actor aliases and project_id foreign keys stay attached
    to the same project while text-scoped rows move to the new key.
    """
    old_key = validate_project_key(old_key)
    new_key = validate_project_key(new_key)
    if old_key == new_key:
        return {"migrated": False, "old_key": old_key, "new_key": new_key, "counts": {}}

    source = await conn.fetchrow(
        "SELECT id::text FROM cortex_projects WHERE project_key = $1 "
        "AND COALESCE(status, 'active') <> 'deleted' LIMIT 1",
        old_key,
    )
    if not source:
        raise HTTPException(404, f"Project '{old_key}' is not registered in Cortex.")
    target = await conn.fetchrow(
        "SELECT id::text FROM cortex_projects WHERE project_key = $1 LIMIT 1",
        new_key,
    )
    if target:
        raise HTTPException(
            409,
            f"Cannot migrate project '{old_key}' to '{new_key}': target project "
            "already exists. Merge explicitly before registering this root.",
        )

    counts: dict[str, int] = {}
    fk_constraints = await ensure_project_key_update_cascade(conn)

    status = await conn.execute(
        """UPDATE cortex_projects
              SET project_key = $2,
                  updated_at = NOW()
            WHERE project_key = $1""",
        old_key,
        new_key,
    )
    counts["cortex_projects.project_key"] = affected_count(status)

    status = await conn.execute(
        """UPDATE cortex_projects
              SET parent_project_key = $2,
                  updated_at = NOW()
            WHERE parent_project_key = $1""",
        old_key,
        new_key,
    )
    counts["cortex_projects.parent_project_key"] = affected_count(status)

    for table in PROJECT_KEY_RENAME_PROJECT_TABLES:
        if not await public_table_has_column(conn, table, "project"):
            continue
        ident = sql_identifier(table)
        status = await conn.execute(
            f"UPDATE {ident} SET project = $2 WHERE project = $1",
            old_key,
            new_key,
        )
        counts[f"{table}.project"] = affected_count(status)
        if await public_table_has_column(conn, table, "project_id"):
            status = await conn.execute(
                f"""UPDATE {ident}
                       SET project_id = $2::uuid
                     WHERE project = $1
                       AND project_id IS DISTINCT FROM $2::uuid""",
                new_key,
                source["id"],
            )
            counts[f"{table}.project_id"] = affected_count(status)

    for table in PROJECT_KEY_RENAME_PROJECT_KEY_TABLES:
        if not await public_table_has_column(conn, table, "project_key"):
            continue
        ident = sql_identifier(table)
        status = await conn.execute(
            f"UPDATE {ident} SET project_key = $2 WHERE project_key = $1",
            old_key,
            new_key,
        )
        counts[f"{table}.project_key"] = affected_count(status)

    appdb_result = (
        await migrate_console_appdb_project_key(
            old_key=old_key,
            new_key=new_key,
            conn=appdb_conn,
        )
        if migrate_appdb
        else {"attempted": False, "available": False, "counts": {}}
    )

    return {
        "migrated": True,
        "old_key": old_key,
        "new_key": new_key,
        "project_id": source["id"],
        "fk_constraints": fk_constraints,
        "appdb": appdb_result,
        "counts": counts,
    }


class AgentRemove(BaseModel):
    project: str
    agent_name: str


class SqlRequest(BaseModel):
    sql: str


class MigrationApplyRequest(BaseModel):
    dry_run: bool = True
    target_ids: Optional[list[str]] = None
    max_count: Optional[int] = None
    applied_by: Optional[str] = None


class EpicIncrement(BaseModel):
    """One row of an epic's increment table (the {num,title,status,pct} shape)."""

    num: int
    title: str = ""
    status: str = "not_started"
    pct: int = 0


class EpicUpsert(BaseModel):
    """Create-or-update payload for POST /epics (admin-gated, upsert on (project, epic_id))."""

    epic_id: str
    title: str = ""
    status: str = "active"
    overall_pct: int = 0
    increments: list[EpicIncrement] = []


class KnowledgeIngest(BaseModel):
    """Bulk-ingest a knowledge row from a markdown source file.

    Idempotent on (project, source_file) — second call with same source_file
    returns the existing row's id with created=false. Embeddings are left
    NULL; Beat's 5-min cortex-embed cron backfills them.

    Migration path away from cortex-ingest-memories' broken sql_escape
    (handoff d6018d86, Option C).
    """
    content: str
    source_file: str
    category: Optional[str] = None
    section: Optional[str] = None  # short title / heading
    on_conflict: Optional[str] = "conflict"  # conflict | update


class LessonIngest(BaseModel):
    """Bulk-ingest a lesson from a feedback / retrospective markdown file.

    Idempotent on (project, summary, COALESCE(category,'')) — matches the
    existing cortex-ingest-memories dedup key.
    """
    summary: str
    detail: Optional[str] = None
    category: Optional[str] = None
    importance: Optional[int] = 5
    agent_name: Optional[str] = "migration"
    on_conflict: Optional[str] = "conflict"  # conflict | update


class DecisionIngest(BaseModel):
    """Bulk-ingest a decision from a markdown ADR / decision-log file.

    Idempotent on (project, summary, COALESCE(category,'')).
    """
    summary: str
    rationale: Optional[str] = None
    category: Optional[str] = None
    agent_name: Optional[str] = "migration"
    on_conflict: Optional[str] = "conflict"  # conflict | update


class EmbeddingBackfillRequest(BaseModel):
    """Backfill missing L2 embeddings through the API boundary."""

    table: str = "all"
    limit: int = 100
    chunk_size: int = 100
    max_errors: int = 10
    error_threshold: int = 3
    dry_run: bool = False
    async_job: bool = False


class SessionMessage(BaseModel):
    """One row for the messages table inside a SessionIngest payload.

    role accepts the canonical PG values (human|agent|system) AND the common
    LLM provider values (user|assistant). The endpoint translates: user→human,
    assistant→agent. Unknown roles → 400.
    """
    role: str
    content: str
    ts: Optional[str] = None  # ISO-8601; defaults to NOW() if omitted
    metadata: Optional[dict] = None


class SessionIngest(BaseModel):
    """Atomic batch ingest of a chat session — replaces cortex-ingest-codex /
    cortex-ingest-session / cortex-ingest-claude-local-state's broken silent-
    fail path through /admin/sql/exec (handoff c8fa34f0 Wave B1).

    Writes 4 tables in one transaction: agents UPSERT + agent_sessions UPSERT
    + session_sources UPSERT + messages bulk INSERT (after DELETE of any prior
    messages for this session_uuid, mirroring legacy semantics for append-only
    session files on disk).

    Idempotent on session_uuid: a second call with N messages leaves exactly
    N rows in PG, not 2N.
    """
    session_uuid: str
    agent: str
    task: Optional[str] = None
    source_path: str
    provider: str
    cwd: Optional[str] = None
    git_branch: Optional[str] = None
    source_kind: Optional[str] = None
    metadata: Optional[dict] = None
    messages: list[SessionMessage] = []


class GraphExtractRequest(BaseModel):
    project: Optional[str] = None
    source: str = GRAPH_DEFAULT_SOURCE
    limit: int = 20
    backfill: bool = False
    dry_run: bool = True
    reprocess: bool = False
    use_llm: bool = False
    model: Optional[str] = None


class GraphBuildRequest(BaseModel):
    repo: str
    full: bool = False
    embed: bool = True
    import_existing: bool = False
    async_job: bool = False
    sync: bool = False

    @model_validator(mode="after")
    def validate_build_mode(self):
        if self.import_existing and self.full:
            raise ValueError("full and import_existing graph modes are mutually exclusive")
        if self.import_existing and self.embed:
            raise ValueError(
                "import_existing requires embed=false; reviewed imports do not run embeddings"
            )
        return self


class GraphPruneRequest(BaseModel):
    dry_run: bool = True
    keep_projects: list[str] = []


class GraphBlastRequest(BaseModel):
    repo: str
    files: list[str]
    depth: int = 2
    max_results: int = 100


class GraphCallersRequest(BaseModel):
    repo: str
    target: str
    pattern: str = "callers_of"
    max_results: int = 100


class GraphImpactRequest(BaseModel):
    repo: str
    base: str = "HEAD~1"
    max_results: int = 100


class GraphLargeFnRequest(BaseModel):
    repo: str
    min_lines: int = 200
    kind: Optional[str] = None
    limit: int = 100


class ArtifactIngestRequest(BaseModel):
    source_file: str
    content_hash: str
    source_type: Optional[str] = None
    modality: Optional[str] = None
    extraction_method: Optional[str] = None
    raw_content: Optional[str] = None
    section_context: Optional[str] = None
    metadata: Optional[dict] = None
    caption: Optional[str] = None
    neighborhood_text: Optional[str] = None
    source_doc_metadata: Optional[dict] = None
    customer_id: Optional[UUID] = None
    org_id: Optional[UUID] = None
    parent_artifact_id: Optional[UUID] = None
    edge_type: Optional[str] = None
    target_type: Optional[str] = None
    target_ref: Optional[str] = None


class SyncEvent(BaseModel):
    agent_name: str
    event_type: str
    summary: str
    detail: Optional[str] = None
    ts: Optional[str] = None


class SyncEntity(BaseModel):
    name: str
    type: str
    description: Optional[str] = None
    metadata: Optional[dict] = None


class SyncRelationship(BaseModel):
    source: str
    target: str
    edge_type: str
    metadata: Optional[dict] = None


class ProjectLocalSyncRequest(BaseModel):
    client_snapshot: Optional[int] = None
    team_events: list[SyncEvent] = []
    entities: list[SyncEntity] = []
    relationships: list[SyncRelationship] = []


class ProjectLocalSyncResponse(BaseModel):
    accepted_events: int
    accepted_entities: int
    accepted_relationships: int
    checkpoint: int


CORTEX_PLATFORM_DEFAULTS = {
    "embedding_provider": EMBED_PROVIDER,
    "embedding_model": EMBED_MODEL,
    "embedding_dims": EMBED_DIMS,
    "rerank_enabled": RERANK_ENABLED,
    "rerank_provider": RERANK_PROVIDER,
    "rerank_model": RERANK_MODEL,
    "analysis_provider": ANALYSIS_PROVIDER,
    "analysis_model": ANALYSIS_MODEL or "google/gemma-4-31b-it:free",
    "cortex_api_url": "http://localhost:8501",
    "boot_context_version": "v2",
    "max_boot_tokens": 250,
    "search_confidence_threshold": 0.015,
    "rrf_k": 60,
    "embed_input_max_chars": 500,
    "rerank_input_max_chars": 500,
    "embed_timeout_ms": 15000,
    "rerank_timeout_ms": 2500,
    "analysis_timeout_ms": 90000,
    "embedding_provider_config_id": None,
    "rerank_provider_config_id": None,
    "analysis_provider_config_id": None,
    "updated_at": None,
}

CORTEX_PLATFORM_PATCHABLE_COLUMNS = frozenset({
    "embedding_provider",
    "embedding_model",
    "embedding_dims",
    "rerank_enabled",
    "rerank_provider",
    "rerank_model",
    "search_confidence_threshold",
    "rrf_k",
    "embed_input_max_chars",
    "rerank_input_max_chars",
    "embed_timeout_ms",
    "rerank_timeout_ms",
    "analysis_model",
    "analysis_provider",
    "analysis_timeout_ms",
    "embedding_provider_config_id",
    "rerank_provider_config_id",
    "analysis_provider_config_id",
})


class CortexAdminConfigUpdate(BaseModel):
    embedding_provider: str | None = None
    embedding_model: str | None = None
    embedding_dims: int | None = None
    rerank_enabled: bool | None = None
    rerank_provider: str | None = None
    rerank_model: str | None = None
    analysis_provider: str | None = None
    analysis_model: str | None = None
    search_confidence_threshold: float | None = None
    rrf_k: int | None = None
    embed_input_max_chars: int | None = None
    rerank_input_max_chars: int | None = None
    embed_timeout_ms: int | None = None
    rerank_timeout_ms: int | None = None
    analysis_timeout_ms: int | None = None
    embedding_provider_config_id: UUID | None = None
    rerank_provider_config_id: UUID | None = None
    analysis_provider_config_id: UUID | None = None


def serialize_cortex_platform_config(row: Any) -> dict[str, Any]:
    data: dict[str, Any] = {}
    for key, default in CORTEX_PLATFORM_DEFAULTS.items():
        value = row.get(key) if row else None
        if value is None:
            data[key] = default
        elif key == "updated_at":
            data[key] = value.isoformat() if hasattr(value, "isoformat") else str(value)
        elif key.endswith("_provider_config_id"):
            data[key] = str(value)
        else:
            data[key] = value
    return data


_PLATFORM_CONFIG_CACHE_SECONDS = 30
_platform_config_cache: dict[str, Any] = {"expires": 0.0, "config": None}


async def load_cortex_platform_config_cached(force: bool = False) -> dict[str, Any]:
    """Effective Cortex platform config.

    The config row is API-owned operational state. Embedding/rerank call sites read
    it at runtime so an operator can change Cortex ingestion models centrally,
    without rebuilding containers or relying on local env drift. Env constants
    remain the fallback when the table is absent or the DB is unreachable.
    """
    now = time.time()
    cached = _platform_config_cache.get("config")
    if (
        not force
        and isinstance(cached, dict)
        and now < float(_platform_config_cache.get("expires") or 0.0)
    ):
        return dict(cached)

    config = dict(CORTEX_PLATFORM_DEFAULTS)
    try:
        if pool_admin is not None:
            async with pool_admin.acquire() as conn:
                has_table = await conn.fetchval(
                    "SELECT EXISTS("
                    "  SELECT 1 FROM information_schema.tables"
                    "  WHERE table_name = 'cortex_platform_config'"
                    ")"
                )
                if has_table:
                    row = await conn.fetchrow("SELECT * FROM cortex_platform_config LIMIT 1")
                    config = serialize_cortex_platform_config(row)
    except Exception:
        config = dict(CORTEX_PLATFORM_DEFAULTS)

    _platform_config_cache["config"] = dict(config)
    _platform_config_cache["expires"] = time.time() + _PLATFORM_CONFIG_CACHE_SECONDS
    return config


# ---------------------------------------------------------------------------
#  Provider credentials — OpenKai is the sole authority.
#
#  The container receives only the host-derived, read-only OpenKai projection.
#  Its manifest revision is compared with the authenticated host's masked status
#  before any key is accepted. app_settings, process env and local Cortex env are
#  deliberately not fallbacks: retaining any would recreate the split plane.
# ---------------------------------------------------------------------------

# The TTL supports synchronous, token-free readiness views. Every actual secret
# use revalidates the projection against the live host revision, so PATCH,
# DELETE, repair, or an interrupted projection update cannot reuse a cached key.
_PROVIDER_KEY_TTL_SECONDS = 60.0
_provider_key_cache: dict[str, Any] = {"keys": {}, "expires": 0.0}


async def _live_openkai_projection_revision(
    client: httpx.AsyncClient,
    *,
    status_url: str,
    token: str,
) -> str:
    """Fetch only the masked composite revision from the host authority."""
    response = await client.get(
        status_url,
        headers={"Authorization": f"Bearer {token}"},
    )
    if response.status_code != 200 or len(response.content) > 512 * 1024:
        return ""
    declared = response.headers.get("content-length")
    if declared is not None and int(declared) > 512 * 1024:
        return ""
    payload = response.json()
    revision = (
        payload.get("projection_revision") if isinstance(payload, dict) else None
    )
    return (
        revision
        if isinstance(revision, str)
        and re.fullmatch(r"[0-9a-f]{64}", revision) is not None
        else ""
    )


async def _load_provider_keys_from_openkai() -> dict[str, str]:
    """Bracket a projection read with live composite-revision checks."""
    try:
        from openkai_provider_env import load_provider_projection

        status_url = (os.getenv("OPENKAI_PROVIDER_STATUS_URL") or "").strip()
        token = (os.getenv("HARNESS_SERVICE_TOKEN") or "").strip()
        if not status_url or not token:
            return {}
        timeout = httpx.Timeout(4.0, connect=2.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            revision_before = await _live_openkai_projection_revision(
                client, status_url=status_url, token=token
            )
            if not revision_before:
                return {}
            keys, projection_revision = await asyncio.to_thread(
                load_provider_projection
            )
            revision_after = await _live_openkai_projection_revision(
                client, status_url=status_url, token=token
            )
        if (
            not revision_after
            or not hmac.compare_digest(projection_revision, revision_before)
            or not hmac.compare_digest(projection_revision, revision_after)
        ):
            return {}
        return keys

    except Exception:
        # No exception text is logged: even parser/library errors must not risk
        # carrying file content into logs.  Callers expose only "not configured".
        return {}


async def refresh_provider_keys(force: bool = False) -> dict[str, str]:
    """Cached in-memory view of the canonical OpenKai provider keys."""
    now = time.time()
    if not force and now < float(_provider_key_cache.get("expires") or 0.0):
        return dict(_provider_key_cache.get("keys") or {})
    keys = await _load_provider_keys_from_openkai()
    _provider_key_cache["keys"] = keys
    _provider_key_cache["expires"] = now + (
        _PROVIDER_KEY_TTL_SECONDS if keys else 5.0
    )
    return dict(keys)


async def resolve_provider_key(provider: str) -> str:
    """Resolve only after validating the projection's current host revision."""
    name = (provider or "").strip().lower()
    keys = await refresh_provider_keys(force=True)
    value = (keys.get(name) or "").strip()
    return value


def _ingestion_key(provider: str) -> str:
    """Synchronous, cache-only OpenKai lookup for doctor/config checks."""
    p = (provider or "").strip().lower()
    cached = (_provider_key_cache.get("keys") or {}).get(p)
    if isinstance(cached, str) and cached.strip():
        return cached.strip()
    return ""


def _provider_purpose_name(config: dict[str, Any], purpose: str) -> str:
    provider_key = "rerank_provider" if purpose == "rerank" else "embedding_provider"
    return str(config.get(provider_key) or "")


async def provider_configured(config: dict[str, Any], purpose: str) -> bool:
    """Whether a key for this purpose actually resolves, refreshing the cache if stale.

    This replaced a SYNCHRONOUS predicate that only READ `_provider_key_cache` and never
    filled it. On a cold boot nothing has populated that cache, and the process-env
    fallback is empty by design since provider keys moved to the settings store -- so the
    sync check answered False permanently, while `resolve_provider_key` (which
    self-refreshes) resolved the very same key fine. The gate and the work it guards
    disagreed.

    The embedding backfill gated on it and refused every job with "embedding provider key
    is not configured for cortex-api" on an appliance whose key was configured and
    working. Observed on marlow 2026-08-18 after a stack restart, across retries spanning
    more than the 60s cache TTL, so it does not self-heal.

    Every caller is async -- including the doctor's config_sanity check, which is why that
    check previously had to warm the cache by hand to avoid emitting a false critical.
    Awaiting the resolver removes that obligation entirely.
    """
    provider = _provider_purpose_name(config, purpose)
    if provider.strip().lower() == LOCAL_SEARCH_PROVIDER:
        if purpose == "rerank":
            return str(config.get("rerank_model") or "").strip() == LOCAL_RERANK_MODEL
        try:
            dims = int(config.get("embedding_dims") or 0)
        except (TypeError, ValueError):
            return False
        return (
            str(config.get("embedding_model") or "").strip() == LOCAL_EMBED_MODEL
            and dims == LOCAL_EMBED_DIMS
        )
    # A warm projection is sufficient for readiness gates. Actual provider calls
    # still use resolve_provider_key(), which validates the live masked revision
    # before returning any secret. On a cold boot, populate the cache here so the
    # gate cannot permanently disagree with a configured OpenKai projection.
    if _ingestion_key(provider):
        return True
    return bool(await resolve_provider_key(provider))


def _provider_timeout(config: dict[str, Any], purpose: str) -> float:
    if purpose == "analysis":
        key = "analysis_timeout_ms"
    elif purpose == "rerank":
        key = "rerank_timeout_ms"
    else:
        key = "embed_timeout_ms"
    try:
        default_ms = 2500 if purpose == "rerank" else 15000
        return max(1.0, float(config.get(key) or default_ms) / 1000.0)
    except (TypeError, ValueError):
        return 15.0


def _provider_input_limit(config: dict[str, Any], purpose: str) -> int:
    key = "rerank_input_max_chars" if purpose == "rerank" else "embed_input_max_chars"
    try:
        return max(10, int(config.get(key) or 500))
    except (TypeError, ValueError):
        return 500


def _embedding_endpoint(provider: str) -> str:
    p = (provider or "").strip().lower()
    if p == "openai":
        return "https://api.openai.com/v1/embeddings"
    if p == "nvidia":
        return "https://integrate.api.nvidia.com/v1/embeddings"
    return "https://openrouter.ai/api/v1/embeddings"


def _extract_embedding(data: Any) -> list[float] | None:
    if not isinstance(data, dict):
        return None
    emb = data.get("embedding")
    if isinstance(emb, list):
        return emb
    rows = data.get("data")
    if isinstance(rows, list) and rows:
        first = rows[0] if isinstance(rows[0], dict) else {}
        emb = first.get("embedding")
        if isinstance(emb, list):
            return emb
    return None


def _extract_rerank_results(data: Any) -> list[dict] | None:
    """Normalize rerank provider responses to OpenRouter-style result rows."""
    if not isinstance(data, dict):
        return None
    rows = data.get("results") or data.get("rankings") or data.get("data")
    if not isinstance(rows, list) or not rows:
        return None
    out: list[dict] = []
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        index = row.get("index", row.get("passage_index", row.get("document_index", i)))
        score = row.get(
            "relevance_score",
            row.get("score", row.get("logit", row.get("ranking_score"))),
        )
        try:
            index = int(index)
        except (TypeError, ValueError):
            index = i
        item = {"index": index}
        if score is not None:
            item["relevance_score"] = score
        out.append(item)
    return out or None


# ---------------------------------------------------------------------------
# GET /health
# ---------------------------------------------------------------------------


@app.get("/health")
async def health():
    pg_ok = False
    schema_version = "unknown"
    notification_queue_usage: float | None = None
    platform_config = dict(CORTEX_PLATFORM_DEFAULTS)
    try:
        async with pool_admin.acquire() as conn:
            await conn.fetchval("SELECT 1")
            schema_version = (
                await conn.fetchval(
                    "SELECT value FROM cortex_meta WHERE key = 'schema_version'"
                )
                or "unknown"
            )
            if event_backend_uses_postgres():
                try:
                    usage = await conn.fetchval("SELECT pg_notification_queue_usage()")
                    notification_queue_usage = float(usage or 0)
                except Exception:
                    notification_queue_usage = None
        pg_ok = True
    except Exception:
        pass
    if pg_ok:
        platform_config = await load_cortex_platform_config_cached()

    # Loud capability signal (F10): embeddings/rerank silently degrade to non-vector
    # when no provider key is resolvable. The healthcheck itself must not flap on it
    # (podman reads the HTTP status only), but any operator or panel reading this
    # payload can see the truth without digging through logs.
    provider_key_state = "unknown"
    local_search_is_keyless = (
        str(platform_config.get("embedding_provider") or "").strip().lower()
        == LOCAL_SEARCH_PROVIDER
        and (
            not bool(platform_config.get("rerank_enabled", True))
            or str(platform_config.get("rerank_provider") or "").strip().lower()
            == LOCAL_SEARCH_PROVIDER
        )
    )
    if pg_ok and local_search_is_keyless:
        provider_key_state = "not_required_for_local_search"
    elif pg_ok:
        try:
            provider_key_state = (
                "configured" if await refresh_provider_keys() else "unconfigured"
            )
        except Exception:
            provider_key_state = "unknown"

    status = "healthy" if pg_ok else "degraded"
    event_bus = "postgres" if pg_ok else "postgres-disconnected"

    return {
        "status": status,
        "postgres": "connected" if pg_ok else "disconnected",
        "event_store": "postgres" if pg_ok else "postgres-disconnected",
        "event_backend": CORTEX_EVENT_BACKEND,
        "event_bus": event_bus,
        "pg_notification_queue_usage": notification_queue_usage,
        "version": CORTEX_API_VERSION,
        "surface_version": CORTEX_SURFACE_VERSION,
        "schema_version": schema_version,
        "embed_provider": platform_config.get("embedding_provider"),
        "embed_model": platform_config.get("embedding_model"),
        "embed_dims": platform_config.get("embedding_dims"),
        "provider_keys": provider_key_state,
        "rerank_enabled": platform_config.get("rerank_enabled"),
        "rerank_provider": platform_config.get("rerank_provider"),
        "rerank_model": platform_config.get("rerank_model"),
        "rls_enforced": RLS_ENFORCED,
    }


# ---------------------------------------------------------------------------
# GET /boot/{agent}
# ---------------------------------------------------------------------------

def truncate_boot_tier(text: str, max_chars: int) -> str:
    """Trim optional boot tiers without cutting in the middle of a line.

    Compact boots are often read by another model as startup instructions.
    A dangling bullet such as ``"  - ["`` is worse than dropping the optional
    tier because it looks like corrupted context instead of omitted context.
    """
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text

    candidate = text[:max_chars].rstrip()
    last_newline = candidate.rfind("\n")
    if last_newline > 0:
        candidate = candidate[:last_newline].rstrip()

    lines = candidate.splitlines()
    while lines:
        tail = lines[-1].strip()
        if tail.endswith(":") or (tail.startswith("---") and tail.endswith("---")):
            lines.pop()
            continue
        break
    if not lines:
        return ""
    return "\n".join(lines) + "\n  ... [truncated]"


@app.get("/boot/{agent}")
async def boot(
    agent: str,
    request: Request,
    x_project: str = Header(alias="X-Project", default=""),
    query: str | None = Query(default=None, min_length=1),
):
    project = require_project_scope(x_project)
    project_info = await require_registered_project(project)
    agent = agent.lower().strip()

    # Token budget: default 250, clamped to [50, 500]
    budget = min(max(int(request.query_params.get("budget", "250")), 50), 500)

    async with acquire_scoped(project) as conn:
        # L0: Identity
        # The agents registry is the runtime roster authority. Prefer its current
        # role over a filesystem-derived agent_profiles row, which may lag after a
        # role rename (for example gem -> graphics or saul -> creative-director).
        profile = await conn.fetchrow(
            """SELECT name as agent_name, role
               FROM agents
               WHERE project = $1 AND lower(name) = $2
               LIMIT 1""",
            project,
            agent,
        )
        if not profile:
            # A profile-only legacy identity can still boot before registry import.
            profile = await conn.fetchrow(
                """SELECT agent_name, role
                   FROM agent_profiles
                   WHERE project = $1 AND lower(agent_name) = $2
                   LIMIT 1""",
                project,
                agent,
            )

        # Roster-aware typo guard (typo-proof agent identity). When the name
        # matches no registered agent, a close match to a real roster name is
        # almost certainly a misspelling — reject with a suggestion instead of
        # silently booting a phantom identity that gets used until someone fixes
        # it. A genuinely novel name still boots but is flagged UNREGISTERED.
        agent_unregistered = profile is None
        if agent_unregistered:
            roster_rows = await conn.fetch(
                f"""SELECT DISTINCT lower(agent_name) AS n
                      FROM (
                            SELECT lower(a.name) AS agent_name
                              FROM agents a
                             WHERE a.project = $1 AND {visible_agent_sql('a')}
                            UNION
                            SELECT lower(ap.agent_name) AS agent_name
                              FROM agent_profiles ap
                             WHERE ap.project = $1
                      ) r
                     WHERE agent_name IS NOT NULL""",
                project,
            )
            roster = sorted({row["n"] for row in roster_rows if row["n"]})
            known = set(roster)
            # System-event writers (beat/migration/system) are synthetic identities
            # — not agents rows — so they come from the project's roster policy, not
            # the live roster query. For a non-enforcing project this set is empty,
            # so this is a no-op for non-enforcing projects.
            boot_policy = await load_roster_policy(project)
            known |= set(boot_policy.system_event_writers)
            if agent_base_name(agent) in known:
                agent_unregistered = False
            else:
                suggestion = suggest_agent_name(agent, roster)
                if suggestion:
                    raise HTTPException(
                        404,
                        f"No agent '{agent}' is registered in project '{project}'. "
                        f"Did you mean '{suggestion}'? Registered agents: "
                        f"{', '.join(roster) or '(none)'}. To add a genuinely new "
                        "agent, register it first with cortex-add-agent.",
                    )

        identity = f"You are {agent_display_name(agent, project)}, an agent on the {project} project."
        if agent_unregistered:
            identity = (
                f"WARNING: '{agent}' is not a registered agent in project '{project}'. "
                + identity
                + " If this is a typo, re-boot with the correct name; to register a"
                " new agent, use cortex-add-agent."
            )
        if profile:
            if profile["role"]:
                identity += f" Role: {profile['role']}."
        identity_discipline = (
            f"Identity discipline: use agent IDs as <name>@{project}. "
            "Do not use retired colon-suffixed or hex-derived values in new memory rows."
        )

        # L1: Pending handoffs for this agent/role only. Canonical recipient OR
        # claimer predicate (the claimer branch is inert here since this is
        # pending-only, but stays canonical to prevent drift).
        bare_agent = agent.lower().strip()
        roles = await resolve_agent_role_set(conn, project, bare_agent)
        handoffs = await conn.fetch(
            f"""SELECT id::text, from_agent, to_agent, priority, LEFT(summary, 80) as summary,
                      files_changed
               FROM handoffs
               WHERE project = $1
                 AND status = $2
                 AND invalidated_at IS NULL
                 AND (
                    {handoff_claimer_sql('$3')}
                    OR {handoff_recipient_sql('$3', '$4')}
                 )
               ORDER BY CASE priority
                 WHEN 'urgent' THEN 0 WHEN 'high' THEN 1
                 WHEN 'medium' THEN 2 ELSE 3 END
               LIMIT 5""",
            project,
            "pending",
            bare_agent,
            [r.lower() for r in roles],
        )
        claimed_handoffs = await conn.fetch(
            """SELECT id::text, from_agent, to_agent, priority,
                      LEFT(summary, 80) as summary, files_changed
                 FROM handoffs
                WHERE project = $1
                  AND status = $2
                  AND invalidated_at IS NULL
                  AND lower(split_part(COALESCE(claimed_by, ''), '@', 1)) = $3
                ORDER BY claimed_at DESC NULLS LAST, created_at DESC
                LIMIT 3""",
            project,
            "claimed",
            bare_agent,
        )

        # Recent decisions (P1)
        decisions = await conn.fetch(
            """SELECT LEFT(COALESCE(summary, ''), 100) as summary
               FROM decisions
               WHERE project = $1
                 AND invalidated_at IS NULL
                 AND COALESCE(summary, '') <> ''
                 AND COALESCE(summary, '') NOT LIKE '[LIB-GUARD-DIRECT-PG:%'
               ORDER BY created_at DESC LIMIT 3""",
            project,
        )

        # Critical lessons (importance >= 8) (P1)
        lessons = await conn.fetch(
            """SELECT LEFT(summary, 100) as summary
               FROM lessons
               WHERE project = $1 AND invalidated_at IS NULL
                 AND COALESCE(importance, 5) >= 8
               ORDER BY created_at DESC LIMIT 5""",
            project,
        )

        # Quality-gated decisions (P2)
        quality_decisions = await conn.fetch(
            "SELECT summary FROM decisions "
            "WHERE project = $1 AND invalidated_at IS NULL "
            "AND quality_score IS NOT NULL AND quality_score > 0.5 "
            "ORDER BY quality_score DESC LIMIT 5",
            project,
        )

        # Degradation alerts (P2)
        degraded = await conn.fetch(
            "SELECT pattern_key, consecutive_failures FROM pattern_metrics "
            "WHERE project = $1 AND degraded = TRUE AND degradation_surfaced = FALSE",
            project,
        )

        topic_recall = None
        if query:
            topic_recall = await execute_search(
                conn,
                project,
                query,
                search_type="all",
                rerank=False,
                hall="project",
                graph=False,
            )

        # Mark degraded patterns as surfaced
        if degraded:
            for d in degraded:
                await conn.execute(
                    "UPDATE pattern_metrics SET degradation_surfaced = TRUE "
                    "WHERE project = $1 AND pattern_key = $2",
                    project, d["pattern_key"],
                )

        # ── PersonaPayload: skills + rules (Phase 1; empty-safe) ──
        # Queries are guarded against the tables not yet existing: if the
        # migration has not been applied, the tables will be absent and we
        # return empty lists rather than crashing the boot response.
        try:
            # Two delivery channels, UNION-ed then deduped by skill_slug:
            #   (1) BOUND skills — agent_skill_bindings JOIN agent_skills for this
            #       project, addressed to this agent or one of its roles (the
            #       original Phase-1 behaviour, unchanged).
            #   (2) GLOBAL skills — the shared skills repo (scope='global',
            #       project='*'). These reach EVERY agent in EVERY project with
            #       no binding required (CTO: one shared skills repo every worker
            #       can use).
            # DISTINCT ON (skill_slug) collapses duplicates so the same slug is
            # never returned twice; the ORDER BY makes a BOUND project skill win
            # over a same-slug GLOBAL one (src=0 sorts before src=1), so a global
            # cannot hide a project's own binding.
            skill_rows = await conn.fetch(
                """SELECT DISTINCT ON (skill_slug)
                          skill_slug, name, description, scope, permission,
                          version, body_ref
                   FROM (
                       SELECT asb.skill_slug, ask.name, ask.description,
                              ask.scope, ask.permission, ask.version, ask.body_ref,
                              0 AS src, asb.priority AS bind_priority
                       FROM agent_skill_bindings asb
                       JOIN agent_skills ask
                         ON ask.project = asb.project
                        AND ask.skill_slug = asb.skill_slug
                        AND (ask.version = asb.version_pin OR asb.version_pin IS NULL)
                       WHERE asb.project = $1
                         AND (
                             (asb.subject_kind = 'agent' AND lower(asb.subject) = $2)
                             OR (asb.subject_kind = 'role'
                                 AND lower(asb.subject) = ANY($3::text[]))
                         )
                         AND ask.status = 'active'
                       UNION ALL
                       SELECT g.skill_slug, g.name, g.description,
                              g.scope, g.permission, g.version, g.body_ref,
                              1 AS src, 0 AS bind_priority
                       FROM agent_skills g
                       WHERE g.scope = 'global'
                         AND g.project = '*'
                         AND g.status = 'active'
                   ) merged
                   ORDER BY skill_slug, src, bind_priority DESC, version DESC""",
                project,
                agent,
                [r.lower() for r in roles],
            )
        except Exception:  # noqa: BLE001  — table may not exist yet
            skill_rows = []

        try:
            rule_rows = await conn.fetch(
                """SELECT rule_slug, title, body, source_file, version
                   FROM rules
                   WHERE project = $1 AND status = 'active'
                   ORDER BY rule_slug""",
                project,
            )
        except Exception:  # noqa: BLE001  — table may not exist yet
            rule_rows = []

        try:
            boot_work_products = await fetch_boot_work_product_briefs(
                conn,
                project,
                handoff_rows=[*claimed_handoffs, *handoffs],
                query=query,
                limit=3,
            )
        except Exception:  # noqa: BLE001  — work-product memory is additive
            boot_work_products = []

    generated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    # ── Build priority tiers ──

    # P0: identity + infrastructure + active handoffs (never cut)
    p0_lines = [identity, identity_discipline]
    if claimed_handoffs:
        p0_lines.append(f"\n{len(claimed_handoffs)} claimed handoff(s):")
        for h in claimed_handoffs:
            p0_lines.append(f"  ({h['priority']}) from {h['from_agent']}: {h['summary']}")
    if handoffs:
        p0_lines.append(f"\n{len(handoffs)} pending handoff(s):")
        for h in handoffs:
            p0_lines.append(f"  ({h['priority']}) from {h['from_agent']}: {h['summary']}")
    else:
        p0_lines.append("\nNo pending handoffs.")
    if boot_work_products:
        p0_lines.append("\n--- CURRENT WORK-PRODUCT BRIEFS (read before rediscovery) ---")
        for wp in boot_work_products[:3]:
            files = normalize_text_list(wp.get("files_changed"))
            file_hint = f" files={', '.join(files[:3])}" if files else ""
            summary = compact_text(wp.get("summary", ""), limit=120)
            p0_lines.append(
                f"  - {wp.get('title') or 'Work product'}: {summary}{file_hint}"
            )
    p0_lines.append("\n--- BOOT CONTEXT PROVENANCE ---")
    p0_lines.append(
        "Sources: live Cortex rows scoped to this project only; no filesystem fallback."
    )
    p0_lines.append(
        "Freshness: handoffs live, decisions 7d, lessons 14d, work-products current rows."
    )
    p0_lines.append(
        "Structured provenance/freshness/projection details are in persona.metadata.boot_context."
    )
    p0_lines.append("\n--- INFRASTRUCTURE (always loaded) ---")
    p0_lines.append("This Kaidera OS deployment: cortex-api:8501")
    p0_lines.append(
        f"Active memory scope: {project}. Local Cortex scope, Kaidera AI platform scope, and external project scope stay isolated."
    )
    p0_lines.append(
        "Do not infer Kaidera AI platform or external project deployment topology from local Cortex context."
    )
    p0_lines.append(
        "Use workspace registry, project docs, and environment-specific deployment config for real infra targets."
    )
    p0_lines.append(
        "Cortex access rule: use cortex-api and cortex-* API-backed commands only; do not connect directly to Postgres, Redis, or worker containers."
    )
    p0_text = "\n".join(p0_lines)

    # P1: critical lessons + sprint decisions (last cut)
    p1_lines = []
    if lessons:
        p1_lines.append("--- CRITICAL LESSONS ---")
        for lesson in lessons:
            p1_lines.append(f"  ⚑ {lesson['summary']}")
    if decisions:
        p1_lines.append("\nRecent decisions:")
        for d in decisions:
            p1_lines.append(f"  - {d['summary']}")
    p1_text = "\n".join(p1_lines)

    # P2: quality-gated decisions + degradation alerts (cut second)
    p2_parts = []
    if quality_decisions:
        p2_parts.append("--- TOP DECISIONS (quality-gated) ---")
        for qd in quality_decisions:
            p2_parts.append(f"  * {qd['summary'][:100]}")
    if degraded:
        p2_parts.append("--- DEGRADATION ALERTS ---")
        for d in degraded:
            p2_parts.append(f"  \u26a0 {d['pattern_key']}: {d['consecutive_failures']} consecutive failures")
    p2_text = "\n".join(p2_parts)

    # P3: topic recall (cut first)
    p3_lines = []
    if query:
        p3_lines.append(f"--- TOPIC RECALL: {query} ---")
        if topic_recall and topic_recall.get("results"):
            for result in topic_recall["results"][:3]:
                source = result.get("source", "?")
                text = (result.get("text") or "").replace("\n", " ").strip()
                p3_lines.append(f"  [{source}] {text[:110]}")
        else:
            p3_lines.append("  No topic recall matches found.")
    p3_text = "\n".join(p3_lines)

    # ── Token budget truncation (chars / 4 ≈ tokens) ──
    tiers = {"P0": p0_text, "P1": p1_text, "P2": p2_text, "P3": p3_text}
    total_tokens = sum(len(v) // 4 for v in tiers.values())
    for tier_key in ["P3", "P2", "P1"]:
        if total_tokens <= budget:
            break
        excess = (total_tokens - budget) * 4  # back to chars
        available = len(tiers[tier_key])
        cut = min(excess, available)
        tiers[tier_key] = truncate_boot_tier(tiers[tier_key], available - cut)
        total_tokens = sum(len(v) // 4 for v in tiers.values())

    boot_text = "\n\n".join(v for v in tiers.values() if v.strip())

    # ── Build structured PersonaPayload (additive; boot/surface_version unchanged) ──
    skill_manifest = [
        SkillManifestEntry(
            skill_slug=row["skill_slug"],
            name=row.get("name"),
            description=row.get("description"),
            scope=row.get("scope") or "project",
            permission=row.get("permission"),
            version=row.get("version") or "1",
            body_ref=row.get("body_ref"),
        )
        for row in skill_rows
    ]
    rules_list = [
        {
            "rule_slug": row["rule_slug"],
            "title": row["title"],
            "body": row["body"],
            "source_file": row.get("source_file"),
            "version": row.get("version") or "1",
        }
        for row in rule_rows
    ]
    pending_handoffs_list = [
        {
            "id": h["id"],
            "priority": h["priority"],
            "summary": h["summary"],
        }
        for h in handoffs
    ]
    persona = PersonaPayload(
        project=project,
        agent=agent,
        agent_identity=agent_display_name(agent, project),
        role=profile["role"] if profile and profile.get("role") else None,
        identity_text=identity,
        skills=skill_manifest,
        rules=rules_list,
        pending_handoffs=pending_handoffs_list,
        harness=None,
        metadata={
            "boot_context": build_boot_context_metadata(
                project=project,
                agent=agent,
                project_info=project_info,
                profile=profile,
                generated_at=generated_at,
                handoffs=handoffs,
                claimed_handoffs=claimed_handoffs,
                decisions=decisions,
                lessons=lessons,
                quality_decisions=quality_decisions,
                degraded=degraded,
                boot_work_products=boot_work_products,
                topic_recall=topic_recall,
            )
        },
    )

    return {
        "boot": boot_text,
        "surface_version": CORTEX_SURFACE_VERSION,
        "persona": persona.model_dump(),
    }


# ---------------------------------------------------------------------------
# GET /bootstrap/{agent}
# ---------------------------------------------------------------------------


@app.get("/bootstrap/{agent}")
async def bootstrap(
    agent: str,
    x_project: str = Header(alias="X-Project", default=""),
):
    """Full agent startup brief through the Cortex API boundary.

    This replaces the legacy shell bootstrap path that read Postgres/Redis
    directly from agent sessions.
    """
    project = require_project_scope(x_project)
    await require_registered_project(project)
    agent = agent.lower().strip()

    async with acquire_scoped(project) as conn:
        profile = await conn.fetchrow(
            """SELECT agent_name, role
               FROM agent_profiles
               WHERE project = $1 AND lower(agent_name) = $2
               LIMIT 1""",
            project,
            agent,
        )
        if not profile:
            profile = await conn.fetchrow(
                """SELECT name AS agent_name, role
                   FROM agents
                   WHERE project = $1 AND lower(name) = $2
                   LIMIT 1""",
                project,
                agent,
            )
        roles = await resolve_agent_roles(conn, project, agent)
        role_filter = roles or [agent]

        roster = await conn.fetch(
            f"""SELECT a.name, a.role, COALESCE(a.model, '') AS model
                  FROM agents a
                 WHERE a.project = $1
                   AND {visible_agent_sql("a")}
                 ORDER BY a.name""",
            project,
        )

        sprints = await conn.fetch(
            """SELECT COALESCE(sprint_label, sprint_number::text, '') AS sprint_ref,
                      COALESCE(goal, '') AS goal,
                      COALESCE(status, '') AS status
                 FROM sprints
                WHERE project = $1 AND status = 'active'
                ORDER BY COALESCE(sprint_number, 2147483647), sprint_label
                LIMIT 10""",
            project,
        )

        handoffs = await conn.fetch(
            """SELECT id::text, from_agent, to_agent, priority, LEFT(COALESCE(summary, ''), 90) AS summary,
                      created_at::date::text AS created_date
                 FROM handoffs
                WHERE project = $1
                  AND status = 'pending'
                  AND invalidated_at IS NULL
                  AND (
                    lower(split_part(COALESCE(to_agent, ''), '@', 1)) = $3
                    OR (
                        COALESCE(to_agent, '') = ''
                        AND (lower(to_role) = ANY($2::text[]) OR lower(to_role) = $3)
                    )
                  )
                ORDER BY CASE priority
                    WHEN 'urgent' THEN 0 WHEN 'high' THEN 1
                    WHEN 'medium' THEN 2 ELSE 3 END,
                    created_at DESC
                LIMIT 12""",
            project,
            [r.lower() for r in role_filter],
            agent,
        )

        inflight = await conn.fetch(
            """SELECT id::text, priority, LEFT(COALESCE(summary, ''), 90) AS summary,
                      COALESCE(claimed_at, created_at)::date::text AS claimed_date
                 FROM handoffs
                WHERE project = $1
                  AND status = 'claimed'
                  AND lower(split_part(COALESCE(claimed_by, ''), '@', 1)) = $2
                ORDER BY claimed_at DESC
                LIMIT 12""",
            project,
            agent,
        )

        tasks = await conn.fetch(
            """SELECT id::text, LEFT(COALESCE(title, ''), 90) AS title,
                      COALESCE(status, '') AS status,
                      COALESCE(priority, 0)::text AS priority
                 FROM tasks
                WHERE project = $1
                  AND status <> 'done'
                  AND (
                    lower(COALESCE(assigned_agent, '')) = $2
                    OR lower(COALESCE(assigned_role, '')) = ANY($3::text[])
                  )
                ORDER BY priority DESC
                LIMIT 12""",
            project,
            agent,
            [r.lower() for r in role_filter],
        )

        decisions = await conn.fetch(
            """SELECT agent_name, COALESCE(category, '') AS category,
                      LEFT(COALESCE(summary, ''), 140) AS summary
                 FROM decisions
                WHERE project = $1
                  AND invalidated_at IS NULL
                  AND created_at >= NOW() - INTERVAL '7 days'
                ORDER BY created_at DESC
                LIMIT 20""",
            project,
        )

        lessons = await conn.fetch(
            """SELECT agent_name, COALESCE(category, '') AS category,
                      LEFT(COALESCE(summary, ''), 140) AS summary
                 FROM lessons
                WHERE project = $1
                  AND invalidated_at IS NULL
                  AND created_at >= NOW() - INTERVAL '14 days'
                ORDER BY created_at DESC
                LIMIT 12""",
            project,
        )

        activity = await conn.fetch(
            """SELECT to_char(ts, 'YYYY-MM-DD HH24:MI') AS ts,
                      agent_name,
                      event_type,
                      LEFT(COALESCE(summary, ''), 140) AS summary
                 FROM team_events
                WHERE project = $1
                  AND ts >= NOW() - INTERVAL '3 days'
                ORDER BY ts DESC
                LIMIT 15""",
            project,
        )

        await emit_team_event(
            conn,
            project=project,
            agent_name=agent,
            event_type="session_start",
            summary=f"Session started by {agent}",
            notify=event_backend_uses_postgres(),
        )

    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    role = profile["role"] if profile and profile["role"] else "agent"
    lines: list[str] = [
        "",
        f"# Agent Cortex - Context for {agent}",
        f"Generated: {generated} | Project: {project} | Agent ID: {agent_display_name(agent, project)}",
        "",
        "## Identity",
        f"You are {agent_display_name(agent, project)}, {role} for {project}.",
        "Cortex access rule: use cortex-api and cortex-* API-backed commands only; do not connect directly to Postgres, Redis, or worker containers.",
        "",
        "## Team Roster",
    ]

    if roster:
        lines.extend(
            f"- {row['name']}: {row['role'] or 'agent'}"
            + (f" ({row['model']})" if row["model"] else "")
            for row in roster
        )
    else:
        lines.append("(No roster data)")

    lines.extend(["", "## Active Sprints"])
    if sprints:
        lines.extend(
            f"- {row['sprint_ref']}: {row['goal']} [{row['status']}]"
            for row in sprints
        )
    else:
        lines.append("(No active sprint data)")

    lines.extend(["", "## Pending Handoffs"])
    if handoffs:
        lines.extend(
            f"- {row['id']} | {row['priority']} | from {row['from_agent']} | {row['summary']} | {row['created_date']}"
            for row in handoffs
        )
        lines.append("Claim with: cortex-handoff --claim <id>")
    else:
        lines.append("(none)")

    lines.extend(["", "## In-flight Claimed By You"])
    if inflight:
        lines.extend(
            f"- {row['id']} | {row['priority']} | {row['summary']} | {row['claimed_date']}"
            for row in inflight
        )
    else:
        lines.append("(none)")

    lines.extend(["", "## Your Tasks"])
    if tasks:
        lines.extend(
            f"- {row['id']} | {row['status']} | priority {row['priority']} | {row['title']}"
            for row in tasks
        )
    else:
        lines.append("(none)")

    lines.extend(["", "## Recent Decisions"])
    if decisions:
        lines.extend(
            f"- {row['agent_name']} | {row['category'] or 'decision'} | {row['summary']}"
            for row in decisions
        )
    else:
        lines.append("(none)")

    lines.extend(["", "## Recent Lessons"])
    if lessons:
        lines.extend(
            f"- {row['agent_name']} | {row['category'] or 'lesson'} | {row['summary']}"
            for row in lessons
        )
    else:
        lines.append("(none)")

    lines.extend(["", "## Recent Team Activity"])
    if activity:
        lines.extend(
            f"- {row['ts']} | {row['agent_name']} | {row['event_type']} | {row['summary']}"
            for row in activity
        )
    else:
        lines.append("(none)")

    lines.extend([
        "",
        "## Cortex Architecture Reminder",
        "L6 Boot Context, L5 Multimodal Artifacts, L4 Knowledge Graph, L3 Code Graph, L2 Vector Embeddings, L1 Verbatim Storage.",
        "Use cortex-search, cortex-graph-*, cortex-handoff, cortex-log, cortex-diary, and other cortex-* commands as the supported API surface.",
        "",
        f"Cortex bootstrap complete. You are {agent} on project {project}.",
    ])

    return {"text": "\n".join(lines), "project": project, "agent": agent}


# ---------------------------------------------------------------------------
# GET /agents/{agent}/persona
# ---------------------------------------------------------------------------


@app.get("/agents/{agent}/persona")
async def get_agent_persona(
    agent: str,
    x_project: str = Header(alias="X-Project", default=""),
):
    project = require_project_scope(x_project)
    await require_registered_project(project)
    agent = validate_profile_agent_name(agent)
    persona_policy = await load_roster_policy(project)
    project_metadata = await fetch_project_metadata(project)
    if persona_policy.enforce and agent_base_name(agent) not in (
        persona_policy.work_writers | persona_policy.system_event_writers
    ):
        raise HTTPException(
            403,
            f"Agent '{agent}' is not a registered runtime persona for project '{project}'",
        )

    async with acquire_scoped(project) as conn:
        profile_row = await conn.fetchrow(
            """SELECT agent_name, role, profile_kind, profile_text, metadata, updated_at
               FROM agent_profiles
               WHERE project = $1 AND lower(agent_name) = $2
               ORDER BY CASE WHEN profile_kind = 'identity' THEN 0 ELSE 1 END,
                        updated_at DESC
               LIMIT 1""",
            project,
            agent,
        )
        agent_row = await conn.fetchrow(
            """SELECT name, role, model, capabilities
               FROM agents
               WHERE project = $1 AND lower(name) = $2
               LIMIT 1""",
            project,
            agent,
        )
        if not profile_row and not agent_row:
            raise HTTPException(404, f"Agent '{agent}' is not registered in {project}")

        roles = sorted({agent, *await resolve_agent_roles(conn, project, agent)})
        pending_handoffs = await conn.fetch(
            """SELECT id::text, priority, LEFT(COALESCE(summary, ''), 140) AS summary
               FROM handoffs
               WHERE project = $1
                 AND status = 'pending'
                 AND invalidated_at IS NULL
                 AND (
                    lower(split_part(COALESCE(to_agent, ''), '@', 1)) = $2
                    OR (
                        COALESCE(to_agent, '') = ''
                        AND lower(to_role) = ANY($3::text[])
                    )
                 )
               ORDER BY CASE priority
                    WHEN 'urgent' THEN 0 WHEN 'high' THEN 1
                    WHEN 'medium' THEN 2 ELSE 3 END,
                    created_at DESC
               LIMIT 5""",
            project,
            agent,
            [r.lower() for r in roles],
        )
        claimed_handoffs = await conn.fetch(
            """SELECT id::text, priority, LEFT(COALESCE(summary, ''), 140) AS summary
               FROM handoffs
               WHERE project = $1
                 AND status = 'claimed'
                 AND lower(split_part(COALESCE(claimed_by, ''), '@', 1)) = $2
               ORDER BY claimed_at DESC
               LIMIT 5""",
            project,
            agent,
        )
        recent_decisions = await conn.fetch(
            """SELECT agent_name, LEFT(COALESCE(summary, ''), 160) AS summary
               FROM decisions
               WHERE project = $1
                 AND invalidated_at IS NULL
                 AND created_at >= NOW() - INTERVAL '7 days'
               ORDER BY created_at DESC
               LIMIT 6""",
            project,
        )

    profile = dict(profile_row) if profile_row else {}
    agent_record = dict(agent_row) if agent_row else {}
    metadata = json_object(profile.get("metadata"))
    capabilities = json_object(agent_record.get("capabilities"))
    roster_metadata = roster_policy_from_metadata(project_metadata)
    roster_roles = json_object(roster_metadata.get("roles")) or dict(persona_policy.roles)
    project_persona = json_object(roster_metadata.get("persona"))
    persona_metadata = {
        **project_persona,
        **json_object(capabilities.get("persona")),
        **json_object(metadata.get("persona")),
    }
    role = profile.get("role") or agent_record.get("role") or "agent"
    model = agent_record.get("model") or persona_metadata.get("model") or ""
    support_agents = json_list(roster_roles.get("support_agents"))
    approved_agents = json_list(roster_roles.get("approved_agents")) or sorted(persona_policy.work_writers)
    runtime_context = {
        "active_epic": metadata.get("active_epic") or persona_metadata.get("active_epic"),
        "active_increment": metadata.get("active_increment") or persona_metadata.get("active_increment"),
        "policy_refs": json_list(metadata.get("policy_refs")) or json_list(persona_metadata.get("policy_refs")),
        "skills": json_list(persona_metadata.get("skills")),
        "non_roster_rule": persona_metadata.get("non_roster_rule") or "Use the project roster and current handoff routing rules.",
        "hard_gates": json_list(persona_metadata.get("hard_gates")),
        "handoff_sequence": json_list(persona_metadata.get("handoff_sequence")),
        "approved_agents": approved_agents,
        "pm_lead": effective_pm_lead(roster_roles),
        "support_agents": support_agents,
        "support_agent": support_agents[0] if support_agents else None,
        "role_assignments": json_object(roster_roles.get("role_assignments")),
        "enforce_writer_roster": persona_policy.enforce,
        "roster_schema_version": roster_metadata.get("roster_schema_version", CORTEX_ROSTER_SCHEMA_VERSION),
    }
    lane = (
        persona_metadata.get("lane")
        or capabilities.get("lane")
        or metadata.get("lane")
        or role
    )
    not_lane = (
        persona_metadata.get("not_lane")
        or capabilities.get("not_lane")
        or metadata.get("not_lane")
        or "work outside the current project, explicit role, or handoff route"
    )
    reports_to = (
        persona_metadata.get("reports_to")
        or metadata.get("reports_to")
        or ""
    )
    sections = build_persona_sections(
        agent=agent,
        project=project,
        role=role,
        lane=lane,
        not_lane=not_lane,
        reports_to=reports_to,
        runtime_context=runtime_context,
        profile_text=profile.get("profile_text") or "",
        pending_handoffs=[dict(row) for row in pending_handoffs],
        claimed_handoffs=[dict(row) for row in claimed_handoffs],
        recent_decisions=[dict(row) for row in recent_decisions],
    )
    additional_context = "\n\n".join(
        sections[key]
        for key in (
            "identity",
            "operating_rules",
            "persona_skills",
            "current_state",
            "architecture_footer",
        )
        if sections.get(key)
    )
    profile_content = {
        "agent": agent,
        "project": project,
        "role": role,
        "model": model,
        "runtime_context": runtime_context,
        "sections": sections,
    }
    profile_hash = "sha256:" + hashlib.sha256(
        json.dumps(profile_content, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()

    return {
        "schema": "cortex.persona.v1",
        "agent": agent,
        "agent_name": display_agent_name(agent),
        "agent_identity": agent_display_name(agent, project),
        "role": role,
        "project": project,
        "profile_version": 1,
        "profile_hash": profile_hash,
        "compiled_at": datetime.now(timezone.utc).isoformat(),
        "lane": lane,
        "not_lane": not_lane,
        "reports_to": reports_to,
        "harness": {
            "default": capabilities.get("harness") or persona_metadata.get("harness") or "",
            "model": model,
            "options": json_list(persona_metadata.get("harness_options")),
        },
        "runtime_context": runtime_context,
        "additionalContext": additional_context,
        "sections": sections,
        "stale": False,
        "stale_reason": None,
        "fetched_via": "live",
    }


# ---------------------------------------------------------------------------
# GET /degradation
# ---------------------------------------------------------------------------


@app.get("/degradation")
async def get_degradation(
    x_project: str = Header(alias="X-Project", default=""),
):
    project = require_project_scope(x_project)
    async with acquire_scoped(project) as conn:
        rows = await conn.fetch(
            "SELECT pattern_key, pattern_type, consecutive_failures, "
            "total_uses, successes, last_success_at, last_failure_at "
            "FROM pattern_metrics "
            "WHERE project = $1 AND degraded = TRUE "
            "ORDER BY consecutive_failures DESC",
            project,
        )
        return {
            "degraded": [
                {
                    "pattern_key": r["pattern_key"],
                    "pattern_type": r["pattern_type"],
                    "consecutive_failures": r["consecutive_failures"],
                    "total_uses": r["total_uses"],
                    "total_successes": r["successes"],
                    "last_failure_at": r["last_failure_at"].isoformat() if r["last_failure_at"] else None,
                    "last_success_at": r["last_success_at"].isoformat() if r["last_success_at"] else None,
                }
                for r in rows
            ]
        }


# ---------------------------------------------------------------------------
# GET /patterns
# ---------------------------------------------------------------------------


@app.get("/patterns")
async def list_patterns(
    active_only: bool = Query(default=True),
    pattern_type: Optional[str] = Query(default=None),
    limit: int = Query(default=20, le=100),
    x_project: str = Header(alias="X-Project", default=""),
):
    project = require_project_scope(x_project)
    conditions = ["project = $1"]
    params: list = [project]
    idx = 2

    if active_only:
        conditions.append("is_active = TRUE")
    if pattern_type:
        conditions.append(f"pattern_type = ${idx}")
        params.append(pattern_type)
        idx += 1

    where = " AND ".join(conditions)
    async with acquire_scoped(project) as conn:
        rows = await conn.fetch(
            f"SELECT id, title, pattern_type, quality_score, generation, "
            f"agent_name, created_at FROM captured_patterns "
            f"WHERE {where} ORDER BY created_at DESC LIMIT {limit}",
            *params,
        )
        return {
            "patterns": [
                {
                    "id": r["id"],
                    "title": r["title"],
                    "pattern_type": r["pattern_type"],
                    "quality_score": r["quality_score"],
                    "generation": r["generation"],
                    "agent_name": r["agent_name"],
                    "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                }
                for r in rows
            ]
        }


# ---------------------------------------------------------------------------
# POST /log
# ---------------------------------------------------------------------------


@app.post("/log")
async def log_event(
    body: LogRequest,
    x_agent: str = Header(alias="X-Agent-Name"),
    x_project: str = Header(alias="X-Project", default=""),
):
    agent = validate_agent_name(x_agent)
    project = require_project_scope(x_project)
    await require_registered_agent_writer(project, agent, scope="system-event")
    agent = await compound_agent(agent, project)

    if body.event_type in ("decision", "lesson"):
        # Auto-embed
        embedding = await embed_text(body.summary)
        vec_str = (
            "[" + ",".join(str(v) for v in embedding) + "]" if embedding else None
        )

        table = "decisions" if body.event_type == "decision" else "lessons"
        async with acquire_scoped(project) as conn:
            async with conn.transaction():
                if vec_str:
                    row_id = await conn.fetchval(
                        f"""INSERT INTO {table} (project, agent_name, summary, category,
                                embedding, metadata)
                            VALUES ($1, $2, $3, $4, $5::vector, $6)
                            RETURNING id""",
                        project,
                        agent,
                        body.summary,
                        body.category,
                        vec_str,
                        json.dumps(body.metadata or {}),
                    )
                else:
                    row_id = await conn.fetchval(
                        f"""INSERT INTO {table} (project, agent_name, summary, category,
                                embedding, metadata)
                            VALUES ($1, $2, $3, $4, NULL, $5)
                            RETURNING id""",
                        project,
                        agent,
                        body.summary,
                        body.category,
                        json.dumps(body.metadata or {}),
                    )
                await verify_memory_write_persisted(
                    conn,
                    table=table,
                    row_id=row_id,
                    project=project,
                    expected_agent=agent,
                    expected_summary=body.summary,
                    expected_category=body.category,
                    expected_metadata=body.metadata,
                )

                # Keep the memory row, DAG updates, and companion team event atomic.
                if body.event_type == "decision" and body.supersedes_id:
                    parent_row = await conn.fetchrow(
                        "SELECT id, generation FROM decisions "
                        "WHERE id::text LIKE $1 || '%' AND project = $2",
                        body.supersedes_id, project,
                    )
                    if parent_row:
                        parent_gen = parent_row["generation"] or 0
                        await conn.execute(
                            "UPDATE decisions SET parent_decision_id = $1, generation = $2, "
                            "supersession_summary = $3 WHERE id = $4",
                            parent_row["id"], parent_gen + 1,
                            body.supersession_summary, row_id,
                        )
                        await conn.execute(
                            "UPDATE decisions SET invalidated_at = NOW() WHERE id = $1",
                            parent_row["id"],
                        )

                if body.event_type == "lesson" and body.importance:
                    await conn.execute(
                        "UPDATE lessons SET importance = $1 WHERE id = $2",
                        body.importance,
                        row_id,
                    )

                event_id = await emit_team_event(
                    conn,
                    project=project,
                    agent_name=agent,
                    event_type=body.event_type,
                    summary=body.summary,
                    detail={
                        "source_table": table,
                        "row_id": str(row_id),
                        **(body.metadata or {}),
                    },
                    files=body.files_affected,
                    notify=event_backend_uses_postgres(),
                    verify=True,
                )

        return {
            "id": str(row_id),
            "embedded": embedding is not None,
            "verified": True,
            "team_event_id": event_id,
        }

    # Generic event (commit, started, stopped, etc.)
    async with acquire_scoped(project) as conn:
        event_id = await emit_team_event(
            conn,
            project=project,
            agent_name=agent,
            event_type=body.event_type,
            summary=body.summary,
            detail=body.metadata,
            files=body.files_affected,
            notify=event_backend_uses_postgres(),
            verify=True,
        )

    return {"logged": True, "id": str(event_id), "event_type": body.event_type, "verified": True}


# ---------------------------------------------------------------------------
# Bulk-ingest endpoints — handoff d6018d86, Option C ratified by Alpha.
#
# Replace cortex-ingest-memories' /admin/sql/exec + bash sql_escape path
# (which silently corrupts apostrophes via a quoting bug in _cortex_lib.sh)
# with parameterized INSERTs immune to escaping issues. Mirrors the B.2
# JSON-payload migration that already shipped for /log and /handoffs.
#
# Embeddings are left NULL; Beat's 5-min cortex-embed cron backfills.
# ---------------------------------------------------------------------------


@asynccontextmanager
async def acquire_ingest_identity(
    project: str,
    kind: str,
    *identity: str | None,
):
    """Serialize one natural ingest identity through SELECT and INSERT/UPDATE.

    ``acquire_scoped`` alone is not a transaction, so a standalone xact advisory lock
    would be released before the following SELECT. Keep the lock, equality decision and
    mutation in one transaction. This preserves the product's ability to store unrelated
    rows with the same source while making exact retry/lost-response semantics atomic.
    """
    key = json.dumps(["cortex-ingest-v1", kind, project, *identity], separators=(",", ":"))
    async with acquire_scoped(project) as conn:
        async with conn.transaction():
            await conn.fetchval(
                "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                key,
            )
            yield conn


class RuleIngest(BaseModel):
    """A project rule every agent receives at boot (PersonaPayload.rules)."""
    rule_slug: str
    title: str
    body: str
    source_file: Optional[str] = None
    version: str = "1"
    status: str = "active"  # active | deprecated | draft
    on_conflict: Optional[str] = "conflict"  # conflict | update


@app.post("/rules/ingest")
async def ingest_rule(
    body: RuleIngest,
    request: Request,
    x_project: str = Header(alias="X-Project", default=""),
):
    """Upsert one project rule (admin). Rules are governance text that every agent boot
    injects (`/boot` selects `rules WHERE project = $1 AND status = 'active'`), so writing
    one is admin-gated and keyed on (project, rule_slug, version) exactly like the seed
    migration. Before 2026-09-02 rules could only arrive by migration, which made "every
    agent sees the canonical architecture" impossible through the product."""
    require_admin_access(request)
    project = require_project_scope(x_project)
    on_conflict = normalize_ingest_mode(body.on_conflict)
    slug = compact_text(body.rule_slug, limit=120)
    if not slug or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,119}", slug):
        raise HTTPException(400, "rule_slug must be a lowercase slug")
    if body.status not in {"active", "deprecated", "draft"}:
        raise HTTPException(400, "status must be active, deprecated or draft")
    title = compact_text(body.title, limit=200)
    text = (body.body or "").strip()
    if not title or not text:
        raise HTTPException(400, "title and body are required")
    async with acquire_scoped(project) as conn:
        existing = await conn.fetchrow(
            "SELECT id, title, body, status FROM rules WHERE project = $1 AND rule_slug = $2 AND version = $3",
            project, slug, body.version,
        )
        if existing:
            if (existing["title"], existing["body"], existing["status"]) == (title, text, body.status):
                return {"id": str(existing["id"]), "status": "unchanged"}
            if on_conflict != "update":
                raise HTTPException(409, f"rule {slug} v{body.version} exists; pass on_conflict=update to replace it")
            await conn.execute(
                """UPDATE rules SET title = $1, body = $2, source_file = $3, status = $4
                    WHERE id = $5""",
                title, text, body.source_file, body.status, existing["id"],
            )
            return {"id": str(existing["id"]), "status": "updated"}
        row_id = await conn.fetchval(
            """INSERT INTO rules (project, rule_slug, title, body, source_file, version, status)
               VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING id""",
            project, slug, title, text, body.source_file, body.version, body.status,
        )
        return {"id": str(row_id), "status": "created"}


@app.post("/knowledge/ingest")
async def ingest_knowledge(
    body: KnowledgeIngest,
    x_project: str = Header(alias="X-Project", default=""),
):
    project = require_project_scope(x_project)
    on_conflict = normalize_ingest_mode(body.on_conflict)
    async with acquire_ingest_identity(project, "knowledge", body.source_file) as conn:
        existing = await conn.fetchrow(
            """SELECT id, content, category, section
                 FROM knowledge
                WHERE project = $1 AND source_file = $2
                LIMIT 1""",
            project,
            body.source_file,
        )
        if existing is not None:
            expected = {
                "content": body.content,
                "category": body.category,
                "section": body.section,
            }
            actual = {
                "content": existing["content"],
                "category": existing["category"],
                "section": existing["section"],
            }
            if actual == expected:
                return {"id": str(existing["id"]), "status": "unchanged", "created": False, "embedded": False}
            if on_conflict != "update":
                ingest_conflict_error("knowledge", existing["id"], expected, actual)
            row_id = await conn.fetchval(
                """UPDATE knowledge
                      SET content = $1,
                          embedding = NULL,
                          category = $2,
                          section = $3,
                          project_id = COALESCE(
                              project_id,
                              (SELECT id FROM cortex_projects WHERE project_key = $5)
                          ),
                          updated_at = NOW()
                    WHERE id = $4 AND project = $5
                    RETURNING id""",
                body.content,
                body.category,
                body.section,
                existing["id"],
                project,
            )
            return {"id": str(row_id), "status": "updated", "created": False, "updated": True, "embedded": False}
        row_id = await conn.fetchval(
            """INSERT INTO knowledge (project, project_id, content, source_file, category, section)
               VALUES (
                   $1,
                   (SELECT id FROM cortex_projects WHERE project_key = $1),
                   $2,
                   $3,
                   $4,
                   $5
               )
               RETURNING id""",
            project,
            body.content,
            body.source_file,
            body.category,
            body.section,
        )
    return {"id": str(row_id), "status": "created", "created": True, "embedded": False}


@app.post("/lessons/ingest")
async def ingest_lesson(
    body: LessonIngest,
    x_project: str = Header(alias="X-Project", default=""),
):
    project = require_project_scope(x_project)
    agent_name = None
    if body.agent_name:
        agent_name = agent_base_for_project(body.agent_name, project, field_name="agent_name")
        await require_registered_agent_writer(
            project,
            agent_name,
            scope="system-event",
        )
        agent_name = await compound_agent(agent_name, project)
    on_conflict = normalize_ingest_mode(body.on_conflict)
    async with acquire_ingest_identity(
        project,
        "lesson",
        body.summary,
        body.category,
    ) as conn:
        existing = await conn.fetchrow(
            """SELECT id, detail, agent_name, importance
                 FROM lessons
                WHERE project = $1
                  AND summary = $2
                  AND COALESCE(category, '') = COALESCE($3, '')
                LIMIT 1""",
            project,
            body.summary,
            body.category,
        )
        if existing is not None:
            expected = {
                "detail": body.detail,
                "agent_name": agent_name,
                "importance": body.importance,
            }
            actual = {
                "detail": existing["detail"],
                "agent_name": existing["agent_name"],
                "importance": existing["importance"],
            }
            if actual == expected:
                return {"id": str(existing["id"]), "status": "unchanged", "created": False, "embedded": False}
            if on_conflict != "update":
                ingest_conflict_error("lesson", existing["id"], expected, actual)
            row_id = await conn.fetchval(
                """UPDATE lessons
                      SET detail = $1,
                          embedding = NULL,
                          agent_name = $2,
                          importance = $3
                    WHERE id = $4 AND project = $5
                    RETURNING id""",
                body.detail,
                agent_name,
                body.importance,
                existing["id"],
                project,
            )
            return {"id": str(row_id), "status": "updated", "created": False, "updated": True, "embedded": False}
        row_id = await conn.fetchval(
            """INSERT INTO lessons
                   (project, summary, detail, category, agent_name, importance, created_at)
               VALUES ($1, $2, $3, $4, $5, $6, NOW())
               RETURNING id""",
            project,
            body.summary,
            body.detail,
            body.category,
            agent_name,
            body.importance,
        )
    return {"id": str(row_id), "status": "created", "created": True, "embedded": False}


@app.post("/decisions/ingest")
async def ingest_decision(
    body: DecisionIngest,
    x_project: str = Header(alias="X-Project", default=""),
):
    project = require_project_scope(x_project)
    agent_name = None
    if body.agent_name:
        agent_name = agent_base_for_project(body.agent_name, project, field_name="agent_name")
        await require_registered_agent_writer(
            project,
            agent_name,
            scope="system-event",
        )
        agent_name = await compound_agent(agent_name, project)
    on_conflict = normalize_ingest_mode(body.on_conflict)
    async with acquire_ingest_identity(
        project,
        "decision",
        body.summary,
        body.category,
    ) as conn:
        existing = await conn.fetchrow(
            """SELECT id, rationale, agent_name
                 FROM decisions
                WHERE project = $1
                  AND summary = $2
                  AND COALESCE(category, '') = COALESCE($3, '')
                LIMIT 1""",
            project,
            body.summary,
            body.category,
        )
        if existing is not None:
            # E4 Compaction check
            final_rationale = body.rationale
            is_compacted = False
            if os.environ.get("CORTEX_E4_COMPACT") == "1":
                final_rationale, is_compacted, _ = compact_for_storage(body.rationale or "")

            expected = {
                "rationale": final_rationale,
                "agent_name": agent_name,
            }
            actual = {
                "rationale": existing["rationale"],
                "agent_name": existing["agent_name"],
            }
            if actual == expected:
                return {"id": str(existing["id"]), "status": "unchanged", "created": False, "embedded": False}
            if on_conflict != "update":
                ingest_conflict_error("decision", existing["id"], expected, actual)

            await conn.execute(
                """UPDATE decisions
                      SET rationale = $1,
                          embedding = NULL,
                          agent_name = $2,
                          compacted = $5,
                          project_id = COALESCE(
                              project_id,
                              (SELECT id FROM cortex_projects WHERE project_key = $4)
                          ),
                          updated_at = NOW()
                    WHERE id = $3 AND project = $4""",
                final_rationale,
                agent_name,
                existing["id"],
                project,
                is_compacted,
            )
            return {"id": str(existing["id"]), "status": "updated", "created": False, "updated": True, "embedded": False}

        # E4 Compaction for new row
        final_rationale = body.rationale
        is_compacted = False
        if os.environ.get("CORTEX_E4_COMPACT") == "1":
            final_rationale, is_compacted, _ = compact_for_storage(body.rationale or "")

        row_id = await conn.fetchval(
            """INSERT INTO decisions
                   (project, summary, rationale, category, agent_name, created_at, compacted)
               VALUES ($1, $2, $3, $4, $5, NOW(), $6)
               RETURNING id""",
            project,
            body.summary,
            final_rationale,
            body.category,
            agent_name,
            is_compacted,
        )
    return {"id": str(row_id), "status": "created", "created": True, "embedded": False}


_ROLE_TRANSLATIONS = {
    "user": "human",
    "assistant": "agent",
    "human": "human",
    "agent": "agent",
    "system": "system",
}


@app.post("/sessions/ingest")
async def ingest_session(
    body: SessionIngest,
    x_project: str = Header(alias="X-Project", default=""),
):
    """Atomic 4-table batch ingest of a chat session. See SessionIngest docstring."""
    project = require_project_scope(x_project)
    await require_registered_project(project)
    agent = agent_base_for_project(body.agent, project, field_name="agent")
    await require_registered_agent_writer(project, agent, scope="system-event")
    try:
        session_uuid = str(UUID(str(body.session_uuid)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise HTTPException(
            400,
            f"session_uuid={body.session_uuid!r} is not a valid UUID",
        ) from exc

    # Translate / validate roles up front so the bulk INSERT never hits the
    # CHECK constraint surprise. Bad roles → 400, not 500. Same for ts —
    # asyncpg needs a datetime, not a string, so parse here and 400 on bad ISO.
    translated_messages: list[tuple[str, str, Optional[datetime], Optional[dict]]] = []
    for idx, msg in enumerate(body.messages):
        canon = _ROLE_TRANSLATIONS.get(msg.role.lower())
        if canon is None:
            raise HTTPException(
                400,
                f"messages[{idx}].role={msg.role!r} not in "
                f"{sorted(set(_ROLE_TRANSLATIONS))}",
            )
        ts_dt: Optional[datetime] = None
        if msg.ts:
            try:
                ts_dt = datetime.fromisoformat(msg.ts.replace("Z", "+00:00"))
            except ValueError as exc:
                raise HTTPException(
                    400, f"messages[{idx}].ts={msg.ts!r} is not ISO-8601: {exc}"
                ) from exc
        translated_messages.append((canon, msg.content, ts_dt, msg.metadata))

    async with pool_admin.acquire() as admin_conn:
        existing_source_path = await admin_conn.fetchrow(
            """
            SELECT session_id::text AS session_id, project
              FROM session_sources
             WHERE source_path = $1
             LIMIT 1
            """,
            body.source_path,
        )
        if existing_source_path and existing_source_path["project"] != project:
            raise HTTPException(
                409,
                "source_path already belongs to project "
                f"'{existing_source_path['project']}'; "
                f"refusing ingest into '{project}'",
            )
        if existing_source_path:
            session_uuid = str(existing_source_path["session_id"])

        existing_cross_project = await admin_conn.fetchrow(
            """
            SELECT project, agent_name, source_table
            FROM (
                SELECT s.project, a.name AS agent_name, 'agent_sessions' AS source_table
                  FROM agent_sessions s
                  LEFT JOIN agents a ON a.id = s.agent_id
                 WHERE s.id = $1::uuid
                UNION ALL
                SELECT ss.project, ss.agent_name, 'session_sources' AS source_table
                  FROM session_sources ss
                 WHERE ss.session_id = $1::uuid
            ) existing
            WHERE project <> $2
            LIMIT 1
            """,
            session_uuid,
            project,
        )
    if existing_cross_project:
        raise HTTPException(
            409,
            "session_uuid already belongs to project "
            f"'{existing_cross_project['project']}'"
            f" via {existing_cross_project['source_table']}; "
            f"refusing ingest into '{project}'",
        )

    metadata = dict(body.metadata or {})
    metadata["agent"] = agent
    metadata_json = json.dumps(metadata)

    async with acquire_scoped(project) as conn:
        async with conn.transaction():
            # 1. Resolve the registered agent. Session ingest must never mint
            #    a new identity from transcript inference; typos belong at the
            #    caller boundary, not in project history.
            agent_id = await conn.fetchval(
                "SELECT id FROM agents WHERE name = $1 AND project = $2",
                agent, project,
            )
            if not agent_id:
                raise HTTPException(
                    403,
                    f"Agent '{agent}' is not registered in {project}; register "
                    "the agent first with POST /agents or cortex-add-agent "
                    "before ingesting session history.",
                )

            # 2. UPSERT the agent_session (idempotent on session id).
            await conn.execute(
                """INSERT INTO agent_sessions
                       (id, agent_id, project, task, started_at, notes)
                   VALUES ($1::uuid, $2, $3, $4, NOW(), $5::jsonb)
                   ON CONFLICT (id) DO UPDATE SET
                       agent_id = EXCLUDED.agent_id,
                       project  = EXCLUDED.project,
                       task     = EXCLUDED.task,
                       notes    = EXCLUDED.notes""",
                session_uuid, agent_id, project,
                body.task or f"{body.provider} {session_uuid[:8]}",
                metadata_json,
            )

            # 3. UPSERT the session_sources row.
            await conn.execute(
                """INSERT INTO session_sources
                       (session_id, project, source_path, provider, agent_name,
                        cwd, git_branch, source_kind, metadata)
                   VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8, $9::jsonb)
                   ON CONFLICT (session_id) DO UPDATE SET
                       project     = EXCLUDED.project,
                       source_path = EXCLUDED.source_path,
                       provider    = EXCLUDED.provider,
                       agent_name  = EXCLUDED.agent_name,
                       cwd         = EXCLUDED.cwd,
                       git_branch  = EXCLUDED.git_branch,
                       source_kind = EXCLUDED.source_kind,
                       metadata    = EXCLUDED.metadata,
                       ingested_at = NOW()""",
                session_uuid, project, body.source_path, body.provider,
                agent, body.cwd, body.git_branch,
                body.source_kind or f"{body.provider}-session",
                metadata_json,
            )

            # 4. Replace messages atomically — DELETE then bulk INSERT, matching
            #    legacy semantics where session files on disk are append-only
            #    so re-ingest treats the file as the source of truth.
            await conn.execute(
                "DELETE FROM messages WHERE session_id = $1::uuid AND project = $2",
                session_uuid,
                project,
            )

            # E2+E4 Config Flags
            e2_enabled = os.environ.get("CORTEX_E2_DISTILL") == "1"
            e4_enabled = os.environ.get("CORTEX_E4_COMPACT") == "1"

            messages_inserted = 0
            hot_message_rows: list[tuple[str, str, str, str, str, str, datetime, bool]] = []
            for role, content, ts, msg_metadata in translated_messages:
                msg_ts = ts or datetime.now(timezone.utc)
                msg_metadata = msg_metadata or {"provider": body.provider}

                # --- Phase 1: Archive (Cold Tier) ---
                # E2 replaces the hot text with distilled commitments, so retain
                # the raw original first. E2/E4 activation remains held in v0.2.002.
                archive_id = None
                if e2_enabled:
                    # Compress content
                    raw_bytes = content.encode("utf-8")
                    if zstd:
                        compressed = zstd.compress(raw_bytes)
                        # Header byte 0x01 = zstd
                        content_zstd = b"\x01" + compressed
                    else:
                        compressed = zlib.compress(raw_bytes)
                        # Header byte 0x00 = zlib
                        content_zstd = b"\x00" + compressed

                    archive_id = await conn.fetchval(
                        """INSERT INTO archive_messages
                               (session_id, project, agent_name, role, content,
                                content_zstd, ts)
                           VALUES ($1::uuid, $2, $3, $4, $5, $6, $7)
                           RETURNING id""",
                        session_uuid, project, agent, role, content,
                        content_zstd, msg_ts,
                    )

                # --- Phase 2: Distillation (E2), then Compaction (E4) ---
                to_insert = []
                if e2_enabled and not is_always_keep(content):
                    # Distill into commitments, then compact each hot commitment.
                    # Compacting the original first is wasted work because E2 replaces
                    # it; more importantly, it used to make the live path diverge from
                    # the E2+E4 recall gate and silently discard E4's result.
                    commitments = distill_message(content)
                    for c in commitments:
                        c_meta = dict(msg_metadata)
                        c_meta.update(c.get("metadata", {}))
                        c_meta["distilled_from_archive_id"] = archive_id
                        commitment = c["content"]
                        if e4_enabled:
                            commitment, is_compacted, _ = compact_for_storage(commitment)
                            if is_compacted:
                                c_meta["compacted"] = True
                        to_insert.append((role, commitment, True, c_meta))
                else:
                    # Keep whole (Baseline or Always-Keep floor)
                    final_content = content
                    is_compacted = False
                    if e4_enabled:
                        final_content, is_compacted, _ = compact_for_storage(content)
                    m_meta = dict(msg_metadata)
                    if archive_id:
                        m_meta["distilled_from_archive_id"] = archive_id
                    if is_compacted:
                        m_meta["compacted"] = True
                    to_insert.append((role, final_content, False, m_meta))

                # --- Phase 4: Insert to Hot Tier ---
                for r, c, dist, meta in to_insert:
                    hot_message_rows.append(
                        (
                            session_uuid, project, agent, r, c,
                            json.dumps(meta), msg_ts, dist,
                        )
                    )
                    messages_inserted += 1

            if hot_message_rows:
                await conn.executemany(
                    """INSERT INTO messages
                           (session_id, project, agent_name, role, content,
                            metadata, ts, distilled)
                       VALUES ($1::uuid, $2, $3, $4, $5, $6::jsonb, $7, $8)""",
                    hot_message_rows,
                )

            # 5. Mark the session ingested (matches legacy UPDATE).
            await conn.execute(
                """UPDATE agent_sessions
                      SET outcome = 'ingested', notes = $1::jsonb
                    WHERE id = $2::uuid AND project = $3""",
                metadata_json, session_uuid, project,
            )

    return {
        "session_id": session_uuid,
        "agent_id": str(agent_id),
        "messages_inserted": messages_inserted,
    }


# ---------------------------------------------------------------------------
# POST /artifacts
# ---------------------------------------------------------------------------


@app.post("/project-local-sync", response_model=ProjectLocalSyncResponse)
async def project_local_sync(
    body: ProjectLocalSyncRequest,
    x_project: str = Header(alias="X-Project", default=""),
):
    """
    Global validation + checkpoint API.
    Receives local edge cache SQLite events/entities/relationships, validates auth boundaries
    using RLS, appends to the canonical global Cortex store, and returns the global checkpoint.
    """
    project = require_project_scope(x_project)
    await require_registered_project(project)

    accepted_events = 0
    accepted_entities = 0
    accepted_relationships = 0

    async with acquire_scoped(project) as conn:
        # 1. Sync team_events
        if body.team_events:
            event_tuples = []
            for ev in body.team_events:
                agent = agent_base_for_project(ev.agent_name, project, field_name="agent_name")
                # NOTE: this guard runs INSIDE the outer acquire_scoped(project)
                # (4881) and inside the per-event loop. require_registered_agent_writer
                # opens its OWN short-lived reads (resolver), so it uses different
                # pooled conns than the outer one; after the first event the 30s TTL
                # cache makes subsequent iterations a dict lookup (no nested acquire).
                await require_registered_agent_writer(
                    project,
                    agent,
                    scope="system-event",
                )
                agent = await compound_agent(agent, project)
                ts_dt: Optional[datetime] = None
                if ev.ts:
                    try:
                        ts_dt = datetime.fromisoformat(ev.ts.replace("Z", "+00:00"))
                    except ValueError as exc:
                        raise HTTPException(
                            400, f"team_events.ts={ev.ts!r} is not ISO-8601: {exc}"
                        ) from exc

                event_tuples.append((agent, ev.event_type, ev.summary, ev.detail, project, ts_dt))

            await conn.executemany(
                """
                INSERT INTO team_events (agent_name, event_type, summary, detail, project, ts)
                VALUES ($1, $2, $3, $4, $5, COALESCE($6::timestamptz, NOW()))
                """,
                event_tuples
            )
            accepted_events = len(event_tuples)

        # 2. Sync entities
        if body.entities:
            entity_tuples = []
            for ent in body.entities:
                entity_tuples.append((ent.name, ent.type, ent.description, project, json.dumps(ent.metadata) if ent.metadata else None))

            await conn.executemany(
                """
                INSERT INTO entities (name, type, description, project, metadata)
                VALUES ($1, $2, $3, $4, $5::jsonb)
                ON CONFLICT (project, name, type) DO UPDATE
                SET description = EXCLUDED.description,
                    metadata = EXCLUDED.metadata,
                    updated_at = NOW()
                """,
                entity_tuples
            )
            accepted_entities = len(entity_tuples)

        # 3. Sync relationships
        if body.relationships:
            rel_tuples = []
            for rel in body.relationships:
                rel_tuples.append((rel.source, rel.target, rel.edge_type, project, json.dumps(rel.metadata) if rel.metadata else None))

            await conn.executemany(
                """
                INSERT INTO relationships (source, target, edge_type, project, metadata)
                VALUES ($1, $2, $3, $4, $5::jsonb)
                ON CONFLICT (project, source, target, edge_type) DO UPDATE
                SET metadata = EXCLUDED.metadata,
                    updated_at = NOW()
                """,
                rel_tuples
            )
            accepted_relationships = len(rel_tuples)

        # 4. Generate checkpoint (max team_events ID for this project)
        checkpoint_row = await conn.fetchrow(
            "SELECT COALESCE(MAX(id), 0)::bigint AS max_id FROM team_events WHERE project = $1",
            project
        )
        checkpoint = checkpoint_row["max_id"] if checkpoint_row else 0

    return ProjectLocalSyncResponse(
        accepted_events=accepted_events,
        accepted_entities=accepted_entities,
        accepted_relationships=accepted_relationships,
        checkpoint=checkpoint
    )


@app.post("/artifacts")
async def ingest_artifact(
    body: ArtifactIngestRequest,
    x_agent: str = Header(alias="X-Agent-Name"),
    x_project: str = Header(alias="X-Project", default=""),
):
    project = require_project_scope(x_project)
    agent = validate_agent_name(x_agent)
    await require_registered_agent_writer(project, agent, scope="system-event")
    agent = await compound_agent(agent, project)

    if not body.source_file.strip():
        raise HTTPException(400, "source_file is required")
    if not re.fullmatch(r"[a-fA-F0-9]{64}", body.content_hash.strip()):
        raise HTTPException(400, "content_hash must be a 64-character sha256 hex digest")
    if any([body.edge_type, body.target_type, body.target_ref]) and not all(
        [body.edge_type, body.target_type, body.target_ref]
    ):
        raise HTTPException(400, "edge_type, target_type, and target_ref must be provided together")

    metadata = dict(body.metadata or {})
    metadata.setdefault("agent_name", agent)
    source_doc_metadata = dict(body.source_doc_metadata or {})

    async with acquire_scoped(project) as conn:
        row = await conn.fetchrow(
            """
            WITH upserted AS (
                INSERT INTO artifacts (
                    project, customer_id, org_id, parent_artifact_id,
                    modality, source_type, source_file, extraction_method,
                    content_hash, raw_content, section_context, metadata,
                    caption, neighborhood_text, source_doc_metadata, updated_at
                ) VALUES (
                    $1, $2::uuid, $3::uuid, $4::uuid,
                    $5, $6, $7, $8,
                    $9, $10, $11, $12::jsonb,
                    $13, $14, $15::jsonb, NOW()
                )
                ON CONFLICT (project, source_file, content_hash) DO UPDATE
                SET customer_id = COALESCE(EXCLUDED.customer_id, artifacts.customer_id),
                    org_id = COALESCE(EXCLUDED.org_id, artifacts.org_id),
                    parent_artifact_id = COALESCE(EXCLUDED.parent_artifact_id, artifacts.parent_artifact_id),
                    modality = EXCLUDED.modality,
                    source_type = EXCLUDED.source_type,
                    extraction_method = EXCLUDED.extraction_method,
                    raw_content = COALESCE(EXCLUDED.raw_content, artifacts.raw_content),
                    section_context = COALESCE(EXCLUDED.section_context, artifacts.section_context),
                    metadata = COALESCE(artifacts.metadata, '{}'::jsonb) || COALESCE(EXCLUDED.metadata, '{}'::jsonb),
                    caption = COALESCE(EXCLUDED.caption, artifacts.caption),
                    neighborhood_text = COALESCE(EXCLUDED.neighborhood_text, artifacts.neighborhood_text),
                    source_doc_metadata = COALESCE(artifacts.source_doc_metadata, '{}'::jsonb)
                        || COALESCE(EXCLUDED.source_doc_metadata, '{}'::jsonb),
                    updated_at = NOW()
                RETURNING id, modality, extraction_method, source_file
            )
            SELECT id::text, modality, extraction_method, source_file
              FROM upserted
            """,
            project,
            str(body.customer_id) if body.customer_id else None,
            str(body.org_id) if body.org_id else None,
            str(body.parent_artifact_id) if body.parent_artifact_id else None,
            body.modality,
            body.source_type,
            body.source_file,
            body.extraction_method,
            body.content_hash.lower(),
            body.raw_content,
            body.section_context,
            json.dumps(metadata),
            body.caption,
            body.neighborhood_text,
            json.dumps(source_doc_metadata),
        )
        edge_created = False
        if body.edge_type and body.target_type and body.target_ref:
            edge_status = await conn.execute(
                """
                INSERT INTO artifact_edges (
                    project, source_id, target_type, target_ref, edge_type, metadata
                ) VALUES ($1, $2::uuid, $3, $4, $5, $6::jsonb)
                ON CONFLICT (project, source_id, target_type, target_ref, edge_type) DO NOTHING
                """,
                project,
                row["id"],
                body.target_type,
                body.target_ref,
                body.edge_type,
                json.dumps({"agent_name": agent}),
            )
            edge_created = edge_status.endswith("1")

        await emit_team_event(
            conn,
            project=project,
            agent_name=agent,
            event_type="artifact",
            summary=f"ingested {body.modality or 'artifact'} {body.source_file}"[:200],
            detail={
                "artifact_id": row["id"],
                "source_file": row["source_file"],
                "source_type": body.source_type,
                "modality": row["modality"],
                "extraction_method": row["extraction_method"],
                "edge_created": edge_created,
            },
            files=[row["source_file"]],
            notify=event_backend_uses_postgres(),
        )

    return {
        "id": row["id"],
        "project": project,
        "agent": agent,
        "modality": row["modality"],
        "extraction_method": row["extraction_method"],
        "source_file": row["source_file"],
        "edge_created": edge_created,
    }


# ---------------------------------------------------------------------------
# Work Product Memory
# ---------------------------------------------------------------------------


@app.post("/work-products")
async def write_work_product(
    body: WorkProductWrite,
    x_agent: str = Header(alias="X-Agent-Name", default=""),
    x_project: str = Header(alias="X-Project", default=""),
):
    project = require_project_scope(x_project)
    actor_header = x_agent or body.agent_name or ""
    if not actor_header:
        raise HTTPException(400, "X-Agent-Name header or agent_name body field is required")
    actor = validate_agent_name(actor_header)
    await require_registered_agent_writer(project, actor, scope="work")
    await require_registered_project(project)
    actor_compound = await compound_agent(actor, project)
    record_agent = actor_compound
    if body.agent_name:
        record_agent = await compound_agent(validate_agent_name(body.agent_name), project)

    title = compact_text(body.title)
    summary = compact_text(body.summary)
    if not title:
        raise HTTPException(400, "work product title is required")
    if not summary:
        raise HTTPException(400, "work product summary is required")

    status = normalize_work_product_status(body.status)
    activity_type = normalize_work_product_activity(body.activity_type)
    files_changed = normalize_text_list(body.files_changed)
    symbols_changed = normalize_text_list(body.symbols_changed)
    subject_entities = normalize_text_list(body.subject_entities)
    artifact_refs = normalize_text_list(body.artifact_refs)
    risks = normalize_text_list(body.risks)
    followups = normalize_text_list(body.followups)
    tests_run = json_list(body.tests_run or [])
    caller_metadata = json_object(body.metadata)
    provenance = {
        "agent": record_agent,
        "actor": actor_compound,
        "used": ["handoff" if body.handoff_id else None],
        "generated": ["work_products"],
        **json_object(caller_metadata.get("provenance")),
    }
    provenance["used"] = [item for item in json_list(provenance.get("used")) if item]
    if not provenance["used"] and body.handoff_id:
        provenance["used"] = ["handoff"]
    if not json_list(provenance.get("generated")):
        provenance["generated"] = ["work_products"]
    metadata = {
        **caller_metadata,
        "schema": WORK_PRODUCT_SCHEMA_VERSION,
        "provenance": provenance,
        "embedding_status": "queued",
        "projection_status": "pending",
    }

    handoff_uuid: str | None = None
    supersedes_uuid: str | None = None
    payload_for_hash = {
        "activity_type": activity_type,
        "title": title,
        "summary": summary,
        "behavior_summary": body.behavior_summary,
        "architecture_notes": body.architecture_notes,
        "files_changed": files_changed,
        "symbols_changed": symbols_changed,
        "subject_entities": subject_entities,
        "artifact_refs": artifact_refs,
        "tests_run": tests_run,
        "risks": risks,
        "followups": followups,
    }
    content_hash = body.content_hash or work_product_content_hash(payload_for_hash)
    file_hashes = (
        {str(key): str(value) for key, value in (body.file_hashes or {}).items() if key and value}
        or compute_file_hashes(files_changed)
    )
    commit_sha = compact_text(
        body.commit_sha
        or caller_metadata.get("commit_sha")
        or json_object(caller_metadata.get("git")).get("commit_sha")
        or current_git_commit_sha()
    )
    freshness_status = "current" if file_hashes else "unknown"
    freshness_reason = None if file_hashes else "file_hashes_unavailable"

    async with acquire_scoped(project) as conn:
        await ensure_work_products_schema(conn)
        if body.handoff_id:
            handoff_row = await resolve_unique_handoff_for_mutation(
                conn,
                project=project,
                handoff_id=strip_compound_suffix(body.handoff_id),
            )
            handoff_uuid = handoff_row["id"]
        if body.supersedes_id:
            superseded = await resolve_unique_work_product(
                conn,
                project=project,
                work_product_id=strip_compound_suffix(body.supersedes_id),
            )
            supersedes_uuid = superseded["id"]

        existing = None
        if handoff_uuid:
            existing = await conn.fetchrow(
                """SELECT id::text
                     FROM work_products
                    WHERE project = $1
                      AND handoff_id = $2::uuid
                      AND invalidated_at IS NULL
                    ORDER BY updated_at DESC
                    LIMIT 1""",
                project,
                handoff_uuid,
            )

        if supersedes_uuid:
            await conn.execute(
                """UPDATE work_products
                      SET status = 'superseded',
                          invalidated_at = COALESCE(invalidated_at, NOW()),
                          valid_to = COALESCE(valid_to, NOW()),
                          updated_at = NOW()
                    WHERE id = $1::uuid
                      AND project = $2""",
                supersedes_uuid,
                project,
            )

        if existing:
            row = await conn.fetchrow(
                """UPDATE work_products
                      SET agent_name = $3,
                          activity_type = $4,
                          status = $5,
                          title = $6,
                          summary = $7,
                          behavior_summary = $8,
                          architecture_notes = $9,
                          files_changed = $10::text[],
                          symbols_changed = $11::text[],
                          subject_entities = $12::text[],
                          artifact_refs = $13::text[],
                          tests_run = $14::jsonb,
                          risks = $15::text[],
                          followups = $16::text[],
                          approval_status = $17,
                          content_hash = $18,
                          commit_sha = $19,
                          file_hashes = $20::jsonb,
                          freshness_status = $21,
                          freshness_reason = $22,
                          freshness_checked_at = NOW(),
                          projection_status = 'pending',
                          projection_error = NULL,
                          projected_at = NULL,
                          supersedes_id = $23::uuid,
                          metadata = COALESCE(metadata, '{}'::jsonb) || $24::jsonb,
                          embedding = NULL,
                          updated_at = NOW(),
                          valid_from = COALESCE(valid_from, NOW()),
                          valid_to = NULL,
                          invalidated_at = NULL
                    WHERE id = $1::uuid
                      AND project = $2
                    RETURNING *""",
                existing["id"],
                project,
                record_agent,
                activity_type,
                status,
                title,
                summary,
                body.behavior_summary,
                body.architecture_notes,
                files_changed,
                symbols_changed,
                subject_entities,
                artifact_refs,
                json.dumps(tests_run),
                risks,
                followups,
                body.approval_status,
                content_hash,
                commit_sha or None,
                json.dumps(file_hashes, sort_keys=True),
                freshness_status,
                freshness_reason,
                supersedes_uuid,
                json.dumps(metadata),
            )
            created = False
        else:
            row = await conn.fetchrow(
                """INSERT INTO work_products (
                        project, handoff_id, agent_name, activity_type, status,
                        title, summary, behavior_summary, architecture_notes,
                        files_changed, symbols_changed, subject_entities,
                        artifact_refs, tests_run, risks, followups,
                        approval_status, content_hash, commit_sha, file_hashes,
                        freshness_status, freshness_reason, supersedes_id,
                        metadata
                   ) VALUES (
                        $1, $2::uuid, $3, $4, $5,
                        $6, $7, $8, $9,
                        $10::text[], $11::text[], $12::text[],
                        $13::text[], $14::jsonb, $15::text[], $16::text[],
                        $17, $18, $19, $20::jsonb,
                        $21, $22, $23::uuid,
                        $24::jsonb
                   )
                   RETURNING *""",
                project,
                handoff_uuid,
                record_agent,
                activity_type,
                status,
                title,
                summary,
                body.behavior_summary,
                body.architecture_notes,
                files_changed,
                symbols_changed,
                subject_entities,
                artifact_refs,
                json.dumps(tests_run),
                risks,
                followups,
                body.approval_status,
                content_hash,
                commit_sha or None,
                json.dumps(file_hashes, sort_keys=True),
                freshness_status,
                freshness_reason,
                supersedes_uuid,
                json.dumps(metadata),
            )
            created = True

        event_id = await emit_team_event(
            conn,
            project=project,
            agent_name=actor_compound,
            event_type="work_product_recorded",
            summary=f"[WORK-PRODUCT:{str(row['id'])[:8]}] {title}"[:200],
            detail={
                "schema": WORK_PRODUCT_SCHEMA_VERSION,
                "work_product_id": str(row["id"]),
                "handoff_id": handoff_uuid,
                "activity_type": activity_type,
                "status": status,
                "content_hash": content_hash,
                "created": created,
                "embedded": False,
                "embedding_status": "queued",
            },
            files=files_changed,
            notify=event_backend_uses_postgres(),
        )
        await conn.execute(
            """UPDATE work_products
                  SET source_event_id = COALESCE(source_event_id, $2),
                      updated_at = NOW()
                WHERE id = $1::uuid""",
            row["id"],
                event_id,
        )
        graph_projection: dict[str, Any] = {"status": "not_run"}
        try:
            await ensure_graph_schema(conn)
            graph_projection = await project_graph_row(
                conn,
                project=project,
                row={
                    **dict(row),
                    "source_table": "work_products",
                    "source_event_id": event_id,
                },
                source_event_id=event_id,
            )
            graph_projection["status"] = "projected"
        except Exception as exc:  # noqa: BLE001 - graph enrichment must not drop L1 writes
            graph_projection = {
                "status": "failed",
                "error": compact_text(f"{type(exc).__name__}: {exc}", limit=240),
            }
            await conn.execute(
                """UPDATE work_products
                      SET projection_status = 'failed',
                          projection_error = $2,
                          metadata = COALESCE(metadata, '{}'::jsonb)
                              || jsonb_build_object(
                                    'entities_extracted', FALSE,
                                    'entity_projection_error', $2,
                                    'entity_projection_error_at', NOW()
                                 ),
                          updated_at = NOW()
                    WHERE id = $1::uuid""",
                row["id"],
                graph_projection["error"],
            )
        refreshed = await conn.fetchrow(
            "SELECT * FROM work_products WHERE id = $1::uuid AND project = $2",
            row["id"],
            project,
        )
        if refreshed:
            row = refreshed

    data = work_product_row_to_dict(row)
    data["source_event_id"] = data.get("source_event_id") or event_id
    return {
        "id": data["id"],
        "project": project,
        "status": data["status"],
        "created": created,
        "embedded": False,
        "embedding_status": "queued",
        "graph_projection": graph_projection,
        "event_id": event_id,
        "work_product": data,
        "warnings": ["embedding_deferred_to_backfill"],
    }


@app.get("/work-products")
async def list_work_products(
    q: str | None = Query(default=None),
    file: str | None = Query(default=None),
    symbol: str | None = Query(default=None),
    handoff_id: str | None = Query(default=None),
    status: str = Query(default="current"),
    limit: int = Query(default=20, ge=1, le=100),
    x_project: str = Header(alias="X-Project", default=""),
):
    project = require_project_scope(x_project)
    await require_registered_project(project)
    status_filter = None if status == "all" else normalize_work_product_status(status)
    handoff_uuid = None
    async with acquire_scoped(project) as conn:
        await ensure_work_products_schema(conn)
        if handoff_id:
            handoff_row = await resolve_unique_handoff_for_mutation(
                conn,
                project=project,
                handoff_id=strip_compound_suffix(handoff_id),
            )
            handoff_uuid = handoff_row["id"]
        rows = await fetch_work_product_briefs(
            conn,
            project,
            query=q,
            file=file,
            symbol=symbol,
            handoff_uuid=handoff_uuid,
            status=status_filter,
            limit=limit,
        )
    return {"project": project, "work_products": rows}


@app.get("/work-products/{work_product_id}")
async def get_work_product(
    work_product_id: str,
    x_project: str = Header(alias="X-Project", default=""),
):
    project = require_project_scope(x_project)
    await require_registered_project(project)
    async with acquire_scoped(project) as conn:
        await ensure_work_products_schema(conn)
        row = await resolve_unique_work_product(
            conn,
            project=project,
            work_product_id=strip_compound_suffix(work_product_id),
        )
    return row


@app.post("/beat/work-products/check-freshness")
async def beat_work_products_check_freshness(
    body: WorkProductFreshnessRequest,
    request: Request,
    x_project: str = Header(alias="X-Project", default=""),
):
    require_admin_access(request)
    project = require_project_scope(x_project)
    limit = max(1, min(int(body.limit or 100), 500))
    work_product_prefix = (
        strip_compound_suffix(body.work_product_id) if body.work_product_id else None
    )

    checked: list[dict[str, Any]] = []
    stale_count = 0
    current_count = 0
    unknown_count = 0
    supplied_hashes = {
        str(key): str(value)
        for key, value in (body.current_file_hashes or {}).items()
        if key and value
    }
    async with acquire_scoped(project) as conn:
        await ensure_work_products_schema(conn)
        rows = await conn.fetch(
            """
            SELECT id::text AS id, title, status, file_hashes
              FROM work_products
             WHERE project = $1
               AND invalidated_at IS NULL
               AND ($2::text IS NULL OR id::text LIKE $2 || '%')
             ORDER BY updated_at DESC
             LIMIT $3
            """,
            project,
            work_product_prefix,
            limit,
        )
        for row in rows:
            stored_hashes = json_object(row["file_hashes"])
            changed: list[str] = []
            missing: list[str] = []
            unavailable: list[str] = []
            if not stored_hashes:
                status = "unknown"
                reason = "file_hashes_unavailable"
                unknown_count += 1
            else:
                for file_ref, expected_hash in stored_hashes.items():
                    if file_ref in supplied_hashes:
                        current_hash = supplied_hashes[file_ref]
                    else:
                        path = resolve_workspace_file(file_ref)
                        current_hash = sha256_file(path) if path else None
                    if current_hash is None:
                        if supplied_hashes or body.treat_missing_as_stale:
                            missing.append(file_ref)
                        else:
                            unavailable.append(file_ref)
                    elif current_hash != expected_hash:
                        changed.append(file_ref)
                if changed or missing:
                    status = "stale"
                    stale_count += 1
                    parts = []
                    if changed:
                        parts.append("file_hash_changed:" + ",".join(changed[:10]))
                    if missing:
                        parts.append("file_missing:" + ",".join(missing[:10]))
                    reason = ";".join(parts)
                elif unavailable:
                    status = "unknown"
                    reason = "workspace_files_unavailable:" + ",".join(unavailable[:10])
                    unknown_count += 1
                else:
                    status = "current"
                    reason = None
                    current_count += 1

            checked.append(
                {
                    "id": row["id"],
                    "title": row["title"],
                    "status": status,
                    "reason": reason,
                    "changed": changed,
                    "missing": missing,
                    "unavailable": unavailable,
                }
            )
            if body.dry_run:
                continue
            await conn.execute(
                """
                UPDATE work_products
                   SET status = CASE WHEN $2 = 'stale' THEN 'stale' ELSE 'current' END,
                       freshness_status = $2,
                       freshness_reason = $3,
                       freshness_checked_at = NOW(),
                       valid_to = CASE
                           WHEN $2 = 'stale' THEN COALESCE(valid_to, NOW())
                           ELSE NULL
                       END,
                       metadata = COALESCE(metadata, '{}'::jsonb)
                           || jsonb_build_object(
                                'freshness_checked_at', NOW(),
                                'freshness_status', $2,
                                'freshness_reason', $3,
                                'freshness_changed_files', $4::jsonb,
                                'freshness_missing_files', $5::jsonb,
                                'freshness_unavailable_files', $7::jsonb
                              ),
                       updated_at = NOW()
                 WHERE id = $1::uuid
                   AND project = $6
                """,
                row["id"],
                status,
                reason,
                json.dumps(changed),
                json.dumps(missing),
                project,
                json.dumps(unavailable),
            )

    return {
        "project": project,
        "dry_run": body.dry_run,
        "checked": len(checked),
        "current": current_count,
        "stale": stale_count,
        "unknown": unknown_count,
        "work_products": checked,
    }


@app.get("/brief")
async def cortex_brief(
    q: str | None = Query(default=None),
    file: str | None = Query(default=None),
    symbol: str | None = Query(default=None),
    handoff_id: str | None = Query(default=None),
    status: str = Query(default="current"),
    limit: int = Query(default=5, ge=1, le=20),
    x_project: str = Header(alias="X-Project", default=""),
):
    project = require_project_scope(x_project)
    await require_registered_project(project)
    if not any([q, file, symbol, handoff_id]):
        raise HTTPException(400, "Provide q, file, symbol, or handoff_id")
    status_filter = None if status == "all" else normalize_work_product_status(status)
    handoff_uuid = None
    async with acquire_scoped(project) as conn:
        await ensure_work_products_schema(conn)
        if handoff_id:
            handoff_row = await resolve_unique_handoff_for_mutation(
                conn,
                project=project,
                handoff_id=strip_compound_suffix(handoff_id),
            )
            handoff_uuid = handoff_row["id"]
        rows = await fetch_work_product_briefs(
            conn,
            project,
            query=q,
            file=file,
            symbol=symbol,
            handoff_uuid=handoff_uuid,
            status=status_filter,
            limit=limit,
        )
    return {
        "project": project,
        "target": {
            "query": q,
            "file": file,
            "symbol": symbol,
            "handoff_id": handoff_id,
            "status": status,
        },
        "work_products": rows,
        "contract": "Use current work-product memory for orientation; read code for verification, edits, or stale/missing briefs.",
    }


# ---------------------------------------------------------------------------
# GET /search
# ---------------------------------------------------------------------------


_SEARCH_SEMAPHORE: asyncio.Semaphore | None = None


def _search_bulkhead() -> asyncio.Semaphore:
    """Lazily-created semaphore capping concurrent /search work.

    Built on first use rather than at import so it binds to the running loop
    (a module-level Semaphore created at import time attaches to whichever loop
    exists then, which breaks under pytest-asyncio's per-test loops).
    """
    global _SEARCH_SEMAPHORE
    if _SEARCH_SEMAPHORE is None:
        _SEARCH_SEMAPHORE = asyncio.Semaphore(SEARCH_MAX_CONCURRENCY)
    return _SEARCH_SEMAPHORE


@asynccontextmanager
async def search_bulkhead():
    """Shed load instead of queueing it (incident 912073e2).

    marlow-aws took 1,520 /search requests against a 10-connection pool while each
    was averaging 46s. Unbounded admission is what turned a slow query into a
    cluster outage: every waiting request still held a backend, so Postgres had
    more work in flight than it could drain, and callers that had already timed
    out were still being served. Refusing excess work with a typed 503 keeps
    boot/handoff/log responsive and lets clients back off honestly.
    """
    sem = _search_bulkhead()
    if sem.locked():
        raise HTTPException(
            503,
            detail={
                "error": "search_overloaded",
                "message": (
                    "Cortex search is at capacity; retry shortly. "
                    "This is load shedding, not a Cortex outage."
                ),
                "max_concurrency": SEARCH_MAX_CONCURRENCY,
            },
        )
    async with sem:
        yield


async def _execute_bounded_search(
    *,
    project: str,
    query: str,
    search_type: str,
    rerank: bool,
    room: str | None,
    hall: str,
    graph: bool,
    limit: int,
) -> dict[str, Any]:
    """Run the complete search path with typed transient-failure semantics.

    PostgreSQL rejects connections while crash recovery is in progress and drops
    existing sockets when a backend restarts. Those are temporary availability
    states, not application defects, so callers receive an explicit 503 and can back
    off. Schema/query errors remain uncaught and visible as real failures.
    """
    try:
        async with search_bulkhead():
            # Registration is a database read too. Admit it inside the bulkhead so
            # rejected search traffic cannot saturate the pool before containment.
            await require_registered_project(project)
            async with acquire_scoped(
                project, statement_timeout_ms=SEARCH_STATEMENT_TIMEOUT_MS
            ) as conn:
                return await execute_search(
                    conn,
                    project,
                    query,
                    search_type=search_type,
                    rerank=rerank,
                    room=room,
                    hall=hall,
                    graph=graph,
                    limit=limit,
                )
    except asyncpg.exceptions.QueryCanceledError as exc:
        raise HTTPException(
            503,
            detail={
                "error": "search_timed_out",
                "message": "Cortex search exceeded its database deadline; retry shortly.",
            },
        ) from exc
    except (
        asyncpg.exceptions.CannotConnectNowError,
        asyncpg.PostgresConnectionError,
        asyncpg.InterfaceError,
    ) as exc:
        raise HTTPException(
            503,
            detail={
                "error": "search_unavailable",
                "message": "Cortex search storage is temporarily unavailable; retry shortly.",
            },
        ) from exc


@app.get("/search")
async def search(
    q: str = Query(..., min_length=1),
    x_project: str = Header(alias="X-Project", default=""),
    type: str = Query(default="all"),
    rerank: bool = Query(default=True),
    room: str | None = Query(default=None),
    hall: str = Query(default="project"),
    graph: bool = Query(default=False),
    limit: int = Query(default=20, ge=1, le=100),
):
    if not isinstance(limit, int):
        limit = 20
    project = require_project_scope(x_project)
    return await _execute_bounded_search(
        project=project,
        query=q,
        search_type=type,
        rerank=rerank,
        room=room,
        hall=hall,
        graph=graph,
        limit=limit,
    )


class SearchRequest(BaseModel):
    query: str
    top_k: int = 25
    rerank: bool = True
    room: Optional[str] = None
    hall: str = "project"
    enable_graph: bool = False


@app.post("/search")
async def search_post(
    body: SearchRequest,
    x_project: str = Header(alias="X-Project", default=""),
):
    project = require_project_scope(x_project)
    return await _execute_bounded_search(
        project=project,
        query=body.query,
        search_type="all",
        rerank=body.rerank,
        room=body.room,
        hall=body.hall,
        graph=body.enable_graph,
        limit=body.top_k,
    )


# ---------------------------------------------------------------------------
# Layer 4 graph endpoints
# ---------------------------------------------------------------------------


@app.get("/cortex-graph-search")
async def cortex_graph_search(
    q: str = Query(..., min_length=1),
    x_project: str = Header(alias="X-Project", default=""),
    expand: bool = Query(default=False),
    high: bool = Query(default=False),
    low: bool = Query(default=False),
    limit: int = Query(default=100, ge=1, le=500),
):
    project = require_project_scope(x_project)
    mode = "both"
    if high and not low:
        mode = "high"
    elif low and not high:
        mode = "low"

    async with acquire_scoped(project) as conn:
        await ensure_graph_schema(conn)

        high_rows: list[dict[str, Any]] = []
        low_rows: list[dict[str, Any]] = []
        if mode in {"both", "high"}:
            high_rows = await graph_entity_search(conn, project, q, GRAPH_HIGH_TYPES, limit)
        if mode in {"both", "low"}:
            low_rows = await graph_entity_search(conn, project, q, GRAPH_LOW_TYPES, limit)

        relationships: list[dict[str, Any]] = []
        if expand:
            matched_ids = [row["id"] for row in high_rows + low_rows if row.get("id")]
            relationships = await graph_relationship_search(conn, project, matched_ids, limit * 3)

    return {
        "query": q,
        "project": project,
        "mode": mode,
        "expanded": expand,
        "high_level": high_rows,
        "low_level": low_rows,
        "relationships": relationships,
    }


@app.get("/cortex-graph/stats")
async def cortex_graph_project_stats(
    x_project: str = Header(alias="X-Project", default=""),
):
    """Project-scoped L4 graph stats.

    `/graph/stats` is the L3 code-graph worker aggregate. This endpoint is the
    complementary Cortex-native L4 view: extracted entities, relationships, and
    source backlogs for the selected project. It stays project-scoped through the
    regular RLS path so console Graph can explain "empty graph" without admin
    access or direct database reads.
    """
    project = require_project_scope(x_project)
    async with acquire_scoped(project) as conn:
        await ensure_graph_schema(conn)
        await ensure_work_products_schema(conn)
        return await graph_stats(conn, project)


@app.get("/cortex-graph/memory")
async def cortex_memory_graph(
    x_project: str = Header(alias="X-Project", default=""),
    limit: int = Query(default=500, ge=1, le=2000),
):
    """Project-scoped L4 memory graph without a search term.

    This returns the current project's extracted Cortex entities and their
    relationships directly, so Kaidera OS can render an Obsidian-style memory graph
    even when the search-seed heuristics return an empty neighbourhood.
    """
    project = require_project_scope(x_project)
    async with acquire_scoped(project) as conn:
        await ensure_graph_schema(conn)
        await ensure_work_products_schema(conn)
        entity_rows = await conn.fetch(
            """
            SELECT id::text,
                   name,
                   entity_type,
                   COALESCE(properties->>'description', '') AS description,
                   CASE
                       WHEN jsonb_typeof(properties->'source_refs') = 'array'
                       THEN properties->'source_refs'
                       ELSE '[]'::jsonb
                   END AS source_refs,
                   CASE
                       WHEN jsonb_typeof(properties->'source_refs') = 'array'
                       THEN jsonb_array_length(properties->'source_refs')
                       ELSE 0
                   END AS source_count,
                   updated_at::text
              FROM cortex_entities
             WHERE project = $1
             ORDER BY
                   source_count DESC,
                   updated_at DESC,
                   name
             LIMIT $2
            """,
            project,
            limit,
        )
        entity_ids = [row["id"] for row in entity_rows]
        relationship_rows = []
        if entity_ids:
            relationship_rows = await conn.fetch(
                """
                SELECT r.id::text,
                       r.relationship_type,
                       COALESCE(r.properties->>'description', '') AS description,
                       s.id::text AS source_id,
                       s.name AS source,
                       s.entity_type AS source_type,
                       t.id::text AS target_id,
                       t.name AS target,
                       t.entity_type AS target_type
                  FROM cortex_relationships r
                  JOIN cortex_entities s ON r.source_entity_id = s.id
                  JOIN cortex_entities t ON r.target_entity_id = t.id
                 WHERE r.project = $1
                   AND s.id::text = ANY($2::text[])
                   AND t.id::text = ANY($2::text[])
                 ORDER BY r.created_at DESC
                 LIMIT $3
                """,
                project,
                entity_ids,
                min(limit * 3, 4000),
            )
        source_refs_by_key: dict[tuple[str, str], set[str]] = {}
        source_ids: dict[str, set[str]] = {
            "decisions": set(),
            "lessons": set(),
            "knowledge": set(),
            "work_products": set(),
        }
        for row in entity_rows:
            entity_name = str(row["name"])
            for ref in json_list(row["source_refs"]):
                if not isinstance(ref, dict):
                    continue
                table = str(ref.get("table") or "").strip()
                source_id = str(ref.get("id") or "").strip()
                if table not in source_ids or not source_id:
                    continue
                source_ids[table].add(source_id)
                source_refs_by_key.setdefault((table, source_id), set()).add(entity_name)

        source_rows: list[dict[str, Any]] = []
        if source_ids["decisions"]:
            rows = await conn.fetch(
                """
                SELECT id::text AS id,
                       'decisions' AS source_table,
                       'decision' AS source_type,
                       LEFT(summary, 180) AS label,
                       LEFT(COALESCE(rationale, outcome, summary), 420) AS description,
                       created_at::text AS updated_at
                  FROM decisions
                 WHERE project = $1
                   AND invalidated_at IS NULL
                   AND id::text = ANY($2::text[])
                """,
                project,
                sorted(source_ids["decisions"]),
            )
            source_rows.extend(dict(row) for row in rows)
        if source_ids["lessons"]:
            rows = await conn.fetch(
                """
                SELECT id::text AS id,
                       'lessons' AS source_table,
                       'lesson' AS source_type,
                       LEFT(summary, 180) AS label,
                       LEFT(CONCAT_WS(E'\n\n', detail, code_right, code_wrong), 420) AS description,
                       created_at::text AS updated_at
                  FROM lessons
                 WHERE project = $1
                   AND invalidated_at IS NULL
                   AND id::text = ANY($2::text[])
                """,
                project,
                sorted(source_ids["lessons"]),
            )
            source_rows.extend(dict(row) for row in rows)
        if source_ids["knowledge"]:
            rows = await conn.fetch(
                """
                SELECT id::text AS id,
                       'knowledge' AS source_table,
                       'knowledge' AS source_type,
                       LEFT(COALESCE(section, category, source_file, content), 180) AS label,
                       LEFT(content, 420) AS description,
                       updated_at::text AS updated_at
                  FROM knowledge
                 WHERE project = $1
                   AND id::text = ANY($2::text[])
                """,
                project,
                sorted(source_ids["knowledge"]),
            )
            source_rows.extend(dict(row) for row in rows)
        if source_ids["work_products"]:
            rows = await conn.fetch(
                """
                SELECT id::text AS id,
                       'work_products' AS source_table,
                       'work_product' AS source_type,
                       LEFT(title, 180) AS label,
                       LEFT(summary, 420) AS description,
                       updated_at::text AS updated_at
                  FROM work_products
                 WHERE project = $1
                   AND invalidated_at IS NULL
                   AND id::text = ANY($2::text[])
                """,
                project,
                sorted(source_ids["work_products"]),
            )
            source_rows.extend(dict(row) for row in rows)

    return {
        "project": project,
        "nodes": [
            {
                "id": row["name"],
                "label": row["name"],
                "name": row["name"],
                "entity_type": row["entity_type"],
                "description": row["description"],
                "source_count": row["source_count"],
                "entity_id": row["id"],
                "updated_at": row["updated_at"],
            }
            for row in entity_rows
        ],
        "edges": [
            {
                "id": row["id"],
                "source": row["source"],
                "target": row["target"],
                "relationship_type": row["relationship_type"],
                "description": row["description"],
                "source_type": row["source_type"],
                "target_type": row["target_type"],
                "source_entity_id": row["source_id"],
                "target_entity_id": row["target_id"],
            }
            for row in relationship_rows
        ],
        "sources": [
            {
                "id": f"source:{row['source_table']}:{row['id']}",
                "source_id": row["id"],
                "source_table": row["source_table"],
                "source_type": row["source_type"],
                "label": row["label"] or f"{row['source_type']} {str(row['id'])[:8]}",
                "description": row["description"] or "",
                "updated_at": row["updated_at"],
            }
            for row in source_rows
        ],
        "source_edges": [
            {
                "id": f"source-edge:{table}:{source_id}:{entity_name}",
                "source": f"source:{table}:{source_id}",
                "target": entity_name,
                "relationship_type": "extracted_entity",
            }
            for (table, source_id), entity_names in sorted(source_refs_by_key.items())
            for entity_name in sorted(entity_names)
        ],
    }


@app.post("/cortex-graph-extract")
async def cortex_graph_extract(
    body: GraphExtractRequest,
    request: Request,
    x_project: str = Header(alias="X-Project", default=""),
):
    require_admin_access(request)
    project = require_project_scope(body.project or x_project)
    source = "all" if body.backfill else graph_source_label(body.source)
    source_tables = graph_source_tables(source)
    if body.limit < 1:
        raise HTTPException(400, "limit must be greater than zero")
    use_llm = bool(body.use_llm or GRAPH_LLM_ENABLED)
    platform_config = await load_cortex_platform_config_cached() if use_llm else {}

    async with acquire_scoped(project) as conn:
        await ensure_graph_schema(conn)
        await ensure_work_products_schema(conn)
        stats_before = await graph_stats(conn, project)
        rows = await fetch_graph_source_rows(
            conn,
            project=project,
            source=source,
            limit=body.limit,
            reprocess=body.reprocess,
        )
        processed: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        if not body.dry_run:
            for row in rows:
                try:
                    processed.append(
                        await project_graph_row(
                            conn,
                            project=project,
                            row=row,
                            source_event_id=row.get("source_event_id"),
                            use_llm=use_llm,
                            platform_config=platform_config,
                            model_override=body.model,
                        )
                    )
                except Exception as exc:  # noqa: BLE001
                    errors.append(
                        {
                            "source": str(row.get("source_table") or ""),
                            "id": str(row.get("id") or ""),
                            "error": compact_text(f"{type(exc).__name__}: {exc}", limit=240),
                        }
                    )
        stats_after = await graph_stats(conn, project)

    return {
        "status": "dry-run" if body.dry_run else "processed",
        "project": project,
        "source": source,
        "source_tables": list(source_tables),
        "limit": body.limit,
        "dry_run": body.dry_run,
        "reprocess": body.reprocess,
        "use_llm": use_llm,
        "selected": len(rows),
        "processed": len(processed),
        "errors": errors,
        "stats_before": stats_before,
        "stats": stats_after,
        "cli": f"cortex-graph-extract --source {source} --limit {body.limit}",
        "note": "Extraction runs inside cortex-api through the scoped API database path; host psql workers are not required.",
    }


@app.get("/beat/projections/status")
async def beat_projections_status(
    request: Request,
    recent_limit: int = Query(default=10, ge=1, le=50),
    x_project: str = Header(alias="X-Project", default=""),
):
    """Read-only operator status for Cortex projection/rebuild surfaces."""
    require_admin_access(request)
    project = require_project_scope(x_project)
    async with acquire_scoped(project) as conn:
        return await projection_status_snapshot(conn, project, recent_limit=recent_limit)


# ---------------------------------------------------------------------------
# Internal worker proxy endpoints
# ---------------------------------------------------------------------------


async def proxy_worker_json(
    worker_url: str,
    path: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    data: Mapping[str, str] | None = None,
    files: Mapping[str, tuple[str, Any, str | None]] | None = None,
    timeout: float = 120.0,
) -> dict[str, Any]:
    """Proxy cortex-api calls to an internal worker on cortex-net."""
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.request(
                method,
                f"{worker_url}{path}",
                json=payload,
                params=params,
                data=data,
                files=files,
            )
            response.raise_for_status()
            result = response.json()
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text[-1000:] or exc.response.reason_phrase
        raise HTTPException(exc.response.status_code, detail) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"worker unavailable: {exc}") from exc
    if not isinstance(result, dict):
        raise HTTPException(502, "worker returned non-object JSON")
    return result


@app.post("/artifacts/transcribe")
async def transcribe_artifact(
    artifact: UploadFile = File(...),
    model: Optional[str] = Form(None),
    x_agent: str = Header(alias="X-Agent-Name"),
    x_project: str = Header(alias="X-Project", default=""),
):
    project = require_project_scope(x_project)
    agent = validate_agent_name(x_agent)
    await require_registered_agent_writer(project, agent, scope="system-event")
    # The legacy alias rides the single bounded forward path (78e727a8):
    # health gate before the caller's bytes are touched, byte cap enforced,
    # response capped and decoded. Normalisation below is unchanged.
    forwarded = await _forward_media_upload(
        AUDIO_WORKER_URL,
        upload=artifact,
        kind="audio",
        path="/transcribe",
        field="audio",
        filename=artifact.filename or "artifact.audio",
        form={"model": model} if model else {},
        max_bytes=MEDIA_TRANSCRIBE_MAX_BYTES,
        timeout=MEDIA_TRANSCRIBE_TIMEOUT,
    )
    if isinstance(forwarded, JSONResponse):
        return forwarded
    _raw_response, result = forwarded
    text = result.get("text")
    if not isinstance(text, str) or not text.strip():
        raise HTTPException(502, "audio worker returned an empty transcription")
    modality = result.get("modality")
    if modality not in {"audio", "video"}:
        modality = "audio"
    return {
        "modality": modality,
        "content": text.strip(),
        "model": result.get("model"),
        "language": result.get("language"),
        "audio_extracted": bool(result.get("audio_extracted")),
        "extraction_method": (
            "transcribed_video_audio" if modality == "video" else "transcribed"
        ),
    }


@app.post("/artifacts/describe-image")
async def describe_artifact_image(
    artifact: UploadFile = File(...),
    prompt: Optional[str] = Form(None),
    model: Optional[str] = Form(None),
    x_agent: str = Header(alias="X-Agent-Name"),
    x_project: str = Header(alias="X-Project", default=""),
):
    project = require_project_scope(x_project)
    agent = validate_agent_name(x_agent)
    await require_registered_agent_writer(project, agent, scope="system-event")
    # Bounded forward path, identical caps to the legacy transcribe alias.
    forwarded = await _forward_media_upload(
        VISION_WORKER_URL,
        upload=artifact,
        kind="vision",
        path="/describe-image",
        field="image",
        filename=artifact.filename or "artifact.image",
        form={
            name: value
            for name, value in (("prompt", prompt), ("model", model))
            if value
        },
        max_bytes=MEDIA_DESCRIBE_MAX_BYTES,
        timeout=MEDIA_DESCRIBE_TIMEOUT,
    )
    if isinstance(forwarded, JSONResponse):
        return forwarded
    _raw_response, result = forwarded
    description = result.get("description")
    if not isinstance(description, str) or not description.strip():
        raise HTTPException(502, "vision worker returned an empty description")
    return {
        "modality": "image",
        "content": description.strip(),
        "model": result.get("model"),
        "extraction_method": "vlm_enriched",
    }


@app.get("/workers/health")
async def workers_health(
    x_project: str = Header(alias="X-Project", default=""),
):
    require_project_scope(x_project)
    workers = {
        "graph": GRAPH_WORKER_URL,
        "vision": VISION_WORKER_URL,
        "audio": AUDIO_WORKER_URL,
        "pdf": PDF_WORKER_URL,
        "embed": EMBED_WORKER_URL,
    }
    out: dict[str, Any] = {}
    for name, url in workers.items():
        try:
            out[name] = await proxy_worker_json(url, "/health", timeout=3.0)
        except HTTPException as exc:
            out[name] = {"ok": False, "status_code": exc.status_code, "error": exc.detail}
    return {"workers": out}


# ---------------------------------------------------------------------------
# Media enrichment worker proxies (audio + vision)
# ---------------------------------------------------------------------------
#
# cortex-audio-worker (:9003) and cortex-vision-worker (:9002) run only under
# the compose `full` profile, so on a default install these routes must
# degrade gracefully: a bounded health gate rejects before the caller's upload
# bytes are consumed, and every failure mode maps to a typed 502/503 — never
# a 500. No toggles or lifecycle control here; the workers stay manually
# activated via compose profiles.

MEDIA_STATUS_TIMEOUT = 2.0
MEDIA_TRANSCRIBE_TIMEOUT = 300.0
MEDIA_DESCRIBE_TIMEOUT = 300.0
MEDIA_TRANSCRIBE_MAX_BYTES = 200 * 1024 * 1024
MEDIA_DESCRIBE_MAX_BYTES = 64 * 1024 * 1024
MEDIA_WORKER_RESPONSE_MAX_BYTES = 32 * 1024 * 1024
_MEDIA_UNREACHABLE_DETAIL = "worker not deployed (profile core)"


async def probe_media_worker(worker_url: str) -> tuple[bool, bool, str]:
    """Return (reachable, ok, detail) for one media worker. Never raises.

    reachable means TCP/HTTP contact within MEDIA_STATUS_TIMEOUT; ok means the
    worker's own /health payload reported ok == True.
    """
    try:
        async with httpx.AsyncClient(timeout=MEDIA_STATUS_TIMEOUT) as client:
            response = await client.get(f"{worker_url}/health")
    except Exception:
        return False, False, _MEDIA_UNREACHABLE_DETAIL
    if response.status_code != 200:
        return True, False, f"health returned {response.status_code}"
    try:
        payload = response.json()
    except ValueError:
        return True, False, "health not ok"
    if isinstance(payload, dict) and payload.get("ok") is True:
        return True, True, "healthy"
    return True, False, "health not ok"


@app.get("/media/status")
async def media_status(
    x_project: str = Header(alias="X-Project", default=""),
):
    """Operational probe for the opt-in audio/vision workers.

    Worker URLs are global (compose-profile services), so the route is global;
    X-Project stays optional and is validated only when supplied. This endpoint
    must always answer with the contract shape — never a 5xx.
    """
    if (x_project or "").strip():
        require_project_scope(x_project)
    (audio_reachable, audio_ok, audio_detail), (
        vision_reachable,
        vision_ok,
        vision_detail,
    ) = await asyncio.gather(
        probe_media_worker(AUDIO_WORKER_URL),
        probe_media_worker(VISION_WORKER_URL),
    )
    return {
        "audio": {"reachable": audio_reachable, "ok": audio_ok, "detail": audio_detail},
        "vision": {"reachable": vision_reachable, "ok": vision_ok, "detail": vision_detail},
    }


async def _read_upload_bounded(upload: UploadFile, max_bytes: int, kind: str) -> bytes:
    """Read an upload fully, but never past its byte cap."""
    chunks: list[bytes] = []
    received = 0
    while chunk := await upload.read(1024 * 1024):
        received += len(chunk)
        if received > max_bytes:
            raise HTTPException(413, f"{kind} upload exceeds the {max_bytes}-byte limit")
        chunks.append(chunk)
    return b"".join(chunks)


async def _proxy_media_upload(
    worker_url: str,
    *,
    kind: str,
    path: str,
    field: str,
    blob: bytes,
    filename: str,
    content_type: str | None,
    form: dict[str, str],
    timeout: float,
) -> bytes:
    """Forward a bounded upload and return a response bounded in memory."""
    files = {field: (filename, blob, content_type or "application/octet-stream")}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream(
                "POST", f"{worker_url}{path}", files=files, data=form
            ) as response:
                if response.status_code // 100 != 2:
                    raise HTTPException(
                        502, f"{kind} worker returned status {response.status_code}"
                    )
                chunks: list[bytes] = []
                received = 0
                async for chunk in response.aiter_bytes():
                    received += len(chunk)
                    if received > MEDIA_WORKER_RESPONSE_MAX_BYTES:
                        raise HTTPException(
                            502,
                            f"{kind} worker response exceeded its "
                            f"{MEDIA_WORKER_RESPONSE_MAX_BYTES}-byte limit",
                        )
                    chunks.append(chunk)
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"{kind} worker unavailable: {exc}") from exc
    return b"".join(chunks)


async def _media_gate_or_503(worker_url: str, kind: str) -> JSONResponse | None:
    """Fail closed before touching the upload when the worker is not healthy."""
    reachable, ok, detail = await probe_media_worker(worker_url)
    if reachable and ok:
        return None
    return JSONResponse(
        status_code=503,
        content={
            "detail": f"{kind} worker unavailable: {detail}",
            "reason_code": "media_worker_unavailable",
        },
    )

def _decode_media_worker_json(body: bytes, kind: str) -> dict[str, Any]:
    """Require one bounded worker response to be a JSON object."""
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(502, f"{kind} worker returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(502, f"{kind} worker returned non-object JSON")
    return payload


async def _forward_media_upload(
    worker_url: str,
    *,
    upload: UploadFile,
    kind: str,
    path: str,
    field: str,
    filename: str,
    form: dict[str, str],
    max_bytes: int,
    timeout: float,
) -> tuple[bytes, dict[str, Any]] | JSONResponse:
    """Health-gate, bound, and forward one media upload through one path.

    The gate intentionally runs before the first upload read. Both the legacy
    CLI-facing ``/artifacts`` routes and the raw ``/media`` routes use this
    helper so neither surface can bypass the input or response caps.
    """
    gate = await _media_gate_or_503(worker_url, kind)
    if gate is not None:
        return gate
    blob = await _read_upload_bounded(upload, max_bytes, kind)
    body = await _proxy_media_upload(
        worker_url,
        kind=kind,
        path=path,
        field=field,
        blob=blob,
        filename=filename,
        content_type=upload.content_type,
        form=form,
        timeout=timeout,
    )
    return body, _decode_media_worker_json(body, kind)


@app.post("/media/transcribe")
async def media_transcribe(
    audio: UploadFile = File(...),
    model: Optional[str] = Form(None),
    x_agent: str = Header(alias="X-Agent-Name"),
    x_project: str = Header(alias="X-Project", default=""),
):
    """Proxy audio transcription to the opt-in cortex-audio-worker (:9003)."""
    agent = validate_agent_name(x_agent)
    project = require_project_scope(x_project)
    await require_registered_agent_writer(project, agent)
    form: dict[str, str] = {}
    if model is not None:
        form["model"] = model
    forwarded = await _forward_media_upload(
        AUDIO_WORKER_URL,
        upload=audio,
        kind="audio",
        path="/transcribe",
        field="audio",
        filename=audio.filename or "upload",
        form=form,
        max_bytes=MEDIA_TRANSCRIBE_MAX_BYTES,
        timeout=MEDIA_TRANSCRIBE_TIMEOUT,
    )
    if isinstance(forwarded, JSONResponse):
        return forwarded
    body, _payload = forwarded
    return Response(content=body, media_type="application/json")


@app.post("/media/describe-image")
async def media_describe_image(
    image: UploadFile = File(...),
    prompt: Optional[str] = Form(None),
    model: Optional[str] = Form(None),
    x_agent: str = Header(alias="X-Agent-Name"),
    x_project: str = Header(alias="X-Project", default=""),
):
    """Proxy image description to the opt-in cortex-vision-worker (:9002)."""
    agent = validate_agent_name(x_agent)
    project = require_project_scope(x_project)
    await require_registered_agent_writer(project, agent)
    form: dict[str, str] = {}
    if prompt is not None:
        form["prompt"] = prompt
    if model is not None:
        form["model"] = model
    forwarded = await _forward_media_upload(
        VISION_WORKER_URL,
        upload=image,
        kind="vision",
        path="/describe-image",
        field="image",
        filename=image.filename or "upload",
        form=form,
        max_bytes=MEDIA_DESCRIBE_MAX_BYTES,
        timeout=MEDIA_DESCRIBE_TIMEOUT,
    )
    if isinstance(forwarded, JSONResponse):
        return forwarded
    body, _payload = forwarded
    return Response(content=body, media_type="application/json")



@app.get("/graph/stats")
async def graph_stats_proxy(
    x_project: str = Header(alias="X-Project", default=""),
):
    project = require_project_scope(x_project)
    async with graph_registry_lease() as registry_conn:
        _repo, storage_key, _registered_repo, _generation = await require_graph_repo_scope(
            project,
            project,
            registry_conn=registry_conn,
        )
        result = await proxy_worker_json(
            GRAPH_WORKER_URL,
            "/stats",
            params={"storage_key": storage_key},
            timeout=30.0,
        )
        return require_graph_stats_success(result, storage_key=storage_key)


def require_graph_stats_success(
    result: Mapping[str, Any],
    *,
    storage_key: str,
) -> dict[str, Any]:
    """Reject partial or malformed graph-store statistics at the API boundary."""

    def count(item: Mapping[str, Any], field: str) -> int:
        value = item.get(field)
        if type(value) is not int or value < 0:
            raise HTTPException(
                502,
                f"graph worker returned an invalid {field!r} statistics count",
            )
        return value

    total_nodes = count(result, "total_nodes")
    total_edges = count(result, "total_edges")
    repos = result.get("repos")
    if not isinstance(repos, list) or len(repos) != 1:
        raise HTTPException(
            502,
            "graph worker did not return the authorized materialized graph store",
        )

    seen_nodes = 0
    seen_edges = 0
    for item in repos:
        if not isinstance(item, Mapping):
            raise HTTPException(502, "graph worker returned a malformed graph store")
        if item.get("name") != storage_key:
            raise HTTPException(502, "graph worker returned the wrong graph store")
        if "error" in item:
            raise HTTPException(
                502,
                f"graph worker could not read the scoped graph store: {item.get('error')}",
            )
        seen_nodes += count(item, "nodes")
        seen_edges += count(item, "edges")
        path = item.get("path")
        if not isinstance(path, str) or not path:
            raise HTTPException(502, "graph worker returned an invalid graph path")

    if seen_nodes != total_nodes or seen_edges != total_edges:
        raise HTTPException(502, "graph worker returned inconsistent graph statistics")
    return dict(result)


@app.post("/graph/prune")
async def graph_prune_proxy(
    body: GraphPruneRequest,
    request: Request,
):
    require_admin_access(request)
    async with graph_registry_lease() as registry_conn:
        keep_project_keys = {
            validate_project_key(str(project))
            for project in body.keep_projects
            if project
        }
        if registry_conn is None:
            async with pool_admin.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT project_key, id::text AS project_id, repo_root,
                           graph_generation::text AS graph_generation
                      FROM cortex_projects
                     WHERE COALESCE(status, 'active') <> 'deleted'
                        OR project_key = ANY($1::text[])
                     ORDER BY project_key
                    """,
                    sorted(keep_project_keys),
                )
        else:
            rows = await registry_conn.fetch(
                """
                SELECT project_key, id::text AS project_id, repo_root,
                       graph_generation::text AS graph_generation
                  FROM cortex_projects
                 WHERE COALESCE(status, 'active') <> 'deleted'
                    OR project_key = ANY($1::text[])
                 ORDER BY project_key
                """,
                sorted(keep_project_keys),
            )
        protected_projects = {
            graph_storage_key(str(row["project_key"]), row) for row in rows
        }
        found_keep_projects = {
            str(row["project_key"])
            for row in rows
            if str(row["project_key"]) in keep_project_keys
        }
        missing_keep_projects = sorted(keep_project_keys - found_keep_projects)
        if missing_keep_projects:
            raise HTTPException(
                404,
                "cannot retain graph storage for unknown project(s): "
                + ", ".join(missing_keep_projects),
            )
        # Keep the registry lease until the worker has identified and (for apply)
        # deleted its descriptor-bound candidates. A concurrent registration or
        # root move cannot create a newly-authorized graph between snapshot/delete.
        return await proxy_worker_json(
            GRAPH_WORKER_URL,
            "/prune",
            method="POST",
            payload={
                "active_projects": sorted(protected_projects),
                "dry_run": bool(body.dry_run),
            },
            timeout=30.0,
        )


async def execute_graph_build_request(
    body: GraphBuildRequest,
    *,
    storage_key: str,
    registered_repo: str,
) -> dict[str, Any]:
    result = await proxy_worker_json(
        GRAPH_WORKER_URL,
        "/build",
        method="POST",
        payload={
            "repo": body.repo,
            "storage_key": storage_key,
            "registered_repo": registered_repo,
            "full": bool(body.full),
            "embed": bool(body.embed),
            "import_existing": bool(body.import_existing),
        },
        timeout=650.0,
    )
    return require_graph_build_success(body, result, storage_key=storage_key)


def _require_graph_receipt_count(result: Mapping[str, Any], field: str) -> int:
    """Return one exact non-negative BCRG receipt count or fail closed."""
    value = result.get(field)
    if type(value) is not int or value < 0:
        raise HTTPException(
            502,
            f"graph worker returned an invalid {field!r} completion count",
        )
    return value


def _require_nonempty_graph_receipt_text(
    result: Mapping[str, Any], field: str
) -> str:
    value = result.get(field)
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(
            502,
            f"graph worker returned an invalid {field!r} completion field",
        )
    return value


def require_graph_build_success(
    body: GraphBuildRequest,
    result: Mapping[str, Any],
    *,
    storage_key: str,
) -> dict[str, Any]:
    """Validate the pinned BCRG 3.21 build/import completion contract.

    BCRG deliberately reports build exceptions as HTTP-200 ``status=error``
    objects.  A transport-level success therefore cannot authorize a project
    generation.  Parse errors likewise produce ``status=ok`` with a non-empty
    ``errors`` list, which is only a partial graph and must remain unreadable.
    """
    if not isinstance(result, Mapping):
        raise HTTPException(502, "graph worker returned a non-object build receipt")

    if body.import_existing:
        if result.get("status") != "imported-existing-graph":
            raise HTTPException(502, "graph worker did not complete the graph import")
        if result.get("storage_key") != storage_key:
            raise HTTPException(
                502,
                "graph worker import receipt does not match the authorized storage key",
            )
        for field in ("repo", "source", "graph_db"):
            _require_nonempty_graph_receipt_text(result, field)
        for field in ("nodes", "edges"):
            _require_graph_receipt_count(result, field)
        return dict(result)

    expected_type = "full" if body.full else "incremental"
    if result.get("status") != "ok" or result.get("build_type") != expected_type:
        raise HTTPException(
            502,
            f"graph worker did not complete the requested {expected_type} build",
        )
    _require_nonempty_graph_receipt_text(result, "summary")
    for field in ("total_nodes", "total_edges"):
        _require_graph_receipt_count(result, field)

    if body.full:
        _require_graph_receipt_count(result, "files_parsed")
        if result.get("errors") != []:
            raise HTTPException(
                502,
                "graph worker full build was partial; parse errors remain",
            )
    else:
        _require_graph_receipt_count(result, "files_updated")
        for field in ("changed_files", "dependent_files"):
            if not isinstance(result.get(field), list):
                raise HTTPException(
                    502,
                    f"graph worker returned an invalid {field!r} completion field",
                )
        if "errors" in result and result.get("errors") != []:
            raise HTTPException(
                502,
                "graph worker incremental build was partial; parse errors remain",
            )

    if body.embed:
        embeddings = result.get("embeddings")
        if not isinstance(embeddings, Mapping) or embeddings.get("status") != "ok":
            raise HTTPException(502, "graph worker did not complete graph embeddings")
        for field in ("summary", "backend"):
            _require_nonempty_graph_receipt_text(embeddings, field)
        for field in ("newly_embedded", "total_embeddings"):
            _require_graph_receipt_count(embeddings, field)

    return dict(result)


def graph_build_materializes_generation(body: GraphBuildRequest) -> bool:
    """Return whether this explicit operation can authorize a new generation."""
    return bool(body.full or body.import_existing)


def _graph_project_identity(
    project: str, project_row: Mapping[str, Any]
) -> tuple[UUID, UUID]:
    """Return the immutable project UUID and current root-generation UUID."""
    validate_project_key(project)
    try:
        project_id = UUID(str(project_row.get("project_id") or ""))
        generation = UUID(str(project_row.get("graph_generation") or ""))
    except (AttributeError, TypeError, ValueError) as exc:
        raise HTTPException(
            503,
            "graph storage generation is unavailable; apply Cortex schema migrations",
        ) from exc
    return project_id, generation


def graph_storage_key(project: str, project_row: Mapping[str, Any]) -> str:
    """Derive a non-reusable directory authority from project and root generation."""
    project_id, generation = _graph_project_identity(project, project_row)
    digest = hashlib.sha256(f"{project_id}:{generation}".encode("ascii")).hexdigest()
    return f"g-{digest[:48]}"


async def require_unique_graph_storage_key(
    project: str,
    storage_key: str,
    project_row: Mapping[str, Any],
) -> None:
    """Reject any caller-supplied or stale project-generation storage authority."""
    if storage_key != graph_storage_key(project, project_row):
        raise HTTPException(
            409,
            "graph request blocked: storage key does not match the scoped project generation",
        )


async def require_graph_repo_scope(
    project: str,
    requested_repo: str,
    *,
    allow_unready_generation: bool = False,
    registry_conn: Any | None = None,
) -> tuple[str, str, str, str]:
    """Bind every L3 graph request to the registered X-Project repository."""
    if registry_conn is None:
        project_row = await require_registered_project(project)
    else:
        project_row = await _require_registered_project_on_conn(
            registry_conn,
            project,
        )
    repo_root = str(project_row.get("repo_root") or "").strip()
    if not repo_root:
        raise HTTPException(403, "graph project has no registered repository root")
    registered_repo = validate_visible_project_root(
        repo_root,
        field_name="registered graph repo",
    )
    requested_repo = str(requested_repo or "").strip()
    if requested_repo in {".", project}:
        requested_repo = registered_repo
    else:
        requested_repo = validate_visible_project_root(
            requested_repo, field_name="graph repo"
        )
        if requested_repo != registered_repo:
            raise HTTPException(
                403,
                "graph repo must exactly match the scoped project's registered root; "
                "register a worktree as a separate project before graphing it",
            )
    storage_key = graph_storage_key(project, project_row)
    await require_unique_graph_storage_key(project, storage_key, project_row)
    if (
        bool(project_row.get("graph_requires_full_rebuild"))
        and not allow_unready_generation
    ):
        raise HTTPException(
            409,
            "graph repository generation changed; run an explicit full graph rebuild "
            "or reviewed existing-graph import before querying or applying incremental updates",
        )
    generation = str(_graph_project_identity(project, project_row)[1])
    return requested_repo, storage_key, registered_repo, generation


async def _set_graph_generation_hold_on_conn(
    conn: Any,
    project: str,
    generation: str,
    *,
    held: bool,
) -> None:
    """Persist one exact generation's readability on a caller-owned connection."""
    try:
        expected = str(UUID(generation))
    except (TypeError, ValueError) as exc:
        action = "mutation" if held else "completion"
        raise HTTPException(500, f"invalid graph generation {action} token") from exc
    updated = await conn.fetchval(
        """UPDATE cortex_projects
              SET graph_requires_full_rebuild = $3,
                  updated_at = NOW()
            WHERE project_key = $1
              AND graph_generation = $2::uuid
            RETURNING graph_generation::text""",
        validate_project_key(project),
        expected,
        held,
    )
    if str(updated or "") != expected:
        action = "be held" if held else "commit"
        raise HTTPException(
            409,
            f"project graph generation changed before mutation could {action}",
        )


async def mark_graph_generation_ready(project: str, generation: str) -> None:
    """Commit graph readability only for the generation just validated."""
    assert pool_admin is not None, "Admin DB pool not initialised"
    async with pool_admin.acquire() as conn:
        await _set_graph_generation_hold_on_conn(
            conn,
            project,
            generation,
            held=False,
        )


async def hold_graph_generation_for_mutation(project: str, generation: str) -> None:
    """Persist a fail-closed hold before any in-place graph mutation starts."""
    assert pool_admin is not None, "Admin DB pool not initialised"
    async with pool_admin.acquire() as conn:
        await _set_graph_generation_hold_on_conn(
            conn,
            project,
            generation,
            held=True,
        )


async def _set_graph_generation_hold_under_lease(
    registry_conn: Any | None,
    project: str,
    generation: str,
    *,
    held: bool,
) -> None:
    """Use the lease connection in production while preserving isolated test seams."""
    if registry_conn is not None:
        await _set_graph_generation_hold_on_conn(
            registry_conn,
            project,
            generation,
            held=held,
        )
    elif held:
        await hold_graph_generation_for_mutation(project, generation)
    else:
        await mark_graph_generation_ready(project, generation)


@asynccontextmanager
async def graph_registry_lease():
    """Hold registry authority on one reusable admin connection.

    A session advisory lock lets hold/ready writes autocommit durably on the same
    connection before and after a long worker call.  It also prevents max-size
    pool deadlock when other graph or registry requests occupy every remaining
    connection while waiting on this lock.
    """
    assert pool_admin is not None, "Admin DB pool not initialised"
    async with pool_admin.acquire() as conn:
        locked = False
        try:
            await conn.fetchval(
                "SELECT pg_advisory_lock(hashtextextended($1, 0))",
                "cortex:project-root-registry",
            )
            locked = True
            yield conn
        finally:
            if locked:
                try:
                    unlocked = await asyncio.shield(
                        conn.fetchval(
                            "SELECT pg_advisory_unlock(hashtextextended($1, 0))",
                            "cortex:project-root-registry",
                        )
                    )
                except BaseException:
                    # Never return a possibly lock-owning connection to the pool.
                    conn.terminate()
                    raise
                if unlocked is not True:
                    conn.terminate()
                    raise RuntimeError("failed to release graph registry advisory lock")


async def run_graph_build_job(project: str, job_id: str, body: GraphBuildRequest) -> None:
    try:
        await persist_graph_build_job(project, job_id, status="running")
        async with graph_registry_lease() as registry_conn:
            repo, storage_key, registered_repo, generation = (
                await require_graph_repo_scope(
                    project,
                    body.repo,
                    allow_unready_generation=graph_build_materializes_generation(body),
                    registry_conn=registry_conn,
                )
            )
            scoped_body = body.model_copy(update={"repo": repo})
            await _set_graph_generation_hold_under_lease(
                registry_conn,
                project,
                generation,
                held=True,
            )
            result = await execute_graph_build_request(
                scoped_body,
                storage_key=storage_key,
                registered_repo=registered_repo,
            )
            result = require_graph_build_success(
                scoped_body,
                result,
                storage_key=storage_key,
            )
            await _set_graph_generation_hold_under_lease(
                registry_conn,
                project,
                generation,
                held=False,
            )
    except Exception as exc:  # noqa: BLE001 - persist background failure for operator polling
        with suppress(Exception):
            await persist_graph_build_job(
                project,
                job_id,
                status="failed",
                error=compact_text(f"{type(exc).__name__}: {exc}", limit=500),
            )
        return
    await persist_graph_build_job(
        project, job_id, status="completed", result=result
    )


@app.post("/graph/build")
async def graph_build_proxy(
    body: GraphBuildRequest,
    x_project: str = Header(alias="X-Project", default=""),
):
    project = require_project_scope(x_project)
    async with graph_registry_lease() as registry_conn:
        requested_repo, storage_key, registered_repo, generation = (
            await require_graph_repo_scope(
                project,
                body.repo,
                allow_unready_generation=graph_build_materializes_generation(body),
                registry_conn=registry_conn,
            )
        )
        scoped_body = body.model_copy(update={"repo": requested_repo})
        if not (scoped_body.async_job or (scoped_body.full and not scoped_body.sync)):
            await _set_graph_generation_hold_under_lease(
                registry_conn,
                project,
                generation,
                held=True,
            )
            result = await execute_graph_build_request(
                scoped_body,
                storage_key=storage_key,
                registered_repo=registered_repo,
            )
            result = require_graph_build_success(
                scoped_body,
                result,
                storage_key=storage_key,
            )
            await _set_graph_generation_hold_under_lease(
                registry_conn,
                project,
                generation,
                held=False,
            )
            return result
        job_id = await create_graph_build_job(project, scoped_body)
    if scoped_body.async_job or (scoped_body.full and not scoped_body.sync):
        schedule_graph_build_job(project, job_id, scoped_body)
        return JSONResponse(
            status_code=202,
            content={
                "project": project,
                "job_id": job_id,
                "status": "queued",
                "repo": requested_repo,
                "full": bool(scoped_body.full),
                "embed": bool(scoped_body.embed),
                "status_url": f"/graph/build/jobs/{job_id}",
                "message": "graph build accepted; poll status_url for progress",
            },
        )
    raise AssertionError("unreachable graph build scheduling branch")


@app.get("/graph/build/jobs/{job_id}")
async def graph_build_job_proxy(
    job_id: str,
    x_project: str = Header(alias="X-Project", default=""),
):
    project = require_project_scope(x_project)
    async with acquire_scoped(project) as conn:
        await ensure_graph_build_jobs_schema()
        row = await conn.fetchrow(
            """
            SELECT *
              FROM graph_build_jobs
             WHERE id = $1::uuid
               AND project = $2
            """,
            strip_compound_suffix(job_id),
            project,
        )
    if not row:
        raise HTTPException(404, "graph build job not found")
    return graph_build_job_to_dict(row)


@app.post("/graph/blast")
async def graph_blast_proxy(
    body: GraphBlastRequest,
    x_project: str = Header(alias="X-Project", default=""),
):
    project = require_project_scope(x_project)
    async with graph_registry_lease() as registry_conn:
        repo, storage_key, registered_repo, _generation = await require_graph_repo_scope(
            project,
            body.repo,
            registry_conn=registry_conn,
        )
        payload = body.model_dump()
        payload.update(
            repo=repo,
            storage_key=storage_key,
            registered_repo=registered_repo,
        )
        return await proxy_worker_json(
            GRAPH_WORKER_URL,
            "/blast",
            method="POST",
            payload=payload,
            timeout=130.0,
        )


@app.post("/graph/callers")
async def graph_callers_proxy(
    body: GraphCallersRequest,
    x_project: str = Header(alias="X-Project", default=""),
):
    project = require_project_scope(x_project)
    async with graph_registry_lease() as registry_conn:
        repo, storage_key, registered_repo, _generation = await require_graph_repo_scope(
            project,
            body.repo,
            registry_conn=registry_conn,
        )
        payload = body.model_dump()
        payload.update(
            repo=repo,
            storage_key=storage_key,
            registered_repo=registered_repo,
        )
        return await proxy_worker_json(
            GRAPH_WORKER_URL,
            "/callers",
            method="POST",
            payload=payload,
            timeout=70.0,
        )


@app.post("/graph/impact")
async def graph_impact_proxy(
    body: GraphImpactRequest,
    x_project: str = Header(alias="X-Project", default=""),
):
    project = require_project_scope(x_project)
    async with graph_registry_lease() as registry_conn:
        repo, storage_key, registered_repo, _generation = await require_graph_repo_scope(
            project,
            body.repo,
            registry_conn=registry_conn,
        )
        payload = body.model_dump()
        payload.update(
            repo=repo,
            storage_key=storage_key,
            registered_repo=registered_repo,
        )
        return await proxy_worker_json(
            GRAPH_WORKER_URL,
            "/impact",
            method="POST",
            payload=payload,
            timeout=130.0,
        )


@app.post("/graph/large-fn")
async def graph_large_fn_proxy(
    body: GraphLargeFnRequest,
    x_project: str = Header(alias="X-Project", default=""),
):
    project = require_project_scope(x_project)
    async with graph_registry_lease() as registry_conn:
        repo, storage_key, registered_repo, _generation = await require_graph_repo_scope(
            project,
            body.repo,
            registry_conn=registry_conn,
        )
        payload = body.model_dump()
        payload.update(
            repo=repo,
            storage_key=storage_key,
            registered_repo=registered_repo,
        )
        return await proxy_worker_json(
            GRAPH_WORKER_URL,
            "/large-fn",
            method="POST",
            payload=payload,
            timeout=70.0,
        )


# ---------------------------------------------------------------------------
# Handoff CRUD
# ---------------------------------------------------------------------------


_HANDOFF_LIST_STATUSES = frozenset(
    {"pending", "claimed", "returned", "completed", "all"}
)

# The actionable working set: rows dispatch iterates, reclaim inspects, and a
# recipient still has to act on. handoff_terminal_cap_sql() must never cap these.
_HANDOFF_ACTIONABLE_STATUSES = ("pending", "claimed", "returned")


def handoff_terminal_cap_sql(status: str, terminal_limit: int) -> tuple[str, str]:
    """SQL fragments applying ``terminal_limit`` to TERMINAL-history rows only (I23).

    The listing query had no LIMIT at all, so ``status=all`` serialized every
    non-invalidated row — the orchestrator's 4s sweep against a table this file
    measured at 47k rows / 2.3s. The cap is starvation-proof BY CONSTRUCTION:
    actionable rows (``_HANDOFF_ACTIONABLE_STATUSES``) are never subject to it.

    Returns ``(where_clause_suffix, limit_suffix)`` — under ``status=all`` the cap
    is a newest-N id-subquery appended to WHERE; on an explicitly terminal listing
    it is a plain LIMIT; on actionable statuses both fragments are empty. 0 (the
    default) keeps the historical unbounded shape. ``terminal_limit`` is
    route-validated (``ge=0, le=5000``) and cast to int here so the interpolation
    can never carry anything but a bounded integer."""
    limit = int(terminal_limit)
    if limit <= 0:
        return "", ""
    actionable_sql = "('" + "','".join(_HANDOFF_ACTIONABLE_STATUSES) + "')"
    if status == "all":
        cap = f"""
                     AND (status IN {actionable_sql}
                          OR id IN (SELECT id FROM handoffs
                                    WHERE project = $1 AND invalidated_at IS NULL
                                      AND status NOT IN {actionable_sql}
                                    ORDER BY created_at DESC
                                    LIMIT {limit}))"""
        return cap, ""
    if status not in _HANDOFF_ACTIONABLE_STATUSES:
        return "", f"\n                   LIMIT {limit}"
    return "", ""


# ---------------------------------------------------------------------------
# Handoff identity/scope reconciliation (Fix #1 — claim-desync bulletproofing).
#
# The autonomous loop's "visible but unclaimable" / claim round-trip desync came
# from THREE predicates that had drifted apart:
#   - the bare list (no recipient filter) showed rows the claim path would reject;
#   - the --mine / boot re-surface used to compare incompatible identity shapes.
#     Full equality could drop an agent's own in-flight row whenever claimed_by
#     was stored in a different display form, so the loop idled on its own claim
#     and raced Beat's auto-release;
#   - role resolution wasn't normalized at the boundary, so a role-addressed row
#     could flicker in/out of the agent's view tick-to-tick.
#
# These helpers are the SINGLE source of truth for the recipient predicate, the
# claimer re-surface predicate, deterministic role resolution, and the
# eligibility verdict (reused for both the bare-list hint and the informative
# claim failure). Every handoff list/claim/re-surface path uses them so they
# cannot diverge again.
# ---------------------------------------------------------------------------

# Recipient predicate: a handoff is "addressed to" an agent if its to_agent base
# name equals the agent, OR it is role-addressed (to_agent empty) and to_role is
# one of the agent's resolved roles. $base-agent is the bare lowercased name;
# $roles is the lowercased role list (which always includes the bare name).
_HANDOFF_RECIPIENT_PREDICATE = (
    "(lower(split_part(COALESCE(to_agent, ''), '@', 1)) = {agent} "
    "OR (COALESCE(to_agent, '') = '' AND lower(to_role) = ANY({roles}::text[])))"
)

# Claimer re-surface predicate: ALWAYS base-name match on claimed_by, never a
# full-compound equality. Robust to bare/display storage so an agent always
# re-detects a row it claimed. (Mirrors the boot/state/diary paths.)
_HANDOFF_CLAIMER_PREDICATE = (
    "lower(split_part(COALESCE(claimed_by, ''), '@', 1)) = {agent}"
)


def handoff_recipient_sql(agent_param: str, roles_param: str) -> str:
    """Canonical recipient SQL fragment, bound to the given param placeholders."""
    return _HANDOFF_RECIPIENT_PREDICATE.format(agent=agent_param, roles=roles_param)


def handoff_claimer_sql(agent_param: str) -> str:
    """Canonical claimer re-surface SQL fragment (base-name match on claimed_by)."""
    return _HANDOFF_CLAIMER_PREDICATE.format(agent=agent_param)


def handoff_claimant_identity(agent: str, project: str) -> str:
    """Identity-v2 value persisted in handoffs.claimed_by.

    Handoff claims are a high-frequency writer path, so keep this derivation
    local to the handoff lifecycle contract instead of relying on legacy
    compound-id helpers. The database trigger rejects retired colon-suffixed
    identities; this function makes the accepted storage shape explicit.
    """
    identity = agent_display_name(agent, project)
    if ":" in identity or "@" not in identity:
        raise HTTPException(500, "Invalid handoff claimant identity derivation")
    return identity


async def resolve_agent_role_set(
    conn: "asyncpg.Connection", project: str, agent: str
) -> list[str]:
    """Deterministic, normalized role set for an agent: the bare name plus every
    resolved role, lowercased, de-duplicated, sorted. Stable per (project, agent)
    so a handoff cannot flicker in and out of visibility across ticks."""
    bare = agent.lower().strip()
    roles = await resolve_agent_roles(conn, project, bare)
    return sorted({bare, *(r.lower().strip() for r in roles if r and r.strip())})


def handoff_claim_eligibility(
    row: dict, *, bare_agent: str, roles: list[str]
) -> tuple[bool, str | None]:
    """Decide whether ``bare_agent`` may claim ``row`` and, if not, WHY.

    Returns (eligible, reason). ``reason`` is a precise, agent-facing string used
    both for the bare-list hint and the informative claim failure — never a bare
    404. Terminal/claimed states report their blocker; a role/agent mismatch
    names the addressee so the agent knows it is simply not theirs.
    """
    role_set = {r.lower().strip() for r in roles}
    status = (row.get("status") or "").lower()
    to_agent = (row.get("to_agent") or "")
    to_agent_base = agent_base_name(to_agent)
    to_role = (row.get("to_role") or "").lower()
    claimed_by = row.get("claimed_by") or ""
    claimed_base = agent_base_name(claimed_by)

    addressed = bool(to_agent_base and to_agent_base == bare_agent) or (
        not to_agent and to_role in role_set
    )

    if status == "completed":
        return False, "already completed"
    if status in {"abandoned", "failed"}:
        return False, f"terminal ({status})"
    if status == "claimed":
        if claimed_base == bare_agent:
            return True, "claimed by you (in progress)"
        who = claimed_by or "another agent"
        return False, f"claimed by {who}"
    # pending from here on
    if addressed:
        return True, None
    if to_agent_base:
        return False, f"addressed to agent {to_agent_base}; you are {bare_agent}"
    return (
        False,
        f"addressed to role {to_role or '(unset)'}; you hold {sorted(role_set)}",
    )


def raise_informative_claim_failure(
    handoff_row: dict, *, handoff_id: str, bare_agent: str, roles: list[str]
) -> None:
    """A claim UPDATE that affected 0 rows is NOT a 404 — the row exists (it was
    just resolved). Re-derive WHY the claim was refused from the row's current
    state and raise a precise 409/403 so the autonomous loop (and humans) get an
    actionable reason instead of a misleading "not found".

      - already completed / terminal           -> 409
      - claimed by someone else                -> 409 (names the claimer)
      - addressed to a different role/agent     -> 403 (names the addressee)
    """
    eligible, reason = handoff_claim_eligibility(
        handoff_row, bare_agent=bare_agent, roles=roles
    )
    if eligible:
        # Eligible yet 0 rows updated = a genuine race (status moved between the
        # SELECT and the UPDATE). Report it honestly as a conflict.
        raise HTTPException(
            409,
            f"Handoff {handoff_id} changed state during claim (race); retry. "
            f"current status={handoff_row.get('status')}",
        )
    status = (handoff_row.get("status") or "").lower()
    code = 403 if status == "pending" else 409
    raise HTTPException(code, f"Cannot claim handoff {handoff_id}: {reason}")


@app.get("/handoffs")
async def list_handoffs(
    request: Request = None,
    x_project: str = Header(alias="X-Project", default=""),
    status: str = Query(default="pending"),
    agent: str | None = Query(default=None),
    mine: bool = Query(default=False),
    terminal_limit: int = Query(default=0, ge=0, le=5000),
):
    # Direct in-process callers pass the raw Query default through; coerce so the
    # cap-SQL arithmetic is safe without changing FastAPI request semantics.
    if not isinstance(terminal_limit, int):
        terminal_limit = 0
    # Fail-loud: validate the status filter instead of silently returning an empty set
    # on a typo — the bug that hid CLAIMED handoffs from the PM watchdog until it learned
    # to pass status=claimed. 'all' is the explicit no-filter escape hatch.
    status = (status or "pending").lower().strip()
    if status not in _HANDOFF_LIST_STATUSES:
        raise HTTPException(
            400,
            f"Invalid handoff status '{status}' — use one of {sorted(_HANDOFF_LIST_STATUSES)}",
        )
    project = require_project_scope(x_project)
    await require_registered_project(project)

    # Two distinct concepts that used to be conflated:
    #   - `viewer`       = whose eligibility to annotate (for honest claim hints);
    #   - `should_filter`= whether to FILTER to only the viewer's rows.
    # A supplied `agent` (with or without `mine`) keeps the historical filter
    # semantics; `mine=True` additionally resolves the viewer from the JWT. The
    # truly agentless call is the operator/bare list: every row, eligibility
    # unknown. When a viewer IS known, every returned row is marked eligible
    # True/False + a reason so the loop never tries to claim what it cannot
    # ("visible but unclaimable").
    viewer = agent
    if mine:
        jwt_claims = getattr(request.state, "jwt_claims", {}) if request else {}
        viewer = jwt_claims.get("agent") or jwt_claims.get("agent_name") or agent
        if not viewer:
            # If mine=True but no agent can be resolved, return empty (don't leak all)
            return {"handoffs": []}
    should_filter = bool(viewer)

    terminal_cap_sql, plain_limit_sql = handoff_terminal_cap_sql(status, terminal_limit)

    rows: list = []
    async with acquire_scoped(project) as conn:
        viewer_bare = viewer.lower().strip() if viewer else ""
        roles = (
            await resolve_agent_role_set(conn, project, viewer_bare)
            if viewer_bare
            else []
        )

        if should_filter:
            # Recipient-aware FILTER: rows addressed to the viewer (recipient
            # predicate) OR already claimed by the viewer (claimer predicate —
            # base-name match, robust to bare/compound/hex storage).
            rows = await conn.fetch(
                f"""SELECT id::text, kind, reply_to_handoff_id::text,
                          from_agent, to_role, to_agent, priority,
                          LEFT(summary, 100) as summary, status,
                          acceptance, evidence, retry, escalation,
                          claimed_by,
                          claimed_at::text,
                          COALESCE(retry_count, 0)::int AS retry_count,
                          created_at::text
                   FROM handoffs
                   WHERE project = $1
                     AND ($2 = 'all' OR status = $2)
                     AND invalidated_at IS NULL
                     AND (
                        {handoff_claimer_sql('$3')}
                        OR {handoff_recipient_sql('$3', '$4')}
                     ){terminal_cap_sql}
                   ORDER BY CASE priority
                     WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 ELSE 2 END,
                     created_at DESC{plain_limit_sql}""",
                project,
                status,
                viewer_bare,
                [r.lower() for r in roles],
            )
        else:
            # Bare/operator list: every row in the status window, annotated below.
            rows = await conn.fetch(
                f"""SELECT id::text, kind, reply_to_handoff_id::text,
                          from_agent, to_role, to_agent, priority,
                          LEFT(summary, 100) as summary, status,
                          acceptance, evidence, retry, escalation,
                          claimed_by,
                          claimed_at::text,
                          COALESCE(retry_count, 0)::int AS retry_count,
                          created_at::text
                   FROM handoffs
                   WHERE project = $1 AND ($2 = 'all' OR status = $2) AND invalidated_at IS NULL{terminal_cap_sql}
                   ORDER BY CASE priority
                     WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 ELSE 2 END,
                     created_at DESC{plain_limit_sql}""",
                project,
                status,
            )

    out: list[dict] = []
    for r in rows:
        d = dict(r)
        if viewer_bare:
            eligible, reason = handoff_claim_eligibility(
                d, bare_agent=viewer_bare, roles=roles
            )
            d["eligible"] = eligible
            d["claim_hint"] = reason
        else:
            # No viewer identity → eligibility is genuinely unknown (operator view).
            d["eligible"] = None
            d["claim_hint"] = None
        out.append(d)
    return {"handoffs": out}


@app.get("/handoffs/{handoff_id}")
async def get_handoff(
    handoff_id: str,
    x_project: str = Header(alias="X-Project", default=""),
):
    """Return the full body of a single handoff by id-prefix match.

    Exists so agents never need to drop into psql to read the context /
    next_steps / verification fields that the listing endpoint truncates.
    Prefix match (id::text LIKE '<handoff_id>%') matches the pattern used
    by --claim / --complete so the 8-char short ID works uniformly.
    """
    project = require_project_scope(x_project)
    await require_registered_project(project)

    async with acquire_scoped(project) as conn:
        handoff_match = await resolve_unique_handoff_for_mutation(
            conn,
            project=project,
            handoff_id=handoff_id,
        )
        row = await conn.fetchrow(
            """SELECT id::text, project, kind, reply_to_handoff_id::text,
                      returned_at::text, completion_report,
                      from_agent, from_role, to_role, to_agent,
                      priority, sprint_id::text, branch, summary,
                      files_changed, verification, next_steps, context,
                      acceptance, evidence, retry, escalation,
                      COALESCE(retry_count, 0)::int AS retry_count,
                      status, claimed_by, claimed_at::text,
                      completed_at::text, created_at::text,
                      invalidated_at::text, terminal_reason
               FROM handoffs
               WHERE id = $1::uuid AND project = $2""",
            handoff_match["id"],
            project,
        )
    if row is None:
        raise HTTPException(404, f"Handoff {handoff_id} not found")
    return dict(row)


async def persist_handoff_record(
    *,
    project: str,
    from_agent: str,
    to_agent: str | None,
    body: HandoffCreate,
    single_use_approval_id: str | None = None,
) -> dict[str, Any]:
    """Persist a project-local handoff, optionally consuming one relay approval."""
    async with acquire_scoped(project) as conn:
        async with conn.transaction():
            if single_use_approval_id:
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtext($1))",
                    f"cross-project-handoff:{single_use_approval_id}",
                )

            # Serialize identical create attempts in this project so two workers
            # racing the same byte-identical handoff cannot both pass the
            # pre-insert lookup before either row exists.
            dedupe_key = (
                f"{project}:handoff-create:"
                f"{handoff_create_dedupe_fingerprint(project=project, from_agent=from_agent, body=body, to_agent=to_agent)}"
            )
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", dedupe_key)
            duplicate = await find_equal_open_handoff(
                conn,
                project=project,
                expected=body,
                expected_from_agent=from_agent,
                expected_to_agent=to_agent,
            )
            if duplicate is not None:
                return {
                    "id": duplicate["id"],
                    "status": duplicate.get("status") or "pending",
                    "verified": True,
                    "deduped": True,
                }

            if single_use_approval_id:
                consumed = await conn.fetchrow(
                    """SELECT id::text, status
                         FROM handoffs
                        WHERE project = $1
                          AND evidence->'cross_project_relay'->>'approval_decision_id' = $2
                        LIMIT 1""",
                    project,
                    single_use_approval_id,
                )
                if consumed is not None:
                    raise HTTPException(
                        409,
                        "Cross-project handoff approval "
                        f"{single_use_approval_id} was already consumed by "
                        f"handoff {consumed['id']}",
                    )

            row_id = await conn.fetchval(
                """INSERT INTO handoffs (project, from_agent, from_role, to_role,
                        to_agent, priority, summary, branch, files_changed,
                        verification, next_steps, context, parent_goal_id,
                        acceptance, evidence, retry, escalation)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13,
                           $14::jsonb, $15::jsonb, $16::jsonb, $17::jsonb)
                   RETURNING id""",
                project,
                from_agent,
                body.from_role,
                body.to_role,
                to_agent,
                body.priority,
                body.summary,
                body.branch,
                body.files_changed,
                body.verification,
                body.next_steps,
                body.context,
                body.parent_goal_id,
                handoff_policy_db(body.acceptance),
                handoff_policy_db(body.evidence),
                handoff_policy_db(body.retry),
                handoff_policy_db(body.escalation),
            )
            await verify_handoff_persisted(
                conn,
                row_id=row_id,
                project=project,
                expected=body,
                expected_from_agent=from_agent,
                expected_to_agent=to_agent,
            )
            await emit_handoff_lifecycle_event(
                conn,
                project=project,
                # The handoff row preserves an approved external sender. Team
                # events remain project-local, so the destination system emits
                # the lifecycle event while detail.from_agent carries provenance.
                actor=f"system@{project}" if single_use_approval_id else from_agent,
                action="created",
                handoff={
                    "id": str(row_id),
                    "status": "pending",
                    "from_agent": from_agent,
                    "from_role": body.from_role,
                    "to_role": body.to_role,
                    "to_agent": to_agent,
                    "priority": body.priority,
                    "summary": body.summary,
                    "claimed_by": None,
                    "retry_count": 0,
                    "acceptance": handoff_policy(body.acceptance),
                    "evidence": handoff_policy(body.evidence),
                    "retry": handoff_policy(body.retry),
                    "escalation": handoff_policy(body.escalation),
                },
                files=body.files_changed,
            )

    return {"id": str(row_id), "status": "pending", "verified": True, "deduped": False}


async def require_cross_project_handoff_approval(
    *,
    approval_decision_id: str,
    source_project: str,
    source_agent: str,
    target_project: str,
    target_agent: str,
) -> str:
    """Validate a one-off CTO decision against the exact relay boundary."""
    try:
        normalized_id = str(UUID(str(approval_decision_id).strip()))
    except (ValueError, TypeError, AttributeError) as exc:
        raise HTTPException(
            403,
            "Cross-project handoffs require a valid CTO approval decision UUID",
        ) from exc

    async with acquire_scoped(source_project) as conn:
        row = await conn.fetchrow(
            """SELECT id::text, agent_name, summary, metadata
                 FROM decisions
                WHERE project = $1
                  AND id = $2::uuid
                  AND invalidated_at IS NULL""",
            source_project,
            normalized_id,
        )
    if row is None:
        raise HTTPException(
            403,
            f"Active CTO approval decision {normalized_id} was not found in {source_project}",
        )

    source_display = f"{source_agent}@{source_project}"
    target_display = f"{target_agent}@{target_project}"
    if agent_base_name(str(row.get("agent_name") or "")) != source_agent:
        raise HTTPException(
            403,
            f"CTO approval decision {normalized_id} does not belong to {source_display}",
        )

    metadata = row.get("metadata") or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except json.JSONDecodeError:
            metadata = {}
    contract = metadata.get("cross_project_handoff") if isinstance(metadata, dict) else None

    structured_match = False
    if isinstance(contract, dict):
        approved_source = str(contract.get("source_agent") or "").lower().strip()
        approved_target = str(contract.get("target_agent") or "").lower().strip()
        try:
            max_handoffs = int(contract.get("max_handoffs") or 0)
        except (TypeError, ValueError):
            max_handoffs = 0
        structured_match = (
            str(contract.get("approved_by") or "").lower().strip() == "cto"
            and str(contract.get("source_project") or "").lower().strip() == source_project
            and agent_base_name(approved_source) == source_agent
            and str(contract.get("target_project") or "").lower().strip() == target_project
            and agent_base_name(approved_target) == target_agent
            and max_handoffs == 1
        )

    summary = str(row.get("summary") or "").lower()
    legacy_match = all(
        token in summary
        for token in (
            "cto authorized",
            source_display,
            "one cross-project handoff",
            f"project {target_project}",
            target_display,
        )
    )
    if not structured_match and not legacy_match:
        raise HTTPException(
            403,
            f"CTO approval decision {normalized_id} does not authorize this exact relay",
        )
    return normalized_id


@app.post("/handoffs")
async def create_handoff(
    body: HandoffCreate,
    x_agent: str = Header(alias="X-Agent-Name"),
    x_project: str = Header(alias="X-Project", default=""),
):
    agent = validate_agent_name(x_agent)
    project = require_project_scope(x_project)
    await require_registered_agent_writer(project, agent, scope="work-handoff")
    to_agent = await require_registered_handoff_target(project, body.to_agent)
    await require_registered_project(project)
    agent_display = await compound_agent(agent, project)
    to_agent_display = await compound_agent(to_agent, project) if to_agent else None
    return await persist_handoff_record(
        project=project,
        from_agent=agent_display,
        to_agent=to_agent_display,
        body=body,
    )


@app.post("/handoffs/cross-project")
async def create_cross_project_handoff(
    request: Request,
    body: CrossProjectHandoffCreate,
    x_agent: str = Header(alias="X-Agent-Name"),
    x_project: str = Header(alias="X-Project", default=""),
    x_cto_override: str = Header(alias="X-Cortex-CTO-Override", default=""),
):
    """Relay one handoff across projects without changing either roster.

    SEC-01 (audit 2026-09-01): the decision receipt alone cannot authorise this —
    the receipt check requires the CALLER's own authorship, so any agent could
    POST /log its own "CTO approval" and consume it. The admin token (operator
    custody, hashed at rest) is the actual authorizer; the decision remains the
    audit trail binding the exact relay boundary. Crossing a project boundary is
    an exceptional operator act, so operator credentials are the right bar.
    """
    require_admin_access(request)
    source_project = require_project_scope(x_project)
    target_project = validate_project_key(body.target_project)
    if source_project == target_project:
        raise HTTPException(400, "Use POST /handoffs for a project-local handoff")
    if not body.to_agent:
        raise HTTPException(400, "Cross-project handoffs require an explicit to_agent")

    source_agent = agent_base_for_project(
        x_agent,
        source_project,
        field_name="X-Agent-Name",
    )
    await require_registered_project(source_project)
    await require_registered_agent_writer(
        source_project,
        source_agent,
        scope="work-handoff",
    )
    await require_registered_project(target_project)
    target_agent = await require_registered_handoff_target(
        target_project,
        body.to_agent,
    )
    assert target_agent is not None

    approval_id = await require_cross_project_handoff_approval(
        approval_decision_id=x_cto_override,
        source_project=source_project,
        source_agent=source_agent,
        target_project=target_project,
        target_agent=target_agent,
    )
    source_display = await compound_agent(source_agent, source_project)
    target_display = await compound_agent(target_agent, target_project)
    relay_evidence = dict(body.evidence)
    relay_evidence["cross_project_relay"] = {
        "schema_version": 1,
        "approval_decision_id": approval_id,
        "source_project": source_project,
        "source_agent": source_display,
        "target_project": target_project,
        "target_agent": target_display,
    }
    handoff_body = HandoffCreate(
        **body.model_dump(exclude={"target_project", "evidence"}),
        evidence=relay_evidence,
    )
    result = await persist_handoff_record(
        project=target_project,
        from_agent=source_display,
        to_agent=target_display,
        body=handoff_body,
        single_use_approval_id=approval_id,
    )
    return {
        **result,
        "source_project": source_project,
        "target_project": target_project,
        "approval_decision_id": approval_id,
    }


# ---------------------------------------------------------------------------
# Skills registry — install / list / bind / soft-delete
# ---------------------------------------------------------------------------
# A skill is a folder (SKILL.md + optional scripts/) registered as a row in
# agent_skills and delivered to agents through the /boot persona skill_manifest.
# Two delivery channels:
#   - GLOBAL skills (scope='global', stored under sentinel project '*') reach
#     EVERY project/agent at boot with no binding (the shared skills repo).
#   - PROJECT/AGENT skills are scoped to the caller's X-Project and delivered
#     only when a binding (POST /skills/{slug}/bind) addresses the agent/role.
# Connections + project header handling mirror the /handoffs handlers above:
# acquire_scoped(storage_project) + require_project_scope/require_registered_*.

_SKILL_SCOPES = {"global", "project", "agent"}
_SKILL_SUBJECT_KINDS = {"role", "agent"}
GLOBAL_SKILL_PROJECT = "*"


def _resolve_skill_storage_project(scope: str, project: str) -> str:
    """Where a skill row lives: global ⇒ sentinel '*', else the caller project."""
    return GLOBAL_SKILL_PROJECT if scope == "global" else project


@app.post("/skills")
async def register_skill(
    request: Request,
    body: SkillRegister,
    x_agent: str = Header(alias="X-Agent-Name"),
    x_project: str = Header(alias="X-Project", default=""),
):
    """Register (upsert) a skill into the agent_skills registry.

    scope='global' stores the row under the sentinel project '*' (shared skills
    repo); any other scope stores it under X-Project. Upsert key is
    (project, skill_slug, version) — re-installing the same version updates the
    row in place. Returns the persisted row.
    """
    agent = validate_agent_name(x_agent)
    project = require_project_scope(x_project)
    await require_registered_project(project)
    # The caller must be a registered writer in their own project even when
    # publishing a global skill — global is a delivery scope, not an auth bypass.
    await require_registered_agent_writer(project, agent, scope="work")

    scope = (body.scope or "global").lower().strip()
    if scope not in _SKILL_SCOPES:
        raise HTTPException(
            400, f"Invalid skill scope '{scope}' — use one of {sorted(_SKILL_SCOPES)}"
        )
    # A GLOBAL skill lands in the shared repo (project '*') that EVERY agent boots and
    # can execute via run_bash — so publishing one is an admin operation, not merely a
    # project-writer one. Require the admin token IN ADDITION to the writer check above.
    # Non-global (project/agent) scopes stay writer-gated only. (cortex-skill must send
    # X-Cortex-Admin-Token for global installs — see the CLI note.)
    if scope == "global":
        require_admin_access(request)
    skill_slug = (body.skill_slug or "").strip()
    if not skill_slug:
        raise HTTPException(400, "skill_slug is required")
    # body_ref is read back at boot and spliced into a worker's system prompt
    # (console run_agent._skill_body). Pin it to a slug-anchored relative path so the
    # DB can never hold a traversal/absolute value (defense-in-depth with the reader's
    # own confinement). None/empty is allowed (a bodyless skill).
    if body.body_ref and not re.match(r"^\.agents/skills/[a-z0-9_-]+/SKILL\.md$", body.body_ref):
        raise HTTPException(
            400, "body_ref must be a path like .agents/skills/<slug>/SKILL.md"
        )
    version = (body.version or "1").strip() or "1"
    storage_project = _resolve_skill_storage_project(scope, project)
    metadata_json = json.dumps(body.metadata) if body.metadata is not None else None

    async with acquire_scoped(storage_project) as conn:
        row = await conn.fetchrow(
            """INSERT INTO agent_skills
                   (project, skill_slug, name, description, skill_type, scope,
                    permission, body_ref, body_hash, version, status, trust_tier,
                    metadata)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, 'active',
                       COALESCE($11, 'standard'),
                       COALESCE($12::jsonb, '{}'::jsonb))
               ON CONFLICT (project, skill_slug, version) DO UPDATE SET
                   name = EXCLUDED.name,
                   description = EXCLUDED.description,
                   skill_type = EXCLUDED.skill_type,
                   scope = EXCLUDED.scope,
                   permission = EXCLUDED.permission,
                   body_ref = EXCLUDED.body_ref,
                   body_hash = EXCLUDED.body_hash,
                   trust_tier = EXCLUDED.trust_tier,
                   metadata = EXCLUDED.metadata,
                   status = 'active'
               RETURNING id::text, project, skill_slug, name, description,
                         skill_type, scope, permission, body_ref, body_hash,
                         version, status, trust_tier, metadata,
                         created_at::text""",
            storage_project,
            skill_slug,
            body.name,
            body.description,
            (body.skill_type or "capability"),
            scope,
            body.permission,
            body.body_ref,
            body.body_hash,
            version,
            body.trust_tier,
            metadata_json,
        )
    return dict(row)


@app.get("/skills")
async def list_skills(
    x_project: str = Header(alias="X-Project", default=""),
    scope: str | None = Query(default=None),
):
    """List active skills visible to a project: all GLOBAL skills (project='*')
    UNION the X-Project project's own skills. Optional ?scope= filter narrows to
    one scope. Ordered global-first then by slug for a stable listing.
    """
    project = require_project_scope(x_project)
    await require_registered_project(project)
    if scope is not None:
        scope = scope.lower().strip()
        if scope not in _SKILL_SCOPES:
            raise HTTPException(
                400,
                f"Invalid skill scope '{scope}' — use one of {sorted(_SKILL_SCOPES)}",
            )

    async with acquire_scoped(project) as conn:
        rows = await conn.fetch(
            """SELECT id::text, project, skill_slug, name, description,
                      skill_type, scope, permission, body_ref, body_hash,
                      version, status, trust_tier, metadata, created_at::text
               FROM agent_skills
               WHERE status = 'active'
                 AND (project = $1 OR (scope = 'global' AND project = $2))
                 AND ($3::text IS NULL OR scope = $3)
               ORDER BY (project = $2) DESC, lower(skill_slug), version DESC""",
            project,
            GLOBAL_SKILL_PROJECT,
            scope,
        )
    return {"skills": [dict(r) for r in rows]}


@app.post("/skills/{slug}/bind")
async def bind_skill(
    slug: str,
    body: SkillBind,
    request: Request,
    x_agent: str = Header(alias="X-Agent-Name"),
    x_project: str = Header(alias="X-Project", default=""),
):
    """Bind a skill to a role or single agent so it reaches that subject at boot.

    Bindings are always project-scoped (you bind a skill — global or local — to a
    subject inside YOUR project). Upsert key is
    (project, subject_kind, subject, skill_slug).
    """
    agent = validate_agent_name(x_agent)
    header_project = require_project_scope(x_project)
    require_jwt_actor_match(request, agent, header_project)
    if body.project is not None:
        body_project = validate_project_key(body.project)
        if body_project != header_project:
            raise HTTPException(403, "Body project does not match X-Project")
    project = header_project
    await require_registered_project(project)
    await require_registered_agent_writer(project, agent, scope="work")

    subject_kind = (body.subject_kind or "role").lower().strip()
    if subject_kind not in _SKILL_SUBJECT_KINDS:
        raise HTTPException(
            400,
            f"Invalid subject_kind '{subject_kind}' — use one of "
            f"{sorted(_SKILL_SUBJECT_KINDS)}",
        )
    subject = (body.subject or "").strip()
    if not subject:
        raise HTTPException(400, "subject is required")
    skill_slug = (slug or "").strip()
    if not skill_slug:
        raise HTTPException(400, "skill slug is required")

    async with acquire_scoped(project) as conn:
        row = await conn.fetchrow(
            """INSERT INTO agent_skill_bindings
                   (project, subject_kind, subject, skill_slug, binding_type,
                    priority, version_pin)
               VALUES ($1, $2, $3, $4, $5, $6, $7)
               ON CONFLICT (project, subject_kind, subject, skill_slug)
               DO UPDATE SET
                   binding_type = EXCLUDED.binding_type,
                   priority = EXCLUDED.priority,
                   version_pin = EXCLUDED.version_pin
               RETURNING id::text, project, subject_kind, subject, skill_slug,
                         binding_type, priority, version_pin, created_at::text""",
            project,
            subject_kind,
            subject,
            skill_slug,
            (body.binding_type or "include"),
            body.priority if body.priority is not None else 50,
            body.version_pin,
        )
    return dict(row)


@app.delete("/skills/{slug}")
async def deprecate_skill(
    request: Request,
    slug: str,
    x_agent: str = Header(alias="X-Agent-Name"),
    x_project: str = Header(alias="X-Project", default=""),
    scope: str | None = Query(default=None),
):
    """Soft-delete a skill: set status='deprecated' for the resolved project.

    scope='global' resolves to the sentinel project '*' (and is admin-gated, like
    publishing a global skill); otherwise the X-Project project. Soft-delete keeps
    the row + provenance; boot/list already filter status='active'.
    """
    agent = validate_agent_name(x_agent)
    project = require_project_scope(x_project)
    await require_registered_project(project)
    await require_registered_agent_writer(project, agent, scope="work")

    skill_slug = (slug or "").strip()
    if not skill_slug:
        raise HTTPException(400, "skill slug is required")
    resolved_scope = (scope or "").lower().strip()
    if resolved_scope and resolved_scope not in _SKILL_SCOPES:
        raise HTTPException(
            400,
            f"Invalid skill scope '{resolved_scope}' — use one of {sorted(_SKILL_SCOPES)}",
        )
    storage_project = (
        GLOBAL_SKILL_PROJECT if resolved_scope == "global" else project
    )
    # Deprecating a GLOBAL skill removes it from every project's boot manifest, so it is
    # an admin operation — same gate as publishing one. It must be EXPLICIT (scope=global):
    # there is deliberately no silent fallback that re-tries the deprecation under '*' when
    # a project-scoped delete finds nothing (that path let a project writer knock out a
    # global skill by omitting the scope).
    if resolved_scope == "global":
        require_admin_access(request)

    async with acquire_scoped(storage_project) as conn:
        updated = await conn.fetch(
            """UPDATE agent_skills
                   SET status = 'deprecated'
               WHERE project = $1 AND skill_slug = $2 AND status <> 'deprecated'
               RETURNING id::text, project, skill_slug, version, status""",
            storage_project,
            skill_slug,
        )
    if not updated:
        raise HTTPException(404, f"Skill '{skill_slug}' not found (or already deprecated)")
    return {"deprecated": [dict(r) for r in updated]}


@app.post("/handoffs/{handoff_id}/claim-with-budget")
async def claim_handoff_with_budget(
    handoff_id: str,
    body: HandoffClaimWithBudget | None = None,
    x_agent: str = Header(alias="X-Agent-Name"),
    x_project: str = Header(alias="X-Project", default=""),
):
    agent = validate_agent_name(x_agent)
    project = require_project_scope(x_project)
    normalized_agent = agent_base_for_project(agent, project, field_name="X-Agent-Name")
    await require_registered_agent_writer(project, agent)
    await require_registered_project(project)
    claimant_identity = handoff_claimant_identity(agent, project)
    budget_request = (body or HandoffClaimWithBudget()).budget
    lane = re.sub(r"[^a-z0-9_.:-]+", "-", (budget_request.lane or "handoff").lower()).strip("-") or "handoff"
    config = handoff_budget_config(budget_request.config)
    seed_usage = handoff_budget_usage(budget_request.usage)
    request_record = handoff_budget_request(budget_request)
    input_tokens = int(request_record["input_tokens"])
    output_tokens = int(request_record["max_output_tokens"])
    estimated_total = input_tokens + output_tokens
    budget = {
        "schema_version": HANDOFF_BUDGET_SCHEMA_VERSION,
        "project": project,
        "agent": claimant_identity,
        "lane": lane,
        "handoff_id": handoff_id,
        "tick_id": budget_request.tick_id,
        "status": "not_enforced",
        "allow_llm": True,
        "reason": "Cortex handoff claims are not budget-gated; budget payload is telemetry only",
        "config": config,
        "usage": seed_usage,
        "request": request_record,
        "remaining_before": handoff_budget_remaining(config, seed_usage),
        "approved": {
            "input_tokens": input_tokens,
            "max_output_tokens": output_tokens,
            "estimated_total_tokens": estimated_total,
            "estimated_cost_usd": handoff_budget_cost(config, input_tokens, output_tokens),
            "limits_applied": ["budget_not_enforced"],
        },
        "remaining_after": handoff_budget_remaining(config, seed_usage),
        "utilization_after": {
            "input": _budget_ratio(seed_usage["input_tokens_used"], config["max_input_tokens"]),
            "output": _budget_ratio(seed_usage["output_tokens_used"], config["max_output_tokens"]),
            "total": _budget_ratio(seed_usage["total_tokens_used"], config["max_total_tokens"]),
            "cost": _budget_ratio(seed_usage["cost_usd_used"], config["max_cost_usd"]),
        },
        "recommended_action": "claim_and_execute",
    }

    async with acquire_scoped(project) as conn:
        async with conn.transaction():
            roles = await resolve_agent_role_set(conn, project, normalized_agent)
            handoff_row = await resolve_unique_handoff_for_mutation(
                conn,
                project=project,
                handoff_id=handoff_id,
            )
            result = await conn.execute(
                f"""UPDATE handoffs SET status = 'claimed', claimed_by = $1,
                          claimed_at = NOW()
                   WHERE id = $2::uuid AND project = $3
                    AND status = 'pending'
                     AND {handoff_recipient_sql('$4', '$5')}""",
                claimant_identity,
                handoff_row["id"],
                project,
                normalized_agent,
                [r.lower() for r in roles],
            )
            affected = int(result.split()[-1]) if result else 0
            if affected == 0:
                # NOT a bare 404 — say precisely why (claimed-by-X / wrong-role / terminal).
                raise_informative_claim_failure(
                    handoff_row,
                    handoff_id=handoff_id,
                    bare_agent=normalized_agent,
                    roles=roles,
                )

            event_id = await insert_handoff_budget_event(
                conn,
                project=project,
                agent=claimant_identity,
                event_type="handoff_budget_observe",
                summary=(
                    f"[BUDGET-OBSERVE:E005] handoff {handoff_id[:8]} claimed by "
                    f"{claimant_identity}; lane={lane}; budget not enforced"
                ),
                detail={
                    **budget,
                    "budget_status": budget["status"],
                    "roles": [r.lower() for r in roles],
                },
            )
            claimed_handoff = {**handoff_row, "status": "claimed", "claimed_by": claimant_identity}
            await emit_handoff_lifecycle_event(
                conn,
                project=project,
                actor=claimant_identity,
                action="claimed",
                handoff=claimed_handoff,
            )

    return {
        "claimed": True,
        "by": claimant_identity,
        "handoff_id": handoff_id,
        "budget": budget,
        "lease": {
            "status": "claimed",
            "budget_event_id": event_id,
            "budget_enforced": False,
            "lane": lane,
        },
    }


@app.put("/handoffs/{handoff_id}/claim")
@app.post("/handoffs/{handoff_id}/claim")
async def claim_handoff(
    handoff_id: str,
    x_agent: str = Header(alias="X-Agent-Name"),
    x_project: str = Header(alias="X-Project", default=""),
):
    agent = validate_agent_name(x_agent)
    project = require_project_scope(x_project)
    normalized_agent = agent_base_for_project(agent, project, field_name="X-Agent-Name")
    await require_registered_agent_writer(project, agent)
    await require_registered_project(project)
    claimant_identity = handoff_claimant_identity(agent, project)

    async with acquire_scoped(project) as conn:
        async with conn.transaction():
            roles = await resolve_agent_role_set(conn, project, normalized_agent)
            handoff_row = await resolve_unique_handoff_for_mutation(
                conn,
                project=project,
                handoff_id=handoff_id,
            )
            result = await conn.execute(
                f"""UPDATE handoffs SET status = 'claimed', claimed_by = $1,
                          claimed_at = NOW()
                   WHERE id = $2::uuid AND project = $3
                     AND status = 'pending'
                     AND {handoff_recipient_sql('$4', '$5')}""",
                claimant_identity,
                handoff_row["id"],
                project,
                normalized_agent,
                [r.lower() for r in roles],
            )
            affected = int(result.split()[-1]) if result else 0
            if affected == 0:
                # NOT a bare 404 — the row exists; say precisely why the claim failed.
                raise_informative_claim_failure(
                    handoff_row,
                    handoff_id=handoff_id,
                    bare_agent=normalized_agent,
                    roles=roles,
                )
            await emit_handoff_lifecycle_event(
                conn,
                project=project,
                actor=claimant_identity,
                action="claimed",
                handoff={**handoff_row, "status": "claimed", "claimed_by": claimant_identity},
            )
    return {"claimed": True, "by": claimant_identity}


async def actor_kind_for(conn: "asyncpg.Connection", project: str, actor: str) -> str | None:
    """The cortex_actors.kind of an actor: agent | human | system.

    Fail closed (K0 rework, D3 closure): a UNIQUE registered identity resolves to its
    kind; missing, ambiguous (more than one distinct registered kind for the slug), or
    unavailable identity data resolves to ``agent`` — never ``human``. An agent kind
    engages the evidence floor, prohibits auto-completion of self-returns, and routes
    handbacks to an independent reviewer."""
    try:
        rows = await conn.fetch(
            """SELECT a.kind FROM cortex_actors a
                 JOIN cortex_projects p ON p.id = a.project_id
                WHERE p.project_key = $1 AND lower(a.slug) = lower($2)""",
            project,
            agent_base_name(actor),
        )
    except Exception:  # noqa: BLE001 — unavailable identity data is fail-closed
        rows = None
    if not rows:
        return "agent"
    kinds = {str(r["kind"]).strip().lower() for r in rows}
    if len(kinds) == 1:
        return kinds.pop()
    return "agent"  # ambiguous identity: treated as agent, never human


async def independent_human_recipient(
    conn: asyncpg.Connection,
    *,
    project: str,
    excluding: str,
) -> str:
    """A registered HUMAN project identity distinct from ``excluding`` (the returning
    actor), or "" when none. Self-review routing must never address the handback to
    the worker under review; a human is the governed reviewer of last resort. Identity
    data that is unavailable resolves to "" so the lead-fallback route and the
    accept-path independence guard keep the loop fail closed."""
    try:
        row = await conn.fetchrow(
            """SELECT a.slug FROM cortex_actors a
                 JOIN cortex_projects p ON p.id = a.project_id
                WHERE p.project_key = $1
                  AND a.kind = 'human'
                  AND lower(a.slug) <> lower($2)
                ORDER BY a.created_at
                LIMIT 1""",
            project,
            agent_base_name(excluding),
        )
    except Exception:  # noqa: BLE001 — covered by the accept-path guard
        return ""
    if row is None:
        return ""
    return await compound_agent(str(row["slug"]).strip().lower(), project)


@app.put("/handoffs/{handoff_id}/return")
@app.post("/handoffs/{handoff_id}/return")
async def return_handoff(
    handoff_id: str,
    body: HandoffReturn,
    request: Request,
    x_agent: str = Header(alias="X-Agent-Name"),
    x_project: str = Header(alias="X-Project", default=""),
):
    """Return completed work to its delegator without losing the review gate.

    A task return and its completion-handback are committed in one transaction.
    Returning a completion-handback accepts (or explicitly requeues) its parent,
    so a handback can never recursively create another handback.
    """
    project = require_project_scope(x_project)
    raw_actor = validate_agent_name(x_agent)
    require_jwt_actor_match(request, raw_actor, project)
    await require_registered_agent_writer(project, raw_actor, scope="work")
    await require_registered_project(project)
    actor = await compound_agent(raw_actor, project)
    report = normalized_handoff_completion_report(body)
    warnings: list[str] = []

    async with acquire_scoped(project) as conn:
        async with conn.transaction():
            initial = await resolve_unique_handoff_for_mutation(
                conn,
                project=project,
                handoff_id=handoff_id,
            )
            root_id = (
                initial.get("reply_to_handoff_id")
                if initial.get("kind") == "completion_handback"
                else initial["id"]
            )
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))",
                f"handoff-return:{project}:{root_id}",
            )
            handoff_row = await locked_handoff_row(
                conn,
                project=project,
                handoff_id=initial["id"],
            )

            if agent_base_name(handoff_row.get("claimed_by") or "") != agent_base_name(actor):
                if handoff_row.get("status") == "completed":
                    return completed_handoff_return_receipt(handoff_row)
                raise HTTPException(
                    409,
                    f"Handoff {handoff_id} is not claimed by {actor}",
                )

            if handoff_row.get("status") == "completed":
                return completed_handoff_return_receipt(handoff_row)

            if handoff_row.get("kind") == "completion_handback":
                parent_id = handoff_row.get("reply_to_handoff_id")
                if not parent_id:
                    raise HTTPException(
                        409,
                        f"Completion handback {handoff_id} has no parent handoff",
                    )
                parent = await locked_handoff_row(
                    conn,
                    project=project,
                    handoff_id=parent_id,
                )
                decision = report.get("decision")
                if decision not in {"accept", "rework"}:
                    raise HTTPException(
                        409,
                        "Completion handback requires explicit decision=accept "
                        "or decision=rework",
                    )
                if decision == "accept":
                    require_supported_completion_approval(parent)
                    original_worker = agent_base_name(handoff_row.get("from_agent") or "") or (
                        agent_base_name(parent.get("to_agent") or "")
                        or agent_base_name(parent.get("claimed_by") or "")
                    )
                    if original_worker and original_worker == agent_base_name(actor):
                        raise HTTPException(
                            403,
                            "Independence required: the original worker cannot accept "
                            "the review of its own work",
                        )
                    parent_routing = str(
                        json_object(json_object(parent.get("completion_report")).get("metadata"))
                        .get("return_routing")
                        or ""
                    )
                    if parent_routing == "self_review_required":
                        accepter_kind = await actor_kind_for(conn, project, actor)
                        if accepter_kind != "human":
                            raise HTTPException(
                                403,
                                "Self-review routing: a human reviewer must accept "
                                "this return",
                            )

                await conn.execute(
                    """UPDATE handoffs
                          SET status = 'completed',
                              completed_at = NOW(),
                              completion_report = $1::jsonb
                        WHERE id = $2::uuid AND project = $3
                          AND status = 'claimed'""",
                    json.dumps(report),
                    handoff_row["id"],
                    project,
                )
                if decision == "rework":
                    if parent.get("status") != "returned":
                        raise HTTPException(
                            409,
                            f"Parent handoff {parent_id} is {parent.get('status')}, not returned",
                        )
                    await conn.execute(
                        """UPDATE handoffs
                              SET status = 'pending',
                                  claimed_by = NULL,
                                  claimed_at = NULL,
                                  completed_at = NULL,
                                  retry_count = COALESCE(retry_count, 0) + 1
                            WHERE id = $1::uuid AND project = $2
                              AND status = 'returned'""",
                        parent_id,
                        project,
                    )
                    updated_parent = {
                        **parent,
                        "status": "pending",
                        "claimed_by": None,
                        "claimed_at": None,
                        "retry_count": int(parent.get("retry_count") or 0) + 1,
                    }
                    await emit_handoff_lifecycle_event(
                        conn,
                        project=project,
                        actor=actor,
                        action="rework_requested",
                        handoff=updated_parent,
                        reason=report["summary"],
                    )
                    await emit_handoff_lifecycle_event(
                        conn,
                        project=project,
                        actor=actor,
                        action="completed",
                        handoff={
                            **handoff_row,
                            "status": "completed",
                            "completion_report": report,
                        },
                    )
                    return {
                        "returned": True,
                        "rework_requested": True,
                        "status": "pending",
                        "parent_handoff_id": parent_id,
                        "handback_id": handoff_row["id"],
                        "deduped": False,
                    }

                if parent.get("status") not in {"returned", "completed"}:
                    raise HTTPException(
                        409,
                        f"Parent handoff {parent_id} is {parent.get('status')}, not returned",
                    )
                if parent.get("status") == "returned":
                    await conn.execute(
                        """UPDATE handoffs
                              SET status = 'completed', completed_at = NOW()
                            WHERE id = $1::uuid AND project = $2
                              AND status = 'returned'""",
                        parent_id,
                        project,
                    )
                await emit_handoff_lifecycle_event(
                    conn,
                    project=project,
                    actor=actor,
                    action="accepted",
                    handoff={**parent, "status": "completed"},
                )
                await emit_handoff_lifecycle_event(
                    conn,
                    project=project,
                    actor=actor,
                    action="completed",
                    handoff={
                        **handoff_row,
                        "status": "completed",
                        "completion_report": report,
                    },
                )
                return {
                    "returned": True,
                    "accepted": True,
                    "status": "completed",
                    "parent_handoff_id": parent_id,
                    "handback_id": handoff_row["id"],
                    "deduped": parent.get("status") == "completed",
                }

            if handoff_row.get("status") == "returned":
                existing = await conn.fetchrow(
                    """SELECT id::text, status
                         FROM handoffs
                        WHERE project = $1
                          AND kind = 'completion_handback'
                          AND reply_to_handoff_id = $2::uuid
                          AND invalidated_at IS NULL
                          AND status IN ('pending', 'claimed')
                        LIMIT 1""",
                    project,
                    handoff_row["id"],
                )
                return {
                    "returned": True,
                    "status": "returned",
                    "parent_handoff_id": handoff_row["id"],
                    "handback_id": existing["id"] if existing else None,
                    "deduped": True,
                }
            if handoff_row.get("status") != "claimed":
                raise HTTPException(
                    409,
                    f"Handoff {handoff_id} is {handoff_row.get('status')}, not claimed",
                )

            work_product_id = report.get("work_product_id")
            if work_product_id:
                await ensure_work_products_schema(conn)
                work_product = await resolve_unique_work_product(
                    conn,
                    project=project,
                    work_product_id=work_product_id,
                )
                linked_handoff = str(work_product.get("handoff_id") or "")
                if work_product.get("invalidated_at") is not None:
                    raise HTTPException(
                        409,
                        f"Work product {work_product_id} is invalidated",
                    )
                if linked_handoff != handoff_row["id"]:
                    raise HTTPException(
                        409,
                        f"Work product {work_product_id} is not linked to this handoff",
                    )
                report["work_product_id"] = work_product["id"]
            elif normalize_text_list(handoff_row.get("files_changed")):
                warnings.append(
                    "work_product_missing: file-changing handoff returned without a Work Product Memory receipt"
                )

            actor_kind = await actor_kind_for(conn, project, actor)
            report = apply_completion_evidence_floor(report, actor_kind=actor_kind)
            recipient = ""
            recipient_role = ""
            used_lead_fallback = False
            if actor_kind != "human":
                recipient = await independent_human_recipient(
                    conn, project=project, excluding=actor,
                )
                if recipient:
                    recipient_role = "lead"
                    report["metadata"] = {
                        **json_object(report.get("metadata")),
                        "return_routing": "self_review_routed_independent",
                        "return_routing_reason": "agent_return_requires_independent_reviewer",
                    }
            if not recipient:
                recipient, recipient_role, used_lead_fallback = await handback_recipient(
                    conn,
                    project=project,
                    handoff=handoff_row,
                    excluding=actor,
                )
            if used_lead_fallback:
                report["metadata"] = {
                    **json_object(report.get("metadata")),
                    "return_routing": "project_lead_fallback",
                    "return_routing_reason": "cross_project_or_system_sender",
                }

            same_recipient = agent_base_name(recipient) == agent_base_name(actor)
            if same_recipient and actor_kind != "human":
                # No agent self-approval by any path (K0 rework, D3 closure): when no
                # independent registered human reviewer exists the completion handback
                # would be addressed to the returning agent itself, so the return fails
                # closed. Legacy rows already carrying self_review_required stay gated
                # by the accept-path human-kind check.
                raise HTTPException(
                    409,
                    "No independent reviewer available: a completion handback "
                    "cannot be addressed to the returning agent",
                )
            if same_recipient and actor_kind == "human":
                await conn.execute(
                    """UPDATE handoffs
                          SET status = 'completed',
                              returned_at = NOW(),
                              completed_at = NOW(),
                              completion_report = $1::jsonb
                        WHERE id = $2::uuid AND project = $3
                          AND status = 'claimed'""",
                    json.dumps(report),
                    handoff_row["id"],
                    project,
                )
                await emit_handoff_lifecycle_event(
                    conn,
                    project=project,
                    actor=actor,
                    action="auto_accepted",
                    handoff={
                        **handoff_row,
                        "status": "completed",
                        "completion_report": report,
                    },
                )
                return {
                    "returned": True,
                    "status": "completed",
                    "parent_handoff_id": handoff_row["id"],
                    "handback_id": None,
                    "auto_accepted": True,
                    "deduped": False,
                    "warnings": warnings,
                }

            await conn.execute(
                """UPDATE handoffs
                      SET status = 'returned',
                          returned_at = NOW(),
                          completion_report = $1::jsonb
                    WHERE id = $2::uuid AND project = $3
                      AND status = 'claimed'""",
                json.dumps(report),
                handoff_row["id"],
                project,
            )
            handback_summary = (
                f"Review completion: {compact_text(handoff_row.get('summary'), limit=300)}"
            )
            handback_context = (
                f"Parent handoff: {handoff_row['id']}\n"
                "Review the completion report against the parent acceptance criteria. "
                "Return this handback with decision=accept to close the parent or "
                "decision=rework to requeue it."
            )
            handback_acceptance = {
                "schema": "cortex.completion_handback_acceptance.v1",
                "parent_handoff_id": handoff_row["id"],
                "parent_acceptance": handoff_policy(handoff_row.get("acceptance")),
                "allowed_decisions": ["accept", "rework"],
            }
            handback_evidence = {
                "parent_handoff_id": handoff_row["id"],
                "completion_report": report,
            }
            handback_id = await conn.fetchval(
                """INSERT INTO handoffs (
                        project, kind, reply_to_handoff_id,
                        from_agent, from_role, to_role, to_agent,
                        priority, summary, branch, context, parent_goal_id,
                        acceptance, evidence, retry, escalation
                   ) VALUES (
                        $1, 'completion_handback', $2::uuid,
                        $3, $4, $5, $6,
                        $7, $8, $9, $10, $11,
                        $12::jsonb, $13::jsonb, $14::jsonb, $15::jsonb
                   )
                   ON CONFLICT (project, reply_to_handoff_id)
                       WHERE kind = 'completion_handback'
                         AND invalidated_at IS NULL
                         AND status IN ('pending', 'claimed')
                   DO UPDATE SET reply_to_handoff_id = EXCLUDED.reply_to_handoff_id
                   RETURNING id""",
                project,
                handoff_row["id"],
                actor,
                handoff_row.get("to_role"),
                recipient_role,
                recipient,
                handoff_row.get("priority") or "medium",
                handback_summary,
                handoff_row.get("branch"),
                handback_context,
                handoff_row.get("parent_goal_id"),
                handoff_policy_db(handback_acceptance),
                handoff_policy_db(handback_evidence),
                handoff_policy_db(handoff_row.get("retry")),
                handoff_policy_db(handoff_row.get("escalation")),
            )
            await emit_handoff_lifecycle_event(
                conn,
                project=project,
                actor=actor,
                action="returned",
                handoff={
                    **handoff_row,
                    "status": "returned",
                    "completion_report": report,
                },
            )
            await emit_handoff_lifecycle_event(
                conn,
                project=project,
                actor=actor,
                action="created",
                handoff={
                    "id": str(handback_id),
                    "status": "pending",
                    "kind": "completion_handback",
                    "reply_to_handoff_id": handoff_row["id"],
                    "from_agent": actor,
                    "from_role": handoff_row.get("to_role"),
                    "to_role": recipient_role,
                    "to_agent": recipient,
                    "priority": handoff_row.get("priority") or "medium",
                    "summary": handback_summary,
                    "claimed_by": None,
                    "retry_count": 0,
                    "acceptance": handback_acceptance,
                    "evidence": handback_evidence,
                    "retry": handoff_policy(handoff_row.get("retry")),
                    "escalation": handoff_policy(handoff_row.get("escalation")),
                },
            )

    return {
        "returned": True,
        "status": "returned",
        "parent_handoff_id": handoff_row["id"],
        "handback_id": str(handback_id),
        "auto_accepted": False,
        "deduped": False,
        "warnings": warnings,
    }


@app.put("/handoffs/{handoff_id}/complete")
@app.post("/handoffs/{handoff_id}/complete")
async def complete_handoff(
    handoff_id: str,
    request: Request,
    x_agent: str = Header(alias="X-Agent-Name", default=""),
    x_project: str = Header(alias="X-Project", default=""),
):
    require_admin_access(request)
    project = require_project_scope(x_project)
    actor = None
    if x_agent:
        raw_agent = validate_agent_name(x_agent)
        await require_registered_agent_writer(project, raw_agent)
        actor = await compound_agent(raw_agent, project)
    await require_registered_project(project)
    handoff_row: dict[str, Any] | None = None
    async with acquire_scoped(project) as conn:
        async with conn.transaction():
            handoff_row = await resolve_unique_handoff_for_mutation(
                conn,
                project=project,
                handoff_id=handoff_id,
            )
            if handoff_row.get("kind") == "completion_handback":
                raise HTTPException(
                    409,
                    "Completion handbacks must be resolved through /return with "
                    "an explicit accept or rework decision",
                )
            result = await conn.execute(
                """UPDATE handoffs SET status = 'completed', completed_at = NOW()
                   WHERE id = $1::uuid AND project = $2""",
                handoff_row["id"],
                project,
            )
            affected = int(result.split()[-1]) if result else 0
            if affected == 0:
                raise HTTPException(404, f"Handoff {handoff_id} not found")
            lifecycle_actor = actor
            if not lifecycle_actor:
                claimed_by = handoff_row.get("claimed_by")
                lifecycle_actor = await compound_agent(claimed_by or "system", project)
            await emit_handoff_lifecycle_event(
                conn,
                project=project,
                actor=lifecycle_actor,
                action="completed",
                handoff={**handoff_row, "status": "completed"},
            )

    warnings: list[str] = []
    if handoff_row and normalize_text_list(handoff_row.get("files_changed")):
        try:
            async with acquire_scoped(project) as conn:
                await ensure_work_products_schema(conn)
                has_work_product = await conn.fetchval(
                    """SELECT EXISTS (
                           SELECT 1
                             FROM work_products
                            WHERE project = $1
                              AND handoff_id = $2::uuid
                              AND invalidated_at IS NULL
                        )""",
                    project,
                    handoff_row["id"],
                )
            if not has_work_product:
                warnings.append(
                    "work_product_missing: file-changing handoff completed without a Work Product Memory receipt"
                )
        except Exception as exc:
            warnings.append(f"work_product_check_unavailable: {type(exc).__name__}")
    return {"completed": True, "warnings": warnings}


# ---------------------------------------------------------------------------
# Terminal-non-success transitions — handoff 63a116e8 Alpha Option B.
# Closes Lux RCA 4337ef2c root cause #1 (schema gap): claimed handoffs had
# no graceful exit other than --complete, forcing agents to either lie or
# leave zombies. These three endpoints mirror /complete but record an
# audit reason in the new terminal_reason column.
#
#   PUT /handoffs/{id}/release   claimed -> pending  (someone else can take it)
#   PUT /handoffs/{id}/abandon   claimed -> abandoned (work no longer needed)
#   PUT /handoffs/{id}/fail      claimed -> failed    (unrecoverable; Alpha triage)
#
# All three accept an optional {"reason": "..."} body. The mutation requires
# either the exact claimant or the backend-held admin token. claimed_by stays
# as audit trail except on release, which clears it for the next exact claim.
# ---------------------------------------------------------------------------


class HandoffTerminate(BaseModel):
    reason: Optional[str] = None


@app.put("/handoffs/{handoff_id}/release")
@app.post("/handoffs/{handoff_id}/release")
async def release_handoff(
    handoff_id: str,
    request: Request,
    body: Optional[HandoffTerminate] = None,
    x_agent: str = Header(alias="X-Agent-Name", default=""),
    x_project: str = Header(alias="X-Project", default=""),
):
    """Drop a claim back into the pending pool. claimed_by is cleared so
    the next /claim runs without the equality-check footgun Lux hit."""
    project = require_project_scope(x_project)
    await require_registered_project(project)
    actor, claimant_actor = await handoff_termination_authority(
        request,
        actor=x_agent,
        project=project,
    )
    reason = (body.reason if body else None) or None
    async with acquire_scoped(project) as conn:
        async with conn.transaction():
            resolved = await resolve_unique_handoff_for_mutation(
                conn,
                project=project,
                handoff_id=handoff_id,
            )
            handoff_row = await locked_handoff_row(
                conn,
                project=project,
                handoff_id=resolved["id"],
            )
            if handoff_row.get("status") != "claimed":
                raise HTTPException(409, "Handoff is not in 'claimed' state")
            claimant = (
                require_handoff_claimant(
                    handoff_row,
                    actor=claimant_actor,
                    project=project,
                )
                if claimant_actor is not None
                else ""
            )
            next_retry_count = int(handoff_row.get("retry_count") or 0) + 1
            result = await conn.execute(
                """UPDATE handoffs
                      SET status = 'pending',
                          claimed_by = NULL,
                          claimed_at = NULL,
                          retry_count = COALESCE(retry_count, 0) + 1,
                          terminal_reason = $3
                    WHERE id = $1::uuid
                      AND project = $2
                      AND status = 'claimed'
                      AND ($4::boolean OR lower(split_part(COALESCE(claimed_by, ''), '@', 1)) = $5)""",
                handoff_row["id"], project, reason, claimant_actor is None, claimant,
            )
            affected = int(result.split()[-1]) if result else 0
            if affected == 0:
                raise HTTPException(
                    404,
                    f"Handoff {handoff_id} not found or not in 'claimed' state",
                )
            await emit_handoff_lifecycle_event(
                conn,
                project=project,
                actor=actor,
                action="released",
                handoff={**handoff_row, "status": "pending", "claimed_by": None, "retry_count": next_retry_count},
                reason=reason,
            )
    return {"released": True, "retry_count": next_retry_count}


@app.put("/handoffs/{handoff_id}/abandon")
@app.post("/handoffs/{handoff_id}/abandon")
async def abandon_handoff(
    handoff_id: str,
    request: Request,
    body: Optional[HandoffTerminate] = None,
    x_agent: str = Header(alias="X-Agent-Name", default=""),
    x_project: str = Header(alias="X-Project", default=""),
):
    """Mark a handoff abandoned (work no longer needed). Terminal."""
    project = require_project_scope(x_project)
    await require_registered_project(project)
    actor, claimant_actor = await handoff_termination_authority(
        request,
        actor=x_agent,
        project=project,
    )
    reason = (body.reason if body else None) or None
    async with acquire_scoped(project) as conn:
        async with conn.transaction():
            resolved = await resolve_unique_handoff_for_mutation(
                conn,
                project=project,
                handoff_id=handoff_id,
            )
            handoff_row = await locked_handoff_row(
                conn,
                project=project,
                handoff_id=resolved["id"],
            )
            if handoff_row.get("status") != "claimed":
                raise HTTPException(409, "Handoff is not in 'claimed' state")
            claimant = (
                require_handoff_claimant(
                    handoff_row,
                    actor=claimant_actor,
                    project=project,
                )
                if claimant_actor is not None
                else ""
            )
            if handoff_row.get("kind") == "completion_handback":
                raise HTTPException(
                    409,
                    "Completion handbacks cannot be abandoned; return an explicit "
                    "accept/rework decision or release the claim",
                )
            result = await conn.execute(
                """UPDATE handoffs
                      SET status = 'abandoned',
                          completed_at = NOW(),
                          terminal_reason = $3
                    WHERE id = $1::uuid
                      AND project = $2
                      AND status = 'claimed'
                      AND ($4::boolean OR lower(split_part(COALESCE(claimed_by, ''), '@', 1)) = $5)""",
                handoff_row["id"], project, reason, claimant_actor is None, claimant,
            )
            affected = int(result.split()[-1]) if result else 0
            if affected == 0:
                raise HTTPException(
                    404,
                    f"Handoff {handoff_id} not found, not claimed, or claimant changed",
                )
            await emit_handoff_lifecycle_event(
                conn,
                project=project,
                actor=actor,
                action="abandoned",
                handoff={**handoff_row, "status": "abandoned"},
                reason=reason,
            )
    return {"abandoned": True}


@app.put("/handoffs/{handoff_id}/fail")
@app.post("/handoffs/{handoff_id}/fail")
async def fail_handoff(
    handoff_id: str,
    request: Request,
    body: Optional[HandoffTerminate] = None,
    x_agent: str = Header(alias="X-Agent-Name", default=""),
    x_project: str = Header(alias="X-Project", default=""),
):
    """Mark a handoff failed (unrecoverable error). Terminal."""
    project = require_project_scope(x_project)
    await require_registered_project(project)
    actor, claimant_actor = await handoff_termination_authority(
        request,
        actor=x_agent,
        project=project,
    )
    reason = (body.reason if body else None) or None
    async with acquire_scoped(project) as conn:
        async with conn.transaction():
            resolved = await resolve_unique_handoff_for_mutation(
                conn,
                project=project,
                handoff_id=handoff_id,
            )
            handoff_row = await locked_handoff_row(
                conn,
                project=project,
                handoff_id=resolved["id"],
            )
            if handoff_row.get("status") != "claimed":
                raise HTTPException(409, "Handoff is not in 'claimed' state")
            claimant = (
                require_handoff_claimant(
                    handoff_row,
                    actor=claimant_actor,
                    project=project,
                )
                if claimant_actor is not None
                else ""
            )
            if handoff_row.get("kind") == "completion_handback":
                raise HTTPException(
                    409,
                    "Completion handbacks cannot fail terminally; release the claim "
                    "so review can be retried",
                )
            result = await conn.execute(
                """UPDATE handoffs
                      SET status = 'failed',
                          completed_at = NOW(),
                          terminal_reason = $3
                    WHERE id = $1::uuid
                      AND project = $2
                      AND status = 'claimed'
                      AND ($4::boolean OR lower(split_part(COALESCE(claimed_by, ''), '@', 1)) = $5)""",
                handoff_row["id"], project, reason, claimant_actor is None, claimant,
            )
            affected = int(result.split()[-1]) if result else 0
            if affected == 0:
                raise HTTPException(
                    404,
                    f"Handoff {handoff_id} not found or not in 'claimed' state",
                )
            await emit_handoff_lifecycle_event(
                conn,
                project=project,
                actor=actor,
                action="failed",
                handoff={**handoff_row, "status": "failed"},
                reason=reason,
            )
    return {"failed": True}


# ---------------------------------------------------------------------------
# Beat operator reads/actions
# ---------------------------------------------------------------------------


@app.get("/dashboard/snapshot")
async def dashboard_snapshot(
    request: Request,
    x_project: str = Header(alias="X-Project", default=""),
    queue_limit: int = Query(default=20, ge=1, le=100),
    activity_limit: int = Query(default=20, ge=1, le=100),
    heartbeat_fresh_seconds: int = Query(default=3300, ge=10, le=86400),
    pending_stale_hours: int = Query(default=24, ge=1, le=720),
    claimed_stale_hours: int = Query(default=12, ge=1, le=720),
):
    """Return one coherent, project-scoped operator-dashboard read model.

    The dashboard previously assembled independent API and direct-SQL reads.
    Missing API data therefore looked like zero work and could render GREEN.
    This endpoint makes the read model an explicit Cortex contract and holds all
    component queries inside one read-only, repeatable-read transaction.
    """
    require_admin_access(request)
    project = require_project_scope(x_project)
    await require_registered_project(project)

    async with acquire_scoped(project) as conn:
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            counts_row = await conn.fetchrow(
                """SELECT
                        COUNT(*) FILTER (
                            WHERE status = 'pending' AND invalidated_at IS NULL
                        )::int AS pending,
                        COUNT(*) FILTER (
                            WHERE status = 'claimed' AND invalidated_at IS NULL
                        )::int AS claimed,
                        COUNT(*) FILTER (
                            WHERE status IN ('pending', 'claimed')
                              AND invalidated_at IS NULL
                        )::int AS open,
                        COUNT(*) FILTER (
                            WHERE status IN ('pending', 'claimed')
                              AND invalidated_at IS NULL
                              AND priority = 'urgent'
                        )::int AS urgent,
                        COUNT(*) FILTER (
                            WHERE status = 'pending'
                              AND invalidated_at IS NULL
                              AND summary LIKE '%[CONSULT%'
                        )::int AS consults,
                        COUNT(*) FILTER (
                            WHERE invalidated_at IS NULL
                              AND (
                                (
                                  status = 'pending'
                                  AND created_at < NOW() - make_interval(hours => $2::int)
                                )
                                OR
                                (
                                  status = 'claimed'
                                  AND COALESCE(claimed_at, created_at)
                                      < NOW() - make_interval(hours => $3::int)
                                )
                              )
                        )::int AS stale
                   FROM handoffs
                  WHERE project = $1""",
                project,
                pending_stale_hours,
                claimed_stale_hours,
            )

            queue_rows = await conn.fetch(
                """SELECT
                        id::text,
                        status,
                        priority,
                        from_agent,
                        to_role,
                        COALESCE(to_agent, '') AS to_agent,
                        COALESCE(claimed_by, '') AS claimed_by,
                        LEFT(COALESCE(summary, ''), 180) AS summary,
                        created_at::text,
                        claimed_at::text,
                        ROUND(EXTRACT(EPOCH FROM (NOW() - created_at)) / 3600.0, 1)::float8
                            AS age_hours,
                        CASE
                            WHEN claimed_at IS NULL THEN NULL
                            ELSE ROUND(EXTRACT(EPOCH FROM (NOW() - claimed_at)) / 3600.0, 1)::float8
                        END AS claimed_age_hours,
                        CASE
                            WHEN status = 'pending' THEN
                                created_at < NOW() - make_interval(hours => $2::int)
                            WHEN status = 'claimed' THEN
                                COALESCE(claimed_at, created_at)
                                    < NOW() - make_interval(hours => $3::int)
                            ELSE FALSE
                        END AS is_stale
                   FROM handoffs
                  WHERE project = $1
                    AND status IN ('pending', 'claimed')
                    AND invalidated_at IS NULL
                  ORDER BY CASE priority
                               WHEN 'urgent' THEN 0
                               WHEN 'high' THEN 1
                               WHEN 'medium' THEN 2
                               WHEN 'low' THEN 3
                               ELSE 4
                           END,
                           CASE
                               WHEN status = 'pending' THEN
                                   created_at < NOW() - make_interval(hours => $2::int)
                               WHEN status = 'claimed' THEN
                                   COALESCE(claimed_at, created_at)
                                       < NOW() - make_interval(hours => $3::int)
                               ELSE FALSE
                           END DESC,
                           created_at ASC
                  LIMIT $4""",
                project,
                pending_stale_hours,
                claimed_stale_hours,
                queue_limit,
            )

            heartbeat_row = await conn.fetchrow(
                """SELECT
                        NOW()::text AS generated_at,
                        MAX(created_at)::text AS last_at,
                        ROUND(EXTRACT(EPOCH FROM (NOW() - MAX(created_at))))::int
                            AS age_seconds
                   FROM decisions
                  WHERE project = $1
                    AND invalidated_at IS NULL
                    AND summary LIKE '%source=launchd%'""",
                project,
            )

            epic_rows = await conn.fetch(
                """SELECT project, epic_id, title, status, overall_pct,
                          increments, updated_at::text
                     FROM epics
                    WHERE project = $1
                    ORDER BY epic_id""",
                project,
            )
            event_rows = await fetch_recent_team_events(conn, project, activity_limit)

    counts = {
        key: int(counts_row[key] or 0) if counts_row else 0
        for key in ("pending", "claimed", "open", "urgent", "consults", "stale")
    }
    age_seconds = (
        int(heartbeat_row["age_seconds"])
        if heartbeat_row and heartbeat_row["age_seconds"] is not None
        else None
    )
    heartbeat_fresh = age_seconds is not None and age_seconds <= heartbeat_fresh_seconds

    return {
        "contract": "cortex.dashboard.snapshot.v1",
        "project": project,
        "generated_at": heartbeat_row["generated_at"] if heartbeat_row else None,
        "source": {
            "status": "ok",
            "kind": "cortex-api",
            "endpoint": "/dashboard/snapshot",
            "consistency": "repeatable-read",
        },
        "queue": {
            "counts": counts,
            "items": [dict(row) for row in queue_rows],
            "limit": queue_limit,
            "pending_stale_hours": pending_stale_hours,
            "claimed_stale_hours": claimed_stale_hours,
        },
        "heartbeat": {
            "last_at": heartbeat_row["last_at"] if heartbeat_row else None,
            "age_seconds": age_seconds,
            "fresh": heartbeat_fresh,
            "stale": not heartbeat_fresh,
            "fresh_seconds": heartbeat_fresh_seconds,
        },
        "epics": [_epic_row_to_dict(row) for row in epic_rows],
        "activity": {
            "events": [team_event_stream_entry(row) for row in event_rows],
            "limit": activity_limit,
        },
    }


@app.get("/beat/status")
async def beat_status(
    request: Request,
    x_project: str = Header(alias="X-Project", default=""),
    recent_minutes: int = Query(default=10, ge=1, le=240),
    fresh_seconds: int = Query(default=90, ge=10, le=3600),
):
    """Operator status payload for beatctl without direct DB access."""
    require_admin_access(request)
    project = require_project_scope(x_project)

    async with acquire_scoped(project) as conn:
        counts_rows = await conn.fetch(
            """SELECT 'pending' AS key, COUNT(*)::int AS value
                 FROM handoffs
                WHERE project = $1
                  AND status = 'pending'
                  AND invalidated_at IS NULL
               UNION ALL
               SELECT 'claimed', COUNT(*)::int
                 FROM handoffs
                WHERE project = $1
                  AND status = 'claimed'
                  AND invalidated_at IS NULL
               UNION ALL
               SELECT 'stale', COUNT(*)::int
                 FROM handoffs
                WHERE project = $1
                  AND status IN ('pending', 'claimed')
                  AND invalidated_at IS NULL
                  AND created_at < NOW() - INTERVAL '24 hours'
               UNION ALL
               SELECT 'consults', COUNT(*)::int
                 FROM handoffs
                WHERE project = $1
                  AND status = 'pending'
                  AND invalidated_at IS NULL
                  AND summary LIKE '%[CONSULT%'""",
            project,
        )
        counts = {row["key"]: int(row["value"] or 0) for row in counts_rows}

        heartbeat_meta = await conn.fetchrow(
            """SELECT
                    to_char(MAX(created_at), 'HH24:MI:SS') AS last_heartbeat,
                    COUNT(*) FILTER (
                        WHERE created_at > NOW() - ($2::int * INTERVAL '1 second')
                    )::int AS heartbeat_count_fresh,
                    COUNT(*) FILTER (
                        WHERE created_at > NOW() - INTERVAL '5 minutes'
                    )::int AS heartbeat_count_5m
               FROM decisions
              WHERE project = $1
                AND summary LIKE '%source=launchd%'""",
            project,
            fresh_seconds,
        )

        recent_heartbeats = await conn.fetch(
            """SELECT to_char(created_at, 'HH24:MI:SS') AS ts,
                      LEFT(COALESCE(summary, ''), 120) AS summary
                FROM decisions
                WHERE project = $1
                  AND summary LIKE '%source=launchd%'
                  AND created_at > NOW() - ($2::int * INTERVAL '1 minute')
                ORDER BY created_at DESC
                LIMIT 5""",
            project,
            recent_minutes,
        )

        cron_runs = await conn.fetch(
            """SELECT to_char(created_at, 'HH24:MI:SS') AS ts,
                      substring(summary FROM 'Beat cron ([a-z_]+):') AS name,
                      CASE WHEN summary LIKE '%Beat cron%: ok%' THEN 'ok'
                           WHEN summary LIKE '%Beat cron%: failed%' THEN 'fail'
                           ELSE '?' END AS status
                 FROM decisions
                WHERE project = $1
                  AND summary LIKE 'Beat cron%'
                  AND created_at > NOW() - ($2::int * INTERVAL '1 minute')
                ORDER BY created_at DESC
                LIMIT 8""",
            project,
            recent_minutes,
        )

        agent_activity = await conn.fetch(
            """SELECT agent_name, COUNT(*)::int AS count
                 FROM decisions
                WHERE project = $1
                  AND created_at > NOW() - INTERVAL '5 minutes'
                GROUP BY agent_name
                ORDER BY COUNT(*) DESC
                LIMIT 6""",
            project,
        )

    return {
        "project": project,
        "counts": counts,
        "last_heartbeat": heartbeat_meta["last_heartbeat"] if heartbeat_meta else None,
        "heartbeat_count_fresh": int(heartbeat_meta["heartbeat_count_fresh"] or 0)
        if heartbeat_meta
        else 0,
        "heartbeat_count_5m": int(heartbeat_meta["heartbeat_count_5m"] or 0)
        if heartbeat_meta
        else 0,
        "recent_heartbeats": [dict(row) for row in recent_heartbeats],
        "cron_runs": [dict(row) for row in cron_runs],
        "agent_activity": [dict(row) for row in agent_activity],
    }


@app.get("/beat/roles")
async def beat_role_map(
    request: Request,
    x_project: str = Header(alias="X-Project", default=""),
):
    require_admin_access(request)
    project = require_project_scope(x_project)

    async with acquire_scoped(project) as conn:
        rows = await conn.fetch(
            """SELECT DISTINCT lower(role) AS role, lower(agent_name) AS agent_name
               FROM agent_profiles
               WHERE project = $1
                 AND COALESCE(role, '') <> ''
                 AND COALESCE(agent_name, '') <> ''
               ORDER BY lower(role), lower(agent_name)""",
            project,
        )

    role_map: dict[str, str | None] = {}
    for row in rows:
        role = row["role"]
        agent = row["agent_name"]
        if role in {"orchestrator"}:
            role_map[role] = None
            continue
        role_map.setdefault(role, agent)
        role_map.setdefault(agent, agent)

    return {
        "roles": [dict(row) for row in rows],
        "role_map": role_map,
    }


@app.get("/beat/handoffs/stale")
async def beat_stale_handoffs(
    request: Request,
    x_project: str = Header(alias="X-Project", default=""),
    pending_hours: int = Query(default=24, ge=1, le=720),
    claimed_hours: int = Query(default=12, ge=1, le=720),
    limit: int = Query(default=20, ge=1, le=100),
):
    require_admin_access(request)
    project = require_project_scope(x_project)

    async with acquire_scoped(project) as conn:
        rows = await conn.fetch(
            """SELECT
                    id::text,
                    status,
                    priority,
                    from_agent,
                    to_role,
                    to_agent,
                    COALESCE(claimed_by, '') AS claimed_by,
                    LEFT(COALESCE(summary, ''), 180) AS summary,
                    ROUND(EXTRACT(EPOCH FROM (NOW() - created_at)) / 3600.0, 1)::float8
                        AS age_hours,
                    CASE
                        WHEN claimed_at IS NULL THEN NULL
                        ELSE ROUND(EXTRACT(EPOCH FROM (NOW() - claimed_at)) / 3600.0, 1)::float8
                    END AS claimed_age_hours
               FROM handoffs
               WHERE project = $1
                 AND status IN ('pending', 'claimed')
                 AND invalidated_at IS NULL
                 AND (
                    (
                        status = 'pending'
                        AND created_at < NOW() - make_interval(hours => $2::int)
                    )
                    OR
                    (
                        status = 'claimed'
                        AND claimed_at < NOW() - make_interval(hours => $3::int)
                    )
                 )
               ORDER BY
                    CASE priority
                        WHEN 'urgent' THEN 0
                        WHEN 'high' THEN 1
                        WHEN 'medium' THEN 2
                        ELSE 3
                    END,
                    created_at ASC
               LIMIT $4""",
            project,
            pending_hours,
            claimed_hours,
            limit,
        )

    return {"handoffs": [dict(row) for row in rows]}


@app.get("/beat/handoffs/open")
async def beat_open_handoffs(
    request: Request,
    x_project: str = Header(alias="X-Project", default=""),
    recent_days: int = Query(default=7, ge=1, le=60),
    limit: int = Query(default=80, ge=1, le=250),
):
    require_admin_access(request)
    project = require_project_scope(x_project)

    async with acquire_scoped(project) as conn:
        rows = await conn.fetch(
            """SELECT
                    id::text,
                    priority,
                    to_role,
                    LEFT(COALESCE(summary, ''), 220) AS summary,
                    LEFT(COALESCE(context, ''), 220) AS context
               FROM handoffs
               WHERE project = $1
                 AND status IN ('pending', 'claimed')
                 AND invalidated_at IS NULL
                 AND created_at > NOW() - make_interval(days => $2::int)
               ORDER BY created_at DESC
               LIMIT $3""",
            project,
            recent_days,
            limit,
        )

    return {"handoffs": [dict(row) for row in rows]}


@app.get("/beat/handoffs/dispatchable")
async def beat_dispatchable_handoffs(
    request: Request,
    x_project: str = Header(alias="X-Project", default=""),
    roles: str = Query(default=""),
    handoff_id: str = Query(default=""),
    recent_hours: int = Query(default=24, ge=1, le=720),
    limit: int = Query(default=30, ge=1, le=100),
):
    require_admin_access(request)
    project = require_project_scope(x_project)
    role_list = sorted({role.lower().strip() for role in roles.split(",") if role.strip()})
    if not role_list:
        return {"handoffs": []}

    handoff_id = handoff_id.strip()
    async with acquire_scoped(project) as conn:
        rows = await conn.fetch(
            """SELECT
                    id::text,
                    from_agent,
                    to_role,
                    priority,
                    summary,
                    next_steps,
                    context,
                    created_at::text
               FROM handoffs
               WHERE project = $1
                 AND status = 'pending'
                 AND invalidated_at IS NULL
                 AND lower(to_role) = ANY($2::text[])
                 AND created_at > NOW() - make_interval(hours => $3::int)
                 AND ($4 = '' OR id::text LIKE $4 || '%')
               ORDER BY
                    CASE priority
                        WHEN 'urgent' THEN 0
                        WHEN 'high' THEN 1
                        WHEN 'medium' THEN 2
                        ELSE 3
                    END,
                    created_at ASC
               LIMIT $5""",
            project,
            role_list,
            recent_hours,
            handoff_id,
            limit,
        )

    return {"handoffs": [dict(row) for row in rows]}


@app.get("/beat/handoffs/orchestrator")
async def beat_orchestrator_handoffs(
    request: Request,
    x_project: str = Header(alias="X-Project", default=""),
    status: str = Query(default="pending"),
    limit: int = Query(default=20, ge=1, le=100),
):
    require_admin_access(request)
    project = require_project_scope(x_project)
    if status not in {"pending", "claimed"}:
        raise HTTPException(400, "status must be pending or claimed")

    async with acquire_scoped(project) as conn:
        rows = await conn.fetch(
            """SELECT id::text,
                      from_agent,
                      from_role,
                      to_role,
                      priority,
                      summary,
                      files_changed,
                      verification,
                      next_steps,
                      context,
                      status,
                      claimed_by,
                      claimed_at::text,
                      created_at::text
               FROM handoffs
               WHERE project = $1
                 AND status = $2
                 AND invalidated_at IS NULL
                 AND lower(to_role) IN ('orchestrator', 'beat')
               ORDER BY
                    CASE priority
                        WHEN 'urgent' THEN 0
                        WHEN 'high' THEN 1
                        WHEN 'medium' THEN 2
                        ELSE 3
                    END,
                    created_at ASC
               LIMIT $3""",
            project,
            status,
            limit,
        )

    return {"handoffs": [dict(row) for row in rows]}


SHIP_EVENT_PATTERNS = ["%HANDOFF-COMPLETE%", "%shipped%"]

DEPLOY_EVENT_PATTERNS = [
    "%DEPLOYED%",
    "%DEPLOY-TO-DEV%",
    "%DEV-DEPLOY%",
    "%ROLLOUT%",
    "%kubectl apply%",
    "%kubectl delete%",
    "%kubectl patch%",
    "%kubectl rollout%",
    "%kubectl scale%",
    "%kubectl set%",
    "%helm upgrade%",
    "%helm install%",
    "%helm rollback%",
    "%argocd app sync%",
    "%argocd app rollback%",
    "%gcloud run deploy%",
    "%gcloud builds submit%",
    "%deploy-dev.sh%",
    "%deploy-prod.sh%",
    "%deploy-test.sh%",
    "%redeploy-dev.sh%",
    "%redeploy-prod.sh%",
    "%redeploy-test.sh%",
]


@app.get("/beat/ship-events/latest")
async def beat_latest_ship_event(
    request: Request,
    x_project: str = Header(alias="X-Project", default=""),
    project: str | None = Query(default=None),
):
    """Latest ship-like decision timestamp for Beat doc-drift checks.

    Typed replacement for Beat's former ad hoc `/admin/sql/query` call.
    """
    require_admin_access(request)
    scoped_project = require_project_scope(project or x_project)
    async with acquire_scoped(scoped_project) as conn:
        latest = await conn.fetchval(
            """SELECT MAX(created_at)::text
                 FROM decisions
                WHERE project = $1
                  AND summary ILIKE ANY($2::text[])""",
            scoped_project,
            SHIP_EVENT_PATTERNS,
        )
    return {"project": scoped_project, "latest": latest}


@app.get("/beat/deploy-events")
async def beat_deploy_events(
    request: Request,
    x_project: str = Header(alias="X-Project", default=""),
    project: str | None = Query(default=None),
    lookback_hours: int = Query(default=24, ge=1, le=720),
    limit: int = Query(default=200, ge=1, le=500),
):
    """Recent deploy-like decision/team-event rows for Beat preemption checks.

    Typed, project-scoped replacement for Beat's former raw SQL UNION.
    """
    require_admin_access(request)
    scoped_project = require_project_scope(project or x_project)
    async with acquire_scoped(scoped_project) as conn:
        rows = await conn.fetch(
            """SELECT created_at::text AS ts,
                      agent_name,
                      'decision' AS source,
                      summary
                 FROM decisions
                WHERE project = $1
                  AND created_at > NOW() - ($2::int * INTERVAL '1 hour')
                  AND summary ILIKE ANY($3::text[])
               UNION ALL
               SELECT ts::text AS ts,
                      agent_name,
                      'team_event:' || event_type AS source,
                      summary
                 FROM team_events
                WHERE project = $1
                  AND ts > NOW() - ($2::int * INTERVAL '1 hour')
                  AND summary ILIKE ANY($3::text[])
                ORDER BY ts DESC
                LIMIT $4""",
            scoped_project,
            lookback_hours,
            DEPLOY_EVENT_PATTERNS,
            limit,
        )
    return {"project": scoped_project, "rows": [list(row) for row in rows]}


EMBEDDING_BACKFILL_TABLES: dict[str, dict[str, str]] = {
    "decisions": {
        "content_col": "summary",
        "project_filter": "project = $1",
        "order_col": "created_at",
    },
    "lessons": {
        "content_col": "summary",
        "project_filter": "project = $1",
        "order_col": "created_at",
    },
    "knowledge": {
        "content_col": "content",
        "project_filter": "(project = $1 OR project = '_global')",
        "order_col": "created_at",
    },
    "messages": {
        "content_col": "content",
        "project_filter": "project = $1",
        "order_col": "ts",
    },
    "artifacts": {
        "content_sql": "COALESCE(NULLIF(raw_content, ''), NULLIF(caption, ''), source_file)",
        "project_filter": "project = $1",
        "order_col": "updated_at",
    },
    "work_products": {
        "content_sql": (
            "CONCAT_WS(E'\\n', "
            f"'{WORK_PRODUCT_SCHEMA_VERSION}', "
            "title, activity_type, status, summary, behavior_summary, architecture_notes, "
            "array_to_string(files_changed, ', '), "
            "array_to_string(symbols_changed, ', '), "
            "array_to_string(subject_entities, ', '), "
            "array_to_string(artifact_refs, ', '), "
            "tests_run::text, "
            "array_to_string(risks, '; '), "
            "array_to_string(followups, '; '))"
        ),
        "project_filter": "project = $1 AND invalidated_at IS NULL",
        "order_col": "updated_at",
    },
}


def embedding_content_sql(cfg: dict[str, str]) -> str:
    return cfg.get("content_sql") or cfg["content_col"]


def embedding_backfill_tables(table: str) -> list[str]:
    normalized = (table or "all").strip().lower()
    if normalized == "all":
        return list(EMBEDDING_BACKFILL_TABLES)
    if normalized not in EMBEDDING_BACKFILL_TABLES:
        allowed = ", ".join(["all", *EMBEDDING_BACKFILL_TABLES])
        raise HTTPException(400, f"table must be one of: {allowed}")
    return [normalized]


def embedding_error_count_expr() -> str:
    return """CASE
                 WHEN COALESCE(metadata, '{}'::jsonb)->>'embedding_error_count' ~ '^[0-9]+$'
                 THEN (metadata->>'embedding_error_count')::int
                 ELSE 0
              END"""


# COALESCE only rescues a SQL NULL. A JSONB *scalar* still reaches `-` and `||`, which
# raise `cannot delete from scalar` / `invalid concatenation of jsonb objects` and abort
# the whole batch with a 500 — there is no per-row rescue around these UPDATEs, so one
# bad row stops every remaining row. On marlow 33 such rows (all the double-encoded
# string "{}") blocked a 225,108-row backlog indefinitely. Any non-object carries no
# recoverable data, so normalise it and let the backfill heal the row as it passes.
METADATA_AS_OBJECT_SQL = (
    "CASE WHEN jsonb_typeof(metadata) = 'object' THEN metadata ELSE '{}'::jsonb END"
)


EMBEDDING_BACKFILL_SYNC_LIMIT = int(os.environ.get("CORTEX_EMBED_SYNC_LIMIT", "100"))
EMBEDDING_BACKFILL_JOB_STATUSES = {"queued", "running", "completed", "failed"}
GRAPH_BUILD_JOB_STATUSES = {"queued", "running", "completed", "failed"}
_GRAPH_BUILD_TASKS: set[asyncio.Task[None]] = set()


async def ensure_embedding_backfill_jobs_schema() -> None:
    """Create the job ledger on the admin pool, per the rule in ensure_roles_schema.

    This used to run on whatever connection the caller held, which is the scoped
    ``pool_app``/``cortex_app`` connection. That role has USAGE but not CREATE on
    schema public, so every async backfill died with
    ``InsufficientPrivilegeError: permission denied for schema public`` — meaning any
    request over CORTEX_EMBED_SYNC_LIMIT (100) 500'd, on every deployment, while the
    sync path below that limit worked fine and hid it.
    """
    assert pool_admin is not None, "Admin DB pool not initialised"
    async with pool_admin.acquire() as conn:
        await _create_embedding_backfill_jobs_schema(conn)


async def _create_embedding_backfill_jobs_schema(conn: asyncpg.Connection) -> None:
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS embedding_backfill_jobs (
            id UUID PRIMARY KEY,
            project TEXT NOT NULL,
            table_name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued'
                CHECK (status IN ('queued', 'running', 'completed', 'failed')),
            limit_requested INTEGER NOT NULL DEFAULT 100,
            chunk_size INTEGER NOT NULL DEFAULT 100,
            dry_run BOOLEAN NOT NULL DEFAULT FALSE,
            max_errors INTEGER NOT NULL DEFAULT 10,
            error_threshold INTEGER NOT NULL DEFAULT 3,
            provider_configured BOOLEAN NOT NULL DEFAULT FALSE,
            processed INTEGER NOT NULL DEFAULT 0,
            embedded INTEGER NOT NULL DEFAULT 0,
            errors INTEGER NOT NULL DEFAULT 0,
            skipped INTEGER NOT NULL DEFAULT 0,
            stopped TEXT NOT NULL DEFAULT '',
            tables JSONB NOT NULL DEFAULT '{}'::jsonb,
            error TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            started_at TIMESTAMPTZ,
            completed_at TIMESTAMPTZ,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    await conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_embedding_backfill_jobs_project_created
            ON embedding_backfill_jobs (project, created_at DESC)
        """
    )


async def ensure_graph_build_jobs_schema() -> None:
    """Create the graph-build job ledger on the admin pool. See the note above —
    this carried the identical privilege defect, so async graph builds failed the
    same way."""
    assert pool_admin is not None, "Admin DB pool not initialised"
    async with pool_admin.acquire() as conn:
        await _create_graph_build_jobs_schema(conn)


async def _create_graph_build_jobs_schema(conn: asyncpg.Connection) -> None:
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS graph_build_jobs (
            id UUID PRIMARY KEY,
            project TEXT NOT NULL,
            repo TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued'
                CHECK (status IN ('queued', 'running', 'completed', 'failed')),
            full_rebuild BOOLEAN NOT NULL DEFAULT FALSE,
            embed BOOLEAN NOT NULL DEFAULT TRUE,
            request JSONB NOT NULL DEFAULT '{}'::jsonb,
            result JSONB,
            error TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            started_at TIMESTAMPTZ,
            completed_at TIMESTAMPTZ,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    await conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_graph_build_jobs_project_created
            ON graph_build_jobs (project, created_at DESC)
        """
    )


async def recover_interrupted_graph_build_jobs() -> None:
    """Close jobs whose in-process executor disappeared on API restart."""
    assert pool_admin is not None, "Admin DB pool not initialised"
    async with pool_admin.acquire() as conn:
        await _create_graph_build_jobs_schema(conn)
        await conn.execute(
            """
            UPDATE graph_build_jobs
               SET status = 'failed',
                   error = 'graph executor restarted before completion; retry the build',
                   completed_at = NOW(),
                   updated_at = NOW()
             WHERE status IN ('queued', 'running')
            """
        )


def schedule_graph_build_job(
    project: str, job_id: str, body: GraphBuildRequest
) -> asyncio.Task[None]:
    """Retain every in-process job and consume its terminal exception."""
    task = asyncio.create_task(run_graph_build_job(project, job_id, body))
    _GRAPH_BUILD_TASKS.add(task)

    def completed(done: asyncio.Task[None]) -> None:
        _GRAPH_BUILD_TASKS.discard(done)
        with suppress(asyncio.CancelledError, Exception):
            done.exception()

    task.add_done_callback(completed)
    return task


def embedding_backfill_job_to_dict(row: Any) -> dict[str, Any]:
    data = dict(row)
    for key in ("created_at", "started_at", "completed_at", "updated_at"):
        value = data.get(key)
        if isinstance(value, datetime):
            data[key] = value.isoformat()
    data["tables"] = json_object(data.get("tables"))
    return data


def graph_build_job_to_dict(row: Any) -> dict[str, Any]:
    data = dict(row)
    for key in ("created_at", "started_at", "completed_at", "updated_at"):
        value = data.get(key)
        if isinstance(value, datetime):
            data[key] = value.isoformat()
    data["request"] = json_object(data.get("request"))
    data["result"] = json_object(data.get("result"))
    return data


async def create_embedding_backfill_job(project: str, body: EmbeddingBackfillRequest) -> str:
    job_id = str(uuid4())
    limit = max(1, min(int(body.limit or 100), 500))
    chunk_size = max(1, min(int(body.chunk_size or 100), limit))
    max_errors = max(0, int(body.max_errors if body.max_errors is not None else 10))
    error_threshold = max(1, int(body.error_threshold or 3))
    platform_config = await load_cortex_platform_config_cached()
    async with acquire_scoped(project) as conn:
        await ensure_embedding_backfill_jobs_schema()
        await conn.execute(
            """
            INSERT INTO embedding_backfill_jobs (
                id, project, table_name, status, limit_requested, chunk_size,
                dry_run, max_errors, error_threshold, provider_configured
            )
            VALUES ($1::uuid, $2, $3, 'queued', $4, $5, $6, $7, $8, $9)
            """,
            job_id,
            project,
            body.table,
            limit,
            chunk_size,
            bool(body.dry_run),
            max_errors,
            error_threshold,
            await provider_configured(platform_config, "embedding"),
        )
    return job_id


async def create_graph_build_job(project: str, body: GraphBuildRequest) -> str:
    job_id = str(uuid4())
    payload = body.model_dump()
    async with acquire_scoped(project) as conn:
        await ensure_graph_build_jobs_schema()
        await conn.execute(
            """
            INSERT INTO graph_build_jobs (
                id, project, repo, status, full_rebuild, embed, request
            )
            VALUES ($1::uuid, $2, $3, 'queued', $4, $5, $6::jsonb)
            """,
            job_id,
            project,
            body.repo,
            bool(body.full),
            bool(body.embed),
            json.dumps(payload),
        )
    return job_id


async def update_embedding_backfill_job(
    project: str,
    job_id: str,
    *,
    status: str | None = None,
    result: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    if status is not None and status not in EMBEDDING_BACKFILL_JOB_STATUSES:
        raise ValueError(f"invalid embedding job status: {status}")
    payload = result or {}
    async with acquire_scoped(project) as conn:
        await ensure_embedding_backfill_jobs_schema()
        await conn.execute(
            """
            UPDATE embedding_backfill_jobs
               SET status = COALESCE($2, status),
                   processed = COALESCE($3, processed),
                   embedded = COALESCE($4, embedded),
                   errors = COALESCE($5, errors),
                   skipped = COALESCE($6, skipped),
                   stopped = COALESCE($7, stopped),
                   tables = COALESCE($8::jsonb, tables),
                   error = COALESCE($9, error),
                   started_at = CASE
                       WHEN $2 = 'running' THEN COALESCE(started_at, NOW())
                       ELSE started_at
                   END,
                   completed_at = CASE
                       WHEN $2 IN ('completed', 'failed') THEN NOW()
                       ELSE completed_at
                   END,
                   updated_at = NOW()
             WHERE id = $1::uuid
               AND project = $10
            """,
            job_id,
            status,
            payload.get("processed"),
            payload.get("embedded"),
            payload.get("errors"),
            payload.get("skipped"),
            payload.get("stopped"),
            json.dumps(payload.get("tables")) if payload.get("tables") is not None else None,
            error,
            project,
        )


async def update_graph_build_job(
    project: str,
    job_id: str,
    *,
    status: str | None = None,
    result: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    if status is not None and status not in GRAPH_BUILD_JOB_STATUSES:
        raise ValueError(f"invalid graph build job status: {status}")
    async with acquire_scoped(project) as conn:
        await ensure_graph_build_jobs_schema()
        await conn.execute(
            """
            UPDATE graph_build_jobs
               SET status = COALESCE($2, status),
                   result = COALESCE($3::jsonb, result),
                   error = COALESCE($4, error),
                   started_at = CASE
                       WHEN $2 = 'running' THEN COALESCE(started_at, NOW())
                       ELSE started_at
                   END,
                   completed_at = CASE
                       WHEN $2 IN ('completed', 'failed') THEN NOW()
                       ELSE completed_at
                   END,
                   updated_at = NOW()
             WHERE id = $1::uuid
               AND project = $5
            """,
            job_id,
            status,
            json.dumps(result) if result is not None else None,
            error,
            project,
        )


async def persist_graph_build_job(
    project: str,
    job_id: str,
    *,
    status: str | None = None,
    result: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    """Retry transient ledger writes before leaving recovery to startup repair."""
    last_error: Exception | None = None
    for delay in (0.0, 0.1, 0.5):
        if delay:
            await asyncio.sleep(delay)
        try:
            await update_graph_build_job(
                project,
                job_id,
                status=status,
                result=result,
                error=error,
            )
            return
        except Exception as exc:  # noqa: BLE001 - bounded persistence retry
            last_error = exc
    assert last_error is not None
    raise last_error


async def execute_embedding_backfill(
    project: str,
    body: EmbeddingBackfillRequest,
    *,
    job_id: str | None = None,
) -> dict[str, Any]:
    tables = embedding_backfill_tables(body.table)
    limit = max(1, min(int(body.limit or 100), 500))
    max_errors = max(0, int(body.max_errors if body.max_errors is not None else 10))
    error_threshold = max(1, int(body.error_threshold or 3))

    platform_config = await load_cortex_platform_config_cached()
    if not body.dry_run and not await provider_configured(platform_config, "embedding"):
        provider = str(platform_config.get("embedding_provider") or EMBED_PROVIDER)
        raise HTTPException(503, f"{provider} embedding provider key is not configured for cortex-api")

    totals: dict[str, dict[str, int]] = {}
    total_processed = 0
    total_embedded = 0
    total_errors = 0
    total_skipped = 0
    stopped = ""

    for table in tables:
        cfg = EMBEDDING_BACKFILL_TABLES[table]
        content_sql = embedding_content_sql(cfg)
        project_filter = cfg["project_filter"]
        order_col = cfg["order_col"]
        error_count_sql = embedding_error_count_expr()
        async with acquire_scoped(project) as conn:
            if table == "work_products":
                await ensure_work_products_schema(conn)
            rows = await conn.fetch(
                # Select the id in its NATIVE type, not ::text. It is only ever fed
                # back into the two UPDATEs below, and stringifying it there forced
                # `WHERE id::text = $n`, which casts the column and so cannot use the
                # primary key. On marlow that turned each single-row update into a
                # 215,943-row index scan: 112.840ms vs 0.057ms for the index-only
                # plan -- roughly 2000x, paid once per row for the whole backlog.
                # asyncpg round-trips uuid and bigint ids alike, so this stays generic
                # across tables (public.messages.id is bigint; the rest are uuid).
                f"""SELECT id,
                          {content_sql} AS content,
                          {error_count_sql} AS embedding_error_count
                   FROM {table}
                   WHERE {project_filter}
                     AND embedding IS NULL
                     AND {content_sql} IS NOT NULL
                     AND LENGTH({content_sql}) > 10
                     AND COALESCE(metadata->>'embedding_skip', 'false') <> 'true'
                     AND {error_count_sql} < $2
                   ORDER BY {order_col} DESC
                   LIMIT $3""",
                project,
                error_threshold,
                limit,
            )

        table_stats = {"selected": len(rows), "processed": 0, "embedded": 0, "errors": 0, "skipped": 0}
        for row in rows:
            if max_errors and total_errors >= max_errors:
                stopped = f"max_errors {max_errors} reached"
                break

            table_stats["processed"] += 1
            total_processed += 1
            row_id = row["id"]
            text = str(row["content"] or "")
            current_errors = int(row["embedding_error_count"] or 0)

            if body.dry_run:
                continue

            error_message = ""
            try:
                embedding = await embed_text(text)
            except Exception as exc:
                embedding = None
                error_message = f"{type(exc).__name__}: {exc}"

            if embedding:
                vec_str = "[" + ",".join(str(v) for v in embedding) + "]"
                patch = {
                    "embedding_error_count": 0,
                    "embedding_last_success_at": datetime.now(timezone.utc).isoformat(),
                }
                async with acquire_scoped(project) as conn:
                    await conn.execute(
                        f"""UPDATE {table}
                            SET embedding = $1::vector,
                                metadata = (
                                    {METADATA_AS_OBJECT_SQL}
                                    - 'embedding_last_error'
                                    - 'embedding_last_error_at'
                                    - 'embedding_skip'
                                ) || $2::jsonb
                            WHERE id = $3""",
                        vec_str,
                        json.dumps(patch),
                        row_id,
                    )
                table_stats["embedded"] += 1
                total_embedded += 1
            else:
                new_error_count = current_errors + 1
                patch = {
                    "embedding_error_count": new_error_count,
                    "embedding_last_error": error_message or "embed_text returned no vector",
                    "embedding_last_error_at": datetime.now(timezone.utc).isoformat(),
                }
                if new_error_count >= error_threshold:
                    patch["embedding_skip"] = True
                    table_stats["skipped"] += 1
                    total_skipped += 1

                async with acquire_scoped(project) as conn:
                    await conn.execute(
                        f"""UPDATE {table}
                            SET metadata = {METADATA_AS_OBJECT_SQL} || $1::jsonb
                            WHERE id = $2""",
                        json.dumps(patch),
                        row_id,
                    )
                table_stats["errors"] += 1
                total_errors += 1

            if job_id:
                totals[table] = table_stats
                await update_embedding_backfill_job(
                    project,
                    job_id,
                    status="running",
                    result={
                        "processed": total_processed,
                        "embedded": total_embedded,
                        "errors": total_errors,
                        "skipped": total_skipped,
                        "stopped": stopped,
                        "tables": totals,
                    },
                )

        totals[table] = table_stats
        if job_id:
            await update_embedding_backfill_job(
                project,
                job_id,
                status="running",
                result={
                    "processed": total_processed,
                    "embedded": total_embedded,
                    "errors": total_errors,
                    "skipped": total_skipped,
                    "stopped": stopped,
                    "tables": totals,
                },
            )
        if stopped:
            break

    return {
        "project": project,
        "table": body.table,
        "limit": limit,
        "dry_run": body.dry_run,
        "provider_configured": await provider_configured(platform_config, "embedding"),
        "error_threshold": error_threshold,
        "max_errors": max_errors,
        "processed": total_processed,
        "embedded": total_embedded,
        "errors": total_errors,
        "skipped": total_skipped,
        "stopped": stopped,
        "tables": totals,
    }


async def run_embedding_backfill_job(project: str, job_id: str, body: EmbeddingBackfillRequest) -> None:
    await update_embedding_backfill_job(project, job_id, status="running")
    try:
        result = await execute_embedding_backfill(project, body, job_id=job_id)
    except Exception as exc:  # noqa: BLE001 - persist background failure for operator polling
        await update_embedding_backfill_job(
            project,
            job_id,
            status="failed",
            error=compact_text(f"{type(exc).__name__}: {exc}", limit=500),
        )
        return
    await update_embedding_backfill_job(project, job_id, status="completed", result=result)


@app.get("/beat/embeddings/backlog")
async def beat_embedding_backlog(
    request: Request,
    x_project: str = Header(alias="X-Project", default=""),
):
    require_admin_access(request)
    project = require_project_scope(x_project)

    counts: dict[str, int] = {}
    coverage: dict[str, dict[str, int | float]] = {}
    async with acquire_scoped(project) as conn:
        if "work_products" in EMBEDDING_BACKFILL_TABLES:
            await ensure_work_products_schema(conn)
        for table, cfg in EMBEDDING_BACKFILL_TABLES.items():
            content_sql = embedding_content_sql(cfg)
            project_filter = cfg["project_filter"]
            row = await conn.fetchrow(
                f"""SELECT
                        COUNT(*)::int AS total,
                        COUNT(embedding)::int AS embedded,
                        COUNT(*) FILTER (
                            WHERE embedding IS NULL
                              AND COALESCE(metadata->>'embedding_skip', 'false') <> 'true'
                        )::int AS backlog,
                        COUNT(*) FILTER (
                            WHERE embedding IS NULL
                              AND COALESCE(metadata->>'embedding_skip', 'false') = 'true'
                        )::int AS skipped
                    FROM {table}
                    WHERE {project_filter}
                      AND {content_sql} IS NOT NULL
                      AND LENGTH({content_sql}) > 10""",
                project,
            )
            total = int(row["total"] or 0) if row else 0
            embedded = int(row["embedded"] or 0) if row else 0
            backlog = int(row["backlog"] or 0) if row else 0
            skipped = int(row["skipped"] or 0) if row else 0
            counts[table] = backlog
            coverage[table] = {
                "total": total,
                "embedded": embedded,
                "backlog": backlog,
                "skipped": skipped,
                "pct": round((embedded / total) * 100, 1) if total else 100.0,
            }

    counts["total"] = sum(counts.values())
    return {"backlog": counts, "coverage": coverage}


@app.post("/beat/embeddings/backfill")
async def beat_embeddings_backfill(
    body: EmbeddingBackfillRequest,
    request: Request,
    x_project: str = Header(alias="X-Project", default=""),
):
    require_admin_access(request)
    project = require_project_scope(x_project)
    limit = max(1, min(int(body.limit or 100), 500))
    embedding_backfill_tables(body.table)

    platform_config = await load_cortex_platform_config_cached()
    if not body.dry_run and not await provider_configured(platform_config, "embedding"):
        provider = str(platform_config.get("embedding_provider") or EMBED_PROVIDER)
        raise HTTPException(503, f"{provider} embedding provider key is not configured for cortex-api")

    start_async = bool(body.async_job) or (not body.dry_run and limit > EMBEDDING_BACKFILL_SYNC_LIMIT)
    if start_async:
        job_id = await create_embedding_backfill_job(project, body)
        asyncio.create_task(run_embedding_backfill_job(project, job_id, body))
        return JSONResponse(
            status_code=202,
            content={
                "project": project,
                "job_id": job_id,
                "status": "queued",
                "table": body.table,
                "limit": limit,
                "chunk_size": max(1, min(int(body.chunk_size or 100), limit)),
                "provider_configured": await provider_configured(platform_config, "embedding"),
                "status_url": f"/beat/embeddings/jobs/{job_id}",
                "message": "embedding backfill accepted; poll status_url for progress",
            },
        )

    return await execute_embedding_backfill(project, body)


@app.get("/beat/embeddings/jobs/{job_id}")
async def beat_embedding_backfill_job(
    job_id: str,
    request: Request,
    x_project: str = Header(alias="X-Project", default=""),
):
    require_admin_access(request)
    project = require_project_scope(x_project)
    async with acquire_scoped(project) as conn:
        await ensure_embedding_backfill_jobs_schema()
        row = await conn.fetchrow(
            """
            SELECT *
              FROM embedding_backfill_jobs
             WHERE id = $1::uuid
               AND project = $2
            """,
            strip_compound_suffix(job_id),
            project,
        )
    if not row:
        raise HTTPException(404, "embedding backfill job not found")
    return embedding_backfill_job_to_dict(row)


@app.get("/beat/events")
async def beat_events(
    request: Request,
    x_project: str = Header(alias="X-Project", default=""),
    last_id: str = Query(default=""),
    count: int = Query(default=50, ge=1, le=200),
    recent: bool = Query(default=False),
    team_events: bool = Query(default=False),
):
    require_admin_access(request)
    project = require_project_scope(x_project)
    stream = f"{project}:cortex:events"
    last_id = last_id.strip()
    recent = recent if isinstance(recent, bool) else False
    team_events = team_events if isinstance(team_events, bool) else False

    try:
        async with acquire_scoped(project) as conn:
            if recent:
                rows = await fetch_recent_team_events(conn, project, count)
                cursor = str(rows[-1]["id"]) if rows else str(await max_team_event_id(conn, project))
                return {
                    "stream": stream,
                    "last_id": cursor,
                    "events": [team_event_stream_entry(row) for row in rows],
                }

            cursor_value = parse_team_event_cursor(last_id)
            if cursor_value is None:
                cursor = str(await max_team_event_id(conn, project))
                return {"stream": stream, "last_id": cursor, "events": []}

            rows = await fetch_team_events_after(conn, project, cursor_value, count)

        if not rows:
            condition = ensure_event_condition()
            with suppress(asyncio.TimeoutError):
                async with condition:
                    await asyncio.wait_for(condition.wait(), timeout=1.0)
            async with acquire_scoped(project) as conn:
                rows = await fetch_team_events_after(conn, project, cursor_value, count)

        if not rows:
            return {"stream": stream, "last_id": str(cursor_value), "events": []}

        return {
            "stream": stream,
            "last_id": str(rows[-1]["id"]),
            "events": [team_event_stream_entry(row) for row in rows],
        }
    except Exception as exc:
        return {"stream": stream, "last_id": last_id, "events": [], "error": str(exc)}


# ---------------------------------------------------------------------------
# GET /events — additive read-only Server-Sent Events bridge
# ---------------------------------------------------------------------------
# Thin live feed of team_events for one project. ADDITIVE: it reuses the exact
# project-scoping + RLS + NOTIFY-wakeup primitives the GET /beat/events poller
# already uses, just framed as text/event-stream instead of one-shot JSON.
#
# Scoping / no-leak (matches GET /roster + GET /handoffs):
#   - require_project_scope(X-Project) resolves the caller's project (no admin
#     token required, unlike /beat/events — the console app consumes this with
#     just X-Project, same as /roster and /handoffs).
#   - Every read goes through acquire_scoped(project), which SETs cortex.project
#     so PostgreSQL RLS confines rows to that project on the cortex_app pool.
#   - fetch_team_events_after()/max_team_event_id() additionally filter
#     `WHERE project = $1` in SQL (defense in depth). A wakeup caused by ANOTHER
#     project's event therefore returns zero rows here and the cursor does not
#     advance, so project X's stream can only ever emit project X's events.
#
# NOTIFY reuse (no second LISTEN, no busy-poll):
#   - The single dedicated LISTEN connection (listen_for_team_events) and its
#     asyncio.Condition (ensure_event_condition) are shared. Each loop iteration
#     waits on condition.wait() with a bounded timeout; the timeout doubles as
#     the keep-alive tick. A new cortex_events NOTIFY wakes every waiter, this
#     stream re-reads from its cursor, and emits only the genuinely-new rows.


async def team_events_sse_generator(
    request: Request,
    project: str,
    cursor: int,
    count: int,
    ping_seconds: float,
):
    """Yield SSE dicts for new project-scoped team_events, reusing the NOTIFY
    Condition for wakeups and emitting `: ping` keep-alive comments on idle.

    Each yielded mapping is the EventSourceResponse contract
    ({"event", "id", "data"}); the StreamingResponse fallback re-encodes it into
    raw `event:`/`id:`/`data:` lines. A keep-alive is represented as
    {"comment": "ping"}.
    """
    while True:
        # Stop promptly once the client goes away (covers both EventSource
        # disconnect and the StreamingResponse fallback path).
        if await request.is_disconnected():
            break

        rows: list[Any] = []
        try:
            async with acquire_scoped(project) as conn:
                rows = await fetch_team_events_after(conn, project, cursor, count)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Surface transient read errors as an SSE error event rather than
            # tearing the stream down; the client can decide whether to retry.
            yield {"event": "error", "data": json.dumps({"error": str(exc)})}
            with suppress(asyncio.TimeoutError, asyncio.CancelledError):
                await asyncio.sleep(min(ping_seconds, 2.0))
            continue

        if rows:
            for row in rows:
                entry = team_event_stream_entry(row)
                cursor = int(row["id"])
                yield {
                    "event": entry["fields"].get("type") or "event",
                    "id": entry["id"],
                    "data": json.dumps(entry),
                }
            # Drain any further backlog immediately before parking on the
            # Condition again (a single wakeup can cover many new rows).
            continue

        # No new rows: park on the shared NOTIFY Condition. The bounded wait
        # acts as the keep-alive cadence and bounds disconnect-detection latency.
        condition = ensure_event_condition()
        woke = False
        try:
            async with condition:
                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(condition.wait(), timeout=ping_seconds)
                    woke = True
        except asyncio.CancelledError:
            raise
        if not woke:
            # Idle tick — emit a keep-alive comment so proxies/clients see life.
            yield {"comment": "ping"}


@app.get("/events")
async def stream_team_events(
    request: Request,
    x_project: str = Header(alias="X-Project", default=""),
    last_id: str = Query(default=""),
    count: int = Query(default=50, ge=1, le=200),
    ping_seconds: float = Query(default=15.0, ge=1.0, le=60.0),
):
    """Additive read-only SSE feed of this project's team_events.

    Project scoping matches GET /roster and GET /handoffs (X-Project only, via
    acquire_scoped + RLS + WHERE project=$1); event delivery reuses the existing
    cortex_events NOTIFY Condition (no second LISTEN, no busy-poll). Emits one
    `data:` frame per new team_events row plus periodic `: ping` keep-alives, and
    cancels cleanly when the client disconnects.
    """
    project = require_project_scope(x_project)
    await require_registered_project(project)

    # Establish the starting cursor under the same scoped/RLS pool. An explicit
    # numeric last_id resumes after it; anything else (empty or a legacy
    # redis-style stream id) starts from the current project max so the client
    # only receives events newer than connect time.
    cursor_value = parse_team_event_cursor(last_id)
    async with acquire_scoped(project) as conn:
        if cursor_value is None:
            cursor_value = await max_team_event_id(conn, project)

    generator = team_events_sse_generator(
        request, project, int(cursor_value), count, float(ping_seconds)
    )

    if EventSourceResponse is not None:
        return EventSourceResponse(generator, ping=int(ping_seconds))

    # Fallback: hand-roll the SSE wire format over a StreamingResponse.
    async def raw_event_stream():
        try:
            async for item in generator:
                comment = item.get("comment")
                if comment is not None:
                    yield f": {comment}\n\n"
                    continue
                chunk = ""
                event_name = item.get("event")
                if event_name:
                    chunk += f"event: {event_name}\n"
                event_id = item.get("id")
                if event_id is not None:
                    chunk += f"id: {event_id}\n"
                chunk += f"data: {item.get('data', '')}\n\n"
                yield chunk
        finally:
            # Ensure the underlying async generator (and its scoped conn parking)
            # is closed when the response stream ends or the client disconnects.
            await generator.aclose()

    return StreamingResponse(
        raw_event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/beat/handoffs/archive-stale")
async def beat_archive_stale_handoffs(
    body: BeatArchiveStaleRequest,
    request: Request,
    x_project: str = Header(alias="X-Project", default=""),
):
    require_admin_access(request)
    project = require_project_scope(x_project)
    older_than_hours = max(1, min(int(body.older_than_hours), 720))

    async with acquire_scoped(project) as conn:
        rows = await conn.fetch(
            """UPDATE handoffs
               SET status = 'archived'
               WHERE project = $1
                 AND status = 'pending'
                 AND COALESCE(kind, 'task') <> 'completion_handback'
                 AND invalidated_at IS NULL
                 AND created_at < NOW() - make_interval(hours => $2::int)
               RETURNING id::text""",
            project,
            older_than_hours,
        )

    return {"archived": len(rows), "ids": [row["id"] for row in rows]}


# ---------------------------------------------------------------------------
# POST /diary/{agent}
# ---------------------------------------------------------------------------


@app.post("/diary/{agent}")
async def write_diary(
    agent: str,
    body: DiaryWrite,
    x_project: str = Header(alias="X-Project", default=""),
):
    project = require_project_scope(x_project)
    agent = agent.lower().strip()

    async with acquire_scoped(project) as conn:
        row_id = await conn.fetchval(
            """INSERT INTO agent_diaries (project, agent_name, summary, outcome,
                    importance, commits, files_modified, metadata)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb)
               RETURNING id""",
            project,
            agent,
            body.summary,
            body.outcome,
            body.importance,
            body.commits,
            body.files_modified,
            json.dumps({"room": body.room} if body.room else {}),
        )

    return {"id": str(row_id), "outcome": body.outcome}


# ---------------------------------------------------------------------------
# POST /save-chat/{agent}
# Atomic checkpoint write across 5 tables: agents (UPSERT), agent_sessions,
# messages, team_events, knowledge. Replaces cortex-save-chat's prior direct
# SQL path which silently failed on em-dashes / apostrophes / long fields
# This endpoint keeps the write path API-owned instead of direct-SQL.
# ---------------------------------------------------------------------------


@app.post("/save-chat/{agent}")
async def save_chat(
    agent: str,
    body: SaveChatRequest,
    x_project: str = Header(alias="X-Project", default=""),
):
    project = require_project_scope(x_project)
    agent = agent.lower().strip()
    session_id = str(uuid4())
    source_ref = f"cortex-save-chat:{body.topic}"

    async with acquire_scoped(project) as conn:
        async with conn.transaction():
            # 1. UPSERT agent
            await conn.execute(
                """INSERT INTO agents (name, project)
                   VALUES ($1, $2)
                   ON CONFLICT (name, project) DO NOTHING""",
                agent,
                project,
            )

            # 2. agent_sessions row
            notes = {
                "source": "manual-save-chat",
                "topic": body.topic,
                "summary": body.summary,
                "agent": agent,
            }
            await conn.execute(
                """INSERT INTO agent_sessions (id, project, task, started_at, outcome, notes)
                   VALUES ($1::uuid, $2, $3, NOW(), $4, $5::jsonb)
                   ON CONFLICT (id) DO UPDATE SET
                       project = EXCLUDED.project,
                       task = EXCLUDED.task,
                       outcome = EXCLUDED.outcome,
                       notes = EXCLUDED.notes""",
                session_id,
                project,
                body.topic,
                body.summary,
                json.dumps(notes),
            )

            # 3. Link agent_sessions.agent_id to agents.id
            await conn.execute(
                """UPDATE agent_sessions
                   SET agent_id = (SELECT id FROM agents WHERE name = $1 AND project = $2)
                   WHERE id = $3::uuid""",
                agent,
                project,
                session_id,
            )

            # 4. messages row (the chat checkpoint as a system message)
            msg_metadata = {"source": "manual-save-chat", "topic": body.topic}
            msg_content = f"Topic: {body.topic}\n\nSummary: {body.summary}"
            await conn.execute(
                """INSERT INTO messages (session_id, project, agent_name, role, content, metadata, ts)
                   VALUES ($1::uuid, $2, $3, 'system', $4, $5::jsonb, NOW())""",
                session_id,
                project,
                agent,
                msg_content,
                json.dumps(msg_metadata),
            )

            # 5. team_events row
            event_detail = {"topic": body.topic, "session_id": session_id}
            await conn.execute(
                """INSERT INTO team_events (agent_name, event_type, summary, detail, project, ts)
                   VALUES ($1, 'summary', $2, $3::jsonb, $4, NOW())""",
                agent,
                f"Saved chat summary: {body.topic}",
                json.dumps(event_detail),
                project,
            )

            # 6. knowledge row (replaces any prior chat-summary for same
            #    source_ref *within this project*). LCX-UR-001: the DELETE must be
            #    project-scoped or a save in one project removes another project's
            #    matching chat-summary row, and the INSERT must set `project`
            #    explicitly or it falls back to the schema default ('tam') — which
            #    also makes this RLS WITH CHECK fail (500) on the cortex_app path.
            await conn.execute(
                """DELETE FROM knowledge
                   WHERE project = $1
                     AND source_file = $2
                     AND category = 'chat-summary'""",
                project,
                source_ref,
            )
            await conn.execute(
                # project_id is REQUIRED by ck_identity_v2_knowledge_project_id_present.
                # Omitting it made every save-chat 500 — see the two sibling knowledge
                # INSERTs, which have always resolved it from cortex_projects this way.
                """INSERT INTO knowledge
                       (project, project_id, content, source_file, category, section, updated_at)
                   VALUES (
                       $1,
                       (SELECT id FROM cortex_projects WHERE project_key = $1),
                       $2, $3, 'chat-summary', $4, NOW()
                   )""",
                project,
                body.summary,
                source_ref,
                agent,
            )

    return {"session_id": session_id, "ok": True}


# ---------------------------------------------------------------------------
# Project registry
# ---------------------------------------------------------------------------


async def table_has_project_column(conn, table: str, column: str) -> bool:
    return bool(
        await conn.fetchval(
            """SELECT EXISTS (
                   SELECT 1
                     FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name = $1
                      AND column_name = $2
               )""",
            table,
            column,
        )
    )


def quote_ident(identifier: str) -> str:
    if not re.fullmatch(r"[a-z_][a-z0-9_]*", identifier):
        raise HTTPException(500, f"Unsafe SQL identifier: {identifier}")
    return f'"{identifier}"'


def parse_update_count(status: str) -> int:
    try:
        return int(str(status).rsplit(" ", 1)[-1])
    except (TypeError, ValueError):
        return 0


def validate_visible_project_root(path: str, *, field_name: str = "repo_root") -> str:
    """Require registered roots to be absolute and visible to Cortex workers."""
    value = (path or "").strip()
    if not value:
        raise HTTPException(400, f"{field_name} cannot be empty")
    if not os.path.isabs(value):
        raise HTTPException(400, f"{field_name} must be an absolute path")

    # Persist the one canonical filesystem identity used by both ownership queries and
    # advisory locks. Keeping a caller's lexical spelling allowed `/x/parent/../shared`
    # and `/x/shared` (or two symlink aliases) to acquire the same lock but then miss each
    # other in the raw-text owner query and create two authorities for one tree.
    candidate = os.path.realpath(os.path.expanduser(value))
    if not os.path.isabs(candidate):
        raise HTTPException(400, f"{field_name} could not be canonicalized")

    configured_values = [
        value
        for value in (
            os.getenv("HOST_PROJECTS_ROOT", "").strip(),
            os.getenv("REGISTERED_PROJECTS_ROOT", "").strip(),
        )
        if value
    ]
    if not configured_values:
        return candidate
    for value in configured_values:
        if not os.path.isabs(value):
            raise HTTPException(
                500, "registered projects root must be an absolute path"
            )
    configured_roots = {
        os.path.realpath(os.path.expanduser(value)) for value in configured_values
    }
    if len(configured_roots) != 1:
        raise HTTPException(500, "conflicting registered projects root configuration")
    configured_root = configured_roots.pop()
    try:
        visible = os.path.commonpath((configured_root, candidate)) == configured_root
    except ValueError:
        visible = False
    if not visible:
        raise HTTPException(
            400,
            f"{field_name} must be inside the registered projects root ({configured_root}) "
            "so Cortex workers can access it",
        )
    return candidate


async def lock_and_validate_project_root_registry(conn: Any) -> dict[str, dict[str, str]]:
    """Serialize root mutations and canonical-audit legacy registry spellings.

    Older releases persisted caller spellings, so SQL equality alone cannot detect that
    ``/x/parent/../shared`` and ``/x/shared`` are the same authority. Every POST/PATCH
    takes one global transaction lock, canonicalizes the bounded registry in application
    code, and refuses ambiguous legacy ownership instead of choosing a winner.
    """
    await conn.fetchval(
        "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
        "cortex:project-root-registry",
    )
    rows = await conn.fetch(
        """SELECT cp.project_key, COALESCE(cp.status, 'active') AS status,
                  cp.repo_root AS root_path
             FROM cortex_projects cp
            WHERE cp.repo_root IS NOT NULL
            UNION ALL
           SELECT cp.project_key, COALESCE(cp.status, 'active') AS status,
                  cpp.root_path
             FROM cortex_projects cp
             JOIN cortex_project_paths cpp ON cpp.project_key = cp.project_key
            WHERE cpp.root_path IS NOT NULL"""
    )
    owners: dict[str, dict[str, str]] = {}
    for row in rows:
        raw_path = str(row["root_path"] or "").strip()
        if not raw_path or not os.path.isabs(raw_path):
            raise HTTPException(
                409,
                "project root registry contains a non-absolute legacy identity; "
                "repair it through the explicit administrative migration workflow",
            )
        canonical = os.path.realpath(os.path.expanduser(raw_path))
        project = str(row["project_key"])
        prior = owners.get(canonical)
        if prior is not None and prior["project_key"] != project:
            raise HTTPException(
                409,
                "project root registry contains ambiguous legacy aliases owned by "
                f"'{prior['project_key']}' and '{project}'; explicit migration is required",
            )
        owners[canonical] = {
            "project_key": project,
            "status": str(row["status"] or "active"),
        }
    return owners


PROJECT_TRANSFER_EXCLUDED_TABLES = frozenset(
    {
        ("public", "cortex_projects"),
        ("public", "cortex_project_paths"),
        ("cortex", "projects"),
        ("public", "embedding_backfill_jobs"),
        ("public", "graph_build_jobs"),
    }
)


async def project_transfer_catalog(conn: Any) -> dict[tuple[str, str], dict[str, Any]]:
    """Discover project-scoped base tables and their destination columns."""
    rows = await conn.fetch(
        """SELECT c.table_schema,
                  c.table_name,
                  c.column_name,
                  c.ordinal_position,
                  c.is_generated
             FROM information_schema.columns c
             JOIN information_schema.tables t
               ON t.table_schema = c.table_schema
              AND t.table_name = c.table_name
            WHERE c.table_schema IN ('public', 'cortex')
              AND t.table_type = 'BASE TABLE'
              AND EXISTS (
                    SELECT 1
                      FROM information_schema.columns scope
                     WHERE scope.table_schema = c.table_schema
                       AND scope.table_name = c.table_name
                       AND scope.column_name IN ('project', 'project_key', 'project_id')
              )
            ORDER BY c.table_schema, c.table_name, c.ordinal_position"""
    )
    catalog: dict[tuple[str, str], dict[str, Any]] = {}
    for raw in rows:
        row = dict(raw)
        key = (str(row["table_schema"]), str(row["table_name"]))
        if key in PROJECT_TRANSFER_EXCLUDED_TABLES:
            continue
        entry = catalog.setdefault(
            key,
            {"columns": [], "insertable_columns": [], "selectors": set()},
        )
        column = str(row["column_name"])
        entry["columns"].append(column)
        if str(row.get("is_generated") or "NEVER") == "NEVER":
            entry["insertable_columns"].append(column)
        if column in {"project", "project_key", "project_id"}:
            entry["selectors"].add(column)
    return catalog


def project_transfer_where(
    selectors: set[str],
    *,
    project_key: str,
    project_id: str,
) -> tuple[str, list[Any]]:
    conditions: list[str] = []
    values: list[Any] = []
    for column, value, cast in (
        ("project", project_key, "text"),
        ("project_key", project_key, "text"),
        ("project_id", project_id, "uuid"),
    ):
        if column not in selectors:
            continue
        values.append(value)
        conditions.append(f"t.{quote_ident(column)} = ${len(values)}::{cast}")
    if not conditions:
        raise HTTPException(500, "project transfer table has no scope selector")
    return " OR ".join(conditions), values


def project_transfer_json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        with suppress(json.JSONDecodeError):
            value = json.loads(value)
    if not isinstance(value, dict):
        raise HTTPException(500, "project export row did not encode as a JSON object")
    return dict(value)


def remap_project_transfer_row(
    row: dict[str, Any],
    *,
    target_columns: set[str],
    target_project_key: str,
    target_project_id: str,
) -> dict[str, Any]:
    """Force all destination scope columns to the registered target project."""
    mapped = dict(row)
    if "project" in target_columns:
        mapped["project"] = target_project_key
    if "project_key" in target_columns:
        mapped["project_key"] = target_project_key
    if "project_id" in target_columns:
        mapped["project_id"] = target_project_id
    return {key: value for key, value in mapped.items() if key in target_columns}


def project_transfer_insert_sql(
    schema_name: str,
    table_name: str,
    columns: list[str],
) -> str:
    if schema_name not in {"public", "cortex"}:
        raise HTTPException(400, f"Unsupported transfer schema: {schema_name}")
    schema_ident = quote_ident(schema_name)
    table_ident = quote_ident(table_name)
    column_sql = ", ".join(quote_ident(column) for column in columns)
    return (
        f"INSERT INTO {schema_ident}.{table_ident} ({column_sql}) "
        "OVERRIDING SYSTEM VALUE "
        f"SELECT {column_sql} FROM jsonb_populate_record("
        f"NULL::{schema_ident}.{table_ident}, $1::jsonb) "
        "ON CONFLICT DO NOTHING"
    )


def project_transfer_order(
    tables: set[tuple[str, str]],
    dependencies: list[tuple[tuple[str, str], tuple[str, str]]],
) -> list[tuple[str, str]]:
    """Return parent-before-child order; append cyclic tables deterministically."""
    remaining = set(tables)
    ordered: list[tuple[str, str]] = []
    while remaining:
        ready = sorted(
            table
            for table in remaining
            if not any(
                child == table and parent in remaining and parent != child
                for child, parent in dependencies
            )
        )
        if not ready:
            ordered.extend(sorted(remaining))
            break
        ordered.extend(ready)
        remaining.difference_update(ready)
    return ordered


async def project_transfer_dependencies(
    conn: Any,
) -> list[tuple[tuple[str, str], tuple[str, str]]]:
    rows = await conn.fetch(
        """SELECT child_ns.nspname AS child_schema,
                  child.relname AS child_table,
                  parent_ns.nspname AS parent_schema,
                  parent.relname AS parent_table
             FROM pg_constraint fk
             JOIN pg_class child ON child.oid = fk.conrelid
             JOIN pg_namespace child_ns ON child_ns.oid = child.relnamespace
             JOIN pg_class parent ON parent.oid = fk.confrelid
             JOIN pg_namespace parent_ns ON parent_ns.oid = parent.relnamespace
            WHERE fk.contype = 'f'
              AND child_ns.nspname IN ('public', 'cortex')
              AND parent_ns.nspname IN ('public', 'cortex')"""
    )
    return [
        (
            (str(row["child_schema"]), str(row["child_table"])),
            (str(row["parent_schema"]), str(row["parent_table"])),
        )
        for row in rows
    ]


async def reset_project_transfer_sequences(
    conn: Any,
    tables: set[tuple[str, str]],
) -> list[str]:
    reset: list[str] = []
    for schema_name, table_name in sorted(tables):
        rows = await conn.fetch(
            """SELECT column_name
                 FROM information_schema.columns
                WHERE table_schema = $1
                  AND table_name = $2
                  AND column_default LIKE 'nextval(%'""",
            schema_name,
            table_name,
        )
        qualified = f"{quote_ident(schema_name)}.{quote_ident(table_name)}"
        for row in rows:
            column = str(row["column_name"])
            sequence = await conn.fetchval(
                "SELECT pg_get_serial_sequence($1, $2)",
                f"{schema_name}.{table_name}",
                column,
            )
            if not sequence:
                continue
            maximum = await conn.fetchval(
                f"SELECT MAX({quote_ident(column)})::bigint FROM {qualified}"
            )
            if maximum is None:
                continue
            await conn.fetchval("SELECT setval($1::regclass, $2::bigint, true)", sequence, maximum)
            reset.append(f"{schema_name}.{table_name}.{column}")
    return reset


def _project_create_only_response(
    *,
    project_key: str,
    project_id: str,
    default_agent: str | None,
    normalized_roots: list[dict[str, Any]],
    registered_agents: list[dict[str, Any]],
    status: str,
    registration_status: str,
) -> dict[str, Any]:
    return {
        "project_key": project_key,
        "project_id": project_id,
        "default_agent": default_agent,
        "roots": normalized_roots,
        "agents": registered_agents,
        "status": status,
        "registration_status": registration_status,
        "migrated_from_project_key": None,
    }


def exact_project_root_projection(row: Any) -> dict[str, Any]:
    metadata = json_object(row["metadata"])
    root_path = str(row["root_path"])
    path_kind = str(row["path_kind"])
    if ("path" in metadata and str(metadata["path"]) != root_path) \
            or ("kind" in metadata and str(metadata["kind"]) != path_kind):
        raise HTTPException(
            409,
            "project root metadata disagrees with authoritative path columns; "
            "explicit registry repair is required",
        )
    return {**metadata, "path": root_path, "kind": path_kind}


def exact_project_roots(project_row: Any, root_rows: list[Any]) -> list[dict[str, Any]]:
    roots = [exact_project_root_projection(row) for row in root_rows]
    repo_root = str(project_row.get("repo_root") or "")
    if not roots or not repo_root:
        raise HTTPException(
            409,
            "project root registry is incomplete; explicit registry repair is required",
        )
    primary = [root for root in roots if root.get("kind") == "primary"]
    if len(primary) != 1 or str(primary[0].get("path")) != repo_root:
        raise HTTPException(
            409,
            "project row and root registry disagree on the canonical primary; "
            "explicit registry repair is required",
        )
    project_metadata = json_object(project_row.get("metadata"))
    metadata_roots = project_metadata.get("roots")
    if not isinstance(metadata_roots, list) or not metadata_roots \
            or any(not isinstance(root, dict) for root in metadata_roots):
        raise HTTPException(
            409,
            "project metadata roots are missing or malformed; explicit registry repair is required",
        )
    sort_key = lambda root: (
        str(root.get("path")),
        str(root.get("kind")),
        json.dumps(root, sort_keys=True, default=str),
    )
    if sorted(metadata_roots, key=sort_key) != sorted(roots, key=sort_key):
        raise HTTPException(
            409,
            "project metadata roots disagree with authoritative path rows; "
            "explicit registry repair is required",
        )
    return roots


async def _project_create_only_existing(
    conn: Any,
    *,
    project_key: str,
    display_name: str,
    parent_project_key: str | None,
    repo_root: str,
    repo_type: str,
    status: str,
    default_agent: str | None,
    metadata: dict[str, Any],
    normalized_roots: list[dict[str, Any]],
    normalized_agents: list[dict[str, Any]],
) -> str | None:
    """Return an exact existing project id, or None when creation may proceed.

    The caller holds the common project/root advisory locks for every registration mode.
    Exact comparison is intentionally broader than the portable
    root identity: a lost response may be retried, but an operator-customized project,
    root, or roster must never be converged back to bootstrap defaults.
    """
    existing = await conn.fetchrow(
        """SELECT id::text AS project_id, display_name, parent_project_key,
                  repo_root, repo_type, status, default_agent, metadata
             FROM cortex_projects
            WHERE project_key = $1
            FOR UPDATE""",
        project_key,
    )
    if not existing:
        return None

    root_rows = await conn.fetch(
        """SELECT root_path, path_kind, metadata
             FROM cortex_project_paths
            WHERE project_key = $1
            ORDER BY root_path""",
        project_key,
    )
    agent_rows = await conn.fetch(
        """SELECT name, role, model, capabilities
             FROM agents
            WHERE project = $1
            ORDER BY name""",
        project_key,
    )
    actual_project = {
        "display_name": existing["display_name"],
        "parent_project_key": existing["parent_project_key"],
        "repo_root": existing["repo_root"],
        "repo_type": existing["repo_type"],
        "status": existing["status"],
        "default_agent": existing["default_agent"],
        "metadata": json_object(existing["metadata"]),
    }
    expected_project = {
        "display_name": display_name,
        "parent_project_key": parent_project_key,
        "repo_root": repo_root,
        "repo_type": repo_type,
        "status": status,
        "default_agent": default_agent,
        "metadata": metadata,
    }
    actual_roots = sorted(
        (
            exact_project_root_projection(row)
            for row in root_rows
        ),
        key=lambda item: (item["path"], item["kind"]),
    )
    expected_roots = sorted(
        normalized_roots,
        key=lambda item: (str(item["path"]), str(item["kind"])),
    )
    actual_agents = sorted(
        (
            {
                "name": row["name"],
                "role": row["role"],
                "model": row["model"],
                "capabilities": json_object(row["capabilities"]),
            }
            for row in agent_rows
        ),
        key=lambda item: item["name"],
    )
    expected_agents = sorted(normalized_agents, key=lambda item: item["name"])
    if (
        actual_project != expected_project
        or actual_roots != expected_roots
        or actual_agents != expected_agents
    ):
        raise HTTPException(
            409,
            "project already exists with different project, root, or roster state; "
            "create-only registration preserved it unchanged",
        )
    return str(existing["project_id"])


@app.post("/projects")
async def register_project(
    body: ProjectRegister,
    request: Request,
):
    """Register or update a Cortex project and its initial agent roster.

    This is the API boundary used by project onboarding/wizard flows. Project
    keys, roots, default agent, and agent roster are first-class data instead
    of Kaidera-local constants. ``X-Cortex-Project-Mode: create-only`` is the
    appliance-safe boundary: a retry is read-only only when every persisted
    project/root/agent field is exact, and otherwise conflicts without mutation.
    """
    require_admin_access(request)
    registration_mode = (request.headers.get("X-Cortex-Project-Mode") or "upsert").strip().lower()
    if registration_mode not in {"upsert", "create-only"}:
        raise HTTPException(400, "X-Cortex-Project-Mode must be upsert or create-only")

    project_key = validate_project_key(body.project_key)
    parent_project_key = (
        validate_project_key(body.parent_project_key)
        if body.parent_project_key
        else None
    )
    status = (body.status or "active").strip().lower()
    if status not in {"active", "paused", "archived", "deleted"}:
        raise HTTPException(400, "status must be one of: active, paused, archived, deleted")

    repo_type = (body.repo_type or "repo").strip().lower()
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", repo_type):
        raise HTTPException(400, "repo_type must be a lowercase identifier")

    roots = list(body.roots or [])
    repo_root = (body.repo_root or "").strip()
    if not roots and repo_root:
        roots = [ProjectRootRegister(path=repo_root, kind="primary")]
    if roots and not repo_root:
        repo_root = roots[0].path.strip()
    if not repo_root:
        raise HTTPException(400, "repo_root or at least one roots[] entry is required")
    repo_root = validate_visible_project_root(repo_root)

    normalized_roots: list[dict[str, Any]] = []
    seen_roots: set[str] = set()
    for root in roots:
        root_path = validate_visible_project_root(
            root.path,
            field_name="roots[].path",
        )
        root_kind = (root.kind or "primary").strip().lower()
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", root_kind):
            raise HTTPException(400, f"Invalid root kind: {root_kind}")
        if root_path in seen_roots:
            raise HTTPException(400, f"Duplicate project root: {root_path}")
        seen_roots.add(root_path)
        normalized_roots.append(
            {
                **(root.metadata or {}),
                "path": root_path,
                "kind": root_kind,
            }
        )
    primary_roots = [root for root in normalized_roots if root["kind"] == "primary"]
    if len(primary_roots) != 1 or primary_roots[0]["path"] != repo_root:
        raise HTTPException(
            400,
            "roots[] must declare exactly one primary path equal to canonical repo_root",
        )

    agent_specs = list(body.agents or [])
    default_agent = (
        validate_registry_agent_name(body.default_agent, project_key, field_name="default_agent")
        if body.default_agent
        else None
    )
    if not default_agent and agent_specs:
        default_agent = validate_registry_agent_name(agent_specs[0].name, project_key, field_name="agents[0].name")
    if default_agent and not any(
        validate_registry_agent_name(spec.name, project_key, field_name="agents[].name") == default_agent for spec in agent_specs
    ):
        agent_specs.insert(0, ProjectAgentRegister(name=default_agent))

    metadata = dict(body.metadata or {})
    if body.roster_policy is not None:
        metadata["roster_policy"] = dict(body.roster_policy)
    if body.enforce_writer_roster is not None:
        metadata["enforce_writer_roster"] = bool(body.enforce_writer_roster)
    roster_metadata = roster_policy_from_metadata(metadata)
    if "enforce_writer_roster" in metadata:
        roster_metadata.setdefault(
            "enforce_writer_roster",
            bool(metadata.get("enforce_writer_roster")),
        )
    elif "enforce_writer_roster" in roster_metadata:
        metadata["enforce_writer_roster"] = bool(roster_metadata.get("enforce_writer_roster"))
    if roster_metadata:
        metadata["roster_policy"] = roster_metadata
    # These projections are registry-owned. Caller metadata cannot create a second root or
    # default-agent authority that disagrees with the normalized first-class fields.
    metadata["roots"] = normalized_roots
    metadata["default_agent"] = default_agent

    normalized_agents: list[dict[str, Any]] = []
    registered_agents: list[dict[str, Any]] = []
    seen_agents: set[str] = set()
    for spec in agent_specs:
        agent_name = validate_registry_agent_name(spec.name, project_key, field_name="agents[].name")
        if agent_name in seen_agents:
            raise HTTPException(400, f"Duplicate project agent: {agent_name}")
        seen_agents.add(agent_name)
        role = validate_role_slug(spec.role or "generalist")
        capabilities = dict(spec.capabilities or {})
        capabilities.setdefault("writer_scope", roster_metadata.get("default_writer_scope", "work"))
        capabilities["writer_scope"] = validate_writer_scope(capabilities.get("writer_scope"))
        capabilities.setdefault("keep_visible", True)
        capabilities.setdefault("visibility", "active")
        normalized_agents.append(
            {
                "name": agent_name,
                "role": role,
                "model": spec.model,
                "capabilities": capabilities,
            }
        )
        registered_agents.append({"name": agent_name, "role": role, "model": spec.model})

    raw_beat_metadata = metadata.get("beat")
    if raw_beat_metadata is not None:
        if not isinstance(raw_beat_metadata, dict):
            raise HTTPException(400, "metadata.beat must be an object")
        beat_metadata = dict(raw_beat_metadata)
        identity_fields = [
            field
            for field in ("orchestrator_agent", "agent")
            if field in beat_metadata
        ]
        if len(identity_fields) > 1:
            raise HTTPException(
                400,
                "metadata.beat must use exactly one Beat identity field",
            )
        for field in ("orchestrator_agent", "agent"):
            if field not in beat_metadata:
                continue
            raw_target = beat_metadata.get(field)
            if not isinstance(raw_target, str) or not raw_target.strip():
                raise HTTPException(
                    400,
                    f"metadata.beat.{field} must reference a registered agent name",
                )
            target = validate_registry_agent_name(
                raw_target,
                project_key,
                field_name=f"metadata.beat.{field}",
            )
            if target not in seen_agents:
                raise HTTPException(
                    400,
                    f"metadata.beat.{field}={target!r} is absent from agents[]",
                )
            beat_metadata[field] = target
        if identity_fields == ["agent"]:
            beat_metadata["orchestrator_agent"] = beat_metadata.pop("agent")
        metadata["beat"] = beat_metadata

    if bool(metadata.get("allow_project_key_migration", False)):
        raise HTTPException(
            409,
            "project-key transfer through POST /projects is held until the explicit "
            "migration workflow shares the canonical project/root lock contract",
        )

    root_lookup_paths: list[str] = []
    for path_value in [repo_root, *(root["path"] for root in normalized_roots)]:
        if not path_value:
            continue
        root_lookup_paths.append(path_value)
        with suppress(OSError, RuntimeError):
            root_lookup_paths.append(os.path.realpath(path_value))
    root_lookup_paths = list(dict.fromkeys(root_lookup_paths))

    migration_result: dict[str, Any] | None = None
    project_id: str | None = None
    async with pool_admin.acquire() as conn:
        async with conn.transaction():
            registry_owners = await lock_and_validate_project_root_registry(conn)
            await conn.fetchval(
                "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                f"cortex:project-register:{project_key}",
            )
            target_root_lookup_paths = list(root_lookup_paths)
            current_root_rows = await conn.fetch(
                "SELECT root_path FROM cortex_project_paths WHERE project_key = $1",
                project_key,
            )
            for current_root in current_root_rows:
                current_path = str(current_root["root_path"])
                root_lookup_paths.append(current_path)
                with suppress(OSError, RuntimeError):
                    root_lookup_paths.append(os.path.realpath(current_path))
            root_lookup_paths = list(dict.fromkeys(root_lookup_paths))
            for root_lock in sorted(root_lookup_paths):
                await conn.fetchval(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                    f"cortex:project-root-register:{root_lock}",
                )
            if root_lookup_paths:
                existing_root_owner = next(
                    (
                        registry_owners[path]
                        for path in target_root_lookup_paths
                        if path in registry_owners
                        and registry_owners[path]["project_key"] != project_key
                    ),
                    None,
                )
                if existing_root_owner:
                    if str(existing_root_owner.get("status") or "active") == "deleted":
                        raise HTTPException(
                            409,
                            "repo_root remains reserved by a deleted project; implicit "
                            "registration cannot reclaim it",
                        )
                    if registration_mode == "create-only":
                        raise HTTPException(
                            409,
                            "repo_root was registered concurrently; create-only registration "
                            "preserved the existing owner unchanged",
                        )
                    raise HTTPException(
                        409,
                        "repo_root is already registered to project "
                        f"'{existing_root_owner['project_key']}'. Project registration "
                        "must write exactly one validated project; ownership transfer "
                        "requires a separately authorized administrative migration.",
                    )

            if registration_mode == "create-only":
                project_id = await _project_create_only_existing(
                    conn,
                    project_key=project_key,
                    display_name=body.display_name or project_key,
                    parent_project_key=parent_project_key,
                    repo_root=repo_root,
                    repo_type=repo_type,
                    status=status,
                    default_agent=default_agent,
                    metadata=metadata,
                    normalized_roots=normalized_roots,
                    normalized_agents=normalized_agents,
                )
                if project_id is not None:
                    return _project_create_only_response(
                        project_key=project_key,
                        project_id=project_id,
                        default_agent=default_agent,
                        normalized_roots=normalized_roots,
                        registered_agents=registered_agents,
                        status=status,
                        registration_status="unchanged",
                    )

            if registration_mode == "create-only":
                project_id = await conn.fetchval(
                    """INSERT INTO cortex_projects
                           (project_key, display_name, parent_project_key,
                            repo_root, repo_type, status, default_agent, metadata,
                            graph_requires_full_rebuild)
                       VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, TRUE)
                       ON CONFLICT (project_key) DO NOTHING
                       RETURNING id::text""",
                    project_key,
                    body.display_name or project_key,
                    parent_project_key,
                    repo_root,
                    repo_type,
                    status,
                    default_agent,
                    json.dumps(metadata),
                )
                if project_id is None:
                    raise HTTPException(
                        409,
                        "project was registered concurrently; retry create-only registration "
                        "to prove an exact match",
                    )
            else:
                await conn.execute(
                    """INSERT INTO cortex_projects
                       (project_key, display_name, parent_project_key,
                        repo_root, repo_type, status, default_agent, metadata,
                        graph_requires_full_rebuild)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, TRUE)
                   ON CONFLICT (project_key) DO UPDATE SET
                       display_name = EXCLUDED.display_name,
                       parent_project_key = EXCLUDED.parent_project_key,
                       graph_generation = CASE
                           WHEN cortex_projects.repo_root IS DISTINCT FROM EXCLUDED.repo_root
                             OR (
                                 cortex_projects.status = 'deleted'
                                 AND EXCLUDED.status <> 'deleted'
                             )
                           THEN gen_random_uuid()
                           ELSE cortex_projects.graph_generation
                       END,
                       graph_requires_full_rebuild = CASE
                           WHEN cortex_projects.repo_root IS DISTINCT FROM EXCLUDED.repo_root
                             OR (
                                 cortex_projects.status = 'deleted'
                                 AND EXCLUDED.status <> 'deleted'
                             )
                           THEN TRUE
                           ELSE cortex_projects.graph_requires_full_rebuild
                       END,
                       repo_root = EXCLUDED.repo_root,
                       repo_type = EXCLUDED.repo_type,
                       status = EXCLUDED.status,
                       default_agent = EXCLUDED.default_agent,
                       metadata = EXCLUDED.metadata,
                           updated_at = NOW()""",
                    project_key,
                    body.display_name or project_key,
                    parent_project_key,
                    repo_root,
                    repo_type,
                    status,
                    default_agent,
                    json.dumps(metadata),
                )

            if registration_mode == "upsert":
                await conn.execute(
                    """DELETE FROM cortex_project_paths
                        WHERE project_key = $1
                          AND NOT (root_path = ANY($2::text[]))""",
                    project_key,
                    [str(root["path"]) for root in normalized_roots],
                )

            for root in normalized_roots:
                if registration_mode == "create-only":
                    inserted_root = await conn.fetchval(
                        """INSERT INTO cortex_project_paths
                               (project_key, root_path, path_kind, metadata)
                           VALUES ($1, $2, $3, $4::jsonb)
                           ON CONFLICT (root_path) DO NOTHING
                           RETURNING root_path""",
                        project_key,
                        root["path"],
                        root["kind"],
                        json.dumps(root),
                    )
                    if inserted_root is None:
                        raise HTTPException(
                            409,
                            "project root was registered concurrently; create-only registration "
                            "preserved it unchanged",
                        )
                else:
                    stored_root = await conn.fetchval(
                        """INSERT INTO cortex_project_paths
                           (project_key, root_path, path_kind, metadata)
                       VALUES ($1, $2, $3, $4::jsonb)
                       ON CONFLICT (root_path) DO UPDATE SET
                           path_kind = EXCLUDED.path_kind,
                           metadata = EXCLUDED.metadata
                       WHERE cortex_project_paths.project_key = EXCLUDED.project_key
                       RETURNING root_path""",
                        project_key,
                        root["path"],
                        root["kind"],
                        json.dumps(root),
                    )
                    if stored_root is None:
                        raise HTTPException(
                            409,
                            "project root ownership changed concurrently; registration "
                            "preserved the existing owner unchanged",
                        )

            final_root_rows = await conn.fetch(
                """SELECT root_path, path_kind, metadata
                     FROM cortex_project_paths
                    WHERE project_key = $1
                    ORDER BY root_path""",
                project_key,
            )
            final_roots = sorted(
                (
                    exact_project_root_projection(row)
                    for row in final_root_rows
                ),
                key=lambda item: (str(item["path"]), str(item["kind"])),
            )
            expected_final_roots = sorted(
                normalized_roots,
                key=lambda item: (str(item["path"]), str(item["kind"])),
            )
            if final_roots != expected_final_roots:
                raise HTTPException(
                    409,
                    "project root set did not converge exactly; transaction was rolled back",
                )
            final_project_row = await conn.fetchrow(
                "SELECT repo_root, metadata FROM cortex_projects "
                "WHERE project_key=$1 FOR UPDATE",
                project_key,
            )
            if not final_project_row:
                raise HTTPException(409, "project disappeared during root readback")
            projected_roots = sorted(
                exact_project_roots(dict(final_project_row), list(final_root_rows)),
                key=lambda item: (str(item["path"]), str(item["kind"])),
            )
            if projected_roots != expected_final_roots:
                raise HTTPException(
                    409,
                    "project root projections did not converge exactly; transaction was rolled back",
                )

            for agent in normalized_agents:
                agent_name = agent["name"]
                role = agent["role"]
                capabilities = agent["capabilities"]
                await upsert_role_record(conn, project_key, role, capabilities)
                if registration_mode == "create-only":
                    inserted_agent = await conn.fetchval(
                        """INSERT INTO agents (name, project, role, model, capabilities)
                           VALUES ($1, $2, $3, $4, $5::jsonb)
                           ON CONFLICT (name, project) DO NOTHING
                           RETURNING name""",
                        agent_name,
                        project_key,
                        role,
                        agent["model"],
                        json.dumps(capabilities),
                    )
                    if inserted_agent is None:
                        raise HTTPException(
                            409,
                            "project agent was registered concurrently; create-only registration "
                            "preserved it unchanged",
                        )
                else:
                    await conn.execute(
                        """INSERT INTO agents (name, project, role, model, capabilities)
                       VALUES ($1, $2, $3, $4, $5::jsonb)
                       ON CONFLICT (name, project) DO UPDATE SET
                           role = EXCLUDED.role,
                           model = COALESCE(EXCLUDED.model, agents.model),
                               capabilities = COALESCE(agents.capabilities, '{}'::jsonb)
                                              || EXCLUDED.capabilities""",
                        agent_name,
                        project_key,
                        role,
                        agent["model"],
                        json.dumps(capabilities),
                    )

        if migration_result:
            migration_result["appdb"] = await migrate_console_appdb_project_key(
                old_key=migration_result["old_key"],
                new_key=migration_result["new_key"],
            )

        await emit_team_event(
            conn,
            project=project_key,
            agent_name="system",
            event_type="project_registered",
            summary=f"Registered project {project_key}",
            detail={
                "project_key": project_key,
                "default_agent": default_agent,
                "agents": registered_agents,
            },
        )

    _invalidate_roster_policy(project_key)
    if project_id is None:
        async with pool_admin.acquire() as conn:
            project_id = await conn.fetchval(
                "SELECT id::text FROM cortex_projects WHERE project_key = $1",
                project_key,
            )

    return {
        "project_key": project_key,
        "project_id": project_id,
        "default_agent": default_agent,
        "roots": normalized_roots,
        "agents": registered_agents,
        "status": status,
        "registration_status": "created" if registration_mode == "create-only" else "upserted",
        "migrated_from_project_key": (
            migration_result.get("old_key") if migration_result else None
        ),
    }


@app.get("/projects")
async def list_projects():
    async with pool_admin.acquire() as conn:
        rows = await conn.fetch(
            f"""SELECT p.project_key,
                       p.id::text AS project_id,
                       p.display_name,
                       p.default_agent,
                       p.status,
                       p.parent_project_key,
                       p.repo_root,
                       p.metadata,
                       p.created_at,
                       p.updated_at,
                       COALESCE((
                           SELECT COUNT(*)
                             FROM agents a
                            WHERE a.project = p.project_key
                              AND {visible_agent_sql("a")}
                       ), 0) AS agent_count,
                       COALESCE((
                           SELECT COUNT(*)
                             FROM agent_profiles ap
                            WHERE ap.project = p.project_key
                       ), 0) AS profile_count
                  FROM cortex_projects p
                 ORDER BY CASE WHEN p.parent_project_key IS NULL THEN 0 ELSE 1 END,
                          p.created_at NULLS LAST,
                          p.project_key"""
        )
        root_rows = await conn.fetch(
            """SELECT project_key, root_path, path_kind, metadata
                 FROM cortex_project_paths
                ORDER BY project_key,
                         CASE WHEN path_kind = 'primary' THEN 0 ELSE 1 END,
                         root_path"""
        )
    roots_by_project: dict[str, list[Any]] = {}
    for root in root_rows:
        roots_by_project.setdefault(str(root["project_key"]), []).append(root)
    projects: list[dict[str, Any]] = []
    for row in rows:
        project = dict(row)
        project["roots"] = exact_project_roots(
            project,
            roots_by_project.get(str(project["project_key"]), []),
        )
        project.pop("metadata", None)
        projects.append(project)
    return {"projects": projects}


@app.get("/projects/{project_key}")
async def get_project(project_key: str):
    project_key = validate_project_key(project_key)
    async with pool_admin.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT project_key, id::text AS project_id, display_name, default_agent,
                      parent_project_key, repo_root, repo_type, status, metadata,
                      created_at, updated_at
                 FROM cortex_projects
                WHERE project_key = $1
                  AND COALESCE(status, 'active') <> 'deleted'
                LIMIT 1""",
            project_key,
        )
        if not row:
            raise HTTPException(404, f"Project '{project_key}' is not registered in Cortex.")
        roots = await conn.fetch(
            """SELECT root_path, path_kind, metadata
                 FROM cortex_project_paths
                WHERE project_key = $1
                ORDER BY CASE WHEN path_kind = 'primary' THEN 0 ELSE 1 END,
                         root_path""",
            project_key,
        )
    data = dict(row)
    data["metadata"] = json_object(data.get("metadata"))
    data["roots"] = exact_project_roots(data, list(roots))
    data["created_at"] = data["created_at"].isoformat() if data.get("created_at") else None
    data["updated_at"] = data["updated_at"].isoformat() if data.get("updated_at") else None
    return data


@app.get("/admin/projects/{project_key}/export")
async def export_project(project_key: str, request: Request):
    """Export one registered project's Cortex rows using named JSON fields."""
    require_admin_access(request)
    project_key = validate_project_key(project_key)
    async with pool_admin.acquire() as conn:
        project = await conn.fetchrow(
            """SELECT project_key,
                      id::text AS project_id,
                      display_name,
                      default_agent,
                      repo_root,
                      metadata,
                      status
                 FROM cortex_projects
                WHERE project_key = $1
                  AND COALESCE(status, 'active') <> 'deleted'
                LIMIT 1""",
            project_key,
        )
        if not project:
            raise HTTPException(404, f"Project '{project_key}' is not registered in Cortex.")
        project_data = dict(project)
        root_rows = await conn.fetch(
            """SELECT root_path, path_kind, metadata
                 FROM cortex_project_paths
                WHERE project_key = $1
                ORDER BY CASE WHEN path_kind = 'primary' THEN 0 ELSE 1 END,
                         root_path""",
            project_key,
        )
        project_data["metadata"] = json_object(project_data.get("metadata"))
        project_data["roots"] = exact_project_roots(project_data, list(root_rows))
        project_id = str(project_data["project_id"])
        catalog = await project_transfer_catalog(conn)
        tables: list[dict[str, Any]] = []
        total_rows = 0
        for (schema_name, table_name), entry in sorted(catalog.items()):
            where_sql, values = project_transfer_where(
                set(entry["selectors"]),
                project_key=project_key,
                project_id=project_id,
            )
            qualified = f"{quote_ident(schema_name)}.{quote_ident(table_name)}"
            raw_rows = await conn.fetch(
                f"SELECT to_jsonb(t) AS payload FROM {qualified} t WHERE {where_sql}",
                *values,
            )
            rows = [project_transfer_json_object(row["payload"]) for row in raw_rows]
            if not rows:
                continue
            rows.sort(key=lambda value: json.dumps(value, sort_keys=True, default=str))
            tables.append(
                {
                    "schema_name": schema_name,
                    "table_name": table_name,
                    "rows": rows,
                }
            )
            total_rows += len(rows)
    return {
        "format": "kaidera.cortex-project.v1",
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "source_project": project_data,
        "table_count": len(tables),
        "row_count": total_rows,
        "tables": tables,
    }


@app.post("/admin/projects/{target_project_key}/import")
async def import_project(
    target_project_key: str,
    body: ProjectImportRequest,
    request: Request,
):
    """Import a project export into an already-registered destination project."""
    require_admin_access(request)
    target_project_key = validate_project_key(target_project_key)
    source_project_key = validate_project_key(str(body.source_project.get("project_key") or ""))
    try:
        source_project_id = str(UUID(str(body.source_project.get("project_id") or "")))
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, "source_project.project_id must be a UUID") from exc

    requested: dict[tuple[str, str], ProjectTransferTable] = {}
    for table in body.tables:
        validate_table_name(table.table_name)
        key = (table.schema_name, table.table_name)
        if key in requested:
            raise HTTPException(400, f"Duplicate transfer table: {table.schema_name}.{table.table_name}")
        requested[key] = table

    async with pool_admin.acquire() as conn:
        target_project_id = await conn.fetchval(
            """SELECT id::text
                 FROM cortex_projects
                WHERE project_key = $1
                  AND COALESCE(status, 'active') <> 'deleted'
                LIMIT 1""",
            target_project_key,
        )
        if not target_project_id:
            raise HTTPException(
                409,
                f"Register destination project '{target_project_key}' before importing Cortex data.",
            )
        target_project_id = str(target_project_id)
        catalog = await project_transfer_catalog(conn)
        unknown = sorted(set(requested) - set(catalog))
        if unknown:
            names = ", ".join(f"{schema}.{table}" for schema, table in unknown)
            raise HTTPException(400, f"Transfer contains unavailable or excluded tables: {names}")

        if not body.allow_existing:
            populated: list[str] = []
            for key, transfer in sorted(requested.items()):
                if not transfer.rows:
                    continue
                entry = catalog[key]
                where_sql, values = project_transfer_where(
                    set(entry["selectors"]),
                    project_key=target_project_key,
                    project_id=target_project_id,
                )
                schema_name, table_name = key
                qualified = f"{quote_ident(schema_name)}.{quote_ident(table_name)}"
                bootstrap_filter = ""
                if table_name == "team_events" and "event_type" in entry["columns"]:
                    bootstrap_filter = " AND COALESCE(t.event_type, '') <> 'project_registered'"
                count = await conn.fetchval(
                    f"SELECT COUNT(*) FROM {qualified} t WHERE ({where_sql}){bootstrap_filter}",
                    *values,
                )
                if count:
                    populated.append(f"{schema_name}.{table_name}={count}")
            if populated:
                raise HTTPException(
                    409,
                    "Destination already contains project data; retry only after review with "
                    "allow_existing=true. Existing rows: " + ", ".join(populated),
                )

        dependencies = await project_transfer_dependencies(conn)
        order = project_transfer_order(set(requested), dependencies)
        results: dict[str, dict[str, int]] = {}
        imported_tables: set[tuple[str, str]] = set()
        async with conn.transaction():
            for key in order:
                transfer = requested[key]
                if not transfer.rows:
                    continue
                entry = catalog[key]
                target_columns = set(entry["insertable_columns"])
                inserted = 0
                skipped = 0
                for source_row in transfer.rows:
                    mapped = remap_project_transfer_row(
                        source_row,
                        target_columns=target_columns,
                        target_project_key=target_project_key,
                        target_project_id=target_project_id,
                    )
                    columns = [
                        column
                        for column in entry["insertable_columns"]
                        if column in mapped
                    ]
                    if not columns:
                        skipped += 1
                        continue
                    status = await conn.execute(
                        project_transfer_insert_sql(key[0], key[1], columns),
                        json.dumps(mapped, default=str),
                    )
                    if affected_count(status):
                        inserted += 1
                    else:
                        skipped += 1
                results[f"{key[0]}.{key[1]}"] = {
                    "inserted": inserted,
                    "skipped": skipped,
                }
                imported_tables.add(key)
            reset_sequences = await reset_project_transfer_sequences(conn, imported_tables)

    return {
        "ok": True,
        "source_project_key": source_project_key,
        "source_project_id": source_project_id,
        "target_project_key": target_project_key,
        "target_project_id": target_project_id,
        "tables": results,
        "inserted": sum(item["inserted"] for item in results.values()),
        "skipped": sum(item["skipped"] for item in results.values()),
        "sequences_reset": reset_sequences,
    }


@app.get("/identity/audit")
async def identity_audit(
    request: Request,
    project: str | None = Query(default=None),
):
    """Return Identity v2 audit counts.

    This is the typed/operator-safe replacement for ad hoc SQL checks after the
    clean cutover to agent@project identity.
    """
    require_admin_access(request)
    project_filter = validate_project_key(project) if project else None
    try:
        async with pool_admin.acquire() as conn:
            rows = await conn.fetch(
                """SELECT issue, table_name, project, row_count::bigint AS row_count
                     FROM cortex_identity_v2_audit_summary
                    WHERE ($1::text IS NULL OR project = $1)
                    ORDER BY issue, table_name, project""",
                project_filter,
            )
            actor_count = int(
                await conn.fetchval(
                    """SELECT COUNT(*)::bigint
                         FROM cortex_actors a
                         JOIN cortex_projects p ON p.id = a.project_id
                        WHERE ($1::text IS NULL OR p.project_key = $1)""",
                    project_filter,
                )
                or 0
            )
            alias_count = int(
                await conn.fetchval(
                    """SELECT COUNT(*)::bigint
                         FROM cortex_actor_aliases aa
                         JOIN cortex_projects p ON p.id = aa.project_id
                        WHERE ($1::text IS NULL OR p.project_key = $1)""",
                    project_filter,
                )
                or 0
            )
    except asyncpg.UndefinedTableError as exc:
        raise HTTPException(
            503,
            "Identity v2 schema is not installed; apply 2026-06-15-identity-v2-1-foundation.sql",
        ) from exc

    issues = [dict(row) for row in rows]
    return {
        "project": project_filter,
        "actor_count": actor_count,
        "alias_count": alias_count,
        "issue_count": sum(int(row["row_count"] or 0) for row in issues),
        "issues": issues,
    }


@app.patch("/projects/{project_key}/roster-policy")
async def patch_project_roster_policy(
    project_key: str,
    body: ProjectRosterPolicyPatch,
    request: Request,
    replace: bool = Query(False),
):
    """MERGE by default: the supplied keys deep-merge into the stored
    roster_policy (a two-key diagnostic patch can no longer wipe
    approved_agents/role_assignments). ``?replace=true`` restores the old
    wholesale-replace contract for callers that genuinely want it. Holder keys
    are validated fail-loud: misplaced (top-level instead of under ``roles``) or
    off-roster holders are a 422, never a silently-echoed 200."""
    require_admin_access(request)
    project_key = validate_project_key(project_key)
    await require_registered_project(project_key)

    patch = dict(body.roster_policy or {})
    if body.enforce_writer_roster is not None:
        patch["enforce_writer_roster"] = bool(body.enforce_writer_roster)

    async with pool_admin.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT metadata FROM cortex_projects WHERE project_key=$1 "
            "AND COALESCE(status,'active')<>'deleted' LIMIT 1",
            project_key,
        )
        if not row:
            raise HTTPException(404, f"Project '{project_key}' is not registered in Cortex.")
        metadata = json_object(row["metadata"])
        await _validate_roster_policy_patch(conn, project_key, patch)
        existing = json_object(metadata.get("roster_policy"))
        roster_policy = _normalise_roster_roles(
            dict(patch) if replace else _deep_merge_dicts(existing, patch),
            patch,
        )
        if roster_policy:
            metadata["roster_policy"] = roster_policy
        if "enforce_writer_roster" in roster_policy:
            metadata["enforce_writer_roster"] = bool(roster_policy["enforce_writer_roster"])
        await conn.execute(
            "UPDATE cortex_projects SET metadata=$2::jsonb, updated_at=NOW() WHERE project_key=$1",
            project_key,
            json.dumps(metadata),
        )
        await emit_team_event(
            conn,
            project=project_key,
            agent_name="system",
            event_type="project_registered",
            summary=f"Updated roster policy for {project_key}",
            detail={
                "project_key": project_key,
                "roster_policy": metadata.get("roster_policy", {}),
                "enforce_writer_roster": metadata.get("enforce_writer_roster"),
            },
        )

    _invalidate_roster_policy(project_key)
    return {
        "project_key": project_key,
        "enforce_writer_roster": bool(metadata.get("enforce_writer_roster", False)),
        "roster_policy": metadata.get("roster_policy", {}),
    }


@app.patch("/projects/{project_key}")
async def patch_project(
    project_key: str,
    body: ProjectPatch,
    request: Request,
):
    """Update a registered project's canonical working folder (repo_root).

    Admin-gated like POST /projects + PATCH /projects/{key}/roster-policy: this
    is a registry mutation. POST /projects already upserts repo_root, but only as
    part of a full re-registration (which re-specifies the agent roster, status,
    and roster policy); this PATCH is the clean SET path that touches repo_root
    alone for an EXISTING project.

    The path must be absolute but is NOT required to exist on disk — Google-Drive
    / network working folders may be offline-cached locally. To keep the row
    consistent with how POST /projects models roots, the primary entry in
    cortex_project_paths and metadata.roots is moved to the new path too.
    """
    require_admin_access(request)
    project_key = validate_project_key(project_key)
    await require_registered_project(project_key)

    if body.repo_root is None:
        raise HTTPException(400, "repo_root is required")
    repo_root = validate_visible_project_root(body.repo_root)

    async with pool_admin.acquire() as conn:
        async with conn.transaction():
            registry_owners = await lock_and_validate_project_root_registry(conn)
            await conn.fetchval(
                "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                f"cortex:project-register:{project_key}",
            )
            row = await conn.fetchrow(
                "SELECT repo_root, metadata FROM cortex_projects WHERE project_key=$1 "
                "AND COALESCE(status,'active')<>'deleted' FOR UPDATE",
                project_key,
            )
            if not row:
                raise HTTPException(404, f"Project '{project_key}' is not registered in Cortex.")
            previous_repo_root = row["repo_root"]
            metadata = json_object(row["metadata"])

            root_lookup_paths: list[str] = []
            for path_value in (previous_repo_root, repo_root):
                if not path_value:
                    continue
                root_lookup_paths.append(path_value)
                with suppress(OSError, RuntimeError):
                    root_lookup_paths.append(os.path.realpath(path_value))
            root_lookup_paths = list(dict.fromkeys(root_lookup_paths))
            for root_lock in sorted(root_lookup_paths):
                await conn.fetchval(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                    f"cortex:project-root-register:{root_lock}",
                )
            target_paths = [
                path
                for path in root_lookup_paths
                if path == repo_root
                or path == os.path.realpath(repo_root)
            ]
            existing_root_owner = next(
                (
                    registry_owners[path]
                    for path in target_paths
                    if path in registry_owners
                    and registry_owners[path]["project_key"] != project_key
                ),
                None,
            )
            if existing_root_owner:
                if str(existing_root_owner.get("status") or "active") == "deleted":
                    raise HTTPException(
                        409,
                        "repo_root remains reserved by a deleted project; PATCH cannot "
                        "implicitly reclaim it",
                    )
                raise HTTPException(
                    409,
                    "repo_root is already reserved by project "
                    f"'{existing_root_owner['project_key']}'; use the explicit "
                    "administrative migration/reclaim path to transfer ownership",
                )

            # Remove every old/duplicate primary except the target. A legacy target may
            # already be secondary; the ownership-preserving upsert below promotes that
            # one row without leaving two metadata projections for the same path.
            await conn.execute(
                "DELETE FROM cortex_project_paths "
                "WHERE project_key=$1 AND path_kind='primary' AND root_path <> $2",
                project_key,
                repo_root,
            )
            stored_root = await conn.fetchval(
                """INSERT INTO cortex_project_paths
                       (project_key, root_path, path_kind, metadata)
                   VALUES ($1, $2, 'primary', $3::jsonb)
                   ON CONFLICT (root_path) DO UPDATE SET
                       path_kind = EXCLUDED.path_kind,
                       metadata = EXCLUDED.metadata
                   WHERE cortex_project_paths.project_key = EXCLUDED.project_key
                   RETURNING root_path""",
                project_key,
                repo_root,
                json.dumps({"path": repo_root, "kind": "primary"}),
            )
            if stored_root is None:
                raise HTTPException(
                    409,
                    "repo_root ownership changed concurrently; project root was preserved",
                )

            final_root_rows = await conn.fetch(
                """SELECT root_path, path_kind, metadata
                     FROM cortex_project_paths
                    WHERE project_key = $1
                    ORDER BY root_path""",
                project_key,
            )
            final_roots = [exact_project_root_projection(root) for root in final_root_rows]
            primary_roots = [
                root for root in final_roots if root["kind"] == "primary"
            ]
            if len(primary_roots) != 1 or primary_roots[0]["path"] != repo_root:
                raise HTTPException(
                    409,
                    "project root registry did not converge to one canonical primary; "
                    "transaction was rolled back",
                )
            metadata["roots"] = final_roots
            updated = await conn.fetchrow(
                """UPDATE cortex_projects
                      SET graph_generation = CASE
                              WHEN repo_root IS DISTINCT FROM $2 THEN gen_random_uuid()
                              ELSE graph_generation
                          END,
                          graph_requires_full_rebuild = CASE
                              WHEN repo_root IS DISTINCT FROM $2 THEN TRUE
                              ELSE graph_requires_full_rebuild
                          END,
                          repo_root=$2, metadata=$3::jsonb, updated_at=NOW()
                    WHERE project_key=$1
                RETURNING repo_root, metadata,
                          graph_generation::text AS graph_generation,
                          graph_requires_full_rebuild""",
                project_key,
                repo_root,
                json.dumps(metadata),
            )
            if not updated or str(updated["repo_root"]) != repo_root \
                    or json_object(updated["metadata"]).get("roots") != final_roots:
                raise HTTPException(
                    409,
                    "project root projections did not read back exactly; transaction was rolled back",
                )
            if exact_project_roots(dict(updated), list(final_root_rows)) != final_roots:
                raise HTTPException(
                    409,
                    "project root registry readback was inconsistent; transaction was rolled back",
                )

        await emit_team_event(
            conn,
            project=project_key,
            agent_name="system",
            event_type="project_registered",
            summary=f"Updated repo_root for {project_key}",
            detail={
                "project_key": project_key,
                "repo_root": repo_root,
                "previous_repo_root": previous_repo_root,
            },
        )

    return {
        "project_key": project_key,
        "repo_root": repo_root,
        "previous_repo_root": previous_repo_root,
    }


@app.get("/onboard/diagnostics")
async def onboard_diagnostics(
    agent: str = Query(default=""),
    closure: bool = Query(default=False),
    x_project: str = Header(alias="X-Project", default=""),
):
    """Project-scoped onboarding and closure diagnostics for cortex-onboard."""
    project = require_project_scope(x_project)
    target_agent = agent_base_name(validate_agent_name(agent)) if agent else ""

    async with acquire_scoped(project) as conn:
        roster_rows = await conn.fetch(
            f"""SELECT DISTINCT agent_name
                  FROM (
                        SELECT lower(a.name) AS agent_name
                          FROM agents a
                         WHERE a.project = $1
                           AND {visible_agent_sql("a")}
                        UNION
                        SELECT lower(ap.agent_name) AS agent_name
                          FROM agent_profiles ap
                         WHERE ap.project = $1
                       ) roster
                 WHERE agent_name IS NOT NULL
                 ORDER BY agent_name""",
            project,
        )
        roster = [row["agent_name"] for row in roster_rows]
        if target_agent:
            roster = [target_agent]

        onboard_rows = await conn.fetch(
            """SELECT lower(agent_name) AS agent_name,
                      COUNT(*)::int AS count,
                      MAX(created_at) AS latest_at
                 FROM decisions
                WHERE project = $1
                  AND summary ILIKE '%Cortex v2 onboarding complete%'
                  AND created_at > NOW() - INTERVAL '7 days'
                GROUP BY lower(agent_name)""",
            project,
        )
        onboard_by_agent: dict[str, dict[str, Any]] = {}
        for row in onboard_rows:
            base = agent_base_name(row["agent_name"])
            current = onboard_by_agent.get(base)
            latest_at = row["latest_at"]
            if current is None or (
                latest_at is not None
                and (
                    current.get("latest_at") is None
                    or latest_at > current["latest_at"]
                )
            ):
                onboard_by_agent[base] = {
                    "count": int(row["count"] or 0),
                    "latest_at": latest_at,
                }

        result: dict[str, Any] = {
            "project": project,
            "agents": [
                {
                    "agent": roster_agent,
                    "onboarded": bool(onboard_by_agent.get(roster_agent, {}).get("count", 0) > 0),
                    "count": int(onboard_by_agent.get(roster_agent, {}).get("count", 0)),
                    "latest_at": (
                        onboard_by_agent[roster_agent]["latest_at"].isoformat()
                        if onboard_by_agent.get(roster_agent, {}).get("latest_at")
                        else None
                    ),
                }
                for roster_agent in roster
            ],
        }

        if closure:
            stale_rows = await conn.fetch(
                """SELECT SUBSTR(id::text, 1, 8) AS handoff_short_id,
                          claimed_by,
                          priority,
                          (EXTRACT(EPOCH FROM (NOW() - claimed_at))::int / 60)::int AS minutes,
                          LEFT(summary, 60) AS summary
                     FROM handoffs
                    WHERE project = $1
                      AND status = 'claimed'
                      AND claimed_at < NOW() - INTERVAL '30 minutes'
                      AND invalidated_at IS NULL
                    ORDER BY claimed_at ASC""",
                project,
            )
            no_report_rows = await conn.fetch(
                """SELECT SUBSTR(h.id::text, 1, 8) AS handoff_short_id,
                          h.claimed_by,
                          LEFT(h.summary, 50) AS summary
                     FROM handoffs h
                    WHERE h.project = $1
                      AND h.status = 'completed'
                      AND h.completed_at > NOW() - INTERVAL '24 hours'
                      AND h.claimed_by IS NOT NULL
                      AND NOT EXISTS (
                          SELECT 1 FROM team_events e
                           WHERE e.project = h.project
                             AND lower(split_part(e.agent_name, '@', 1)) = lower(split_part(h.claimed_by, '@', 1))
                             AND e.event_type = 'handoff_completed'
                             AND e.ts > h.claimed_at
                             AND e.ts < h.completed_at + INTERVAL '10 minutes'
                             AND (
                                  e.detail->>'handoff_id' = h.id::text
                                  OR e.summary ILIKE '%' || SUBSTR(h.id::text, 1, 8) || '%'
                             )
                      )
                    ORDER BY h.completed_at DESC
                    LIMIT 10""",
                project,
            )
            missing_diary_rows = await conn.fetch(
                f"""WITH roster AS (
                        SELECT DISTINCT agent_name
                          FROM (
                                SELECT lower(a.name) AS agent_name
                                  FROM agents a
                                 WHERE a.project = $1
                                   AND {visible_agent_sql("a")}
                                UNION
                                SELECT lower(ap.agent_name) AS agent_name
                                  FROM agent_profiles ap
                                 WHERE ap.project = $1
                               ) raw_roster
                         WHERE agent_name IS NOT NULL
                    ),
                    work AS (
                        SELECT lower(split_part(claimed_by, '@', 1)) AS agent_name,
                               COUNT(*)::int AS count
                          FROM handoffs
                         WHERE project = $1
                           AND claimed_at::date = CURRENT_DATE
                           AND claimed_by IS NOT NULL
                         GROUP BY lower(split_part(claimed_by, '@', 1))
                    ),
                    diary AS (
                        SELECT lower(split_part(agent_name, '@', 1)) AS agent_name,
                               COUNT(*)::int AS count
                          FROM agent_diaries
                         WHERE project = $1
                           AND created_at::date = CURRENT_DATE
                         GROUP BY lower(split_part(agent_name, '@', 1))
                    )
                    SELECT r.agent_name,
                           COALESCE(w.count, 0)::int AS claimed_today,
                           COALESCE(d.count, 0)::int AS diary_today
                      FROM roster r
                      JOIN work w ON w.agent_name = r.agent_name
                 LEFT JOIN diary d ON d.agent_name = r.agent_name
                     WHERE COALESCE(w.count, 0) > 0
                       AND COALESCE(d.count, 0) = 0
                     ORDER BY r.agent_name""",
                project,
            )

            result["closure"] = {
                "stale_handoffs": [dict(row) for row in stale_rows],
                "completed_without_lifecycle_report": [dict(row) for row in no_report_rows],
                "agents_missing_diary": [dict(row) for row in missing_diary_rows],
            }

    return result


@app.get("/projects/{project_key}/runtime")
async def get_project_runtime(project_key: str):
    """Return the effective runtime profile for launchers, Beat, and setup tools."""

    project_key = validate_project_key(project_key)
    async with pool_admin.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT project_key, id::text AS project_id, display_name, default_agent,
                      parent_project_key, repo_root, repo_type, status, metadata
                 FROM cortex_projects
                WHERE project_key = $1
                  AND COALESCE(status, 'active') <> 'deleted'
                LIMIT 1""",
            project_key,
        )
        if not row:
            raise HTTPException(404, f"Project '{project_key}' is not registered in Cortex.")

        roots = await conn.fetch(
            """SELECT root_path, path_kind, metadata
                 FROM cortex_project_paths
                WHERE project_key = $1
                ORDER BY CASE WHEN path_kind = 'primary' THEN 0 ELSE 1 END,
                         root_path""",
            project_key,
        )
        agents = await conn.fetch(
            f"""SELECT name, role, model, capabilities
                  FROM agents a
                 WHERE project = $1
                   AND {visible_agent_sql("a")}
                 ORDER BY
                   CASE
                     WHEN capabilities->'pane'->>'order' ~ '^[0-9]+$'
                     THEN (capabilities->'pane'->>'order')::int
                     ELSE 999
                   END,
                   name""",
            project_key,
        )
        has_platform_config = await conn.fetchval(
            "SELECT EXISTS("
            "  SELECT 1 FROM information_schema.tables"
            "  WHERE table_name = 'cortex_platform_config'"
            ")"
        )
        platform_row = (
            await conn.fetchrow("SELECT * FROM cortex_platform_config LIMIT 1")
            if has_platform_config
            else None
        )

    platform_config = (
        serialize_cortex_platform_config(platform_row)
        if platform_row
        else dict(CORTEX_PLATFORM_DEFAULTS)
    )
    roster_policy = await load_roster_policy(project_key)
    return build_runtime_profile(
        dict(row),
        [dict(root) for root in roots],
        [dict(agent) for agent in agents],
        platform_config,
        roster_policy,
    )


@app.get("/projects/{project_key}/writers")
async def get_project_writers(project_key: str):
    project_key = validate_project_key(project_key)
    project_metadata = await fetch_project_metadata(project_key)
    if not project_metadata:
        await require_registered_project(project_key)
    policy = await load_roster_policy(project_key)
    roster_metadata = roster_policy_from_metadata(project_metadata)
    roles = json_object(roster_metadata.get("roles")) or dict(policy.roles)

    async with acquire_scoped(project_key) as conn:
        rows = await conn.fetch(
            f"""SELECT a.name, a.role, a.model, a.capabilities
                  FROM agents a
                 WHERE a.project = $1
                   AND {visible_agent_sql("a")}
                 ORDER BY a.name""",
            project_key,
        )

    writers: list[dict[str, Any]] = []
    for row in rows:
        capabilities = json_object(row.get("capabilities"))
        scope = writer_scope_for_agent(row["name"], capabilities, policy, project_metadata)
        base = agent_base_name(row["name"])
        writers.append(
            {
                "name": row["name"],
                "role": row["role"],
                "model": row["model"],
                "writer_scope": scope,
                "is_writer": base in policy.work_writers,
                "is_handoff_target": base in policy.handoff_targets,
            }
        )

    return {
        "project": project_key,
        "enforce": policy.enforce,
        "roster_schema_version": roster_metadata.get("roster_schema_version", CORTEX_ROSTER_SCHEMA_VERSION),
        "default_writer_scope": policy.default_writer_scope,
        "writers": writers,
        "system_event_writers": sorted(policy.system_event_writers),
        "pm_lead": effective_pm_lead(roles),
        "support_agents": json_list(roles.get("support_agents")),
        "approved_agents": json_list(roles.get("approved_agents")) or sorted(policy.work_writers),
    }


# ---------------------------------------------------------------------------
# GET /state
# ---------------------------------------------------------------------------


@app.get("/state")
async def get_state(
    x_project: str = Header(alias="X-Project", default=""),
):
    project = require_project_scope(x_project)

    async with acquire_scoped(project) as conn:
        sprints = await conn.fetch(
            """SELECT COALESCE(sprint_label, sprint_number::text) AS sprint_ref,
                      goal,
                      status
               FROM sprints
               WHERE project = $1 AND status = 'active'
               ORDER BY COALESCE(sprint_number, 2147483647), sprint_label""",
            project,
        )
        summary = await conn.fetchrow(
            """SELECT
                   (SELECT COUNT(*) FROM tasks WHERE project = $1 AND status != 'done') AS active_tasks,
                   (SELECT COUNT(*) FROM handoffs WHERE project = $1 AND status = 'pending' AND invalidated_at IS NULL) AS pending_handoffs,
                   (SELECT COUNT(*) FROM team_events WHERE project = $1 AND ts > NOW() - INTERVAL '24 hours') AS events_24h
            """,
            project,
        )

    return {
        "project": project,
        "sprints": [dict(row) for row in sprints],
        "summary": dict(summary or {}),
    }


# ---------------------------------------------------------------------------
# GET /roster
# ---------------------------------------------------------------------------


@app.get("/roster")
async def get_roster(
    x_project: str = Header(alias="X-Project", default=""),
):
    project = require_project_scope(x_project)

    policy = await load_roster_policy(project)
    project_metadata = await fetch_project_metadata(project)
    async with acquire_scoped(project) as conn:
        rows = await conn.fetch(
            f"""SELECT a.name, a.role, a.model, a.capabilities
                FROM agents a
                WHERE a.project = $1
                  AND {visible_agent_sql("a")}
                ORDER BY a.name""",
            project,
        )

    agents: list[dict[str, Any]] = []
    for row in rows:
        data = dict(row)
        capabilities = json_object(data.pop("capabilities", None))
        data["writer_scope"] = writer_scope_for_agent(
            data["name"],
            capabilities,
            policy,
            project_metadata,
        )
        agents.append(data)

    return {"project": project, "agents": agents}


# ---------------------------------------------------------------------------
# Epic surface (E006: epics as first-class Cortex data)
#
# Backs the console's real /epics surface (replaces the parsed-markdown TODO in
# local-cortex/console/app/main.py:_epic_view). Project-scoping is identical to
# GET /roster and GET /state: require_project_scope(X-Project) + acquire_scoped()
# (sets the cortex.project GUC) + WHERE project = $1, so RLS (epics_project_isolation,
# 2026-06-01-epic-surface.sql) is the final guard and there is no cross-project leak.
# POST /epics is admin-gated (require_admin_access) like POST /projects — it is an
# operator/registry write, not an agent work-write, so it does NOT touch the writer
# guard (require_registered_agent_writer) at all.
# ---------------------------------------------------------------------------


def _epic_row_to_dict(row: Any) -> dict[str, Any]:
    """Shape one `epics` row for the API, parsing the increments JSONB column."""
    data = dict(row)
    increments = data.get("increments")
    if isinstance(increments, str):
        try:
            increments = json.loads(increments)
        except json.JSONDecodeError:
            increments = []
    data["increments"] = increments if isinstance(increments, list) else []
    return data


@app.get("/epics")
async def list_epics(
    x_project: str = Header(alias="X-Project", default=""),
):
    """List the calling project's epics with their increment tables + progress.

    Project-scoped exactly like GET /roster: require_project_scope + acquire_scoped
    + WHERE project = $1. RLS (epics_project_isolation) is the final guard.
    """
    project = require_project_scope(x_project)

    async with acquire_scoped(project) as conn:
        rows = await conn.fetch(
            """SELECT project, epic_id, title, status, overall_pct,
                      increments, updated_at::text
                 FROM epics
                WHERE project = $1
                ORDER BY epic_id""",
            project,
        )

    return {"project": project, "epics": [_epic_row_to_dict(row) for row in rows]}


@app.get("/epics/{epic_id}")
async def get_epic(
    epic_id: str,
    x_project: str = Header(alias="X-Project", default=""),
):
    """Return one epic (with increments/progress) by epic_id within the project scope."""
    project = require_project_scope(x_project)

    async with acquire_scoped(project) as conn:
        row = await conn.fetchrow(
            """SELECT project, epic_id, title, status, overall_pct,
                      increments, updated_at::text
                 FROM epics
                WHERE project = $1 AND epic_id = $2""",
            project,
            epic_id,
        )

    if row is None:
        raise HTTPException(404, f"Epic '{epic_id}' not found in project '{project}'")
    return _epic_row_to_dict(row)


@app.post("/epics")
async def upsert_epic(
    body: EpicUpsert,
    request: Request,
    x_project: str = Header(alias="X-Project", default=""),
):
    """Create or update an epic (upsert on (project, epic_id)).

    Admin-gated (require_admin_access) like POST /projects: this is a registry/
    operator write, not agent work, so the writer guard is intentionally not
    involved. Still project-scoped + RLS-protected via acquire_scoped().
    """
    require_admin_access(request)
    project = require_project_scope(x_project)
    await require_registered_project(project)

    epic_id = (body.epic_id or "").strip()
    if not epic_id:
        raise HTTPException(400, "epic_id is required")

    overall_pct = body.overall_pct
    if not 0 <= overall_pct <= 100:
        raise HTTPException(400, "overall_pct must be between 0 and 100")

    increments_payload = [inc.model_dump() for inc in (body.increments or [])]

    async with acquire_scoped(project) as conn:
        row = await conn.fetchrow(
            """INSERT INTO epics
                   (project, epic_id, title, status, overall_pct, increments, updated_at)
               VALUES ($1, $2, $3, $4, $5, $6::jsonb, NOW())
               ON CONFLICT (project, epic_id) DO UPDATE SET
                   title       = EXCLUDED.title,
                   status      = EXCLUDED.status,
                   overall_pct = EXCLUDED.overall_pct,
                   increments  = EXCLUDED.increments,
                   updated_at  = NOW()
               RETURNING project, epic_id, title, status, overall_pct,
                         increments, updated_at::text""",
            project,
            epic_id,
            body.title,
            body.status,
            overall_pct,
            json.dumps(increments_payload),
        )

    return {"upserted": True, "epic": _epic_row_to_dict(row)}


# ---------------------------------------------------------------------------
# Board CRUD
# ---------------------------------------------------------------------------


@app.get("/board")
async def list_board_tasks(
    x_project: str = Header(alias="X-Project", default=""),
    agent: str = Query(default=""),
    sprint: str = Query(default=""),
    status: str = Query(default=""),
    include_done: bool = Query(default=False),
):
    project = require_project_scope(x_project)

    clauses = ["t.project = $1"]
    params: list[Any] = [project]
    idx = 2

    if not include_done:
        clauses.append("t.status != 'done'")

    if agent:
        clauses.append(
            f"(lower(COALESCE(t.assigned_agent, '')) = lower(${idx}) OR t.assigned_role IN ("
            "SELECT DISTINCT derived.role "
            "FROM ("
            "  SELECT NULLIF(a.role, '') AS role FROM agents a WHERE a.project = $1 AND lower(a.name) = lower($"
            f"{idx}) "
            "  UNION ALL "
            "  SELECT NULLIF(ap.role, '') AS role FROM agent_profiles ap WHERE ap.project = $1 AND lower(ap.agent_name) = lower($"
            f"{idx})"
            ") AS derived WHERE derived.role IS NOT NULL))"
        )
        params.append(agent)
        idx += 1

    if sprint:
        clauses.append(
            f"""t.sprint_id IN (
                    SELECT id FROM sprints
                    WHERE project = $1
                      AND (
                            sprint_label = ${idx}
                            OR sprint_number::text = ${idx}
                          )
                )"""
        )
        params.append(sprint)
        idx += 1

    if status:
        clauses.append(f"t.status = ${idx}")
        params.append(status)
        idx += 1

    async with acquire_scoped(project) as conn:
        rows = await conn.fetch(
            f"""SELECT t.id::text AS id,
                       t.title,
                       COALESCE(t.assigned_agent, '-') AS assigned_agent,
                       t.status,
                       t.priority
                FROM tasks t
                WHERE {' AND '.join(clauses)}
                ORDER BY t.priority DESC, t.created_at""",
            *params,
        )

    return {"project": project, "tasks": [dict(row) for row in rows]}


@app.post("/board")
async def create_board_task(
    body: TaskCreate,
    x_project: str = Header(alias="X-Project", default=""),
):
    project = require_project_scope(x_project)
    async with acquire_scoped(project) as conn:
        row = await conn.fetchrow(
            """INSERT INTO tasks (project, title, description, assigned_role, assigned_agent, priority)
               VALUES ($1, $2, $3, $4, $5, $6)
               RETURNING id::text AS id, status""",
            project,
            body.title,
            body.description,
            body.assigned_role,
            body.assigned_agent,
            body.priority,
        )

    return dict(row)


@app.patch("/board/{task_id}")
async def update_board_task(
    task_id: str,
    body: TaskUpdate,
    x_project: str = Header(alias="X-Project", default=""),
):
    project = require_project_scope(x_project)

    async with acquire_scoped(project) as conn:
        row = await conn.fetchrow(
            """UPDATE tasks
               SET status = $1, updated_at = NOW()
               WHERE project = $2 AND id::text LIKE $3 || '%'
               RETURNING id::text AS id, status""",
            body.status,
            project,
            task_id,
        )

    if not row:
        raise HTTPException(404, f"Task {task_id} not found")

    return dict(row)


# ---------------------------------------------------------------------------
# GET /history
# ---------------------------------------------------------------------------


@app.get("/history")
async def get_history(
    x_project: str = Header(alias="X-Project", default=""),
    agent: str = Query(default=""),
    last: int = Query(default=20, ge=1, le=200),
    since: str = Query(default=""),
):
    project = require_project_scope(x_project)

    clauses = ["project = $1"]
    params: list[Any] = [project]
    idx = 2

    if agent:
        clauses.append(f"lower(agent_name) = lower(${idx})")
        params.append(agent)
        idx += 1

    if since:
        clauses.append(f"ts >= ${idx}")
        params.append(since)
        idx += 1

    async with acquire_scoped(project) as conn:
        rows = await conn.fetch(
            f"""SELECT to_char(ts, 'MM-DD HH24:MI') AS when,
                       agent_name,
                       role,
                       LEFT(content, 120) AS content
                FROM messages
                WHERE {' AND '.join(clauses)}
                ORDER BY ts DESC
                LIMIT {last}""",
            *params,
        )

    return {"project": project, "messages": [dict(row) for row in rows]}


# ---------------------------------------------------------------------------
# GET /diary/{agent}
# ---------------------------------------------------------------------------


@app.get("/diary/{agent}")
async def read_diary(
    agent: str,
    x_project: str = Header(alias="X-Project", default=""),
    limit: int = Query(default=10, ge=1, le=100),
):
    project = require_project_scope(x_project)
    agent_name = agent.lower().strip()

    async with acquire_scoped(project) as conn:
        rows = await conn.fetch(
            """SELECT id::text AS id,
                      summary,
                      outcome,
                      importance,
                      created_at::text AS created_at
               FROM agent_diaries
               WHERE project = $1 AND lower(agent_name) = $2
               ORDER BY created_at DESC
               LIMIT $3""",
            project,
            agent_name,
            limit,
        )

    return {"project": project, "agent": agent_name, "entries": [dict(row) for row in rows]}


@app.get("/diary/{agent}/stats")
async def diary_stats(
    agent: str,
    x_project: str = Header(alias="X-Project", default=""),
):
    project = require_project_scope(x_project)
    agent_name = agent.lower().strip()

    async with acquire_scoped(project) as conn:
        row = await conn.fetchrow(
            """SELECT COUNT(*) AS total,
                      COUNT(*) FILTER (WHERE outcome = 'completed') AS completed,
                      COUNT(*) FILTER (WHERE outcome = 'blocked') AS blocked,
                      COUNT(*) FILTER (WHERE outcome = 'handed-off') AS handed_off,
                      COUNT(*) FILTER (WHERE outcome = 'partial') AS partial,
                      COALESCE(ROUND(AVG(importance), 1), 0) AS avg_importance,
                      to_char(MAX(created_at), 'YYYY-MM-DD HH24:MI') AS last_entry
               FROM agent_diaries
               WHERE project = $1 AND lower(agent_name) = $2""",
            project,
            agent_name,
        )

    return {"project": project, "agent": agent_name, "stats": dict(row or {})}


# ---------------------------------------------------------------------------
# POST /memory
# ---------------------------------------------------------------------------


@app.post("/memory")
async def write_memory(
    body: MemoryWrite,
    x_agent: str = Header(alias="X-Agent-Name"),
    x_project: str = Header(alias="X-Project", default=""),
):
    agent = validate_agent_name(x_agent)
    project = require_project_scope(x_project)
    await require_registered_agent_writer(project, agent, scope="system-event")
    source = body.source or f"manual:{agent}"

    async with acquire_scoped(project) as conn:
        existing = await conn.fetchrow(
            """SELECT id::text AS id
               FROM knowledge
               WHERE project = $1
                 AND source_file = $2
               LIMIT 1""",
            project,
            source,
        )

        if existing:
            row = await conn.fetchrow(
                """UPDATE knowledge
                   SET content = $1,
                       embedding = NULL,
                       category = $2,
                       section = $3,
                       project_id = COALESCE(
                           project_id,
                           (SELECT id FROM cortex_projects WHERE project_key = $5)
                       ),
                       updated_at = NOW()
                   WHERE id = $4::uuid AND project = $5
                   RETURNING id::text AS id, 'updated' AS action""",
                body.content,
                body.category,
                body.section,
                existing["id"],
                project,
            )
        else:
            row = await conn.fetchrow(
                """INSERT INTO knowledge (content, source_file, category, section, project, project_id)
                    VALUES (
                        $1, $2, $3, $4, $5,
                        (SELECT id FROM cortex_projects WHERE project_key = $5)
                    )
                    RETURNING id::text AS id, 'created' AS action""",
                body.content,
                source,
                body.category,
                body.section,
                project,
            )

    action = row["action"]
    return {
        "id": row["id"],
        "action": action,
        "status": action,
        "created": action == "created",
        "updated": action == "updated",
        "embedded": False,
    }


# ---------------------------------------------------------------------------
# POST /invalidate/{id}
# ---------------------------------------------------------------------------


@app.post("/invalidate/{item_id}")
async def invalidate_item(
    item_id: str,
    body: InvalidateRequest,
    x_project: str = Header(alias="X-Project", default=""),
):
    project = require_project_scope(x_project)

    async with acquire_scoped(project) as conn:
        table, resolved_id = await find_invalidation_target(conn, project, item_id)
        if not table or not resolved_id:
            raise HTTPException(404, f"{item_id} not found")

        if body.undo:
            if table in {"decisions", "lessons"}:
                await conn.execute(
                    f"""UPDATE {table}
                        SET invalidated_at = NULL,
                            metadata = COALESCE(metadata, '{{}}'::jsonb) - 'invalidation_reason' - 'superseded_by'
                        WHERE project = $1 AND id = $2::uuid""",
                    project,
                    resolved_id,
                )
            else:
                await conn.execute(
                    f"""UPDATE {table}
                        SET invalidated_at = NULL
                        WHERE project = $1 AND id = $2::uuid""",
                    project,
                    resolved_id,
                )
            return {"id": resolved_id, "table": table, "action": "undo"}

        metadata_patch = {}
        if body.reason:
            metadata_patch["invalidation_reason"] = body.reason
        if body.superseded_by:
            metadata_patch["superseded_by"] = body.superseded_by

        if table in {"decisions", "lessons"}:
            await conn.execute(
                f"""UPDATE {table}
                    SET invalidated_at = NOW(),
                        metadata = COALESCE(metadata, '{{}}'::jsonb) || $3::jsonb
                    WHERE project = $1 AND id = $2::uuid""",
                project,
                resolved_id,
                json.dumps(metadata_patch),
            )
        else:
            await conn.execute(
                f"""UPDATE {table}
                    SET invalidated_at = NOW()
                    WHERE project = $1 AND id = $2::uuid""",
                project,
                resolved_id,
            )
        if body.successor_summary and table == "decisions":
            await conn.execute(
                "UPDATE decisions SET supersession_summary = $1 WHERE id = $2",
                body.successor_summary, resolved_id,
            )

        summary = await conn.fetchval(
            f"SELECT LEFT(summary, 120) FROM {table} WHERE project = $1 AND id = $2::uuid",
            project,
            resolved_id,
        )

    return {
        "id": resolved_id,
        "table": table,
        "action": "invalidated",
        "summary": summary,
    }


# ---------------------------------------------------------------------------
# GET /decisions/{id}/lineage  (v2.3 DAG)
# ---------------------------------------------------------------------------


@app.get("/decisions/{decision_id}/lineage")
async def decision_lineage(
    decision_id: str,
    x_project: str = Header(alias="X-Project", default=""),
):
    project = require_project_scope(x_project)
    async with acquire_scoped(project) as conn:
        # Find the target decision
        target = await conn.fetchrow(
            "SELECT id, parent_decision_id, generation FROM decisions "
            "WHERE id::text LIKE $1 || '%' AND project = $2",
            decision_id, project,
        )
        if not target:
            raise HTTPException(404, "Decision not found")

        # Walk up to root
        root_id = target["id"]
        visited = {str(root_id)}
        while True:
            row = await conn.fetchrow(
                "SELECT parent_decision_id FROM decisions WHERE id = $1",
                root_id,
            )
            if not row or not row["parent_decision_id"]:
                break
            pid = row["parent_decision_id"]
            if str(pid) in visited:
                break
            visited.add(str(pid))
            root_id = pid

        # Walk down from root collecting chain
        chain = []
        current_id = root_id
        while current_id:
            row = await conn.fetchrow(
                "SELECT id::text, generation, summary, supersession_summary, "
                "created_at, invalidated_at FROM decisions WHERE id = $1",
                current_id,
            )
            if not row:
                break
            chain.append({
                "id": row["id"],
                "generation": row["generation"] or 0,
                "summary": row["summary"],
                "supersession_summary": row["supersession_summary"],
                "created_at": row["created_at"].isoformat() if row["created_at"] else None,
                "invalidated_at": row["invalidated_at"].isoformat() if row["invalidated_at"] else None,
            })
            child = await conn.fetchrow(
                "SELECT id FROM decisions WHERE parent_decision_id = $1 AND project = $2 "
                "ORDER BY created_at DESC LIMIT 1",
                current_id, project,
            )
            current_id = child["id"] if child else None

        latest = chain[-1] if chain else None
        return {"chain": chain, "latest": latest}


# ---------------------------------------------------------------------------
# GET /verify/*
# ---------------------------------------------------------------------------


@app.get("/verify/decision")
async def verify_decision(
    q: str = Query(..., min_length=1),
    x_project: str = Header(alias="X-Project", default=""),
):
    project = require_project_scope(x_project)
    async with acquire_scoped(project) as conn:
        count = await conn.fetchval(
            """SELECT COUNT(*)
               FROM decisions
               WHERE project = $1
                 AND invalidated_at IS NULL
                 AND summary ILIKE '%' || $2 || '%'""",
            project,
            q,
        )
        latest = None
        if count:
            latest = await conn.fetchval(
                """SELECT agent_name || ' (' || to_char(created_at, 'YYYY-MM-DD') || '): ' || LEFT(summary, 100)
                   FROM decisions
                   WHERE project = $1
                     AND invalidated_at IS NULL
                     AND summary ILIKE '%' || $2 || '%'
                   ORDER BY created_at DESC
                   LIMIT 1""",
                project,
                q,
            )

    return {"matches": int(count or 0), "latest": latest}


@app.get("/verify/table/{table_name}")
async def verify_table_rows(
    table_name: str,
    x_project: str = Header(alias="X-Project", default=""),
):
    project = require_project_scope(x_project)
    table = validate_table_name(table_name)
    async with acquire_scoped(project) as conn:
        try:
            count = await conn.fetchval(
                f"SELECT COUNT(*) FROM {table} WHERE project = $1",
                project,
            )
        except Exception:
            count = await conn.fetchval(f"SELECT COUNT(*) FROM {table}")

    return {"table": table, "count": int(count or 0)}


@app.get("/verify/write")
async def verify_write(
    kind: str = Query(...),
    write_id: str = Query(..., alias="id"),
    x_project: str = Header(alias="X-Project", default=""),
):
    project = require_project_scope(x_project)
    kind = kind.strip().lower()
    async with acquire_scoped(project) as conn:
        if kind in {"decision", "lesson"}:
            table = "decisions" if kind == "decision" else "lessons"
            row = await conn.fetchrow(
                f"""SELECT id::text, project, agent_name, summary, category, metadata
                      FROM {table}
                     WHERE id::text = $1 AND project = $2""",
                write_id,
                project,
            )
        elif kind == "handoff":
            row = await conn.fetchrow(
                """SELECT id::text, project, from_agent, from_role, to_role, to_agent,
                          priority, summary, branch, files_changed, verification,
                          next_steps, context, status, claimed_by,
                          parent_goal_id::text, acceptance, evidence, retry, escalation
                     FROM handoffs
                    WHERE id::text LIKE $1 || '%' AND project = $2""",
                write_id,
                project,
            )
        elif kind == "team_event":
            row = await conn.fetchrow(
                """SELECT id::text, project, agent_name, event_type, summary, detail, files
                     FROM team_events
                    WHERE id::text = $1 AND project = $2""",
                write_id,
                project,
            )
        # LCX-UR-007: extend round-trip confirmation beyond decision/lesson/handoff/
        # team_event to the remaining durable agent writes (knowledge, artifacts,
        # diary, sessions, messages) so a CLI can verify the write actually landed
        # in this project rather than trusting a 200.
        elif kind == "knowledge":
            row = await conn.fetchrow(
                """SELECT id::text, project, content, source_file, category, section
                     FROM knowledge
                    WHERE id::text = $1 AND project = $2""",
                write_id,
                project,
            )
        elif kind == "artifact":
            row = await conn.fetchrow(
                """SELECT id::text, project, modality, source_type, source_file,
                          content_hash, extraction_method,
                          LENGTH(raw_content) AS raw_content_len
                     FROM artifacts
                    WHERE id::text = $1 AND project = $2""",
                write_id,
                project,
            )
        elif kind == "diary":
            row = await conn.fetchrow(
                """SELECT id::text, project, agent_name, summary, outcome, importance
                     FROM agent_diaries
                    WHERE id::text = $1 AND project = $2""",
                write_id,
                project,
            )
        elif kind == "session":
            row = await conn.fetchrow(
                """SELECT id::text, project, task, outcome
                     FROM agent_sessions
                    WHERE id::text = $1 AND project = $2""",
                write_id,
                project,
            )
        elif kind == "message":
            row = await conn.fetchrow(
                """SELECT id::text, project, session_id::text, agent_name, role, content
                     FROM messages
                    WHERE id::text = $1 AND project = $2""",
                write_id,
                project,
            )
        elif kind in {"work_product", "work-product"}:
            await ensure_work_products_schema(conn)
            row = await conn.fetchrow(
                """SELECT id::text, project, handoff_id::text, agent_name, status,
                          title, summary, files_changed, freshness_status,
                          projection_status
                     FROM work_products
                    WHERE id::text = $1 AND project = $2""",
                write_id,
                project,
            )
        else:
            raise HTTPException(
                400,
                "kind must be one of: decision, lesson, handoff, team_event, "
                "knowledge, artifact, diary, session, message, work_product",
            )

    if row is None:
        raise HTTPException(404, f"{kind} write {write_id} not found in project {project}")
    data = dict(row)
    if "metadata" in data:
        data["metadata"] = normalize_db_json(data["metadata"]) or {}
    if "detail" in data:
        data["detail"] = normalize_db_json(data["detail"]) or {}
    for key in ("files", "files_changed"):
        if key in data:
            data[key] = list(data[key] or [])
    return {"kind": kind, "id": str(data.get("id") or write_id), "row": data, "verified": True}


# ---------------------------------------------------------------------------
# POST /agents
# ---------------------------------------------------------------------------


@app.post("/agents")
async def register_agent(
    body: AgentRegister,
    x_agent: str = Header(alias="X-Agent-Name"),
    x_project: str = Header(alias="X-Project", default=""),
):
    project = require_project_scope(x_project)
    await require_registered_project(project)
    caller = agent_base_for_project(x_agent, project, field_name="X-Agent-Name")
    agent = (
        validate_registry_agent_name(body.name, project, field_name="name")
        if body.name
        else caller
    )
    # E006 Inc04 Step 6: gate the CALLER (X-Agent-Name), not the subject being
    # registered. This lets an existing writer add another worker as DATA while preserving
    # the security property that unknown callers cannot self-register.
    await require_registered_agent_writer(project, caller)
    role = body.role.lower().strip()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", role):
        raise HTTPException(
            400,
            f"Invalid role '{body.role}' — must be a slug matching [a-z0-9-]+",
        )
    capabilities = dict(body.capabilities or {})
    if body.writer_scope is not None:
        capabilities["writer_scope"] = body.writer_scope
    capabilities = {
        **capabilities,
        "writer_scope": validate_writer_scope(capabilities.get("writer_scope"), default="work"),
        "keep_visible": True,
        "visibility": capabilities.get("visibility", "active"),
    }
    runtime_state = {
        "agent": agent,
        "registered_by": caller,
        "role_profile": role,
        "project": project,
        "last_registered_at": datetime.now(timezone.utc).isoformat(),
    }

    async with acquire_scoped(project) as conn:
        await upsert_role_record(
            conn,
            project,
            role,
            capabilities,
            body.role_description,
            body.role_is_builtin,
            body.role_source_file,
        )
        await conn.execute(
            """INSERT INTO agents (name, role, project, capabilities, status, runtime_state)
               VALUES ($1, $2, $3, $4::jsonb, $5, $6::jsonb)
               ON CONFLICT (name, project)
               DO UPDATE SET
                   role = EXCLUDED.role,
                   status = EXCLUDED.status,
                   runtime_state = COALESCE(agents.runtime_state, '{}'::jsonb)
                                   || EXCLUDED.runtime_state,
                   capabilities = COALESCE(agents.capabilities, '{}'::jsonb)
                                  || EXCLUDED.capabilities""",
            agent,
            role,
            project,
            json.dumps(capabilities),
            "available",
            json.dumps(runtime_state),
        )
        await emit_team_event(
            conn,
            project=project,
            agent_name=agent,
            event_type="agent_registered",
            summary=f"Registered {agent} as {role}",
            detail={
                "agent": agent,
                "role": role,
                "project": project,
                "status": "available",
                "writer_scope": capabilities["writer_scope"],
                "registered_by": caller,
            },
        )

    _invalidate_roster_policy(project)

    return {
        "registered": True,
        "agent": agent,
        "role": role,
        "writer_scope": capabilities["writer_scope"],
        "registered_by": caller,
    }


# ---------------------------------------------------------------------------
# POST /admin/agents/remove
# ---------------------------------------------------------------------------
# Roster removal (deactivation), NOT a memory wipe. This is the exact inverse of
# the register_agent capability stamp ({keep_visible:true, visibility:active}):
# it sets {keep_visible:false, visibility:history-only} so visible_agent_sql()
# stops counting the agent toward the project roster, the computed work-writer
# set, handoff targets, and project agent_count. The agents row itself is KEPT,
# and every history surface (decisions, lessons, knowledge, handoffs, messages,
# team_events) keys on agent_name TEXT — none of it is touched, so the agent's
# past work stays fully attributable and searchable.
#
# Admin-gated (require_admin_access) like POST /projects + POST /epics: this is a
# registry/operator mutation, not an agent self-service action. Single-project
# scoped (WHERE name=$1 AND project=$2) — it can never affect another project's
# copy of the same agent name. Idempotent: removing an agent that is absent or
# already deactivated is a clean no-op.
@app.post("/admin/agents/remove")
async def remove_agent(
    body: AgentRemove,
    request: Request,
):
    """Remove (deactivate) an agent from ONE project's roster, preserving history.

    "Remove" here is roster-only: the agents row is retained and marked
    history-only so it disappears from the live roster / writer set while all of
    the agent's decisions, lessons, handoffs, and messages remain intact. Use
    POST /agents (cortex-add-agent) to re-activate.
    """
    require_admin_access(request)

    project = validate_project_key(body.project)
    # Admin cleanup must also handle orphaned roster rows whose project was
    # retired before the row was deactivated. The parameterized, project-scoped
    # update remains bounded to this exact project/name pair.
    agent = normalize_agent_removal_name(body.agent_name)

    async with acquire_scoped(project) as conn:
        row = await conn.fetchrow(
            "SELECT name, role, capabilities, status FROM agents "
            "WHERE name = $1 AND project = $2",
            agent,
            project,
        )
        if row is None:
            # Idempotent no-op: nothing on this project's roster to remove.
            return {
                "removed": False,
                "agent": agent,
                "project": project,
                "already_absent": True,
                "message": (
                    f"Agent '{agent}' is not on the {project} roster; nothing to "
                    "remove."
                ),
            }

        capabilities = json_object(row["capabilities"])
        was_visible = (
            capabilities.get("visibility", "active") != "history-only"
            or str(capabilities.get("keep_visible", "false")).lower() == "true"
        )
        if not was_visible:
            # Already deactivated — idempotent no-op (row + history left intact).
            return {
                "removed": False,
                "agent": agent,
                "project": project,
                "already_inactive": True,
                "message": (
                    f"Agent '{agent}' is already deactivated on the {project} "
                    "roster; history preserved."
                ),
            }

        # Inverse of register_agent's visibility stamp. jsonb `||` merge keeps any
        # other capabilities (writer_scope, model, display_name) so the row can be
        # cleanly re-activated later via cortex-add-agent.
        await conn.execute(
            """UPDATE agents
                  SET capabilities = COALESCE(capabilities, '{}'::jsonb)
                                     || jsonb_build_object(
                                         'visibility', 'history-only',
                                         'keep_visible', 'false'
                                     )
                WHERE name = $1 AND project = $2""",
            agent,
            project,
        )
        await emit_team_event(
            conn,
            project=project,
            agent_name=agent,
            event_type="agent_removed",
            summary=f"Removed {agent} from {project} roster (history preserved)",
            detail={
                "agent": agent,
                "project": project,
                "previous_role": row["role"],
                "removal": "roster-deactivation",
                "history_preserved": True,
            },
        )

    _invalidate_roster_policy(project)

    return {
        "removed": True,
        "agent": agent,
        "project": project,
        "history_preserved": True,
        "message": (
            f"Agent '{agent}' removed from the {project} roster. The agents row "
            "and all decisions/lessons/handoffs/messages are preserved; "
            "re-activate with cortex-add-agent."
        ),
    }


# ---------------------------------------------------------------------------
# POST /analysis/session/{session_id}
# ---------------------------------------------------------------------------


@app.post("/analysis/session/{session_id}")
async def analyse_session(
    session_id: str,
    x_agent: str = Header(alias="X-Agent-Name", default="system"),
    x_project: str = Header(alias="X-Project", default=""),
):
    project = require_project_scope(x_project)
    agent = x_agent.lower().strip()

    async with acquire_scoped(project) as conn:
        # Check if already analysed
        existing = await conn.fetchval(
            "SELECT id FROM execution_analyses WHERE session_id::text = $1 AND project = $2",
            session_id, project,
        )
        if existing:
            return {"id": existing, "status": "already_analysed"}

        # Fetch messages
        rows = await conn.fetch(
            "SELECT role, content FROM messages WHERE session_id::text = $1 "
            "AND project = $2 ORDER BY ts ASC",
            session_id, project,
        )
        if not rows:
            raise HTTPException(404, "No messages found for session")

        messages = [{"role": r["role"], "content": r["content"]} for r in rows]
        transcript = prioritise_messages(messages, budget_chars=12000)

        # Call LLM for analysis — provider-configurable with fallback chain
        analysis_json = None
        llm_text = ""
        prompt_content = ANALYSIS_PROMPT + transcript

        # Build model list: explicit model first, then fallback chain
        if ANALYSIS_MODEL:
            models_to_try = [ANALYSIS_MODEL]
        else:
            models_to_try = list(ANALYSIS_FALLBACK_MODELS)

        anthropic_key = await resolve_provider_key("anthropic")
        openrouter_key = await resolve_provider_key("openrouter")
        if not anthropic_key and not openrouter_key:
            raise HTTPException(502, "No analysis LLM key is configured in OpenKai")

        try:
            async with httpx.AsyncClient(timeout=90) as client:
                for model_id in models_to_try:
                    llm_text = ""

                    if ANALYSIS_PROVIDER == "anthropic" and anthropic_key:
                        resp = await client.post(
                            "https://api.anthropic.com/v1/messages",
                            headers={
                                "x-api-key": anthropic_key,
                                "anthropic-version": "2023-06-01",
                                "content-type": "application/json",
                            },
                            json={
                                "model": model_id,
                                "max_tokens": 1000,
                                "messages": [{"role": "user", "content": prompt_content}],
                            },
                        )
                        if resp.status_code == 200:
                            data = resp.json()
                            blocks = data.get("content", [])
                            llm_text = "".join(
                                b.get("text", "") for b in blocks if b.get("type") == "text"
                            )
                        elif resp.status_code == 429 and len(models_to_try) > 1:
                            continue
                    elif openrouter_key:
                        resp = await client.post(
                            "https://openrouter.ai/api/v1/chat/completions",
                            headers={"Authorization": f"Bearer {openrouter_key}"},
                            json={
                                "model": model_id,
                                "messages": [{"role": "user", "content": prompt_content}],
                                "max_tokens": 1000,
                            },
                        )
                        if resp.status_code == 200:
                            data = resp.json()
                            if data.get("error"):
                                # OpenRouter wraps some errors in 200 responses
                                if data["error"].get("code") == 429 and len(models_to_try) > 1:
                                    continue
                            llm_text = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                        elif resp.status_code == 429 and len(models_to_try) > 1:
                            continue

                    if llm_text:
                        ANALYSIS_CALLS.labels(model=model_id, status="success").inc()
                        break  # Got a response, stop trying
                    else:
                        ANALYSIS_CALLS.labels(model=model_id, status="rate_limited").inc()

                if llm_text:
                    # Strip markdown fences if present
                    llm_text = llm_text.strip()
                    if llm_text.startswith("```"):
                        llm_text = llm_text.split("\n", 1)[-1]
                    if llm_text.endswith("```"):
                        llm_text = llm_text.rsplit("```", 1)[0]
                    llm_text = llm_text.strip()
                    analysis_json = json.loads(llm_text)
        except json.JSONDecodeError as exc:
            ANALYSIS_CALLS.labels(model=models_to_try[0] if models_to_try else "unknown", status="parse_error").inc()
            raise HTTPException(502, f"Analysis LLM returned invalid JSON: {exc}")
        except HTTPException:
            raise
        except Exception as exc:
            ANALYSIS_CALLS.labels(model=models_to_try[0] if models_to_try else "unknown", status="error").inc()
            raise HTTPException(502, f"Analysis LLM error: {exc}")

        if not analysis_json:
            raise HTTPException(502, "Analysis LLM call failed — check the OpenKai provider configuration")

        # Store the analysis
        embedding = await embed_text(analysis_json.get("summary", ""))
        vec_sql = "[" + ",".join(str(v) for v in embedding) + "]" if embedding else None

        insert_sql = (
            "INSERT INTO execution_analyses "
            "(project, session_id, agent_name, task_completed, quality_score, "
            "patterns_used, patterns_failed, novel_patterns, tools_used, "
            "summary, raw_analysis" + (", embedding" if vec_sql else "") + ") "
            "VALUES ($1, $2::uuid, $3, $4, $5, $6, $7, $8, $9::jsonb, $10, $11::jsonb"
            + (", $12::vector" if vec_sql else "") + ") RETURNING id"
        )
        params = [
            project, session_id, agent,
            analysis_json.get("task_completed"),
            analysis_json.get("quality_score"),
            analysis_json.get("patterns_used", []),
            analysis_json.get("patterns_failed", []),
            analysis_json.get("novel_patterns", []),
            json.dumps(analysis_json.get("tools_used", [])),
            analysis_json.get("summary"),
            json.dumps(analysis_json),
        ]
        if vec_sql:
            params.append(vec_sql)

        row_id = await conn.fetchval(insert_sql, *params)

        # ── Feature 3: Extract captured patterns from novel_patterns ──
        if analysis_json.get("task_completed") and analysis_json.get("novel_patterns"):
            for pattern_text in analysis_json["novel_patterns"]:
                parts = pattern_text.split(":", 1)
                title = parts[0].strip()
                desc = parts[1].strip() if len(parts) > 1 else ""
                if not title:
                    continue
                p_embed = await embed_text(title + " " + desc)
                p_vec = "[" + ",".join(str(v) for v in p_embed) + "]" if p_embed else None
                p_sql = (
                    "INSERT INTO captured_patterns "
                    "(project, session_id, agent_name, pattern_type, title, description"
                    + (", embedding" if p_vec else "") + ") "
                    "VALUES ($1, $2::uuid, $3, 'other', $4, $5"
                    + (", $6::vector" if p_vec else "") + ")"
                )
                p_params = [project, session_id, agent, title, desc]
                if p_vec:
                    p_params.append(p_vec)
                try:
                    await conn.execute(p_sql, *p_params)
                except Exception:
                    pass  # Don't fail analysis over pattern insert

        # ── Feature 7: Update pattern_metrics from tools_used ──
        for tool_entry in analysis_json.get("tools_used", []):
            tool_name = tool_entry.get("tool", "").lower().strip()
            if not tool_name:
                continue
            successes = tool_entry.get("successes", 0) or 0
            failures = tool_entry.get("failures", 0) or 0
            uses = tool_entry.get("uses", successes + failures) or (successes + failures)

            try:
                await conn.execute(
                    "INSERT INTO pattern_metrics "
                    "(project, pattern_key, pattern_type, total_uses, successes, "
                    "failures, consecutive_failures, "
                    "last_success_at, last_failure_at, degraded, updated_at) "
                    "VALUES ($1, $2, 'command', $3, $4, $5, "
                    "CASE WHEN $5 > 0 THEN $5 ELSE 0 END, "
                    "CASE WHEN $4 > 0 THEN NOW() ELSE NULL END, "
                    "CASE WHEN $5 > 0 THEN NOW() ELSE NULL END, "
                    "FALSE, NOW()) "
                    "ON CONFLICT (project, pattern_key) DO UPDATE SET "
                    "total_uses = pattern_metrics.total_uses + EXCLUDED.total_uses, "
                    "successes = pattern_metrics.successes + EXCLUDED.successes, "
                    "failures = pattern_metrics.failures + EXCLUDED.failures, "
                    "consecutive_failures = CASE "
                    "  WHEN EXCLUDED.failures > 0 "
                    "  THEN pattern_metrics.consecutive_failures + EXCLUDED.failures "
                    "  ELSE 0 END, "
                    "last_success_at = CASE "
                    "  WHEN EXCLUDED.successes > 0 THEN NOW() "
                    "  ELSE pattern_metrics.last_success_at END, "
                    "last_failure_at = CASE "
                    "  WHEN EXCLUDED.failures > 0 THEN NOW() "
                    "  ELSE pattern_metrics.last_failure_at END, "
                    "degraded = CASE "
                    "  WHEN EXCLUDED.failures > 0 "
                    "  AND pattern_metrics.consecutive_failures + EXCLUDED.failures >= 3 "
                    "  THEN TRUE "
                    "  WHEN EXCLUDED.successes > 0 THEN FALSE "
                    "  ELSE pattern_metrics.degraded END, "
                    "updated_at = NOW()",
                    project, tool_name, uses, successes, failures,
                )
            except Exception:
                pass  # Don't fail analysis over metrics insert

        return {
            "id": row_id,
            "session_id": session_id,
            "task_completed": analysis_json.get("task_completed"),
            "quality_score": analysis_json.get("quality_score"),
            "novel_patterns": analysis_json.get("novel_patterns", []),
            "tools_used": analysis_json.get("tools_used", []),
            "summary": analysis_json.get("summary"),
        }


# ---------------------------------------------------------------------------
# Admin compatibility endpoints
# ---------------------------------------------------------------------------


CORTEX_DOCTOR_VECTOR_TABLES = (
    "messages",
    "decisions",
    "lessons",
    "knowledge",
    "work_products",
)
CORTEX_DOCTOR_L4_TABLES = ("cortex_entities", "cortex_relationships")
CORTEX_LOG_EVENT_TYPES = frozenset(
    {"commit", "decision", "lesson", "started", "stopped", "blocked", "unblocked", "bug", "handoff", "question"}
)
CORTEX_DOCTOR_STATUS_RANK = {"ok": 0, "unknown": 1, "warn": 2, "critical": 3}
CORTEX_TRANSCRIPT_5M_WARN = 2000
CORTEX_TRANSCRIPT_5M_CRITICAL = 5000
CORTEX_TRANSCRIPT_1H_WARN = 10000
CORTEX_TRANSCRIPT_1H_CRITICAL = 25000
CORTEX_TRANSCRIPT_SESSION_WARN = 5000
CORTEX_TRANSCRIPT_SESSION_CRITICAL = 20000


def cortex_doctor_check(
    check_id: str,
    title: str,
    status: str,
    summary: str,
    *,
    evidence: dict[str, Any] | None = None,
    recommendation: str | None = None,
) -> dict[str, Any]:
    return {
        "id": check_id,
        "title": title,
        "status": status,
        "summary": summary,
        "evidence": evidence or {},
        "recommendation": recommendation,
    }


def cortex_doctor_overall_status(checks: list[dict[str, Any]]) -> str:
    if not checks:
        return "unknown"
    return max(checks, key=lambda item: CORTEX_DOCTOR_STATUS_RANK.get(item["status"], 1))["status"]


def parse_ivfflat_lists(indexdef: str) -> int | None:
    match = re.search(r"lists\s*=\s*'?(\d+)'?", indexdef or "", flags=re.IGNORECASE)
    return int(match.group(1)) if match else None


async def cortex_doctor_table_stats(conn: asyncpg.Connection) -> dict[str, dict[str, Any]]:
    tables = tuple(dict.fromkeys((*CORTEX_DOCTOR_VECTOR_TABLES, "team_events")))
    stats: dict[str, dict[str, Any]] = {}
    for table in tables:
        row = await conn.fetchrow(
            """
            SELECT c.relname AS table_name,
                   pg_relation_size(c.oid) AS heap_bytes,
                   pg_indexes_size(c.oid) AS index_bytes,
                   pg_total_relation_size(c.oid) AS total_bytes,
                   COALESCE(s.n_live_tup, 0) AS estimated_live_rows,
                   COALESCE(s.n_dead_tup, 0) AS dead_rows,
                   s.last_autovacuum
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            LEFT JOIN pg_stat_user_tables s ON s.relid = c.oid
            WHERE n.nspname = 'public'
              AND c.relkind IN ('r', 'p')
              AND c.relname = $1
            """,
            table,
        )
        if row is None:
            stats[table] = {"exists": False}
            continue
        has_embedding = await conn.fetchval(
            """
            SELECT EXISTS(
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = $1
                  AND column_name = 'embedding'
            )
            """,
            table,
        )
        total_rows = int(
            await conn.fetchval(f'SELECT COUNT(*) FROM public."{table}"') or 0
        )
        embedded_rows = None
        null_embeddings = None
        if has_embedding:
            embedding_counts = await conn.fetchrow(
                f"""
                SELECT COUNT(embedding)::bigint AS embedded_rows,
                       COUNT(*) FILTER (WHERE embedding IS NULL)::bigint AS null_embeddings
                FROM public."{table}"
                """
            )
            embedded_rows = int(embedding_counts["embedded_rows"] or 0)
            null_embeddings = int(embedding_counts["null_embeddings"] or 0)
        stats[table] = {
            "exists": True,
            "rows": total_rows,
            "estimated_live_rows": int(row["estimated_live_rows"] or 0),
            "heap_bytes": int(row["heap_bytes"] or 0),
            "index_bytes": int(row["index_bytes"] or 0),
            "total_bytes": int(row["total_bytes"] or 0),
            "dead_rows": int(row["dead_rows"] or 0),
            "last_autovacuum": row["last_autovacuum"].isoformat() if row["last_autovacuum"] else None,
            "has_embedding": bool(has_embedding),
            "embedded_rows": embedded_rows,
            "null_embeddings": null_embeddings,
        }
    return stats


async def cortex_doctor_indexes(conn: asyncpg.Connection) -> dict[str, list[dict[str, str]]]:
    rows = await conn.fetch(
        """
        SELECT tablename, indexname, indexdef
        FROM pg_indexes
        WHERE schemaname = 'public'
          AND tablename = ANY($1::text[])
        ORDER BY tablename, indexname
        """,
        list(dict.fromkeys((*CORTEX_DOCTOR_VECTOR_TABLES, "messages", "team_events"))),
    )
    indexes: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        indexes.setdefault(row["tablename"], []).append(
            {"name": row["indexname"], "def": row["indexdef"]}
        )
    return indexes


def cortex_doctor_vector_index_check(
    table_stats: dict[str, dict[str, Any]],
    indexes: dict[str, list[dict[str, str]]],
) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    status = "ok"
    for table in CORTEX_DOCTOR_VECTOR_TABLES:
        stats = table_stats.get(table) or {}
        if not stats.get("exists") or not stats.get("has_embedding"):
            findings.append({"table": table, "status": "unknown", "reason": "table or embedding column missing"})
            status = "unknown" if status == "ok" else status
            continue
        rows = int(stats.get("rows") or 0)
        embedded = int(stats.get("embedded_rows") or 0)
        table_indexes = indexes.get(table, [])
        hnsw = [ix for ix in table_indexes if "USING hnsw" in ix["def"]]
        ivfflat = [
            {**ix, "lists": parse_ivfflat_lists(ix["def"])}
            for ix in table_indexes
            if "USING ivfflat" in ix["def"] and "embedding" in ix["def"]
        ]
        bad_lists = [
            ix for ix in ivfflat
            if (ix.get("lists") or 0) <= 1 and max(rows, embedded) > 50000
        ]
        weak_lists = []
        for ix in ivfflat:
            lists = ix.get("lists")
            if lists is None:
                continue
            recommended = max(10, int(math.sqrt(max(rows, embedded, 1)) * 0.5))
            if max(rows, embedded) > 10000 and lists < recommended:
                weak_lists.append({**ix, "recommended_min_lists": recommended})
        if bad_lists and not hnsw:
            status = "critical"
        elif bad_lists or (max(rows, embedded) > 50000 and not hnsw) or weak_lists:
            status = "warn" if status != "critical" else status
        findings.append(
            {
                "table": table,
                "rows": rows,
                "embedded_rows": embedded,
                "hnsw_indexes": [ix["name"] for ix in hnsw],
                "ivfflat_indexes": [
                    {"name": ix["name"], "lists": ix.get("lists")} for ix in ivfflat
                ],
                "bad_lists": [{"name": ix["name"], "lists": ix.get("lists")} for ix in bad_lists],
                "weak_lists": [
                    {
                        "name": ix["name"],
                        "lists": ix.get("lists"),
                        "recommended_min_lists": ix.get("recommended_min_lists"),
                    }
                    for ix in weak_lists
                ],
            }
        )
    return cortex_doctor_check(
        "vector_index_health",
        "Vector index health",
        status,
        "Vector tables have usable embedding indexes." if status == "ok" else "Vector index drift detected.",
        evidence={"tables": findings},
        recommendation="Use HNSW for embedding tables above ~50k embedded rows; remove degenerate ivfflat lists=1 indexes.",
    )


def cortex_doctor_index_bloat_check(table_stats: dict[str, dict[str, Any]]) -> dict[str, Any]:
    bloated: list[dict[str, Any]] = []
    for table, stats in table_stats.items():
        if not stats.get("exists"):
            continue
        heap = int(stats.get("heap_bytes") or 0)
        index = int(stats.get("index_bytes") or 0)
        if heap <= 0 or index <= 100 * 1024 * 1024:
            continue
        ratio = index / max(heap, 1)
        if ratio >= 3.0:
            bloated.append(
                {
                    "table": table,
                    "heap_mb": round(heap / 1048576, 1),
                    "index_mb": round(index / 1048576, 1),
                    "index_to_heap_ratio": round(ratio, 2),
                }
            )
    status = "warn" if bloated else "ok"
    return cortex_doctor_check(
        "index_bloat",
        "Index bloat",
        status,
        "No severe index bloat detected." if not bloated else "Large index-to-heap ratios detected.",
        evidence={"bloated_tables": bloated},
        recommendation="Review oversized indexes; rebuild or drop obsolete ANN/GIN indexes when ratio exceeds policy.",
    )


def cortex_doctor_hot_path_index_check(indexes: dict[str, list[dict[str, str]]]) -> dict[str, Any]:
    message_indexes = indexes.get("messages", [])
    team_event_indexes = indexes.get("team_events", [])
    has_messages_project_ts = any(
        re.search(r"\(\s*project\s*,\s*ts(?:\s+DESC)?", ix["def"], re.IGNORECASE)
        for ix in message_indexes
    )
    has_team_events_project_ts = any(
        re.search(r"\(\s*project\s*,\s*ts(?:\s+DESC)?", ix["def"], re.IGNORECASE)
        for ix in team_event_indexes
    )
    missing = []
    if not has_messages_project_ts:
        missing.append("messages(project, ts DESC)")
    if team_event_indexes and not has_team_events_project_ts:
        missing.append("team_events(project, ts DESC)")
    status = "warn" if missing else "ok"
    return cortex_doctor_check(
        "hot_path_indexes",
        "Hot-path composite indexes",
        status,
        "History/event hot-path composite indexes are present." if not missing else "Hot-path composite indexes are missing.",
        evidence={
            "missing": missing,
            "messages_indexes": [ix["name"] for ix in message_indexes],
            "team_events_indexes": [ix["name"] for ix in team_event_indexes],
        },
        recommendation="Add composite project+timestamp indexes for high-frequency history/event reads.",
    )


async def cortex_doctor_config_check(conn: asyncpg.Connection) -> dict[str, Any]:
    has_table = await conn.fetchval(
        "SELECT EXISTS(SELECT 1 FROM information_schema.tables WHERE table_name = 'cortex_platform_config')"
    )
    db_config = dict(CORTEX_PLATFORM_DEFAULTS)
    if has_table:
        row = await conn.fetchrow("SELECT * FROM cortex_platform_config LIMIT 1")
        db_config = serialize_cortex_platform_config(row)
    issues: list[str] = []
    try:
        rerank_timeout_ms = int(db_config.get("rerank_timeout_ms") or 0)
    except (TypeError, ValueError):
        rerank_timeout_ms = 0
    if bool(db_config.get("rerank_enabled", True)):
        if rerank_timeout_ms > 5000:
            issues.append(f"rerank_timeout_ms={rerank_timeout_ms} exceeds interactive bound 5000")
        if not await provider_configured(db_config, "rerank"):
            if str(db_config.get("rerank_provider") or "").strip().lower() == LOCAL_SEARCH_PROVIDER:
                issues.append("local rerank model does not match the appliance artifact contract")
            else:
                issues.append(f"rerank provider {db_config.get('rerank_provider')!r} has no configured key")
    if not await provider_configured(db_config, "embedding"):
        if str(db_config.get("embedding_provider") or "").strip().lower() == LOCAL_SEARCH_PROVIDER:
            issues.append("local embedding model or dimensions do not match the appliance artifact contract")
        else:
            issues.append(f"embedding provider {db_config.get('embedding_provider')!r} has no configured key")

    cached = _platform_config_cache.get("config")
    cache_fresh = (
        isinstance(cached, dict)
        and time.time() < float(_platform_config_cache.get("expires") or 0.0)
    )
    cache_mismatches: list[str] = []
    if cache_fresh and isinstance(cached, dict):
        for key in CORTEX_PLATFORM_DEFAULTS:
            if cached.get(key) != db_config.get(key):
                cache_mismatches.append(key)
        if cache_mismatches:
            issues.append("platform config cache differs from DB row")

    status = "warn" if issues else "ok"
    return cortex_doctor_check(
        "config_sanity",
        "Cortex platform config sanity",
        status,
        "Cortex platform config is internally consistent." if not issues else "Cortex platform config issues detected.",
        evidence={
            "issues": issues,
            "config_table_present": bool(has_table),
            "rerank_enabled": bool(db_config.get("rerank_enabled", True)),
            "rerank_provider": db_config.get("rerank_provider"),
            "rerank_timeout_ms": db_config.get("rerank_timeout_ms"),
            "embedding_provider": db_config.get("embedding_provider"),
            "cache_fresh": cache_fresh,
            "cache_mismatches": cache_mismatches,
        },
        recommendation="Keep rerank off hot paths or below 5s, ensure selected providers have keys, and bust config cache on writes.",
    )


async def cortex_doctor_schema_ownership_check(conn: asyncpg.Connection) -> dict[str, Any]:
    rows = await conn.fetch(
        """
        SELECT c.relname AS table_name,
               pg_get_userbyid(c.relowner) AS owner,
               c.relrowsecurity,
               c.relforcerowsecurity
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public'
          AND c.relname = ANY($1::text[])
          AND c.relkind IN ('r', 'p')
        """,
        list(CORTEX_DOCTOR_L4_TABLES),
    )
    by_table = {row["table_name"]: row for row in rows}
    issues: list[dict[str, Any]] = []
    for table in CORTEX_DOCTOR_L4_TABLES:
        row = by_table.get(table)
        if row is None:
            issues.append({"table": table, "issue": "missing"})
            continue
        if row["owner"] != "cortex_app":
            issues.append({"table": table, "issue": "owner", "actual": row["owner"], "expected": "cortex_app"})
        if not row["relrowsecurity"]:
            issues.append({"table": table, "issue": "rls_disabled"})
        if not row["relforcerowsecurity"]:
            issues.append({"table": table, "issue": "force_rls_disabled"})
    status = "critical" if issues else "ok"
    return cortex_doctor_check(
        "schema_ownership",
        "L4 graph schema ownership/RLS",
        status,
        "L4 graph tables are cortex_app-owned with FORCE RLS." if not issues else "L4 graph table ownership/RLS drift detected.",
        evidence={
            "tables": {
                name: {
                    "owner": row["owner"],
                    "rls_enabled": bool(row["relrowsecurity"]),
                    "force_rls": bool(row["relforcerowsecurity"]),
                }
                for name, row in by_table.items()
            },
            "issues": issues,
        },
        recommendation="Apply the graph table ownership/FORCE RLS migration before enabling graph extraction.",
    )


def cortex_doctor_embedding_backlog_check(table_stats: dict[str, dict[str, Any]]) -> dict[str, Any]:
    backlog: list[dict[str, Any]] = []
    status = "ok"
    for table in CORTEX_DOCTOR_VECTOR_TABLES:
        stats = table_stats.get(table) or {}
        if not stats.get("exists") or not stats.get("has_embedding"):
            continue
        total = int(stats.get("rows") or 0)
        missing = int(stats.get("null_embeddings") or 0)
        if total <= 0:
            ratio = 0.0
        else:
            ratio = missing / total
        table_status = "ok"
        if total > 1000 and ratio >= 0.8:
            table_status = "critical"
            status = "critical"
        elif total > 1000 and ratio >= 0.2 and status != "critical":
            table_status = "warn"
            status = "warn"
        backlog.append(
            {
                "table": table,
                "rows": total,
                "null_embeddings": missing,
                "null_ratio": round(ratio, 4),
                "status": table_status,
            }
        )
    return cortex_doctor_check(
        "embedding_backlog",
        "Embedding backlog",
        status,
        "Embedding backlog is within thresholds." if status == "ok" else "Embedding backlog exceeds thresholds.",
        evidence={"tables": backlog},
        recommendation="Run or schedule /beat/embeddings/backfill for high-ratio tables; investigate provider key/timeouts first.",
    )


def cortex_doctor_vacuum_lag_check(table_stats: dict[str, dict[str, Any]]) -> dict[str, Any]:
    lagging: list[dict[str, Any]] = []
    for table, stats in table_stats.items():
        if not stats.get("exists"):
            continue
        live = int(stats.get("estimated_live_rows") or stats.get("rows") or 0)
        dead = int(stats.get("dead_rows") or 0)
        dead_ratio = dead / max(live + dead, 1)
        if live > 50000 and not stats.get("last_autovacuum"):
            lagging.append({"table": table, "live_rows": live, "dead_rows": dead, "dead_ratio": round(dead_ratio, 4), "issue": "no_last_autovacuum"})
        elif live > 10000 and dead_ratio >= 0.2:
            lagging.append({"table": table, "live_rows": live, "dead_rows": dead, "dead_ratio": round(dead_ratio, 4), "issue": "dead_tuple_ratio"})
    status = "warn" if lagging else "ok"
    return cortex_doctor_check(
        "vacuum_lag",
        "Vacuum lag",
        status,
        "No severe vacuum lag detected." if not lagging else "Vacuum lag detected on hot tables.",
        evidence={"lagging_tables": lagging},
        recommendation="Tune autovacuum for hot Cortex tables or run manual VACUUM/ANALYZE as an operator action.",
    )


async def cortex_doctor_transcript_pressure_check(conn: asyncpg.Connection) -> dict[str, Any]:
    """Detect the Finding-6 class: transcript write bursts that trigger hot-table storms."""
    recent = await conn.fetchrow(
        """
        SELECT COUNT(*) FILTER (WHERE ts >= NOW() - INTERVAL '5 minutes')::bigint AS messages_5m,
               COUNT(*) FILTER (WHERE ts >= NOW() - INTERVAL '1 hour')::bigint AS messages_1h,
               COUNT(*) FILTER (WHERE ts >= NOW() - INTERVAL '2 hours')::bigint AS messages_2h
          FROM public.messages
        """
    )
    top_sessions = await conn.fetch(
        """
        SELECT project,
               agent_name,
               session_id::text AS session_id,
               COUNT(*)::bigint AS message_count,
               MIN(ts) AS first_ts,
               MAX(ts) AS last_ts
          FROM public.messages
         WHERE ts >= NOW() - INTERVAL '2 hours'
           AND session_id IS NOT NULL
         GROUP BY project, agent_name, session_id
         ORDER BY COUNT(*) DESC
         LIMIT 5
        """
    )
    autovacuum_rows = await conn.fetch(
        """
        SELECT pid,
               state,
               now() - query_start AS age,
               query
          FROM pg_stat_activity
         WHERE lower(query) LIKE '%vacuum%'
           AND lower(query) LIKE '%messages%'
           -- This predicate's own text contains both 'vacuum' and 'messages', so
           -- without these two exclusions the query MATCHES ITSELF. A non-empty
           -- result forces status=warn below, which made this check warn on every
           -- deployment unconditionally -- marlow reported it with messages_5m,
           -- messages_1h and messages_2h all at 0, and the "vacuum" it found was
           -- this SELECT at age_seconds -0.0. Exclude our own backend, and any
           -- other backend that is also just reading pg_stat_activity.
           AND pid <> pg_backend_pid()
           AND lower(query) NOT LIKE '%pg_stat_activity%'
         ORDER BY query_start NULLS LAST
         LIMIT 10
        """
    )

    m5 = int((recent or {}).get("messages_5m") or 0)
    m1h = int((recent or {}).get("messages_1h") or 0)
    m2h = int((recent or {}).get("messages_2h") or 0)
    session_rows = [dict(row) for row in top_sessions]
    top_session = int(session_rows[0]["message_count"] or 0) if session_rows else 0
    autovacuum = [
        {
            "pid": row["pid"],
            "state": row["state"],
            "age_seconds": round(row["age"].total_seconds(), 1) if row["age"] else None,
            "query": str(row["query"] or "")[:220],
        }
        for row in autovacuum_rows
    ]

    issues: list[str] = []
    status = "ok"
    if m5 >= CORTEX_TRANSCRIPT_5M_CRITICAL:
        status = "critical"
        issues.append(f"messages_5m={m5} exceeds critical threshold {CORTEX_TRANSCRIPT_5M_CRITICAL}")
    elif m5 >= CORTEX_TRANSCRIPT_5M_WARN:
        status = "warn"
        issues.append(f"messages_5m={m5} exceeds warning threshold {CORTEX_TRANSCRIPT_5M_WARN}")
    if m1h >= CORTEX_TRANSCRIPT_1H_CRITICAL:
        status = "critical"
        issues.append(f"messages_1h={m1h} exceeds critical threshold {CORTEX_TRANSCRIPT_1H_CRITICAL}")
    elif m1h >= CORTEX_TRANSCRIPT_1H_WARN and status != "critical":
        status = "warn"
        issues.append(f"messages_1h={m1h} exceeds warning threshold {CORTEX_TRANSCRIPT_1H_WARN}")
    if top_session >= CORTEX_TRANSCRIPT_SESSION_CRITICAL:
        status = "critical"
        issues.append(f"top_session_messages_2h={top_session} exceeds critical threshold {CORTEX_TRANSCRIPT_SESSION_CRITICAL}")
    elif top_session >= CORTEX_TRANSCRIPT_SESSION_WARN and status != "critical":
        status = "warn"
        issues.append(f"top_session_messages_2h={top_session} exceeds warning threshold {CORTEX_TRANSCRIPT_SESSION_WARN}")
    if autovacuum:
        if status == "ok":
            status = "warn"
        issues.append("VACUUM activity is currently touching messages")

    return cortex_doctor_check(
        "transcript_write_pressure",
        "Transcript write pressure",
        status,
        "Transcript write pressure is within thresholds." if not issues else "Transcript write pressure or hot-table maintenance detected.",
        evidence={
            "messages_5m": m5,
            "messages_1h": m1h,
            "messages_2h": m2h,
            "top_sessions_2h": session_rows,
            "active_vacuum_on_messages": autovacuum,
            "thresholds": {
                "messages_5m_warn": CORTEX_TRANSCRIPT_5M_WARN,
                "messages_5m_critical": CORTEX_TRANSCRIPT_5M_CRITICAL,
                "messages_1h_warn": CORTEX_TRANSCRIPT_1H_WARN,
                "messages_1h_critical": CORTEX_TRANSCRIPT_1H_CRITICAL,
                "session_2h_warn": CORTEX_TRANSCRIPT_SESSION_WARN,
                "session_2h_critical": CORTEX_TRANSCRIPT_SESSION_CRITICAL,
            },
            "issues": issues,
        },
        recommendation=(
            "Batch/throttle session transcript writes, coalesce spans where safe, tune "
            "messages autovacuum cost settings, and enforce transcript retention before "
            "single-session bursts can starve Cortex queries."
        ),
    )


def _read_cortex_log_valid_types() -> set[str] | None:
    script = Path(".agents/scripts/cortex-log")
    try:
        text = script.read_text(encoding="utf-8")
    except OSError:
        return None
    match = re.search(r'^VALID_TYPES="([^"]+)"', text, flags=re.MULTILINE)
    if not match:
        return None
    return {item.strip() for item in match.group(1).split() if item.strip()}


def cortex_doctor_contract_check() -> dict[str, Any]:
    issues: list[str] = []
    # This is a SOURCE-TREE lint, not a runtime health property. The paths it reads are
    # relative to the process CWD, and cortex-api runs from /app in a container with no
    # source tree mounted -- so "cannot read" is the normal containerised case, not
    # evidence of drift. Reporting warn for it made this check fire on every
    # containerised deployment forever (marlow: the ONLY issue was the unreadable file),
    # which is how a doctor stops being read. Report `unknown`, which ranks below warn,
    # so a real drift is still distinguishable from an uninspectable tree.
    uninspectable = False
    script_types = _read_cortex_log_valid_types()
    if script_types is None:
        uninspectable = True
    elif script_types != set(CORTEX_LOG_EVENT_TYPES):
        issues.append("cortex-log VALID_TYPES differ from API doctor canonical event set")
    progress_doc_refs: list[str] = []
    for doc_path in (Path("cortex.md"), Path("CORTEX_REFERENCE.md")):
        try:
            text = doc_path.read_text(encoding="utf-8")
        except OSError:
            continue
        # "progress" is ordinary prose throughout the docs. It is drift only when
        # a documented cortex-log invocation presents it as the event-type argument.
        if re.search(r"cortex-log[^\n]*\bprogress\b", text):
            progress_doc_refs.append(str(doc_path))
    if progress_doc_refs and "progress" not in CORTEX_LOG_EVENT_TYPES:
        issues.append("docs still mention progress as a log event type but cortex-log rejects it")
    if issues:
        status = "warn"
        summary = "Log event-type contract drift detected."
    elif uninspectable:
        status = "unknown"
        summary = (
            "Source tree is not readable from this process, so log event-type drift "
            "cannot be checked here. Expected on a containerised deployment; run this "
            "lint where the repository is checked out."
        )
    else:
        status = "ok"
        summary = "Log event-type docs and client constants are aligned."
    return cortex_doctor_check(
        "contract_enum_drift",
        "Contract/enum drift",
        status,
        summary,
        evidence={
            "api_canonical_log_event_types": sorted(CORTEX_LOG_EVENT_TYPES),
            "cortex_log_valid_types": sorted(script_types) if script_types else None,
            "progress_doc_refs": progress_doc_refs,
            "source_tree_readable": not uninspectable,
            "issues": issues,
        },
        recommendation="Generate docs/client event-type lists from one registry; remove stale progress examples or add runtime support intentionally.",
    )


async def cortex_doctor_growth_retention_check(conn: asyncpg.Connection, table_stats: dict[str, dict[str, Any]]) -> dict[str, Any]:
    issues: list[str] = []
    has_retention = await conn.fetchval(
        "SELECT EXISTS(SELECT 1 FROM information_schema.tables WHERE table_name = 'retention_config')"
    )
    retention_row = None
    if has_retention:
        retention_row = await conn.fetchrow(
            "SELECT table_name, tier2_days FROM retention_config WHERE table_name = 'messages' LIMIT 1"
        )
    if not retention_row:
        issues.append("messages retention policy row missing")
    else:
        # COMPLIANCE, not presence. This check previously asked only whether a policy row
        # existed, so a table 71 days past its own retention reported ok -- measured
        # 2026-08-30: messages policy 90d, oldest row 161d, 1.78M rows, 8.6 GB. Nothing in
        # the tree enforces retention_config; it is read to INSERT the defaults and to
        # confirm this row exists, and nowhere else. A declared policy nobody applies is
        # the same defect class as configuration nobody reads, and it is how the
        # 2026-07-19 memory-saturation outage is quietly rebuilding itself.
        tier2_days = retention_row["tier2_days"]
        oldest_days = await conn.fetchval(
            """
            SELECT EXTRACT(DAY FROM NOW() - MIN(ts))::bigint
            FROM public.messages
            """
        )
        if tier2_days and oldest_days and int(oldest_days) > int(tier2_days):
            issues.append(
                f"messages retention is declared {int(tier2_days)}d but the oldest row is "
                f"{int(oldest_days)}d; the policy is not being enforced"
            )
    recent = await conn.fetchrow(
        """
        SELECT COUNT(*)::bigint AS total_rows,
               COUNT(*) FILTER (WHERE ts >= NOW() - INTERVAL '7 days')::bigint AS last_7d_rows
        FROM public.messages
        """
    )
    total = int(recent["total_rows"] or table_stats.get("messages", {}).get("rows") or 0)
    last_7d = int(recent["last_7d_rows"] or 0)
    growth_ratio = last_7d / max(total, 1)
    if total > 10000 and growth_ratio >= 0.25:
        issues.append("messages last-7d growth ratio exceeds 25%")
    status = "warn" if issues else "ok"
    return cortex_doctor_check(
        "growth_retention",
        "Growth and retention",
        status,
        "Messages growth has a retention policy and is within threshold." if not issues else "Messages growth/retention needs attention.",
        evidence={
            "messages_rows": total,
            "messages_last_7d_rows": last_7d,
            "last_7d_growth_ratio": round(growth_ratio, 4),
            "retention_policy_present": bool(retention_row),
            "messages_tier2_days": retention_row["tier2_days"] if retention_row else None,
            "issues": issues,
        },
        recommendation="Keep transcript retention explicit; archive or compact high-growth message history before it dominates Cortex.",
    )


async def cortex_doctor_latency_check(conn: asyncpg.Connection) -> dict[str, Any]:
    has_latency_table = await conn.fetchval(
        "SELECT EXISTS(SELECT 1 FROM information_schema.tables WHERE table_name = 'cortex_latency_baselines')"
    )
    if not has_latency_table:
        return cortex_doctor_check(
            "latency_baselines",
            "Search/history latency baselines",
            "unknown",
            "No persisted latency-baseline table exists yet.",
            evidence={"latency_table_present": False},
            recommendation="Persist search/history p95 snapshots from Prometheus or request metrics, then make this check thresholded.",
        )
    rows = await conn.fetch(
        "SELECT path, p95_ms, threshold_ms, measured_at FROM cortex_latency_baselines ORDER BY measured_at DESC LIMIT 20"
    )
    violations = [
        dict(row)
        for row in rows
        if row["p95_ms"] is not None and row["threshold_ms"] is not None and row["p95_ms"] > row["threshold_ms"]
    ]
    return cortex_doctor_check(
        "latency_baselines",
        "Search/history latency baselines",
        "warn" if violations else "ok",
        "Latency baselines are within thresholds." if not violations else "Latency baseline violations detected.",
        evidence={
            "latency_table_present": True,
            "violations": violations,
            "samples": [dict(row) for row in rows],
        },
        recommendation="Tune indexes/rerank/history paths when p95 exceeds threshold.",
    )


def cortex_doctor_registry_agent_reasons(row: dict[str, Any]) -> list[str]:
    name = str(row.get("name") or "").lower().strip()
    project = str(row.get("project") or "").lower().strip()
    role = str(row.get("role") or "").lower().strip()
    reasons: list[str] = []
    if not _VALID_PROJECT_KEY_RE.fullmatch(project):
        reasons.append("invalid_project_key")
    if not role:
        reasons.append("missing_role")
    if "@" in name:
        reasons.append("project_suffix_in_name")
    if _EPHEMERAL_AGENT_RE.match(name) or _EPHEMERAL_AGENT_RE.match(agent_base_name(name)):
        reasons.append("ephemeral_name")
    if name in _BLOCKED_AGENT_NAMES or agent_base_name(name) in _BLOCKED_AGENT_NAMES:
        reasons.append("blocked_sentence_fragment")
    if not _REGISTRY_AGENT_RE.fullmatch(name):
        reasons.append("invalid_registry_name")
    return reasons


def cortex_doctor_registry_safe_cleanup(row: dict[str, Any], reasons: list[str]) -> bool:
    project = str(row.get("project") or "").lower().strip()
    if not _VALID_PROJECT_KEY_RE.fullmatch(project):
        return False
    hard_garbage = {
        "project_suffix_in_name",
        "ephemeral_name",
        "blocked_sentence_fragment",
    }
    if hard_garbage.intersection(reasons):
        return True
    return "missing_role" in reasons and "invalid_registry_name" in reasons


async def cortex_doctor_registry_health_check(conn: asyncpg.Connection) -> dict[str, Any]:
    rows = await conn.fetch(
        """
        SELECT a.name, a.project, a.role, a.status, a.capabilities
          FROM agents a
     LEFT JOIN cortex_projects p ON p.project_key = a.project
         WHERE COALESCE(p.status, 'active') <> 'deleted'
           AND COALESCE(a.capabilities->>'visibility', 'active') <> 'history-only'
         ORDER BY a.project, a.name
         LIMIT 5000
        """
    )
    duplicate_rows = await conn.fetch(
        """
        SELECT lower(a.name) AS name,
               array_agg(DISTINCT a.project ORDER BY a.project) AS projects
          FROM agents a
     LEFT JOIN cortex_projects p ON p.project_key = a.project
         WHERE COALESCE(p.status, 'active') <> 'deleted'
           AND COALESCE(a.capabilities->>'visibility', 'active') <> 'history-only'
         GROUP BY lower(a.name)
        HAVING COUNT(DISTINCT a.project) > 1
         ORDER BY lower(a.name)
         LIMIT 100
        """
    )

    issues: list[dict[str, Any]] = []
    cleanup_candidates: list[dict[str, Any]] = []
    reason_counts: dict[str, int] = {}
    for raw in rows:
        row = dict(raw)
        row["capabilities"] = json_object(row.get("capabilities"))
        reasons = cortex_doctor_registry_agent_reasons(row)
        if not reasons:
            continue
        for reason in reasons:
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
        issue = {
            "project": row.get("project"),
            "agent": row.get("name"),
            "role": row.get("role"),
            "status": row.get("status"),
            "reasons": reasons,
        }
        issues.append(issue)
        if cortex_doctor_registry_safe_cleanup(row, reasons):
            cleanup_candidates.append(issue)

    duplicates = [
        {"agent": row["name"], "projects": list(row["projects"] or [])}
        for row in duplicate_rows
    ]
    status = "warn" if issues or duplicates else "ok"
    return cortex_doctor_check(
        "registry_health",
        "Agent registry health",
        status,
        "Agent registry rows are scoped and clean." if status == "ok" else "Agent registry pollution detected.",
        evidence={
            "scanned_agent_rows": len(rows),
            "issue_counts": reason_counts,
            "issues": issues[:100],
            "issues_truncated": max(0, len(issues) - 100),
            "cross_project_duplicates": duplicates,
            "safe_cleanup_candidate_count": len(cleanup_candidates),
            "safe_cleanup_candidates": cleanup_candidates,
            "safe_cleanup_truncated": 0,
        },
        recommendation=(
            "Create roster rows only through validated project-scoped registration. "
            "Run cortex-registry-doctor for a dry-run report; use --clean --confirm "
            "to deactivate only the listed safe cleanup candidates through the API."
        ),
    )


async def build_cortex_doctor_report(conn: asyncpg.Connection) -> dict[str, Any]:
    table_stats = await cortex_doctor_table_stats(conn)
    indexes = await cortex_doctor_indexes(conn)
    checks = [
        cortex_doctor_vector_index_check(table_stats, indexes),
        cortex_doctor_index_bloat_check(table_stats),
        cortex_doctor_hot_path_index_check(indexes),
        await cortex_doctor_config_check(conn),
        await cortex_doctor_schema_ownership_check(conn),
        cortex_doctor_embedding_backlog_check(table_stats),
        cortex_doctor_vacuum_lag_check(table_stats),
        await cortex_doctor_transcript_pressure_check(conn),
        cortex_doctor_contract_check(),
        await cortex_doctor_growth_retention_check(conn, table_stats),
        await cortex_doctor_latency_check(conn),
        await cortex_doctor_registry_health_check(conn),
    ]
    return {
        "status": cortex_doctor_overall_status(checks),
        "mode": "read_only",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": {
            status: sum(1 for check in checks if check["status"] == status)
            for status in ("critical", "warn", "unknown", "ok")
        },
        "checks": checks,
    }


@app.get("/admin/cortex/doctor")
async def admin_cortex_doctor(request: Request):
    """Read-only Cortex Autopilot v0 doctor.

    Reports assumed-state vs actual-state gaps; it never mutates DB/config.
    """
    require_admin_access(request)
    # Warm the canonical provider-key cache first. config_sanity's check is sync and
    # reads that cache, so without this the doctor reports "no configured key" for a
    # key the operator can see working in Settings — the exact false critical that
    # made the doctor look like it was lying on marlow.
    await refresh_provider_keys(force=True)
    async with pool_admin.acquire() as conn:
        return await build_cortex_doctor_report(conn)


@app.get("/admin/cortex/health")
async def admin_cortex_health(request: Request, project: str | None = Query(default=None)):
    require_admin_access(request)
    if project is not None and not isinstance(project, str):
        project = None
    project_filter = ""
    args: tuple[Any, ...] = ()
    if project:
        scoped_project = validate_project_key(project)
        project_filter = " WHERE project = $1"
        args = (scoped_project,)
    else:
        project_filter = " WHERE project IN (SELECT project_key FROM cortex_projects)"
    async with pool_admin.acquire() as conn:
        total_decisions = await conn.fetchval(
            "SELECT count(*) FROM decisions" + project_filter,
            *args,
        )
        embedded_decisions = await conn.fetchval(
            "SELECT count(*) FROM decisions" + project_filter + " AND embedding IS NOT NULL",
            *args,
        )
        has_entities = await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM information_schema.tables WHERE table_name = 'cortex_entities')"
        )
        has_relationships = await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM information_schema.tables WHERE table_name = 'cortex_relationships')"
        )
        entity_count = (
            await conn.fetchval("SELECT count(*) FROM cortex_entities")
            if has_entities
            else 0
        )
        relationship_count = (
            await conn.fetchval("SELECT count(*) FROM cortex_relationships")
            if has_relationships
            else 0
        )
        agent_count = await conn.fetchval(
            "SELECT count(DISTINCT agent_name) FROM decisions WHERE agent_name IS NOT NULL"
        )

    embedding_coverage = (
        round(embedded_decisions / total_decisions * 100, 1)
        if total_decisions
        else 0.0
    )
    return {
        "status": "healthy",
        "embedding_coverage_pct": embedding_coverage,
        "total_decisions": total_decisions,
        "embedded_decisions": embedded_decisions,
        "embedding_backlog": total_decisions - embedded_decisions,
        "entity_count": entity_count,
        "relationship_count": relationship_count,
        "active_agents": agent_count,
    }


@app.get("/admin/stats")
async def admin_stats(request: Request):
    """Read-only Cortex storage + index profile (memory-efficiency E0).

    Per memory table: on-disk size, row count, embedded-row count, and the indexes
    present (so the index inventory itself confirms gaps — e.g. a missing trigram
    index or a degenerate ivfflat ``WITH (lists='1')`` shows in ``indexdef``). Plus the
    DB total. This is the proper surface for sizing the storage/retrieval work; direct
    psql is off-limits by house rule."""
    require_admin_access(request)
    names = [
        "messages", "archive_messages", "decisions", "lessons", "knowledge", "work_products",
        "captured_patterns", "artifacts", "handoffs",
    ]
    kind_label = {"r": "table", "p": "partitioned", "v": "view", "m": "matview"}
    out: dict[str, Any] = {}
    async with pool_admin.acquire() as conn:
        db_size = int(await conn.fetchval("SELECT pg_database_size(current_database())") or 0)
        # EVERY (schema, relation) for our names — exposes cortex.* vs public.* duplication.
        locs = await conn.fetch(
            "SELECT n.nspname AS schema, c.relname AS rel, c.relkind AS kind, c.oid AS oid, "
            "       pg_total_relation_size(c.oid) AS total, pg_relation_size(c.oid) AS heap "
            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE c.relname = ANY($1::text[]) AND c.relkind IN ('r','p','v','m') "
            "ORDER BY pg_total_relation_size(c.oid) DESC",
            names,
        )
        for r in locs:
            key = f"{r['schema']}.{r['rel']}"
            total, heap = int(r["total"] or 0), int(r["heap"] or 0)
            rows = None
            if r["kind"] in ("r", "p"):
                try:
                    qualified = (
                        f"{quote_ident(str(r['schema']))}."
                        f"{quote_ident(str(r['rel']))}"
                    )
                    rows = int(
                        await conn.fetchval(f"SELECT count(*) FROM {qualified}") or 0  # nosec B608
                    )
                except Exception:
                    rows = None
            # ``scans`` is what makes this surface actionable. The docstring above
            # always promised a degenerate ivfflat would "show in indexdef" — and it
            # did — but a definition cannot tell you an index is DEAD. Five
            # ``lists=1`` indexes sat here at 1,277 MB with zero reads because
            # nothing exposed idx_scan, so the case for dropping them could only be
            # made from a manual psql query nobody else could reproduce.
            idx = await conn.fetch(
                "SELECT i.relname AS name, pg_relation_size(i.oid) AS size, pg_get_indexdef(i.oid) AS def, "
                "       COALESCE(s.idx_scan, 0) AS scans "
                "FROM pg_index x JOIN pg_class i ON i.oid = x.indexrelid "
                "JOIN pg_class t ON t.oid = x.indrelid JOIN pg_namespace n ON n.oid = t.relnamespace "
                "LEFT JOIN pg_stat_all_indexes s ON s.indexrelid = i.oid "
                "WHERE t.oid = $1 ORDER BY pg_relation_size(i.oid) DESC",
                r["oid"],
            )
            out[key] = {
                "kind": kind_label.get(r["kind"], r["kind"]),
                "total_mb": round(total / 1048576, 1),
                "heap_mb": round(heap / 1048576, 1),
                "index_mb": round((total - heap) / 1048576, 1),
                "rows": rows,
                "indexes": [
                    {
                        "name": ix["name"],
                        "size_mb": round(int(ix["size"] or 0) / 1048576, 1),
                        "scans": int(ix["scans"] or 0),
                        # Never-read AND big enough to be worth a look. Advisory, not
                        # a verdict: a zero-scan index whose feature has no working
                        # replacement means the FEATURE is broken, not that the index
                        # is useless. Confirm a replacement exists before dropping.
                        "unused": int(ix["scans"] or 0) == 0 and int(ix["size"] or 0) >= 8 * 1048576,
                        "def": ix["def"],
                    }
                    for ix in idx
                ],
            }
        # A zero scan count is meaningless without knowing when counting started —
        # right after a stats reset EVERY index reads as dead. Returning this beside
        # the counts is what stops an automated housekeeper dropping live indexes.
        stats_since = await conn.fetchval(
            "SELECT stats_reset FROM pg_stat_database WHERE datname = current_database()"
        )
    unused_mb = round(
        sum(ix["size_mb"] for o in out.values() for ix in o["indexes"] if ix["unused"]), 1
    )
    return {
        "db_size_mb": round(db_size / 1048576, 1),
        "index_stats_since": stats_since.isoformat() if stats_since else None,
        "unused_index_mb": unused_mb,
        "objects": out,
    }


@app.get("/admin/recall-check")
async def admin_recall_check(
    request: Request,
    tables: str = "messages,decisions,knowledge,lessons",
    n: int = 200,
    k: int = 8,
    project: str | None = None,
):
    """Recall gate (memory-efficiency E2/E3/E4 enable-gate).

    For a random sample of embedded rows per table, compares the float32 (``$1::vector``)
    vs halfvec (``$1::halfvec(768)``) top-k nearest neighbours and reports recall@k overlap
    — the MEASURED impact, on THIS deployment's data, of flipping ``CORTEX_VECTOR_PRECISION``
    to ``halfvec`` (E3) before it is activated. The same harness gates the lossy levers
    (E2/E4): a transform must hold recall >= the gate before it ships enabled. Read-only.

    Each sampled row's OWN embedding is the query (no /embed calls; real data distribution),
    with the row itself excluded so overlap is over genuine neighbours. If the halfvec
    indexes (migrations 08-11) aren't applied the halfvec side seq-scans (exact) — a
    pessimistic lower bound, so a pass here stays safe once the index is in place."""
    require_admin_access(request)
    max_delta = 0.02  # halfvec may lose at most this much recall vs float32 at equal probes
    want = [t.strip() for t in tables.split(",") if t.strip()]
    n = max(1, min(int(n), 1000))
    k = max(1, min(int(k), 50))
    out: dict[str, Any] = {}
    async with pool_admin.acquire() as conn:
        for table in want:
            if not re.fullmatch(r"[a-z_]+", table):
                out[table] = {"error": "invalid table name"}
                continue
            try:
                if project:
                    samples = await conn.fetch(
                        f"SELECT id::text AS id, embedding::text AS emb FROM {table} "
                        f"WHERE embedding IS NOT NULL AND project = $2 ORDER BY random() LIMIT $1",
                        n, project,
                    )
                    where = "WHERE embedding IS NOT NULL AND project = $3 AND id::text <> $2"
                else:
                    samples = await conn.fetch(
                        f"SELECT id::text AS id, embedding::text AS emb FROM {table} "
                        f"WHERE embedding IS NOT NULL ORDER BY random() LIMIT $1",
                        n,
                    )
                    where = "WHERE embedding IS NOT NULL AND id::text <> $2"
                if not samples:
                    out[table] = {"error": "no embedded rows for this scope"}
                    continue
                hv_rec: list[float] = []
                f32_rec: list[float] = []
                for s in samples:
                    emb, sid = s["emb"], s["id"]
                    qargs = (emb, sid, project) if project else (emb, sid)
                    # EXACT float32 ground truth: ivfflat at max probes ≈ exhaustive (txn-scoped
                    # SET LOCAL, so the pooled conn's default probes is restored after).
                    async with conn.transaction():
                        await conn.execute("SET LOCAL ivfflat.probes = 2000")
                        exact = {r[0] for r in await conn.fetch(
                            f"SELECT id::text FROM {table} {where} "
                            f"ORDER BY embedding <=> $1::vector LIMIT {k}", *qargs)}
                    # Serving-path candidates at the DB-default probes (the real behaviour).
                    hv = {r[0] for r in await conn.fetch(
                        f"SELECT id::text FROM {table} {where} "
                        f"ORDER BY embedding <=> $1::halfvec(768) LIMIT {k}", *qargs)}
                    f32 = {r[0] for r in await conn.fetch(
                        f"SELECT id::text FROM {table} {where} "
                        f"ORDER BY embedding <=> $1::vector LIMIT {k}", *qargs)}
                    hv_rec.append(len(hv & exact) / float(k))
                    f32_rec.append(len(f32 & exact) / float(k))
                hv_mean = sum(hv_rec) / len(hv_rec)
                f32_mean = sum(f32_rec) / len(f32_rec)
                # The halfvec question is the DELTA vs float32 at equal probes; the absolute
                # level (if < 1.0) is the orthogonal ivfflat-probes tuning question.
                out[table] = {
                    "n": len(samples),
                    "k": k,
                    "halfvec_recall_vs_exact": round(hv_mean, 4),
                    "float32_recall_vs_exact": round(f32_mean, 4),
                    "halfvec_delta_vs_float32": round(hv_mean - f32_mean, 4),
                    "halfvec_min_recall": round(min(hv_rec), 4),
                    "verdict": "pass" if (f32_mean - hv_mean) <= max_delta else "review",
                }
            except Exception as e:  # noqa: BLE001 — per-table isolation: report + continue
                out[table] = {"error": str(e)[:200]}
    deltas = [v["halfvec_delta_vs_float32"] for v in out.values() if isinstance(v, dict) and "halfvec_delta_vs_float32" in v]
    recs = [v["halfvec_recall_vs_exact"] for v in out.values() if isinstance(v, dict) and "halfvec_recall_vs_exact" in v]
    return {
        "compared": "halfvec(768) vs float32 — both recall@k against EXACT float32 ground truth",
        "current_precision": CORTEX_VECTOR_PRECISION,
        "gate": f"halfvec loses <= {max_delta} recall vs float32 at equal probes",
        "overall_halfvec_recall": round(sum(recs) / len(recs), 4) if recs else None,
        "worst_delta_vs_float32": round(min(deltas), 4) if deltas else None,
        "overall_verdict": "pass" if deltas and min(deltas) >= -max_delta else "review",
        "tables": out,
    }


@app.get("/admin/cortex/entities")
async def admin_cortex_entities(
    request: Request,
    search: str | None = Query(default=None, max_length=200),
    entity_type: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
):
    require_admin_access(request)
    async with pool_admin.acquire() as conn:
        has_entities = await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM information_schema.tables WHERE table_name = 'cortex_entities')"
        )
        if not has_entities:
            return {
                "entities": [],
                "total": 0,
                "message": "Entity table not yet created",
            }

        if search:
            rows = await conn.fetch(
                "SELECT id, name, entity_type, project, created_at "
                "FROM cortex_entities "
                "WHERE name ILIKE $1 "
                "ORDER BY created_at DESC LIMIT $2",
                f"%{search}%",
                limit,
            )
        elif entity_type:
            rows = await conn.fetch(
                "SELECT id, name, entity_type, project, created_at "
                "FROM cortex_entities "
                "WHERE entity_type = $1 "
                "ORDER BY created_at DESC LIMIT $2",
                entity_type,
                limit,
            )
        else:
            rows = await conn.fetch(
                "SELECT id, name, entity_type, project, created_at "
                "FROM cortex_entities "
                "ORDER BY created_at DESC LIMIT $1",
                limit,
            )

    return {
        "entities": [
            {
                "id": str(row["id"]),
                "name": row["name"],
                "entity_type": row["entity_type"],
                "project": row["project"],
                "created_at": row["created_at"].isoformat() if row["created_at"] else None,
            }
            for row in rows
        ],
        "total": len(rows),
    }


@app.get("/admin/cortex/config")
async def admin_cortex_config(request: Request):
    require_admin_access(request)
    async with pool_admin.acquire() as conn:
        has_table = await conn.fetchval(
            "SELECT EXISTS("
            "  SELECT 1 FROM information_schema.tables"
            "  WHERE table_name = 'cortex_platform_config'"
            ")"
        )
        if not has_table:
            return dict(CORTEX_PLATFORM_DEFAULTS)

        row = await conn.fetchrow("SELECT * FROM cortex_platform_config LIMIT 1")
        if row is None:
            return dict(CORTEX_PLATFORM_DEFAULTS)
        return serialize_cortex_platform_config(row)


@app.patch("/admin/cortex/config")
async def admin_cortex_update_config(
    body: CortexAdminConfigUpdate,
    request: Request,
):
    require_admin_access(request)
    raw_updates = body.model_dump(exclude_unset=True)
    updates = {
        key: value
        for key, value in raw_updates.items()
        if key in CORTEX_PLATFORM_PATCHABLE_COLUMNS
        and (value is not None or key.endswith("_provider_config_id"))
    }
    if not updates:
        raise HTTPException(400, "No valid fields provided to update.")

    async with pool_admin.acquire() as conn:
        has_table = await conn.fetchval(
            "SELECT EXISTS("
            "  SELECT 1 FROM information_schema.tables"
            "  WHERE table_name = 'cortex_platform_config'"
            ")"
        )
        if not has_table:
            raise HTTPException(503, "cortex_platform_config table does not exist yet.")

        set_parts: list[str] = []
        values: list[Any] = []
        for index, (column, value) in enumerate(updates.items(), start=1):
            set_parts.append(f"{column} = ${index}")
            values.append(value)
        set_parts.append("updated_at = now()")

        row = await conn.fetchrow(
            "UPDATE cortex_platform_config "
            f"SET {', '.join(set_parts)} "
            "WHERE true "
            "RETURNING *",
            *values,
        )
        if row is None:
            await conn.execute(
                "INSERT INTO cortex_platform_config (id) VALUES (TRUE) "
                "ON CONFLICT (id) DO NOTHING"
            )
            row = await conn.fetchrow(
                "UPDATE cortex_platform_config "
                f"SET {', '.join(set_parts)} "
                "WHERE true "
                "RETURNING *",
                *values,
            )
        if row is None:
            raise HTTPException(503, "cortex_platform_config row missing.")
        out = serialize_cortex_platform_config(row)
        _platform_config_cache["config"] = dict(out)
        _platform_config_cache["expires"] = time.time() + _PLATFORM_CONFIG_CACHE_SECONDS
        return out


_COUNTABLE_TABLES = frozenset(
    {
        "agents",
        "agent_diaries",
        "agent_sessions",
        "archive_decisions",
        "archive_events",
        "archive_handoffs",
        "archive_lessons",
        "archive_messages",
        "cortex_entities",
        "cortex_projects",
        "cortex_relationships",
        "decisions",
        "events",
        "handoffs",
        "knowledge",
        "lessons",
        "messages",
        "session_sources",
        "sprints",
        "tasks",
        "team_events",
    }
)


@app.get("/counts/{table}")
async def count_rows(table: str, project: str | None = None, request: Request = None):
    """Purpose-built replacement for ``SELECT COUNT(*) FROM {table} WHERE project = X``.

    Wave 2C (handoff 72fcb38f) — the first of several domain
    endpoints added to retire ``/admin/sql/query``. Only the tables in
    the allow-list can be counted; the column for scoping is always
    ``project`` to match the rest of the Cortex API.
    """
    validate_table_name(table)
    if table not in _COUNTABLE_TABLES:
        raise HTTPException(400, f"Table {table!r} is not exposed via /counts")
    if project is None and request is not None:
        project = request.headers.get("X-Project") or None
    # Phase A of the Cortex isolation design: every
    # memory-touching route MUST resolve project scope before storage I/O.
    # Pre-fix this endpoint silently fell back to a GLOBAL count when no
    # project was supplied — exactly the cross-project leak surface the
    # design closes. Now reject missing scope explicitly.
    if not project:
        raise HTTPException(400, "project query param or X-Project header required")
    async with acquire_scoped(project) as conn:
        n = await conn.fetchval(
            f'SELECT count(*) FROM "{table}" WHERE project = $1', project
        )
    return {"table": table, "project": project, "count": int(n or 0)}


# ---------------------------------------------------------------------------
# Wave 2C Bucket A.2 (handoff 7c9a3cee)
# Purpose-built filtered-count endpoints — replace direct helper patterns like
# COUNT WHERE metadata->>'x'='y' and WHERE invalidated_at IS NULL, which the
# generic /counts/{table} can't express. Filters are hard-coded and
# project-scoped so this never becomes a SQL passthrough.
# ---------------------------------------------------------------------------


@app.get("/decisions/stats")
async def decisions_stats(project: str | None = None, request: Request = None):
    """Return aggregate decision stats for a project.

    Replaces callsites that compute processed-vs-total-vs-invalidated from
    raw SQL against the decisions table. Scoped by project from query
    param (preferred) or X-Project header as fallback.

    Response: {total, processed, unprocessed, invalidated, by_agent: {...}}
    where processed = metadata->>'entities_extracted' = 'true' (the only
    metadata flag the scripts use today). by_agent is a compact map of
    agent_name → decision count, top 20 only — this is for dashboards, not
    exports.
    """
    if project is None and request is not None:
        project = request.headers.get("X-Project") or None
    if not project:
        raise HTTPException(400, "project query param or X-Project header required")

    async with acquire_scoped(project) as conn:
        total = await conn.fetchval(
            "SELECT count(*) FROM decisions WHERE project = $1", project
        )
        processed = await conn.fetchval(
            "SELECT count(*) FROM decisions "
            "WHERE project = $1 AND metadata->>'entities_extracted' = 'true'",
            project,
        )
        invalidated = await conn.fetchval(
            "SELECT count(*) FROM decisions "
            "WHERE project = $1 AND invalidated_at IS NOT NULL",
            project,
        )
        not_invalidated = await conn.fetchval(
            "SELECT count(*) FROM decisions "
            "WHERE project = $1 AND invalidated_at IS NULL",
            project,
        )
        agent_rows = await conn.fetch(
            "SELECT agent_name, count(*) AS n FROM decisions "
            "WHERE project = $1 AND agent_name IS NOT NULL "
            "GROUP BY agent_name ORDER BY n DESC LIMIT 20",
            project,
        )

    return {
        "project": project,
        "total": int(total or 0),
        "processed": int(processed or 0),
        "unprocessed": int((total or 0) - (processed or 0)),
        "invalidated": int(invalidated or 0),
        "active": int(not_invalidated or 0),
        "by_agent": {row["agent_name"]: int(row["n"]) for row in agent_rows},
    }


@app.get("/decisions/recent-count")
async def decisions_recent_count(
    project: str | None = None,
    since: str | None = None,
    max_window_days: int = 30,
    request: Request = None,
):
    """Count decisions created at/after ``since`` for a project.

    ``since`` is an ISO-8601 timestamp (with or without timezone). The
    window is capped at ``max_window_days`` to keep this endpoint cheap —
    if you need a larger window, use /decisions/stats and paginate by
    created_at yourself.
    """
    if project is None and request is not None:
        project = request.headers.get("X-Project") or None
    if not project:
        raise HTTPException(400, "project query param or X-Project header required")
    if not since:
        raise HTTPException(400, "since query param required (ISO-8601)")
    if max_window_days <= 0 or max_window_days > 365:
        raise HTTPException(400, "max_window_days must be 1..365")

    from datetime import datetime, timedelta, timezone as _tz

    try:
        since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(400, f"since is not ISO-8601: {exc}") from exc
    if since_dt.tzinfo is None:
        since_dt = since_dt.replace(tzinfo=_tz.utc)

    earliest = datetime.now(tz=_tz.utc) - timedelta(days=max_window_days)
    if since_dt < earliest:
        since_dt = earliest

    async with acquire_scoped(project) as conn:
        n = await conn.fetchval(
            "SELECT count(*) FROM decisions "
            "WHERE project = $1 AND created_at >= $2",
            project,
            since_dt,
        )
    return {
        "project": project,
        "since": since_dt.isoformat(),
        "max_window_days": max_window_days,
        "count": int(n or 0),
    }


@app.get("/sessions/ingested-ids")
async def sessions_ingested_ids(
    project: str | None = None,
    request: Request = None,
):
    """Return session ids already ingested for a project.

    Purpose-built replacement for cortex-ingest-all's former raw SQL union
    across agent_sessions and messages.
    """
    if project is None and request is not None:
        project = request.headers.get("X-Project") or None
    if not project:
        raise HTTPException(400, "project query param or X-Project header required")

    async with pool_admin.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id::text AS id
            FROM agent_sessions
            WHERE project = $1
            UNION
            SELECT DISTINCT session_id::text AS id
            FROM messages
            WHERE project = $1 AND session_id IS NOT NULL
            ORDER BY id
            """,
            project,
        )
    return {"project": project, "ids": [row["id"] for row in rows]}


@app.get("/messages/counts/by-agent-role")
async def message_counts_by_agent_role(
    project: str | None = None,
    request: Request = None,
):
    """Return message counts grouped by agent and role for a project."""
    if project is None and request is not None:
        project = request.headers.get("X-Project") or None
    if not project:
        raise HTTPException(400, "project query param or X-Project header required")

    async with pool_admin.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT agent_name, role, count(*)::int AS count
            FROM messages
            WHERE project = $1
            GROUP BY agent_name, role
            ORDER BY count(*) DESC
            """,
            project,
        )
    return {
        "project": project,
        "rows": [
            {
                "agent_name": row["agent_name"],
                "role": row["role"],
                "count": int(row["count"] or 0),
            }
            for row in rows
        ],
    }


@app.get("/admin/migrations")
async def admin_migrations(request: Request):
    """List checked-in schema migrations and their applied ledger status."""
    require_admin_access(request)
    async with pool_admin.acquire() as conn:
        return await schema_migration_plan(conn)


@app.post("/admin/migrations/apply")
async def admin_migrations_apply(body: MigrationApplyRequest, request: Request):
    """Apply checked-in schema migrations through the API-owned admin pool.

    This is the sanctioned alternative to agent-run psql for local Cortex
    schema/backfill changes. It only executes files from the mounted migration
    directory and records an idempotency/checksum ledger.
    """
    require_admin_access(request)
    target_ids = [item.strip() for item in (body.target_ids or []) if item.strip()]
    applied_by = (body.applied_by or "admin-api").strip() or "admin-api"
    async with pool_admin.acquire() as conn:
        return await apply_schema_migrations(
            conn,
            dry_run=body.dry_run,
            target_ids=target_ids,
            max_count=body.max_count,
            applied_by=applied_by,
        )


# Row cap for /admin/sql/query. A `conn.fetch()` materialises the ENTIRE result
# set in API memory, so one careless admin query could OOM the API (outage-
# prevention Part C, handoff 8b4b4b49). The bounded handler streams via a
# server-side cursor and stops at this cap, so the API holds at most this many
# rows regardless of the full result size. Override with CORTEX_ADMIN_SQL_MAX_ROWS.
_ADMIN_SQL_MAX_ROWS_DEFAULT = 10000
_ADMIN_SQL_PREFETCH = 500  # rows per cursor round-trip (bounds per-fetch buffering)


def _admin_sql_max_rows() -> int:
    try:
        return max(1, int(os.environ.get("CORTEX_ADMIN_SQL_MAX_ROWS", "") or _ADMIN_SQL_MAX_ROWS_DEFAULT))
    except (TypeError, ValueError):
        return _ADMIN_SQL_MAX_ROWS_DEFAULT


@app.post("/admin/sql/query")
async def admin_sql_query(body: SqlRequest, request: Request):
    require_admin_access(request)
    max_rows = _admin_sql_max_rows()
    rows: list = []
    truncated = False
    async with pool_admin.acquire() as conn:
        # A cursor requires a transaction; the SELECT commits (no-op) on exit.
        async with conn.transaction():
            stmt = await conn.prepare(body.sql)
            # Non-row-returning statement (INSERT/UPDATE/DDL) sent to /query: run it
            # and return no rows — behaviour-compatible with the old fetch() path,
            # which returned [] for a command that produced no result set.
            if not stmt.get_attributes():
                await stmt.fetch()
            else:
                # Row-returning: stream and STOP at the cap so a huge result never
                # materialises wholesale in API memory.
                async for record in stmt.cursor(prefetch=_ADMIN_SQL_PREFETCH):
                    if len(rows) >= max_rows:
                        truncated = True
                        break
                    rows.append([normalize_cell(value) for value in record])
    # `rows` is the historical field (backward-compatible); `truncated`/`row_count`
    # are additive so callers can detect a capped result.
    return {"rows": rows, "truncated": truncated, "row_count": len(rows)}


@app.post("/admin/sql/exec")
async def admin_sql_exec(body: SqlRequest, request: Request):
    require_admin_access(request)
    async with pool_admin.acquire() as conn:
        status = await conn.execute(body.sql)
    return {"status": status}


@app.post("/admin/retention/sweep")
async def admin_retention_sweep(request: Request):
    """Run one retention-enforcement sweep NOW and return per-table outcomes.

    The background loop runs every RETENTION_SWEEP_S; this exists so an operator
    (or a test) can verify the EFFECT — rows actually moved — instead of trusting
    the declaration. Admin-gated like every other /admin surface."""
    require_admin_access(request)
    return {
        "results": await run_retention_sweep(),
        "sweep_interval_s": RETENTION_SWEEP_S,
        "batch_rows": RETENTION_BATCH_ROWS,
        "max_batches": RETENTION_MAX_BATCHES,
    }


@app.post("/admin/redis")
async def admin_redis(request: Request):
    require_admin_access(request)
    raise HTTPException(
        status_code=410,
        detail=(
            "/admin/redis has been removed for local Cortex. Use typed Cortex API "
            "routes backed by Postgres team_events."
        ),
    )
