"""Inactive authorization foundation; not an HTTP authentication adapter.

Each exact route template has an explicit required permission or a temporary
authorization hold (None). These checks grant neither object ownership nor
cross-project visibility, and do not replace existing handler authorization.
No running application imports this module yet. Owner issuance stays separate.
"""

import re
from uuid import UUID

from fastapi import HTTPException

from service_auth import SCOPES


PUBLIC_ROUTES = frozenset({("GET", "/health/live")})

# Literal keys are intentional. New routes must be classified by a reviewed
# change; prefix matching and a catch-all administrator permission are forbidden.
ROUTE_POLICIES = {
    ("GET", "/health"): frozenset({"runtime:read"}),
    ("GET", "/boot/{agent}"): frozenset({"runtime:read"}),
    ("GET", "/bootstrap/{agent}"): frozenset({"runtime:read"}),
    ("GET", "/agents/{agent}/persona"): frozenset({"runtime:read"}),
    ("GET", "/degradation"): frozenset({"runtime:read"}),
    ("GET", "/patterns"): frozenset({"runtime:read"}),
    ("GET", "/brief"): frozenset({"runtime:read"}),
    ("GET", "/workers/health"): frozenset({"runtime:read"}),
    ("GET", "/media/status"): frozenset({"runtime:read"}),
    ("GET", "/onboard/diagnostics"): frozenset({"runtime:read"}),
    ("GET", "/projects/{project_key}/runtime"): frozenset({"runtime:read"}),
    ("GET", "/projects/{project_key}/writers"): frozenset({"runtime:read"}),
    ("GET", "/state"): frozenset({"runtime:read"}),
    ("GET", "/work-products"): frozenset({"memory:read"}),
    ("GET", "/work-products/{work_product_id}"): frozenset({"memory:read"}),
    ("GET", "/search"): frozenset({"memory:read"}),
    ("GET", "/cortex-graph-search"): frozenset({"memory:read"}),
    ("GET", "/cortex-graph/stats"): frozenset({"memory:read"}),
    ("GET", "/cortex-graph/memory"): frozenset({"memory:read"}),
    ("GET", "/diary/{agent}"): frozenset({"memory:read"}),
    ("GET", "/diary/{agent}/stats"): frozenset({"memory:read"}),
    ("GET", "/history"): frozenset({"memory:read"}),
    ("GET", "/decisions/{decision_id}/lineage"): frozenset({"memory:read"}),
    ("GET", "/verify/decision"): frozenset({"memory:read"}),
    ("GET", "/verify/write"): frozenset({"memory:read"}),
    ("GET", "/counts/{table}"): frozenset({"memory:read"}),
    ("GET", "/decisions/stats"): frozenset({"memory:read"}),
    ("GET", "/decisions/recent-count"): frozenset({"memory:read"}),
    ("GET", "/sessions/ingested-ids"): frozenset({"memory:read"}),
    ("GET", "/messages/counts/by-agent-role"): frozenset({"memory:read"}),
    ("POST", "/search"): frozenset({"memory:read"}),
    ("POST", "/log"): frozenset({"memory:write"}),
    ("POST", "/artifacts"): frozenset({"memory:write"}),
    ("POST", "/work-products"): frozenset({"memory:write"}),
    ("POST", "/diary/{agent}"): frozenset({"memory:write"}),
    ("POST", "/save-chat/{agent}"): frozenset({"memory:write"}),
    ("POST", "/memory"): frozenset({"memory:write"}),
    ("POST", "/invalidate/{item_id}"): frozenset({"memory:write"}),
    ("POST", "/knowledge/ingest"): frozenset({"ingest:write"}),
    ("POST", "/lessons/ingest"): frozenset({"ingest:write"}),
    ("POST", "/decisions/ingest"): frozenset({"ingest:write"}),
    ("POST", "/sessions/ingest"): frozenset({"ingest:write"}),
    ("POST", "/artifacts/transcribe"): frozenset({"ingest:write"}),
    ("POST", "/artifacts/describe-image"): frozenset({"ingest:write"}),
    ("POST", "/media/transcribe"): frozenset({"ingest:write"}),
    ("POST", "/media/describe-image"): frozenset({"ingest:write"}),
    ("POST", "/analysis/session/{session_id}"): frozenset({"ingest:write"}),
    ("GET", "/projects/{project_key}"): frozenset({"registry:read"}),
    ("GET", "/roster"): frozenset({"registry:read"}),
    ("GET", "/skills"): frozenset({"registry:read"}),
    ("POST", "/agents"): frozenset({"registry:write"}),
    ("GET", "/handoffs"): frozenset({"coordination:read"}),
    ("GET", "/handoffs/{handoff_id}"): frozenset({"coordination:read"}),
    ("GET", "/events"): frozenset({"coordination:read"}),
    ("GET", "/epics"): frozenset({"coordination:read"}),
    ("GET", "/epics/{epic_id}"): frozenset({"coordination:read"}),
    ("GET", "/board"): frozenset({"coordination:read"}),
    ("POST", "/handoffs"): frozenset({"coordination:write"}),
    ("POST", "/handoffs/{handoff_id}/claim-with-budget"): frozenset({"coordination:write"}),
    ("POST", "/handoffs/{handoff_id}/claim"): frozenset({"coordination:write"}),
    ("POST", "/handoffs/{handoff_id}/return"): frozenset({"coordination:write"}),
    ("POST", "/handoffs/{handoff_id}/release"): frozenset({"coordination:write"}),
    ("POST", "/handoffs/{handoff_id}/abandon"): frozenset({"coordination:write"}),
    ("POST", "/handoffs/{handoff_id}/fail"): frozenset({"coordination:write"}),
    ("POST", "/board"): frozenset({"coordination:write"}),
    ("PUT", "/handoffs/{handoff_id}/claim"): frozenset({"coordination:write"}),
    ("PUT", "/handoffs/{handoff_id}/return"): frozenset({"coordination:write"}),
    ("PUT", "/handoffs/{handoff_id}/release"): frozenset({"coordination:write"}),
    ("PUT", "/handoffs/{handoff_id}/abandon"): frozenset({"coordination:write"}),
    ("PUT", "/handoffs/{handoff_id}/fail"): frozenset({"coordination:write"}),
    ("PATCH", "/board/{task_id}"): frozenset({"coordination:write"}),
    ("GET", "/metrics"): frozenset({"instance:admin"}),
    ("GET", "/beat/projections/status"): frozenset({"instance:admin"}),
    ("GET", "/graph/stats"): frozenset({"instance:admin"}),
    ("GET", "/graph/build/jobs/{job_id}"): frozenset({"instance:admin"}),
    ("GET", "/beat/embeddings/backlog"): frozenset({"instance:admin"}),
    ("GET", "/beat/embeddings/jobs/{job_id}"): frozenset({"instance:admin"}),
    ("GET", "/admin/projects/{project_key}/export"): frozenset({"instance:admin"}),
    ("GET", "/identity/audit"): frozenset({"instance:admin"}),
    ("GET", "/verify/table/{table_name}"): frozenset({"instance:admin"}),
    ("GET", "/admin/cortex/doctor"): frozenset({"instance:admin"}),
    ("GET", "/admin/cortex/health"): frozenset({"instance:admin"}),
    ("GET", "/admin/stats"): frozenset({"instance:admin"}),
    ("GET", "/admin/recall-check"): frozenset({"instance:admin"}),
    ("GET", "/admin/cortex/entities"): frozenset({"instance:admin"}),
    ("GET", "/admin/cortex/config"): frozenset({"instance:admin"}),
    ("GET", "/admin/migrations"): frozenset({"instance:admin"}),
    ("GET", "/dashboard/snapshot"): frozenset({"instance:admin"}),
    ("GET", "/beat/status"): frozenset({"instance:admin"}),
    ("GET", "/beat/roles"): frozenset({"instance:admin"}),
    ("GET", "/beat/handoffs/stale"): frozenset({"instance:admin"}),
    ("GET", "/beat/handoffs/open"): frozenset({"instance:admin"}),
    ("GET", "/beat/handoffs/dispatchable"): frozenset({"instance:admin"}),
    ("GET", "/beat/handoffs/orchestrator"): frozenset({"instance:admin"}),
    ("GET", "/beat/ship-events/latest"): frozenset({"instance:admin"}),
    ("GET", "/beat/deploy-events"): frozenset({"instance:admin"}),
    ("GET", "/beat/events"): frozenset({"instance:admin"}),
    ("POST", "/rules/ingest"): frozenset({"instance:admin"}),
    ("POST", "/beat/work-products/check-freshness"): frozenset({"instance:admin"}),
    ("POST", "/graph/prune"): frozenset({"instance:admin"}),
    ("POST", "/graph/build"): frozenset({"instance:admin"}),
    ("POST", "/graph/blast"): frozenset({"instance:admin"}),
    ("POST", "/graph/callers"): frozenset({"instance:admin"}),
    ("POST", "/graph/impact"): frozenset({"instance:admin"}),
    ("POST", "/graph/large-fn"): frozenset({"instance:admin"}),
    ("POST", "/skills"): frozenset({"instance:admin"}),
    ("POST", "/skills/{slug}/bind"): frozenset({"instance:admin"}),
    ("POST", "/beat/embeddings/backfill"): frozenset({"instance:admin"}),
    ("POST", "/beat/handoffs/archive-stale"): frozenset({"instance:admin"}),
    ("POST", "/admin/projects/{target_project_key}/import"): frozenset({"instance:admin"}),
    ("POST", "/epics"): frozenset({"instance:admin"}),
    ("POST", "/admin/migrations/apply"): frozenset({"instance:admin"}),
    ("POST", "/admin/retention/sweep"): frozenset({"instance:admin"}),
    ("POST", "/cortex-graph-extract"): frozenset({"instance:admin"}),
    ("POST", "/handoffs/{handoff_id}/complete"): frozenset({"instance:admin"}),
    ("POST", "/projects"): frozenset({"instance:admin"}),
    ("POST", "/admin/agents/remove"): frozenset({"instance:admin"}),
    ("PUT", "/handoffs/{handoff_id}/complete"): frozenset({"instance:admin"}),
    ("PATCH", "/admin/cortex/config"): frozenset({"instance:admin"}),
    ("PATCH", "/projects/{project_key}/roster-policy"): frozenset({"instance:admin"}),
    ("PATCH", "/projects/{project_key}"): frozenset({"instance:admin"}),
    ("DELETE", "/skills/{slug}"): frozenset({"instance:admin"}),
    # Temporary holds, not removed capabilities. Each requires a separate
    # accepted authorization/handler change before ordinary bearer admission.
    ("GET", "/projects"): None,
    ("POST", "/admin/sql/query"): None,
    ("POST", "/admin/sql/exec"): None,
    ("POST", "/admin/redis"): None,
    ("POST", "/handoffs/cross-project"): None,
    ("POST", "/project-local-sync"): None,
    # Planned liveness contract only; this module does not create the endpoint.
    ("GET", "/health/live"): frozenset(),
}

ACTOR_BODY_FIELDS = {
    ("POST", "/sessions/ingest"): ("agent",),
    ("POST", "/lessons/ingest"): ("agent_name",),
    ("POST", "/decisions/ingest"): ("agent_name",),
    ("POST", "/work-products"): ("agent_name",),
    ("POST", "/handoffs/{handoff_id}/claim"): ("agent",),
    ("PUT", "/handoffs/{handoff_id}/claim"): ("agent",),
    ("POST", "/handoffs/{handoff_id}/claim-with-budget"): ("agent",),
}
ACTOR_PATH_FIELDS = {
    ("POST", "/diary/{agent}"): ("agent",),
    ("POST", "/save-chat/{agent}"): ("agent",),
}

# Match canonical registry syntax without importing main (which owns runtime).
# Unlike ingress normalization, authorization accepts only exact canonical text.
_PROJECT_KEY = re.compile(r"[a-z0-9][a-z0-9-]{1,63}")
_AGENT_NAME = re.compile(r"[a-z][a-z0-9_-]{1,31}")


def _deny():
    raise HTTPException(403, "Credential does not permit this operation")


def require_policy(method, template, context):
    """Check explicit scope requirements; never infer permissions or ownership."""
    if not isinstance(method, str) or not isinstance(template, str):
        _deny()
    required = ROUTE_POLICIES.get((method, template))
    if required is None:
        _deny()
    if not required:
        return required
    scopes = getattr(context, "scopes", None)
    if not isinstance(scopes, (set, frozenset, list, tuple)):
        _deny()
    try:
        granted = frozenset(scopes)
    except TypeError:
        _deny()
    if not granted <= SCOPES or not required <= granted:
        _deny()
    return required


def _project_identity(context):
    key = getattr(context, "project_key", None)
    identifier = getattr(context, "project_id", None)
    if not isinstance(key, str) or not _PROJECT_KEY.fullmatch(key):
        _deny()
    if not isinstance(identifier, (str, UUID)):
        _deny()
    try:
        canonical_id = str(UUID(str(identifier)))
    except ValueError:
        _deny()
    if str(identifier) != canonical_id:
        _deny()
    return key, canonical_id


def require_project_selector(value, context):
    """Only the credential's exact project key or canonical UUID is a selector."""
    key, identifier = _project_identity(context)
    if not isinstance(value, str) or value not in (key, identifier):
        _deny()


def require_actor_selector(value, context):
    """Validate acting identity, never a registration target or imported author."""
    project, _ = _project_identity(context)
    actor = getattr(context, "agent", None)
    if not isinstance(actor, str):
        _deny()
    base, separator, suffix = actor.partition("@")
    if not _AGENT_NAME.fullmatch(base) or (separator and suffix != project):
        _deny()
    if not isinstance(value, str) or value not in (base, base + "@" + project):
        _deny()
