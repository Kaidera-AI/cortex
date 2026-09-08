"""Default-deny route contract for the standalone opaque-token boundary."""
import ast
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

SOURCE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("cortex_service_policy_test", SOURCE / "service_auth_policy.py")
policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)


def principal(**changes):
    values = dict(project_key="school-notes", project_id="9b0cfd5c-7208-4a4f-b6ca-c983f354db48", agent="homework-bot", scopes=frozenset({"memory:read", "memory:write", "runtime:read"}))
    values.update(changes)
    return SimpleNamespace(**values)


def test_every_existing_http_route_has_an_explicit_policy():
    tree = ast.parse((SOURCE / "main.py").read_text())
    routes = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        for d in node.decorator_list:
            if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and isinstance(d.func.value, ast.Name) and d.func.value.id == "app" and d.func.attr in {"get", "post", "put", "patch", "delete"}:
                routes.append((d.func.attr.upper(), ast.literal_eval(d.args[0])))
    assert routes
    assert set(routes) <= set(policy.ROUTE_POLICIES)


def test_new_unclassified_route_is_denied_even_to_admin():
    with pytest.raises(HTTPException) as error:
        policy.require_policy("POST", "/new-route", principal(scopes={"instance:admin"}))
    assert error.value.status_code == 403


def test_read_does_not_grant_write_or_registry():
    policy.require_policy("GET", "/search", principal())
    for method, route in [("POST", "/projects"), ("POST", "/handoffs"), ("POST", "/admin/sql/exec")]:
        with pytest.raises(HTTPException) as error:
            policy.require_policy(method, route, principal())
        assert error.value.status_code == 403


@pytest.mark.parametrize("value", ["other-project", "*", "_global", "", "9b0cfd5c-7208-4a4f-b6ca-c983f354db49"])
def test_foreign_or_unscoped_selector_fails(value):
    with pytest.raises(HTTPException) as error:
        policy.require_project_selector(value, principal())
    assert error.value.status_code == 403


def test_project_name_and_uuid_are_the_same_checked_selector():
    context = principal()
    policy.require_project_selector(context.project_key, context)
    policy.require_project_selector(context.project_id, context)


@pytest.mark.parametrize("actor", ["someone-else", "homework-bot@another-project", "homework-bot:deadbeef", ""])
def test_asserted_actor_cannot_impersonate(actor):
    with pytest.raises(HTTPException) as error:
        policy.require_actor_selector(actor, principal())
    assert error.value.status_code == 403


def test_bare_and_compound_canonical_actor_are_allowed():
    policy.require_actor_selector("homework-bot", principal())
    policy.require_actor_selector("homework-bot@school-notes", principal())


def test_registration_target_is_not_automatically_acting_identity():
    # Adding another agent with an explicitly permitted registry token is not
    # impersonating that target agent; write attribution still uses the caller.
    assert "agent" not in policy.ACTOR_BODY_FIELDS.get(("POST", "/agents"), ())
    assert "agent" not in policy.ACTOR_PATH_FIELDS.get(("GET", "/boot/{agent}"), ())
    assert "agent" in policy.ACTOR_PATH_FIELDS[("POST", "/diary/{agent}")]


def test_sensitive_discovery_is_not_public_liveness():
    assert policy.PUBLIC_ROUTES == {("GET", "/health/live")}
    for route in ["/health", "/metrics", "/projects", "/workers/health"]:
        assert ("GET", route) in policy.ROUTE_POLICIES
        assert ("GET", route) not in policy.PUBLIC_ROUTES


def test_unknown_scope_and_wildcard_do_not_implicitly_administer():
    with pytest.raises(HTTPException):
        policy.require_policy("POST", "/admin/sql/exec", principal(scopes={"*", "admin"}))
