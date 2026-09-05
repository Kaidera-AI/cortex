"""Phase A of handoff 12fce72e (Kai Cortex Isolation & Identity design §4.2; renamed from kai 2026-05-07).

Contract test: every cortex-api route that touches storage MUST resolve
project scope before any pool.acquire(). Silent global queries are exactly
the cross-project leak surface ADR-22C closes.

This test reads main.py source and asserts: for every @app.{get,post,put,
patch,delete} route handler that contains `pool.acquire()`, the function
body must ALSO contain either:
  - `require_project_scope(` (canonical helper), OR
  - both `request.headers.get("X-Project")` AND a `raise HTTPException(400`
    that fires when project is unresolved.

Exempt routes (admin/registry/health) are listed explicitly. Adding a new
exempt path requires a code review touch to this file — exactly the
guardrail Phase A is meant to be.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


MAIN_PY = Path(__file__).resolve().parent.parent / "main.py"

# Paths that intentionally operate without per-project scope. Adding to this
# list requires explicit review — that's the point.
EXEMPT_PATHS = {
    # Health — no storage scope needed
    "/health",
    # Admin family — token-gated, project scope is operator's responsibility
    "/admin/sql/query",
    "/admin/sql/exec",
    "/admin/cortex/health",
    "/admin/cortex/config",
    "/admin/cortex/doctor",
    # Admin graph volume hygiene — global active-project registry comparison
    "/graph/prune",
    # Boot — bootstrap response IS the project scope output
    "/boot",
    # Beat operator reads — own scope discipline
    "/beat/roles",
    "/beat/handoffs/dispatchable",
    # Project registry — intentionally global (lists all projects)
    "/projects",
}


ROUTE_RE = re.compile(r'^@app\.(get|post|put|patch|delete)\(["\'](.*?)["\']')
DEF_RE = re.compile(r"^async def (\w+)\(")


def _scan_routes() -> list[tuple[str, str, str, int, str]]:
    """Return list of (method, path, fn_name, lineno, body_text)."""
    lines = MAIN_PY.read_text().split("\n")
    routes = []
    i = 0
    while i < len(lines):
        m = ROUTE_RE.match(lines[i])
        if not m:
            i += 1
            continue
        method, path = m.group(1).upper(), m.group(2)
        # find the async def line
        j = i + 1
        while j < len(lines) and not DEF_RE.match(lines[j]):
            j += 1
        if j >= len(lines):
            i += 1
            continue
        fn_name = DEF_RE.match(lines[j]).group(1)
        # body extends until next @app./class/top-level def
        k = j + 1
        while k < len(lines):
            stripped = lines[k]
            if (stripped.startswith("@app.")
                or stripped.startswith("class ")
                or (stripped
                    and not stripped.startswith(" ")
                    and not stripped.startswith("\t")
                    and not stripped.startswith("#"))):
                break
            k += 1
        body = "\n".join(lines[j:k])
        routes.append((method, path, fn_name, j + 1, body))
        i = k
    return routes


def _is_exempt(path: str) -> bool:
    """A route is exempt if its path matches an exempt prefix exactly or as a parent."""
    for ep in EXEMPT_PATHS:
        if path == ep or path.startswith(ep + "/") or path.startswith(ep + "?"):
            return True
    return False


def _has_project_resolution(body: str) -> tuple[bool, str]:
    """Return (ok, evidence). A route is OK if it either:
       - calls require_project_scope(...)
       - reads X-Project header AND raises HTTPException(400) on missing
    """
    if "require_project_scope" in body:
        return True, "require_project_scope"
    has_header_read = (
        'request.headers.get("X-Project")' in body
        or "X-Project" in body and "headers" in body
    )
    has_400_raise = re.search(r"raise HTTPException\(\s*400", body) is not None
    if has_header_read and has_400_raise:
        return True, "header+400-raise"
    return False, "neither"


def test_every_storage_route_resolves_project_scope():
    """The Phase A contract: pool.acquire() must be preceded by project scope.

    If this test fails, either:
      (a) the new route should call require_project_scope(...), OR
      (b) the new path should be added to EXEMPT_PATHS with explicit review.
    """
    routes = _scan_routes()
    assert routes, "No routes found in main.py — scanner is broken?"

    violations = []
    for method, path, fn_name, lineno, body in routes:
        if _is_exempt(path):
            continue
        if "pool.acquire()" not in body:
            continue  # route doesn't touch storage; nothing to enforce
        ok, evidence = _has_project_resolution(body)
        if not ok:
            violations.append(
                f"  {method:<6} {path:<40} ({fn_name}, line {lineno}): "
                f"touches pool.acquire() without project scope resolution"
            )

    if violations:
        msg = (
            "Phase A contract violation — these routes touch storage without "
            "resolving project scope first.\n"
            "Either add require_project_scope(x_project) to the handler, "
            "or add the path to EXEMPT_PATHS with a code-review note.\n\n"
            + "\n".join(violations)
        )
        pytest.fail(msg)


def test_admin_redis_is_not_project_scope_exempt():
    assert "/admin/redis" not in EXEMPT_PATHS


def test_scanner_finds_routes():
    """Sanity: the scanner finds enough routes that the contract test is meaningful."""
    routes = _scan_routes()
    assert len(routes) >= 50, f"Expected 50+ routes, scanner found {len(routes)}"


def test_exempt_paths_are_real_paths():
    """Every entry in EXEMPT_PATHS should match an actual route path prefix."""
    routes = _scan_routes()
    real_path_prefixes = {r[1] for r in routes}
    # Build a set of "any actual path that starts with this exempt entry"
    valid_exempts = set()
    for ep in EXEMPT_PATHS:
        for path in real_path_prefixes:
            if path == ep or path.startswith(ep + "/") or path.startswith(ep + "?"):
                valid_exempts.add(ep)
                break
    stale = EXEMPT_PATHS - valid_exempts
    assert not stale, (
        f"EXEMPT_PATHS contains entries that no longer match any route: {stale}. "
        f"Remove them from the exempt list."
    )
