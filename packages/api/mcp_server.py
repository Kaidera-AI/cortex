"""Cortex MCP server — thin MCP wrapper over the cortex-api HTTP boundary.

Exposes Cortex operations (handoffs, decisions, lessons, search, persona, graph)
as MCP tools so any MCP-capable harness (Claude Code, Codex CLI, Gemini CLI,
future) can invoke them without per-harness integration code.

Architecture: Option A from MCP_SERVER_DESIGN.md — every tool call → HTTP
request to cortex-api on :8501. Single source of truth in cortex-api;
this module is pure translation layer.

Status: B.1.1 SKELETON — 3 representative tools wired (cortex_bootstrap,
cortex_handoff_list, cortex_log_decision). Remaining 19 tools listed in
design doc §5; impl in B.1.2.

Owner: Kaidera OS maintainers
Design: .agents/api/MCP_SERVER_DESIGN.md
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from contextlib import asynccontextmanager
from typing import Any, Literal, NotRequired, TypedDict, cast

# ── Dependencies ─────────────────────────────────────────────────────────────
# pip install "mcp[cli]>=1.27" httpx
# (mcp ships FastMCP under mcp.server.fastmcp; do not also install standalone fastmcp.)
try:
    from mcp.server.fastmcp import FastMCP, Context
except ImportError as exc:  # pragma: no cover — surface a clear setup error
    sys.stderr.write(
        "ERROR: mcp SDK not installed. Run: pip install 'mcp[cli]>=1.27' httpx\n"
    )
    raise SystemExit(1) from exc

import httpx


# ── Configuration ────────────────────────────────────────────────────────────
CORTEX_API_URL = os.environ.get("CORTEX_API_URL", "http://localhost:8501")
CORTEX_PROJECT = os.environ.get("CORTEX_PROJECT", "").strip()
CORTEX_AGENT = os.environ.get("CORTEX_AGENT", "")  # used as X-Agent-Name header
CORTEX_API_BEARER_TOKEN = os.environ.get("CORTEX_API_BEARER_TOKEN", "")
HTTP_TIMEOUT = float(os.environ.get("CORTEX_API_TIMEOUT", "30"))
# These client deadlines must remain above cortex-api's 70s/130s worker
# deadlines. That seam lets cortex-api return a structured timeout instead of
# having the MCP transport disappear first.
GRAPH_CALLERS_TIMEOUT = 80.0
GRAPH_BLAST_IMPACT_TIMEOUT = 140.0

# Transport-aware auth (B.1.5 scaffold).
# - stdio (Phase 1, local): no auth — OS process boundary is the trust boundary.
# - streamable-http (Phase E70, cloud per-pod): bearer token required when set.
# Token comes from CORTEX_MCP_BEARER_TOKEN env (loaded from local-cortex/.env in
# Phase E70 deployment). Empty token = auth disabled (dev/test only).
_TRANSPORT = os.environ.get("CORTEX_MCP_TRANSPORT", "stdio")
_BEARER_TOKEN = os.environ.get("CORTEX_MCP_BEARER_TOKEN", "")

SERVER_NAME = "cortex"
SERVER_VERSION = "0.1.0"


# ── Typed code-graph output contracts ───────────────────────────────────────
# These mirror the bounded better-code-review-graph 3.21 normalization emitted
# by graph-worker. A concrete TypedDict return keeps FastMCP's advertised output
# schema aligned with the HTTP contract rather than exposing an untyped object.

GraphBlastTotals = TypedDict(
    "GraphBlastTotals",
    {
        "changed_files": int,
        "changed_nodes": int,
        "impacted_nodes": int,
        "impacted_files": int,
        "edges": int,
    },
)
GraphBlastResponse = TypedDict(
    "GraphBlastResponse",
    {
        "status": Literal["ok", "error"],
        "summary": str,
        "error": NotRequired[str],
        "changed_files": list[str],
        "changed_nodes": list[dict[str, Any]],
        "impacted_nodes": list[dict[str, Any]],
        "impacted_files": list[str],
        "edges": list[dict[str, Any]],
        "totals": GraphBlastTotals,
        "total_impacted": int,
        "truncated": bool,
    },
)
GraphCallersTotals = TypedDict(
    "GraphCallersTotals",
    {
        "results": int,
        "edges": int,
        "candidates": int,
        "indexed_kinds": int,
        "indexed_under": int,
    },
)
GraphCallersResponse = TypedDict(
    "GraphCallersResponse",
    {
        "status": Literal["ok", "ambiguous", "not_found", "error"],
        "target_indexed": bool,
        "pattern": str,
        "target": str,
        "description": str,
        "summary": str,
        "reason": str,
        "error": str,
        "hint": str,
        "results": list[dict[str, Any]],
        "edges": list[dict[str, Any]],
        "candidates": list[dict[str, Any]],
        "indexed_kinds": list[str],
        "indexed_under": list[str],
        "totals": GraphCallersTotals,
        "truncated": bool,
        "header": NotRequired[dict[str, Any]],
        "dynamic_dispatch_hints": NotRequired[Any],
        "resolved_from_unqualified": NotRequired[bool],
    },
)
GraphImpactGraph = TypedDict(
    "GraphImpactGraph",
    {
        "changed_nodes": list[dict[str, Any]],
        "impacted_nodes": list[dict[str, Any]],
        "edges": list[dict[str, Any]],
    },
)
GraphImpactContext = TypedDict(
    "GraphImpactContext",
    {
        "changed_files": list[str],
        "impacted_files": list[str],
        "graph": GraphImpactGraph,
        "review_guidance": str,
        "untested_functions": list[dict[str, Any]],
    },
)
GraphImpactTotals = TypedDict(
    "GraphImpactTotals",
    {
        "changed_files": int,
        "impacted_files": int,
        "changed_nodes": int,
        "impacted_nodes": int,
        "edges": int,
        "untested_functions": int,
    },
)
GraphImpactResponse = TypedDict(
    "GraphImpactResponse",
    {
        "status": Literal["ok", "error"],
        "summary": str,
        "error": NotRequired[str],
        "context": GraphImpactContext,
        "totals": GraphImpactTotals,
        "truncated": bool,
    },
)


# ── Stdin-EOF watchdog (mandatory per MCP_SERVER_DESIGN.md §6) ───────────────
# Codex Issue #16256 + Claude Code #33947: when the parent harness dies, the
# stdio MCP server child gets reparented to PID 1 and leaks. Documented case:
# 213 orphan processes / 13.6 GB on a single user box. We MUST self-terminate
# on stdin EOF; never trust the parent to clean us up.

async def _stdin_watchdog() -> None:
    """Self-terminate when stdin closes (parent harness died)."""
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    try:
        await loop.connect_read_pipe(lambda: protocol, sys.stdin)
    except Exception:
        return  # stdin already closed / non-fd; skip watchdog
    while True:
        chunk = await reader.read(1)
        if not chunk:
            sys.stderr.write("cortex-mcp: stdin EOF — parent died, terminating\n")
            try:
                os.killpg(os.getpgrp(), signal.SIGTERM)
            except (ProcessLookupError, OSError):
                pass
            os._exit(0)


def _setup_pgroup() -> None:
    """Become a process group leader so SIGTERM kills our children too."""
    try:
        os.setpgid(0, 0)
    except (PermissionError, OSError):
        pass


# ── Lifespan: shared httpx client + watchdog task ────────────────────────────

@asynccontextmanager
async def lifespan(_server: FastMCP):
    """Module-scope httpx client + stdin watchdog."""
    if not CORTEX_PROJECT:
        raise RuntimeError("CORTEX_PROJECT is required; Cortex MCP will not guess a project key")
    headers: dict[str, str] = {"X-Project": CORTEX_PROJECT}
    if CORTEX_AGENT:
        headers["X-Agent-Name"] = CORTEX_AGENT
    if CORTEX_API_BEARER_TOKEN:
        headers["Authorization"] = f"Bearer {CORTEX_API_BEARER_TOKEN}"

    async with httpx.AsyncClient(
        base_url=CORTEX_API_URL,
        timeout=HTTP_TIMEOUT,
        headers=headers,
    ) as client:
        watchdog_task = asyncio.create_task(_stdin_watchdog())
        try:
            yield {"http": client}
        finally:
            watchdog_task.cancel()


mcp = FastMCP(SERVER_NAME, lifespan=lifespan)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _http(ctx: Context) -> httpx.AsyncClient:
    return ctx.request_context.lifespan_context["http"]


def _check_bearer(ctx: Context) -> dict | None:
    """Per-tool bearer-token check (kept as defense-in-depth; primary auth
    is now Starlette middleware in main() for streamable-http transport).

    Returns None when:
      - stdio transport (process boundary is the trust boundary)
      - middleware already validated (we get here only on success)
      - no token configured (dev/test mode)

    The streamable-http path now rejects unauthenticated requests at the
    middleware layer BEFORE any tool dispatch — see BearerAuthMiddleware
    in main(). This function is retained for explicit per-tool guard if
    a future tool needs role-based scoping beyond the global token.
    """
    return None  # middleware now enforces; per-tool check is no-op


async def _safe_call(ctx: Context, method: str, path: str, **kwargs: Any) -> dict:
    """Wrap HTTP calls; surface structured errors instead of raising.

    MCP clients handle structured errors more gracefully than uncaught
    exceptions. Returns either the parsed response body or a dict with
    {"error": str, "status": int, "detail": str} on failure.
    """
    auth_err = _check_bearer(ctx)
    if auth_err is not None:
        return auth_err
    http = _http(ctx)
    try:
        r = await http.request(method, path, **kwargs)
        r.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return {
            "error": "http_error",
            "status": exc.response.status_code,
            "detail": exc.response.text[:500],
        }
    except httpx.HTTPError as exc:
        return {
            "error": "http_transport_error",
            "status": 0,
            "detail": str(exc)[:500],
        }
    if r.headers.get("content-type", "").startswith("application/json"):
        return r.json()
    return {"text": r.text}


async def _post_with_agent(
    ctx: Context, path: str, body: dict, agent: str,
) -> dict:
    """POST with X-Agent-Name override (per-call). Used by writes (log,
    handoff_create, diary) where the agent doing the action may differ from
    the lifespan-default CORTEX_AGENT.
    """
    auth_err = _check_bearer(ctx)
    if auth_err is not None:
        return auth_err
    http = _http(ctx)
    try:
        r = await http.post(path, json=body, headers={"X-Agent-Name": agent})
        r.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return {
            "error": "http_error",
            "status": exc.response.status_code,
            "detail": exc.response.text[:500],
        }
    except httpx.HTTPError as exc:
        return {"error": "http_transport_error", "status": 0, "detail": str(exc)[:500]}
    if r.headers.get("content-type", "").startswith("application/json"):
        return r.json()
    return {"text": r.text}


async def _put_with_agent(
    ctx: Context, path: str, body: dict, agent: str,
) -> dict:
    """PUT with X-Agent-Name override. Used by handoff claim/complete."""
    auth_err = _check_bearer(ctx)
    if auth_err is not None:
        return auth_err
    http = _http(ctx)
    try:
        r = await http.put(path, json=body, headers={"X-Agent-Name": agent})
        r.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return {
            "error": "http_error",
            "status": exc.response.status_code,
            "detail": exc.response.text[:500],
        }
    except httpx.HTTPError as exc:
        return {"error": "http_transport_error", "status": 0, "detail": str(exc)[:500]}
    if r.headers.get("content-type", "").startswith("application/json"):
        return r.json()
    return {"text": r.text}


async def _send_beat_heartbeat(
    ctx: Context,
    handoff_id: str,
    agent: str,
    evidence_summary: str = "",
) -> dict:
    """Send the Beat heartbeat backing call without going through FastMCP."""
    body: dict[str, Any] = {}
    if evidence_summary:
        body["evidence_summary"] = evidence_summary[:200]
    return await _post_with_agent(
        ctx, f"/beat/tasks/{handoff_id}/heartbeat", body, agent
    )


async def _send_beat_claim_done(
    ctx: Context,
    handoff_id: str,
    agent: str,
    outcome: str = "completed",
    evidence_summary: str = "",
) -> dict:
    """Send the Beat claim-done backing call without going through FastMCP.

    Pairs with `_send_beat_heartbeat`. Where heartbeat keeps a CLAIMED task
    in EXECUTING state, claim-done transitions it to a terminal state per
    ADR-019 task lifecycle (verified / closed / failed).
    """
    body: dict[str, Any] = {"outcome": outcome}
    if evidence_summary:
        body["evidence_summary"] = evidence_summary[:200]
    return await _post_with_agent(
        ctx, f"/beat/tasks/{handoff_id}/claim-done", body, agent
    )


# ─────────────────────────────────────────────────────────────────────────────
# Tool surface — full §5 from MCP_SERVER_DESIGN.md.
# Grouped: identity/boot · handoffs · memory writes · search/retrieval ·
# code-graph · diagnostic. ~24 tools total (B.1.2, 2026-05-06).
# ─────────────────────────────────────────────────────────────────────────────

# ─── Identity + boot ────────────────────────────────────────────────────────


@mcp.tool()
async def cortex_bootstrap(agent: str, ctx: Context) -> dict:
    """Get the full team brief for an agent — identity, lane, pending handoffs,
    recent decisions, recent lessons, team activity, and 6-layer architecture
    footer. Equivalent to running `cortex-bootstrap <agent>` in shell.

    Args:
        agent: agent name (e.g. 'lead', 'worker', 'reviewer'). Lowercase canonical.

    Returns:
        Full bootstrap text under {"text": ...} on success, or
        {"error": ..., "status": ..., "detail": ...} on failure.
    """
    return await _safe_call(ctx, "GET", f"/boot/{agent}", params={"full": "true"})


@mcp.tool()
async def cortex_handoff_list(
    ctx: Context,
    agent: str = "",
    status: str = "pending",
) -> dict:
    """List handoffs in Cortex. Filter by agent (mine) and/or status.

    Args:
        agent: agent role or direct agent to filter (e.g. 'backend', 'reviewer'). Empty = all.
        status: 'pending' | 'claimed' | 'completed'. Default 'pending'.

    Returns:
        {"handoffs": [...]} on success, or {"error": ...} on failure.
    """
    params: dict[str, str] = {"status": status}
    if agent:
        params["agent"] = agent
    return await _safe_call(ctx, "GET", "/handoffs", params=params)


@mcp.tool()
async def cortex_log_decision(
    ctx: Context,
    agent: str,
    summary: str,
    category: str = "",
) -> dict:
    """Log a decision to Cortex. Auto-embeds via OpenRouter for vector search.

    Args:
        agent: agent name (lowercase canonical). Used as X-Agent-Name header.
        summary: the decision text. Plain ASCII preferred; em-dashes/apostrophes
            now safe per B.2 fix 2026-05-05 but keep under ~2KB for embedding.
        category: optional tag (e.g. 'milestone', 'pivot', 'completion').

    Returns:
        {"id": "<uuid>", "embedded": bool} on success, or {"error": ...}.
    """
    body: dict[str, Any] = {"event_type": "decision", "summary": summary}
    if category:
        body["category"] = category
    return await _post_with_agent(ctx, "/log", body, agent)


@mcp.tool()
async def cortex_boot(agent: str, ctx: Context) -> dict:
    """Compact identity + handoffs boot context (~250 tokens, L0+L1 only).

    Lighter than cortex_bootstrap; suitable for SessionStart hook injection.

    Args:
        agent: agent name (e.g. 'lead'). Lowercase canonical.
    """
    return await _safe_call(ctx, "GET", f"/boot/{agent}")


@mcp.tool()
async def cortex_persona(agent: str, ctx: Context) -> dict:
    """Full persona payload for an agent.

    Returns identity + lane + harness + sections (operating_rules, persona_skills,
    current_state, architecture_footer) ready for direct injection via
    --append-system-prompt / SessionStart hook / GEMINI_SYSTEM_MD.

    May return {"error": "http_error", "status": 404} on older Cortex
    deployments; in that case, fall back to cortex_bootstrap.
    """
    return await _safe_call(ctx, "GET", f"/agents/{agent}/persona")


# ─── Handoffs ───────────────────────────────────────────────────────────────


@mcp.tool()
async def cortex_handoff_get(handoff_id: str, ctx: Context) -> dict:
    """Fetch a single handoff by id.

    Args:
        handoff_id: full UUID or compound `<uuid>:<hex>` form. The id-prefix
            form (e.g. 'b68ec204') also works against the existing API.
    """
    return await _safe_call(ctx, "GET", f"/handoffs/{handoff_id}")


@mcp.tool()
async def cortex_handoff_create(
    ctx: Context,
    from_role: str,
    to_role: str,
    summary: str,
    agent: str,
    priority: str = "medium",
    branch: str = "",
    files_changed: list[str] | None = None,
    verification: str = "",
    next_steps: str = "",
    context: str = "",
    acceptance: dict[str, Any] | None = None,
    evidence: dict[str, Any] | None = None,
    retry: dict[str, Any] | None = None,
    escalation: dict[str, Any] | None = None,
) -> dict:
    """Create a new handoff. The 'agent' arg is sent as X-Agent-Name (from_agent).

    Args:
        from_role: sender's role (e.g. 'cortex-architect').
        to_role: recipient role (e.g. 'qa', 'backend', 'orchestrator').
        summary: handoff summary (≤400 chars recommended per silent-fail discipline).
        agent: from_agent (sender's canonical name). Used as X-Agent-Name.
        priority: 'low' | 'medium' | 'high' | 'urgent'. Default 'medium'.
        branch: optional git branch name.
        files_changed: optional list of file paths affected.
        verification: optional verification criteria.
        next_steps: optional next-steps text (≤400 chars per silent-fail rule).
        context: optional context text (≤400 chars per silent-fail rule).
        acceptance: optional structured acceptance contract.
        evidence: optional structured evidence requirements.
        retry: optional structured retry policy.
        escalation: optional structured escalation policy.
    """
    body: dict[str, Any] = {
        "from_role": from_role,
        "to_role": to_role,
        "priority": priority,
        "summary": summary,
    }
    if branch:
        body["branch"] = branch
    if files_changed:
        body["files_changed"] = files_changed
    if verification:
        body["verification"] = verification
    if next_steps:
        body["next_steps"] = next_steps
    if context:
        body["context"] = context
    if acceptance:
        body["acceptance"] = acceptance
    if evidence:
        body["evidence"] = evidence
    if retry:
        body["retry"] = retry
    if escalation:
        body["escalation"] = escalation
    return await _post_with_agent(ctx, "/handoffs", body, agent)


@mcp.tool()
async def cortex_handoff_claim(
    handoff_id: str,
    agent: str,
    ctx: Context,
    budget_input_tokens: int = 1200,
    budget_output_tokens: int = 1200,
    deterministic_no_model: bool = False,
) -> dict:
    """Claim a pending handoff for an agent and start Beat active tracking.

    Args:
        handoff_id: handoff UUID (or prefix).
        agent: claiming agent's canonical name.
        budget_input_tokens: ignored compatibility parameter; Cortex handoff claims are not budget-gated.
        budget_output_tokens: ignored compatibility parameter; Cortex handoff claims are not budget-gated.
        deterministic_no_model: ignored compatibility flag; Cortex handoff claims are not budget-gated.
    """
    _ = (budget_input_tokens, budget_output_tokens, deterministic_no_model)
    claim = await _put_with_agent(ctx, f"/handoffs/{handoff_id}/claim", {}, agent)
    if claim.get("error"):
        return claim
    if claim.get("claimed") is False:
        return claim

    heartbeat = await _send_beat_heartbeat(
        ctx,
        handoff_id,
        agent,
        evidence_summary="claimed handoff",
    )
    claim["beat_heartbeat"] = heartbeat
    return claim


@mcp.tool()
async def cortex_handoff_complete(
    handoff_id: str,
    ctx: Context,
    agent: str = "",
    outcome: str = "completed",
    evidence_summary: str = "",
) -> dict:
    """Compatibility wrapper for normal task returns.

    The old direct completion mutation is admin-only. MCP workers must provide
    their identity and evidence so Cortex can create the typed review handback.
    """
    if not agent or not evidence_summary.strip():
        return {
            "error": "completion_requires_return",
            "detail": (
                "Provide agent and evidence_summary, or call "
                "cortex_handoff_return with a structured completion report"
            ),
        }
    result = await _post_with_agent(
        ctx,
        f"/handoffs/{handoff_id}/return",
        {
            "outcome": outcome if outcome in {"completed", "partial", "blocked"} else "partial",
            "summary": evidence_summary,
            "tests_run": [],
            "artifacts": [],
            "risks": [],
            "followups": [],
            "metadata": {"surface": "mcp-legacy-complete"},
        },
        agent,
    )
    if result.get("error"):
        return result

    result["beat_claim_done"] = await _send_beat_claim_done(
        ctx, handoff_id, agent, outcome=outcome, evidence_summary=evidence_summary,
    )
    return result


@mcp.tool()
async def cortex_handoff_return(
    handoff_id: str,
    agent: str,
    summary: str,
    ctx: Context,
    outcome: str = "completed",
    decision: str = "",
    work_product_id: str = "",
    tests_run: list[Any] | None = None,
    artifacts: list[str] | None = None,
    risks: list[str] | None = None,
    followups: list[str] | None = None,
) -> dict:
    """Atomically return completed work to the delegator.

    This is the autonomous terminal action. Cortex either creates one typed
    completion-handback, auto-accepts self-delegated work, or resolves an
    existing completion-handback with ``decision=accept|rework``.
    """
    body: dict[str, Any] = {
        "outcome": outcome,
        "summary": summary,
        "tests_run": tests_run or [],
        "artifacts": artifacts or [],
        "risks": risks or [],
        "followups": followups or [],
        "metadata": {"surface": "mcp"},
    }
    if decision:
        body["decision"] = decision
    if work_product_id:
        body["work_product_id"] = work_product_id

    result = await _post_with_agent(
        ctx,
        f"/handoffs/{handoff_id}/return",
        body,
        agent,
    )
    if result.get("error") or result.get("returned") is False:
        return result
    result["beat_claim_done"] = await _send_beat_claim_done(
        ctx,
        handoff_id,
        agent,
        outcome=outcome,
        evidence_summary=summary,
    )
    return result


# ─── Memory writes ──────────────────────────────────────────────────────────


@mcp.tool()
async def cortex_log_lesson(
    ctx: Context,
    agent: str,
    summary: str,
    importance: int = 5,
) -> dict:
    """Log a lesson — a rule worth keeping past the current task.

    Args:
        agent: agent name (lowercase canonical).
        summary: lesson text.
        importance: 1-10. Lessons ≥ 8 surface in future agent boots.
    """
    body: dict[str, Any] = {
        "event_type": "lesson",
        "summary": summary,
        "importance": importance,
    }
    return await _post_with_agent(ctx, "/log", body, agent)


@mcp.tool()
async def cortex_log_event(
    ctx: Context,
    agent: str,
    event_type: str,
    summary: str,
) -> dict:
    """Log a non-decision/lesson event (commit, started, stopped, blocked,
    unblocked, bug, handoff, question).

    Args:
        agent: agent name.
        event_type: one of: commit, started, stopped, blocked, unblocked, bug,
            handoff, question.
        summary: event text.
    """
    body: dict[str, Any] = {"event_type": event_type, "summary": summary}
    return await _post_with_agent(ctx, "/log", body, agent)


@mcp.tool()
async def cortex_beat_heartbeat(
    ctx: Context,
    handoff_id: str,
    agent: str,
    evidence_summary: str = "",
) -> dict:
    """Send a heartbeat for a claimed handoff.

    The active-management layer transitions a CLAIMED handoff to EXECUTING on
    first heartbeat, then monitors heartbeat freshness to detect STALLED work
    using the deployment's configured timeout.

    Call this periodically (~30s cadence) while an agent is actively working
    a claimed handoff. Older Cortex deployments may return a graceful HTTP
    error (404) if the heartbeat endpoint is not installed.

    Args:
        handoff_id: handoff UUID being worked.
        agent: agent name making the heartbeat (X-Agent-Name).
        evidence_summary: optional one-liner of progress since last heartbeat
            (truncated to 200 chars; surfaces in /admin/beat/dispatch).
    """
    return await _send_beat_heartbeat(ctx, handoff_id, agent, evidence_summary)


@mcp.tool()
async def cortex_beat_claim_done(
    ctx: Context,
    handoff_id: str,
    agent: str,
    outcome: str = "completed",
    evidence_summary: str = "",
) -> dict:
    """Send a claim-done signal for a completed handoff (Platform Beat terminal state).

    Pairs with `cortex_beat_heartbeat`. Where heartbeat keeps a claimed
    handoff in EXECUTING state, claim-done transitions it to a terminal
    state per ADR-019 task lifecycle (verified / closed / failed).

    Auto-fired by `cortex_handoff_complete` when called with `agent` arg —
    use this standalone tool when reporting a non-completion terminal
    state (failed / blocked) without flipping the local handoff status, OR
    when retrying a missed claim-done after a network hiccup.

    Args:
        handoff_id: handoff UUID being closed out.
        agent: agent name making the call (X-Agent-Name).
        outcome: 'completed' | 'blocked' | 'failed' | 'partial'. Default 'completed'.
        evidence_summary: optional one-liner of what shipped (200 char cap;
            surfaces in /admin/beat/dispatch).
    """
    return await _send_beat_claim_done(
        ctx, handoff_id, agent, outcome=outcome, evidence_summary=evidence_summary,
    )


@mcp.tool()
async def cortex_diary_write(
    ctx: Context,
    agent: str,
    summary: str,
    outcome: str = "completed",
    importance: int = 5,
) -> dict:
    """Write a session-end diary entry. Importance ≥ 8 surfaces in future boots.

    Args:
        agent: agent name.
        summary: what you did + decisions.
        outcome: 'completed' | 'blocked' | 'handed-off' | 'partial'. Default 'completed'.
        importance: 1-10. Default 5.
    """
    body: dict[str, Any] = {
        "summary": summary,
        "outcome": outcome,
        "importance": importance,
    }
    return await _post_with_agent(ctx, f"/diary/{agent}", body, agent)


# ─── Search + retrieval ─────────────────────────────────────────────────────


@mcp.tool()
async def cortex_search(
    ctx: Context,
    query: str,
    type: str = "all",
    rerank: bool = True,
) -> dict:
    """Layer 2 vector + verbatim search across decisions/lessons/knowledge/
    messages/handoffs. Best for exact-phrase recall (handoff IDs, exact CTO
    phrases, document titles, incident strings).

    For broad architectural / thematic queries prefer cortex_graph_search.

    Args:
        query: search phrase.
        type: 'all' | 'decisions' | 'lessons' | 'knowledge' | 'handoffs'.
        rerank: rerank with cohere/rerank-4-fast (default True).
    """
    return await _safe_call(
        ctx, "GET", "/search",
        params={"q": query, "type": type, "rerank": "true" if rerank else "false"},
    )


@mcp.tool()
async def cortex_graph_search(
    ctx: Context,
    query: str,
    limit: int = 5,
    expand: bool = False,
) -> dict:
    """Layer 4 knowledge-graph search — best for thematic, architecture, epic,
    service, tool, and process questions. ~80% smaller token footprint than
    cortex_search on broad queries.

    Args:
        query: search phrase.
        limit: max results. Default 5.
        expand: include 1-hop neighbours of top hit. Use only when one-hop
            relationship context is needed.
    """
    params: dict[str, Any] = {"q": query, "limit": limit}
    if expand:
        params["expand"] = "true"
    return await _safe_call(ctx, "GET", "/cortex-graph-search", params=params)


@mcp.tool()
async def cortex_entities_search(query: str, ctx: Context) -> dict:
    """Layer 4 entity browser — search cortex_entities directly.

    Args:
        query: entity name or substring.
    """
    return await _safe_call(
        ctx, "GET", "/admin/cortex/entities", params={"q": query},
    )


@mcp.tool()
async def cortex_history(
    ctx: Context,
    agent: str = "",
    days: int = 7,
) -> dict:
    """Recent decisions + lessons + handoffs for an agent (or all agents) in
    the last N days.

    Args:
        agent: agent name to filter, or empty for all.
        days: lookback window. Default 7.
    """
    params: dict[str, Any] = {"days": days}
    if agent:
        params["agent"] = agent
    return await _safe_call(ctx, "GET", "/history", params=params)


# ─── Code graph (L3) ────────────────────────────────────────────────────────


def _graph_failure_detail(result: dict[str, Any]) -> str:
    """Turn an HTTP/MCP boundary failure into one bounded graph error string."""
    detail = result.get("detail") or result.get("error") or result.get("text")
    return str(detail or "malformed code graph response")[:500]


def _as_graph_blast_response(result: dict[str, Any]) -> GraphBlastResponse:
    required = {
        "status", "summary", "changed_files", "changed_nodes", "impacted_nodes",
        "impacted_files", "edges", "totals", "total_impacted", "truncated",
    }
    if result.get("status") in {"ok", "error"} and required <= result.keys():
        return cast(GraphBlastResponse, result)
    return {
        "status": "error",
        "summary": "",
        "error": _graph_failure_detail(result),
        "changed_files": [],
        "changed_nodes": [],
        "impacted_nodes": [],
        "impacted_files": [],
        "edges": [],
        "totals": {
            "changed_files": 0,
            "changed_nodes": 0,
            "impacted_nodes": 0,
            "impacted_files": 0,
            "edges": 0,
        },
        "total_impacted": 0,
        "truncated": False,
    }


def _as_graph_callers_response(result: dict[str, Any]) -> GraphCallersResponse:
    required = {
        "status", "target_indexed", "pattern", "target", "description", "summary",
        "reason", "error", "hint", "results", "edges", "candidates",
        "indexed_kinds", "indexed_under", "totals", "truncated",
    }
    if (
        result.get("status") in {"ok", "ambiguous", "not_found", "error"}
        and required <= result.keys()
    ):
        return cast(GraphCallersResponse, result)
    detail = _graph_failure_detail(result)
    return {
        "status": "error",
        "target_indexed": False,
        "pattern": "",
        "target": "",
        "description": "",
        "summary": "",
        "reason": "transport_or_contract_error",
        "error": detail,
        "hint": "",
        "results": [],
        "edges": [],
        "candidates": [],
        "indexed_kinds": [],
        "indexed_under": [],
        "totals": {
            "results": 0,
            "edges": 0,
            "candidates": 0,
            "indexed_kinds": 0,
            "indexed_under": 0,
        },
        "truncated": False,
    }


def _as_graph_impact_response(result: dict[str, Any]) -> GraphImpactResponse:
    required = {"status", "summary", "context", "totals", "truncated"}
    if result.get("status") in {"ok", "error"} and required <= result.keys():
        return cast(GraphImpactResponse, result)
    return {
        "status": "error",
        "summary": "",
        "error": _graph_failure_detail(result),
        "context": {
            "changed_files": [],
            "impacted_files": [],
            "graph": {"changed_nodes": [], "impacted_nodes": [], "edges": []},
            "review_guidance": "",
            "untested_functions": [],
        },
        "totals": {
            "changed_files": 0,
            "impacted_files": 0,
            "changed_nodes": 0,
            "impacted_nodes": 0,
            "edges": 0,
            "untested_functions": 0,
        },
        "truncated": False,
    }


@mcp.tool()
async def cortex_graph_blast(
    ctx: Context,
    files: list[str],
    repo: str,
    depth: int = 2,
    max_results: int = 20,
) -> GraphBlastResponse:
    """Blast-radius — callers + dependencies of changed files. **Run this
    before editing any shared code** per blast-radius mandate.

    Args:
        files: changed file paths to analyse.
        repo: repo path (e.g. '.', '02-cust-portal', or absolute path).
        depth: maximum transitive graph depth. Default 2.
        max_results: cap. Default 20.

    Returns:
        Bounded status, changed/impacted collections, exact totals,
        total_impacted, and truncation metadata.
    """
    return _as_graph_blast_response(
        await _safe_call(
            ctx, "POST", "/graph/blast",
            json={
                "files": files,
                "repo": repo,
                "depth": depth,
                "max_results": max_results,
            },
            timeout=GRAPH_BLAST_IMPACT_TIMEOUT,
        )
    )


@mcp.tool()
async def cortex_graph_callers(
    ctx: Context,
    target: str,
    repo: str,
    pattern: str = "callers_of",
    max_results: int = 100,
) -> GraphCallersResponse:
    """Direct callers of a function. Subset of blast (no transitive deps).

    Args:
        target: function name.
        repo: repo path.
        pattern: query relationship, such as callers_of or callees_of.
        max_results: maximum returned results. Default 100.

    Returns:
        Bounded query status, target_indexed proof, results, edges, candidates,
        indexed context, exact totals, hints, and truncation metadata.
    """
    return _as_graph_callers_response(
        await _safe_call(
            ctx, "POST", "/graph/callers",
            json={
                "target": target,
                "repo": repo,
                "pattern": pattern,
                "max_results": max_results,
            },
            timeout=GRAPH_CALLERS_TIMEOUT,
        )
    )


@mcp.tool()
async def cortex_graph_impact(
    ctx: Context,
    base: str,
    repo: str,
    max_results: int = 100,
) -> GraphImpactResponse:
    """Review impact relative to a Git base revision.

    Args:
        base: Git base revision, such as HEAD~1 or origin/main.
        repo: repo path.
        max_results: maximum returned items per collection. Default 100.

    Returns:
        Bounded status and review context containing graph collections,
        impacted files, string guidance, untested functions, exact totals,
        and truncation metadata.
    """
    return _as_graph_impact_response(
        await _safe_call(
            ctx, "POST", "/graph/impact",
            json={"base": base, "repo": repo, "max_results": max_results},
            timeout=GRAPH_BLAST_IMPACT_TIMEOUT,
        )
    )


@mcp.tool()
async def cortex_graph_stats(ctx: Context) -> dict:
    """Code-graph statistics for the MCP server's scoped project."""
    return await _safe_call(ctx, "GET", "/graph/stats")


# ─── Diagnostic ─────────────────────────────────────────────────────────────


@mcp.tool()
async def cortex_doctor(ctx: Context) -> dict:
    """Diagnostic — cortex-api health + version + schema + connection status.

    Less complete than the shell-side `cortex doctor` (which also checks
    Docker containers + workspace project resolution). MCP clients run as
    children of harness processes; they don't have local Docker visibility.
    For operator-side diagnostics, use the shell `cortex doctor`.
    """
    return await _safe_call(ctx, "GET", "/health")


@mcp.tool()
async def cortex_verify_decision(claim: str, ctx: Context) -> dict:
    """Check whether a claim matches an active decision in Cortex.

    Useful for fact-checking before asserting something in a handoff or
    log entry.

    Args:
        claim: the claim text to verify (substring match against decision summaries).
    """
    return await _safe_call(
        ctx, "GET", "/verify/decision", params={"claim": claim},
    )


@mcp.tool()
async def cortex_state(ctx: Context, project: str = "") -> dict:
    """Project overview — active sprints, open handoffs, recent activity.

    Args:
        project: project key (default: current project from X-Project header).
    """
    params = {"project": project} if project else {}
    return await _safe_call(ctx, "GET", "/state", params=params)


@mcp.tool()
async def cortex_roster(ctx: Context, project: str = "") -> dict:
    """Roster — agents in the project with their roles + models.

    Args:
        project: project key (default: current).
    """
    params = {"project": project} if project else {}
    return await _safe_call(ctx, "GET", "/roster", params=params)


# ─────────────────────────────────────────────────────────────────────────────
# Main — transport selection
# ─────────────────────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────────────────────
# Bearer-auth Starlette middleware (B.5 proper integration, 2026-05-06)
# ─────────────────────────────────────────────────────────────────────────────

class BearerAuthMiddleware:
    """Starlette ASGI middleware for bearer-token auth on streamable-http.

    Mounted on `mcp.streamable_http_app()` in main(). Runs once per request,
    BEFORE any MCP tool dispatch. Replaces the per-tool `_check_bearer`
    scaffold from B.1.5 — that scaffold ran on every tool call within a
    session (24× per typical pane); this middleware runs once per HTTP
    request, the correct integration layer.

    Auth disabled when CORTEX_MCP_BEARER_TOKEN is empty (dev/test convenience).
    Production deployments MUST set the env var.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            # Pass through non-HTTP (lifespan events etc.)
            await self.app(scope, receive, send)
            return

        if not _BEARER_TOKEN:
            # Auth disabled (dev/test)
            await self.app(scope, receive, send)
            return

        # Extract Authorization header
        headers = dict(scope.get("headers") or [])
        auth_bytes = headers.get(b"authorization", b"")
        try:
            auth_header = auth_bytes.decode("ascii", errors="replace")
        except Exception:
            auth_header = ""

        if not auth_header.startswith("Bearer "):
            await self._reject(send, "missing or malformed Bearer token")
            return

        token = auth_header[7:].strip()
        if token != _BEARER_TOKEN:
            await self._reject(send, "invalid Bearer token")
            return

        # Token valid — pass through
        await self.app(scope, receive, send)

    async def _reject(self, send, detail: str) -> None:
        body = (
            b'{"error":"unauthorized","status":401,"detail":"'
            + detail.encode("utf-8")
            + b'"}'
        )
        await send({
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        })
        await send({"type": "http.response.body", "body": body})


def main() -> None:
    """Entry point — selects transport from CORTEX_MCP_TRANSPORT env var.

    - 'stdio' (default) — Phase 1, harness spawns server as child process.
      No auth (process boundary is the trust boundary).
    - 'streamable-http' — Phase E70, shared service per customer pod.
      Bearer-token auth via BearerAuthMiddleware mounted on the Starlette app.

    SSE is deprecated by spec 2025-03-26; do not use.
    """
    _setup_pgroup()
    transport = os.environ.get("CORTEX_MCP_TRANSPORT", "stdio")

    if transport == "stdio":
        mcp.run(transport="stdio")
    elif transport == "streamable-http":
        # Build the Starlette app, add bearer auth middleware, run via uvicorn.
        # This is the lower-level path vs mcp.run(transport="streamable-http"),
        # required to inject middleware before tool dispatch.
        try:
            import uvicorn
        except ImportError as exc:
            sys.stderr.write(
                "ERROR: uvicorn not installed. "
                "Run: pip install 'mcp[cli]>=1.27' 'uvicorn[standard]'\n"
            )
            raise SystemExit(1) from exc

        host = os.environ.get("CORTEX_MCP_HOST", "127.0.0.1")
        port = int(os.environ.get("CORTEX_MCP_PORT", "8502"))

        app = mcp.streamable_http_app()
        # Wrap with bearer-auth middleware (ASGI middleware pattern)
        app = BearerAuthMiddleware(app)

        if not _BEARER_TOKEN:
            sys.stderr.write(
                "WARN: CORTEX_MCP_BEARER_TOKEN not set; auth disabled. "
                "Production deployments MUST set the env var.\n"
            )

        uvicorn.run(app, host=host, port=port, log_level="info")
    else:
        sys.stderr.write(
            f"ERROR: unsupported CORTEX_MCP_TRANSPORT={transport!r}\n"
            "Supported: 'stdio' (default) | 'streamable-http'\n"
        )
        sys.exit(2)


if __name__ == "__main__":
    main()
