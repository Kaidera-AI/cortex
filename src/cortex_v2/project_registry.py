"""Native, grant-scoped project registry projection for current KOS consumers.

No legacy credential or actor-header adaptation. Missing preservation records
are a typed conflict; source metadata and original JSON remain authoritative.
"""
from __future__ import annotations

import json

import asyncpg

from .store import ApiProblem, Principal


async def list_projects(connection: asyncpg.Connection, principal: Principal) -> list[dict]:
    scopes = await connection.fetch(
        "SELECT s.scope_id, a.alias FROM cortex_core.scopes s "
        "JOIN cortex_auth.scope_grants g USING(scope_id) "
        "JOIN cortex_core.scope_aliases a USING(scope_id) "
        "WHERE g.principal_id=$1 AND g.revoked_at IS NULL AND g.can_read "
        "AND s.scope_kind='project' AND s.is_active AND a.is_primary AND a.retired_at IS NULL "
        "ORDER BY a.alias", principal.principal_id,
    )
    await connection.execute(
        "SELECT set_config('cortex.read_scope_ids',$1,true)",
        ",".join(str(row["scope_id"]) for row in scopes),
    )
    records = await connection.fetch(
        """
        SELECT r.*,
          (SELECT count(*) FROM cortex_core.project_identities i
            WHERE i.project_scope_id=r.project_scope_id AND i.identity_kind='agent'
              AND coalesce(i.original_record->'capabilities'->>'visibility','active') <> 'history-only'
              AND (coalesce(i.original_record->'capabilities'->>'keep_visible','false')='true'
                   OR EXISTS(SELECT 1 FROM cortex_core.project_profiles p
                       WHERE p.project_scope_id=i.project_scope_id AND p.agent_name=i.identity_name))) AS agent_count,
          (SELECT count(*) FROM cortex_core.project_profiles p
            WHERE p.project_scope_id=r.project_scope_id) AS profile_count
        FROM cortex_core.project_registry r
        WHERE r.project_scope_id=ANY($1::uuid[])
        """, [row["scope_id"] for row in scopes],
    )
    by_scope = {row["project_scope_id"]: row for row in records}
    if any(row["scope_id"] not in by_scope for row in scopes):
        raise ApiProblem(409, "project_registry_incomplete",
                         "The preserved project registry is incomplete; complete conversion before use.")
    projects = []
    for scope in scopes:
        row = by_scope[scope["scope_id"]]
        try:
            roots = json.loads(row["roots"])
        except (ValueError, TypeError, UnicodeError):
            raise ApiProblem(409, "project_registry_incomplete", "The preserved project roots are incomplete.") from None
        if not isinstance(roots, list) or not roots or any(
            not isinstance(root, dict) or not isinstance(root.get("path"), str)
            or not root["path"] or not isinstance(root.get("kind"), str) or not root["kind"]
            for root in roots
        ):
            raise ApiProblem(409, "project_registry_incomplete", "The preserved project roots are incomplete.")
        primary = [root for root in roots if root["kind"] == "primary"]
        if len(primary) != 1 or primary[0].get("path") != row["repo_root"]:
            raise ApiProblem(409, "project_registry_incomplete", "The preserved project roots are incomplete.")
        projects.append({
            "project_key": scope["alias"], "project_id": row["original_project_id"],
            "display_name": row["display_name"], "default_agent": row["default_agent"],
            "status": row["status"], "parent_project_key": row["parent_project_key"],
            "repo_root": row["repo_root"], "roots": roots,
            "created_at": row["created_at"].isoformat(), "updated_at": row["updated_at"].isoformat(),
            "agent_count": row["agent_count"], "profile_count": row["profile_count"],
        })
    return projects
