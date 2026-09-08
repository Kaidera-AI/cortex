"""Offline acceptance tests for the inactive, explicitly reviewed route inventory."""

import ast
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

from fastapi import HTTPException
import pytest

from service_auth import SCOPES


API = Path(__file__).resolve().parents[1]
PROJECT_ID = "9b0cfd5c-7208-4a4f-b6ca-c983f354db48"

# Independent expectation from POLICY_FOUNDATION_PLAN, not from implementation
# route registration or the discarded external draft.
EXPECTED_GROUPS = [
    ("runtime:read", "GET", "/health /boot/{agent} /bootstrap/{agent} /agents/{agent}/persona /degradation /patterns /brief /workers/health /media/status /onboard/diagnostics /projects/{project_key}/runtime /projects/{project_key}/writers /state"),
    ("memory:read", "GET", "/work-products /work-products/{work_product_id} /search /cortex-graph-search /cortex-graph/stats /cortex-graph/memory /diary/{agent} /diary/{agent}/stats /history /decisions/{decision_id}/lineage /verify/decision /verify/write /counts/{table} /decisions/stats /decisions/recent-count /sessions/ingested-ids /messages/counts/by-agent-role"),
    ("memory:read", "POST", "/search"),
    ("memory:write", "POST", "/log /artifacts /work-products /diary/{agent} /save-chat/{agent} /memory /invalidate/{item_id}"),
    ("ingest:write", "POST", "/knowledge/ingest /lessons/ingest /decisions/ingest /sessions/ingest /artifacts/transcribe /artifacts/describe-image /media/transcribe /media/describe-image /analysis/session/{session_id}"),
    ("registry:read", "GET", "/projects/{project_key} /roster /skills"),
    ("registry:write", "POST", "/agents"),
    ("coordination:read", "GET", "/handoffs /handoffs/{handoff_id} /events /epics /epics/{epic_id} /board"),
    ("coordination:write", "POST", "/handoffs /handoffs/{handoff_id}/claim-with-budget /handoffs/{handoff_id}/claim /handoffs/{handoff_id}/return /handoffs/{handoff_id}/release /handoffs/{handoff_id}/abandon /handoffs/{handoff_id}/fail /board"),
    ("coordination:write", "PUT", "/handoffs/{handoff_id}/claim /handoffs/{handoff_id}/return /handoffs/{handoff_id}/release /handoffs/{handoff_id}/abandon /handoffs/{handoff_id}/fail"),
    ("coordination:write", "PATCH", "/board/{task_id}"),
    ("instance:admin", "GET", "/metrics /beat/projections/status /graph/stats /graph/build/jobs/{job_id} /beat/embeddings/backlog /beat/embeddings/jobs/{job_id} /admin/projects/{project_key}/export /identity/audit /verify/table/{table_name} /admin/cortex/doctor /admin/cortex/health /admin/stats /admin/recall-check /admin/cortex/entities /admin/cortex/config /admin/migrations /dashboard/snapshot /beat/status /beat/roles /beat/handoffs/stale /beat/handoffs/open /beat/handoffs/dispatchable /beat/handoffs/orchestrator /beat/ship-events/latest /beat/deploy-events /beat/events"),
    ("instance:admin", "POST", "/rules/ingest /beat/work-products/check-freshness /graph/prune /graph/build /graph/blast /graph/callers /graph/impact /graph/large-fn /skills /skills/{slug}/bind /beat/embeddings/backfill /beat/handoffs/archive-stale /admin/projects/{target_project_key}/import /epics /admin/migrations/apply /admin/retention/sweep /cortex-graph-extract /handoffs/{handoff_id}/complete /projects /admin/agents/remove"),
    ("instance:admin", "PUT", "/handoffs/{handoff_id}/complete"),
    ("instance:admin", "PATCH", "/admin/cortex/config /projects/{project_key}/roster-policy /projects/{project_key}"),
    ("instance:admin", "DELETE", "/skills/{slug}"),
    (None, "GET", "/projects"),
    (None, "POST", "/admin/sql/query /admin/sql/exec /admin/redis /handoffs/cross-project /project-local-sync"),
]
EXPECTED = {
    (method, path): None if scope is None else frozenset({scope})
    for scope, method, paths in EXPECTED_GROUPS for path in paths.split()
}
ADMITTED = [(key, value) for key, value in EXPECTED.items() if value is not None]
HELD = [key for key, value in EXPECTED.items() if value is None]


@pytest.fixture
def policy():
    # A fixture-level load gives each independent RED assertion a visible error
    # while the source is absent; it never imports main or starts an API lifespan.
    spec = importlib.util.spec_from_file_location("policy_foundation_under_test", API / "service_auth_policy.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def principal(**changes):
    values = dict(project_key="school-notes", project_id=PROJECT_ID,
                  agent="homework-bot", scopes=frozenset({"memory:read"}))
    values.update(changes)
    return SimpleNamespace(**values)


def denied(function, *args):
    with pytest.raises(HTTPException) as failure:
        function(*args)
    assert failure.value.status_code == 403
    assert isinstance(failure.value.detail, str)
    assert len(failure.value.detail) < 120
    return failure.value.detail


def current_routes():
    found = []
    for node in ast.walk(ast.parse((API / "main.py").read_text())):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if (isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute)
                    and isinstance(decorator.func.value, ast.Name)
                    and decorator.func.value.id == "app"
                    and decorator.func.attr in {"get", "post", "put", "patch", "delete"}):
                found.append((decorator.func.attr.upper(), ast.literal_eval(decorator.args[0])))
    return found


def test_inventory_exactly_matches_current_handlers_and_reviewed_classification(policy):
    routes = current_routes()
    assert len(routes) == len(set(routes)) == len(EXPECTED) == 128
    assert set(routes) == set(EXPECTED)
    assert policy.ROUTE_POLICIES == {**EXPECTED, ("GET", "/health/live"): frozenset()}
    assert len(HELD) == 6
    assert policy.PUBLIC_ROUTES == {("GET", "/health/live")}
    assert not (set(routes) & policy.PUBLIC_ROUTES), "liveness is planned, not implemented"
    declared_scopes = set().union(*(value for value in EXPECTED.values() if value))
    assert declared_scopes == SCOPES - {"tokens:manage"}


@pytest.mark.parametrize("key,required", ADMITTED)
def test_exact_scope_admits_only_the_reviewed_route(policy, key, required):
    assert policy.require_policy(*key, principal(scopes=required)) == required


@pytest.mark.parametrize("key,required", ADMITTED)
def test_every_missing_required_scope_is_denied_without_admin_override(policy, key, required):
    denied(policy.require_policy, *key, principal(scopes=SCOPES - required))


@pytest.mark.parametrize("key", HELD)
def test_unfinished_authorization_hold_denies_even_all_known_scopes(policy, key):
    denied(policy.require_policy, *key, principal(scopes=SCOPES))


@pytest.mark.parametrize("method,path", [
    ("POST", "/brand-new-route"), ("GET", "/admin/new-route"),
    ("POST", "/auth/bootstrap"), ("POST", "/admin/tokens"),
    ("GET", "/auth/whoami"), ("GET", "/openapi.json"),
    ("GET", "/docs"), ("GET", "/redoc"),
    ("HEAD", "/health/live"), ("OPTIONS", "/search"),
    ("get", "/search"), ("GET", "/search/"), ("GET", "/search?project=school-notes"),
    ("GET", "/projects/school-notes"),
])
def test_unclassified_method_or_template_is_not_inferred(policy, method, path):
    denied(policy.require_policy, method, path, principal(scopes=SCOPES))


@pytest.mark.parametrize("scope", ["memory:write", "instance:admin", "tokens:manage", "coordination:read", "registry:read"])
def test_scope_names_do_not_imply_other_permissions(policy, scope):
    denied(policy.require_policy, "GET", "/search", principal(scopes={scope}))


@pytest.mark.parametrize("value", [None, "memory:read", b"memory:read", {"memory:read": True},
    {"memory:read", "*"}, {"memory:read", "unknown:scope"}, ["memory:read", []], 4, [None]])
def test_malformed_or_unknown_scope_collection_fails_closed(policy, value):
    denied(policy.require_policy, "GET", "/search", principal(scopes=value))


@pytest.mark.parametrize("value", [set(), frozenset(), [], ()])
def test_empty_collection_grants_no_protected_access(policy, value):
    denied(policy.require_policy, "GET", "/search", principal(scopes=value))


@pytest.mark.parametrize("value", [{"memory:read"}, frozenset({"memory:read"}), ["memory:read"], ("memory:read",)])
def test_explicit_scope_collections_have_the_same_meaning(policy, value):
    assert policy.require_policy("GET", "/search", principal(scopes=value)) == frozenset({"memory:read"})


@pytest.mark.parametrize("context", [None, object(), SimpleNamespace()])
def test_absent_credential_context_cannot_authorize_protected_route(policy, context):
    denied(policy.require_policy, "GET", "/search", context)


@pytest.mark.parametrize("value", ["school-notes", PROJECT_ID])
@pytest.mark.parametrize("project_id", [PROJECT_ID, UUID(PROJECT_ID)])
def test_exact_project_key_and_uuid_are_equivalent_selectors(policy, value, project_id):
    policy.require_project_selector(value, principal(project_id=project_id))


@pytest.mark.parametrize("value", [None, "", "*", "_global", "other-project", "School-notes",
    " school-notes", "school-notes ", "school-notes\n", PROJECT_ID.upper(), PROJECT_ID.replace("-", ""),
    "9b0cfd5c-7208-4a4f-b6ca-c983f354db49", UUID(PROJECT_ID), ["school-notes"], b"school-notes", 0])
def test_untrusted_project_selectors_are_rejected_without_normalization(policy, value):
    denied(policy.require_project_selector, value, principal())


@pytest.mark.parametrize("changes", [
    {"project_key": "*"}, {"project_key": "_global"}, {"project_key": ""},
    {"project_key": "School-notes"}, {"project_key": "school-notes\n"}, {"project_key": None},
    {"project_id": None}, {"project_id": "not-a-uuid"}, {"project_id": PROJECT_ID.upper()},
])
def test_malformed_or_reserved_project_context_cannot_authorize_even_itself(policy, changes):
    context = principal(**changes)
    denied(policy.require_project_selector, context.project_key, context)
    denied(policy.require_project_selector, str(context.project_id), context)


@pytest.mark.parametrize("agent", ["homework-bot", "homework-bot@school-notes", "homework_bot2"])
def test_actor_accepts_only_exact_bare_or_project_compound_name(policy, agent):
    context = principal(agent=agent)
    base = agent.split("@")[0]
    policy.require_actor_selector(base, context)
    policy.require_actor_selector(base + "@school-notes", context)


@pytest.mark.parametrize("value", [None, "", "someone-else", "homework-bot@other-project",
    "homework-bot:deadbeef", "Homework-bot", " homework-bot", "homework-bot ", "homework-bot\n",
    "homework-bot@school-notes@school-notes", "homework-bot@" + PROJECT_ID, b"homework-bot", 0, ["homework-bot"]])
def test_asserted_actor_cannot_impersonate_or_normalize_into_authority(policy, value):
    denied(policy.require_actor_selector, value, principal())


@pytest.mark.parametrize("agent", [None, "", "*", "_global", "Homework-bot", "homework-bot\n",
    "homework-bot@other-project", "homework-bot:deadbeef", "homework-bot@@school-notes", []])
def test_invalid_canonical_actor_context_fails_closed(policy, agent):
    denied(policy.require_actor_selector, agent, principal(agent=agent))


def test_actor_fields_preserve_registration_and_read_targets(policy):
    assert policy.ACTOR_BODY_FIELDS == {
        ("POST", "/sessions/ingest"): ("agent",),
        ("POST", "/lessons/ingest"): ("agent_name",),
        ("POST", "/decisions/ingest"): ("agent_name",),
        ("POST", "/work-products"): ("agent_name",),
        ("POST", "/handoffs/{handoff_id}/claim"): ("agent",),
        ("PUT", "/handoffs/{handoff_id}/claim"): ("agent",),
        ("POST", "/handoffs/{handoff_id}/claim-with-budget"): ("agent",),
    }
    assert policy.ACTOR_PATH_FIELDS == {
        ("POST", "/diary/{agent}"): ("agent",),
        ("POST", "/save-chat/{agent}"): ("agent",),
    }
    assert ("POST", "/agents") not in policy.ACTOR_BODY_FIELDS
    assert ("GET", "/boot/{agent}") not in policy.ACTOR_PATH_FIELDS
    assert ("GET", "/diary/{agent}") not in policy.ACTOR_PATH_FIELDS


def test_errors_do_not_echo_assertions_or_credential_metadata(policy):
    secret = "synthetic-do-not-disclose-secret"
    messages = [
        denied(policy.require_policy, "POST", "/" + secret, principal(scopes={secret})),
        denied(policy.require_project_selector, secret, principal()),
        denied(policy.require_actor_selector, secret, principal()),
    ]
    assert all(secret not in message and PROJECT_ID not in message for message in messages)


def test_policy_stays_inactive_and_has_no_operational_imports(policy):
    tree = ast.parse((API / "service_auth_policy.py").read_text())
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.add(node.module)
    assert imports <= {"re", "uuid", "fastapi", "service_auth"}
    main_tree = ast.parse((API / "main.py").read_text())
    assert not any(isinstance(node, ast.ImportFrom) and node.module == "service_auth_policy" for node in ast.walk(main_tree))
    assert not any(isinstance(node, ast.Import) and any(alias.name == "service_auth_policy" for alias in node.names) for node in ast.walk(main_tree))
