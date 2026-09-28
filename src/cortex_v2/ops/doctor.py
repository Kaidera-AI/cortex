"""Effect-based doctor: reports configured intent versus applied effect."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

import asyncpg

from ..config import active_profile
from ..store import ScopeContext

MIGRATION_DIRECTORY = Path(__file__).resolve().parents[3] / "migrations"
OUTBOX_DEPTH_WARNING = 1000
OUTBOX_AGE_WARNING_SECONDS = 3600


def _check(
    check_id: str,
    configured: Any,
    applied: Any,
    ok: bool,
) -> dict[str, Any]:
    return {
        "check_id": check_id,
        "configured": configured,
        "applied": applied,
        "status": "ok" if ok else "degraded",
    }


async def run_doctor(
    connection: asyncpg.Connection,
    context: ScopeContext,
    payload: Any,
    path_params: dict[str, Any],
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    role = await connection.fetchrow(
        """
        SELECT current_user AS role_name, rolbypassrls, rolsuper
          FROM pg_roles
         WHERE rolname = current_user
        """
    )
    checks.append(
        _check(
            "runtime_role",
            {"role": "cortex_v2_app", "bypassrls": False, "superuser": False},
            {
                "role": role["role_name"],
                "bypassrls": role["rolbypassrls"],
                "superuser": role["rolsuper"],
            },
            role["role_name"] == "cortex_v2_app"
            and not role["rolbypassrls"]
            and not role["rolsuper"],
        )
    )

    relations = await connection.fetch(
        """
        SELECT n.nspname, c.relname, c.relrowsecurity, c.relforcerowsecurity,
               (has_table_privilege('cortex_v2_app', c.oid, 'SELECT')
                OR has_table_privilege('cortex_v2_app', c.oid, 'INSERT')
                OR has_table_privilege('cortex_v2_app', c.oid, 'UPDATE')
                OR has_table_privilege('cortex_v2_app', c.oid, 'DELETE'))
                   AS app_direct_grant
          FROM pg_class AS c
          JOIN pg_namespace AS n ON n.oid = c.relnamespace
         WHERE n.nspname IN ('cortex_auth', 'cortex_core')
           AND c.relkind IN ('r', 'p')
        """
    )
    # Two protection classes: scope-data relations enforce RLS for every
    # caller; installation-plane relations (credentials, principals, ...) are
    # function-mediated and must carry no direct app-role table grants.
    unenforced = [
        f"{row['nspname']}.{row['relname']}"
        for row in relations
        if row["relrowsecurity"] and not row["relforcerowsecurity"]
    ]
    leaked = [
        f"{row['nspname']}.{row['relname']}"
        for row in relations
        if not row["relrowsecurity"] and row["app_direct_grant"]
    ]
    checks.append(
        _check(
            "forced_rls",
            {
                "rls_relations_forced": True,
                "function_mediated_relations_without_app_grants": True,
            },
            {
                "relations": len(relations),
                "unenforced": unenforced,
                "direct_grant_leaks": leaked,
            },
            len(relations) > 0 and not unenforced and not leaked,
        )
    )

    auth_fn = await connection.fetchrow(
        """
        SELECT p.prosecdef, p.proacl::text AS acl
          FROM pg_proc AS p
          JOIN pg_namespace AS n ON n.oid = p.pronamespace
         WHERE n.nspname = 'cortex_auth' AND p.proname = 'authenticate'
        """
    )
    # A PUBLIC grant appears as an ACL item with an empty grantee ("=X/...");
    # a bare substring match would false-positive on named grantees.
    public_execute = bool(
        auth_fn and auth_fn["acl"] and re.search(r"[{,]=X/", auth_fn["acl"])
    )
    checks.append(
        _check(
            "auth_function_definer",
            {"security_definer": True, "public_execute": False},
            {
                "security_definer": bool(auth_fn["prosecdef"]) if auth_fn else None,
                "public_execute": public_execute,
            },
            bool(auth_fn) and bool(auth_fn["prosecdef"]) and not public_execute,
        )
    )

    ledger = {
        row["migration_id"]: row["checksum_sha256"]
        for row in await connection.fetch(
            "SELECT migration_id, checksum_sha256 FROM cortex_core.schema_migrations"
        )
    }
    profile = active_profile()
    missing: list[str] = []
    mismatched: list[str] = []
    for migration_id in profile.migrations:
        if migration_id not in ledger:
            missing.append(migration_id)
            continue
        migration_path = MIGRATION_DIRECTORY / migration_id
        if migration_path.is_file():
            expected = hashlib.sha256(
                migration_path.read_text().encode("utf-8")
            ).hexdigest()
            if ledger[migration_id] != expected:
                mismatched.append(migration_id)
    checks.append(
        _check(
            "migrations",
            {"expected": list(profile.migrations)},
            {
                "applied": sorted(ledger),
                "missing": missing,
                "checksum_mismatch": mismatched,
            },
            not missing and not mismatched,
        )
    )

    ctx = await connection.fetchrow(
        """
        SELECT current_setting('cortex.principal_id', true) AS principal,
               current_setting('cortex.read_scope_ids', true) AS read_scopes
        """
    )
    checks.append(
        _check(
            "transaction_context",
            "principal and read-scope GUCs are set for every command",
            {
                "principal_set": bool(ctx["principal"]),
                "read_scopes_set": bool(ctx["read_scopes"]),
            },
            bool(ctx["principal"]) and bool(ctx["read_scopes"]),
        )
    )

    outbox = await connection.fetchrow(
        """
        SELECT count(*) FILTER (WHERE delivered_at IS NULL) AS depth,
               COALESCE(
                   max(extract(epoch FROM now() - created_at))
                       FILTER (WHERE delivered_at IS NULL),
                   0
               ) AS oldest_seconds
          FROM cortex_core.content_outbox_events
        """
    )
    depth = int(outbox["depth"])
    oldest = int(outbox["oldest_seconds"])
    checks.append(
        _check(
            "content_outbox",
            {
                "depth_warning": OUTBOX_DEPTH_WARNING,
                "age_warning_seconds": OUTBOX_AGE_WARNING_SECONDS,
            },
            {"undelivered_depth": depth, "oldest_undelivered_seconds": oldest},
            depth <= OUTBOX_DEPTH_WARNING and oldest <= OUTBOX_AGE_WARNING_SECONDS,
        )
    )

    overall = "ok" if all(check["status"] == "ok" for check in checks) else "degraded"
    return {
        "state": overall,
        "checks": checks,
        "coverage": {
            "selected_scope": context.selected.alias,
            "read_scopes": [scope.alias for scope in context.read_scopes],
        },
    }
