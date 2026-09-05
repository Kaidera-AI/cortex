"""Shared test helpers for the Cortex API test suite.

E006 Inc04 (Roster-as-Data) made the writer guard registry-driven: every write/
handoff endpoint now calls the async ``load_roster_policy(project)`` resolver,
which issues two reads —

  1. ``SELECT metadata FROM cortex_projects WHERE project_key=$1 ...``  (admin pool)
  2. ``SELECT lower(name), capabilities->>'writer_scope', role FROM agents a ...``
     and ``... FROM roles ...``                                         (scoped pool)

Tests that drive a guard-protected endpoint through a hand-rolled fake connection
need to answer those reads. The helpers below provide the seeded-kaidera-os answers
(work writers {kai,ren}; system writers {beat,migration,system}) so a fake Conn
can delegate to them in its ``fetchrow``/``fetch`` dispatch before falling back to
its own ``AssertionError``. This keeps the registry shape defined in exactly one
place and behaviour-preserving for the existing endpoint tests.

The dedicated guard tests (test_kaidera_os_writer_guard / test_agent_name_guard /
test_roster_as_data) deliberately DO NOT use these — they exercise the real
resolver against their own fixtures to prove the data path.
"""

import os
from pathlib import Path

# Source CORTEX_ADMIN_TOKEN from local-cortex/.env — the SAME file the cortex-api container reads
# (compose `env_file:`). Without it, admin-guarded endpoints 403 for BOTH the in-process guard
# (module-level ADMIN_TOKEN, read at import) AND the live integration tests (http_client → :8501,
# whose token must match the running container). We read the dev .env (no hardcoded secret),
# exactly as the container does — this is what made all 19 admin-token tests fail.
if not os.environ.get("CORTEX_ADMIN_TOKEN"):
    _env = Path(__file__).resolve().parents[3] / "local-cortex" / ".env"
    if _env.exists():
        for _line in _env.read_text().splitlines():
            if _line.startswith("CORTEX_ADMIN_TOKEN="):
                os.environ["CORTEX_ADMIN_TOKEN"] = _line.split("=", 1)[1].strip()
                break

# Seeded kaidera-os cortex_projects.metadata — mirrors the Step-1 backfill verbatim
# in the shape the resolver reads. Computed -> work={kai,ren}, system={beat,migration,system}.
SEEDED_kaidera_os_METADATA = {
    "enforce_writer_roster": True,
    "roster_policy": {
        "enforce_writer_roster": True,
        "roster_schema_version": "1",
        "default_writer_scope": "work",
        "system_event_writers": ["beat", "migration", "system"],
        "beat_may_create_handoff": True,
        "handoff_targets": "writers",
        "suggest_cutoff": 0.6,
    },
}

_SEEDED_kaidera_os_AGENTS = [
    {"n": "kai", "scope": "work", "role": "full-stack-developer"},
    {"n": "ren", "scope": "work", "role": "full-stack-developer"},
]


class _NotARosterRead(Exception):
    """Raised when a SQL string is not one of the resolver's registry reads."""


def roster_fetchrow(sql, args):
    """Answer the resolver's cortex_projects.metadata read; else signal _NotARosterRead.

    Usage inside a fake Conn.fetchrow, just before its own AssertionError fallback:

        try:
            return roster_fetchrow(sql, args)
        except _NotARosterRead:
            pass
        raise AssertionError(...)
    """
    if "SELECT metadata FROM cortex_projects" in sql:
        return {"metadata": SEEDED_kaidera_os_METADATA}
    raise _NotARosterRead


def roster_fetch(sql, args):
    """Answer the resolver's agents/roles reads; else signal _NotARosterRead."""
    if "writer_scope" in sql and "FROM agents a" in sql:
        project = args[0] if args else None
        return list(_SEEDED_kaidera_os_AGENTS) if project == "kaidera-os" else []
    if "FROM roles" in sql and "default_capabilities" in sql:
        return []
    raise _NotARosterRead
