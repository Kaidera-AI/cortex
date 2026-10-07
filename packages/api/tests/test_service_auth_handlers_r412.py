"""Canonical handlers behind the real admission boundary; SQL is in-memory only.

No lifespan, live pool, key, provider or network. Real route handlers, registry
validators, approval validation, dedupe and persistence/readback are exercised.
Roster policy loading and event publication have explicit isolated seams.
"""
import asyncio
from copy import deepcopy
from dataclasses import replace
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

from fastapi import FastAPI
import pytest

from service_auth_http import ServiceAuthMiddleware
from test_service_auth_http import Exchange, Store, TOKEN

APPROVAL = "e60b027e-4007-44f1-8009-8b6ee7c36291"
HANDOFF = "40000000-0000-0000-0000-000000000001"


class Context:
    def __init__(self, value): self.value = value
    async def __aenter__(self): return self.value
    async def __aexit__(self, *args): return False


class SQL:
    def __init__(self):
        self.calls = []
        self.handoff = None
        self.events = []
        self.projects = {}
        for key in ("school-notes", "other-school"):
            root = {"path": "/PUBLIC/" + key, "kind": "primary"}
            self.projects[key] = {"project_key": key, "project_id": key,
                "repo_root": root["path"], "metadata": {"roots": [root]}, "status": "active"}
        self.decision = {"id": APPROVAL, "agent_name": "homework-bot@school-notes", "summary": "",
            "metadata": {"cross_project_handoff": {"approved_by": "cto",
                "source_project": "school-notes", "source_agent": "homework-bot",
                "target_project": "other-school", "target_agent": "other-bot", "max_handoffs": 1}}}

    def acquire(self): return Context(self)
    def transaction(self): return Context(self)
    async def execute(self, sql, *args):
        self.calls.append(("execute", sql, args))
        assert "pg_advisory_xact_lock" in sql
        return "SELECT 1"

    async def fetch(self, sql, *args):
        self.calls.append(("fetch", sql, args))
        if "FROM cortex_projects p" in sql:
            assert "($1::text IS NULL OR p.project_key = $1)" in sql
            return [deepcopy(v) for k, v in self.projects.items() if args[0] is None or k == args[0]]
        if "FROM cortex_project_paths" in sql:
            assert "($1::text IS NULL OR project_key = $1)" in sql
            return [{"project_key": k, "root_path": v["repo_root"], "path_kind": "primary", "metadata": {}}
                    for k, v in self.projects.items() if args[0] is None or k == args[0]]
        raise AssertionError("unexpected SQL read")

    async def fetchrow(self, sql, *args):
        self.calls.append(("fetchrow", sql, args))
        if "FROM cortex_projects" in sql:
            return deepcopy(self.projects.get(args[0]))
        if "FROM decisions" in sql:
            assert args == ("school-notes", APPROVAL) and "invalidated_at IS NULL" in sql
            return deepcopy(self.decision)
        if "FROM handoffs" in sql:
            if "cross_project_relay" in sql:
                assert args == ("other-school", APPROVAL)
                return deepcopy(self.handoff)
            if "status = ANY" in sql:
                if self.handoff and self.handoff["summary"] == args[7]:
                    return deepcopy(self.handoff)
                return None
            if "WHERE id = $1 AND project = $2" in sql:
                assert args == (UUID(HANDOFF), "other-school")
                return deepcopy(self.handoff)
        raise AssertionError("unexpected SQL row read")

    async def fetchval(self, sql, *args):
        self.calls.append(("fetchval", sql, args))
        assert "INSERT INTO handoffs" in sql and self.handoff is None
        keys = ("project", "from_agent", "from_role", "to_role", "to_agent", "priority", "summary",
                "branch", "files_changed", "verification", "next_steps", "context", "parent_goal_id",
                "acceptance", "evidence", "retry", "escalation")
        self.handoff = dict(zip(keys, args), id=HANDOFF, status="pending")
        return UUID(HANDOFF)


@pytest.fixture
def setup(monkeypatch):
    spec = importlib.util.spec_from_file_location("r412_canonical_handlers", Path(__file__).resolve().parents[1] / "main.py")
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    monkeypatch.setattr(api, "CORTEX_SERVICE_AUTH_ENABLED", True)
    sql = SQL()
    monkeypatch.setattr(api, "pool_admin", sql)
    monkeypatch.setattr(api, "acquire_scoped", lambda project: Context(sql))
    allowed = {"school-notes": {"homework-bot"}, "other-school": {"other-bot"}}

    async def roster(project):
        return SimpleNamespace(enforce=True, work_writers=allowed.get(project, set()),
            system_event_writers=set(), handoff_targets=allowed.get(project, set()),
            beat_may_create_handoff=False, suggest_cutoff=0.6)
    async def event(conn, **kwargs): sql.events.append(kwargs)
    monkeypatch.setattr(api, "load_roster_policy", roster)
    monkeypatch.setattr(api, "emit_handoff_lifecycle_event", event)
    router = FastAPI()
    router.add_api_route("/projects", api.list_projects, methods=["GET"])
    router.add_api_route("/handoffs/cross-project", api.create_cross_project_handoff, methods=["POST"])
    store = Store()
    adapter = ServiceAuthMiddleware(router, router.router, lambda: store, transport_is_verified=lambda scope: True)
    return api, sql, store, adapter, allowed


def run(wire, adapter): return asyncio.run(wire.run(adapter))


def relay(adapter, *, summary="PUBLIC relay", approval=APPROVAL, **body_changes):
    body = {"target_project": "other-school", "to_agent": "other-bot@other-school",
            "from_role": "lead", "to_role": "worker", "summary": summary, **body_changes}
    headers = [(b"authorization", ("Bearer " + TOKEN).encode()), (b"content-type", b"application/json")]
    if approval is not None: headers.append((b"x-cortex-cto-override", approval.encode()))
    return run(Exchange("/handoffs/cross-project", "POST", headers=headers,
                        body=json.dumps(body).encode()), adapter)


@pytest.mark.parametrize("admin", (False, True))
def test_projects_handler_filters_project_rows_and_roots(setup, admin):
    _, sql, store, adapter, _ = setup
    scopes = {"registry:read"} | ({"instance:admin"} if admin else set())
    store.current = replace(store.current, scopes=frozenset(scopes))
    response = run(Exchange("/projects"), adapter)
    assert response.status == 200
    rows = json.loads(response.body)["projects"]
    expected = {"school-notes", "other-school"} if admin else {"school-notes"}
    assert {p["project_key"] for p in rows} == expected
    assert all(p["roots"] == [{"path": "/PUBLIC/" + p["project_key"], "kind": "primary"}]
               and "metadata" not in p for p in rows)
    assert [args[0] for kind, query, args in sql.calls if kind == "fetch"] == ([None, None] if admin else ["school-notes", "school-notes"])


@pytest.mark.parametrize("failure", ("unverified", "no-scope", "admin-only"))
def test_projects_denies_before_any_database_effect(setup, failure):
    _, sql, store, adapter, _ = setup
    if failure != "unverified":
        store.current = replace(store.current, scopes=frozenset({"instance:admin"} if failure == "admin-only" else set()))
    response = run(Exchange("/projects", headers=[] if failure == "unverified" else None), adapter)
    assert response.status == (401 if failure == "unverified" else 403)
    assert sql.calls == []


def test_verified_admin_relay_preserves_exact_boundary_and_consumes_approval(setup):
    _, sql, _, adapter, _ = setup
    response = relay(adapter)
    assert response.status == 200
    assert json.loads(response.body)["id"] == HANDOFF
    assert sql.handoff["project"] == "other-school"
    assert sql.handoff["from_agent"] == "homework-bot@school-notes"
    assert sql.handoff["to_agent"] == "other-bot@other-school"
    evidence = json.loads(sql.handoff["evidence"])["cross_project_relay"]
    assert evidence["approval_decision_id"] == APPROVAL and evidence["source_project"] == "school-notes"
    assert sql.events[0]["actor"] == "system@other-school"
    assert len([x for x in sql.calls if x[0] == "fetchval"]) == 1


@pytest.mark.parametrize("failure", ("coordination-only", "no-admin-self-authored", "missing", "invalid",
    "wrong-target", "wrong-source", "unregistered-source", "unregistered-target", "unregistered-project"))
def test_relay_denies_without_insert_or_event(setup, failure):
    _, sql, store, adapter, allowed = setup
    approval = APPROVAL
    if failure in {"coordination-only", "no-admin-self-authored"}:
        store.current = replace(store.current, scopes=frozenset({"coordination:write"}))
    elif failure == "missing": approval = None
    elif failure == "invalid": approval = "not-a-decision"
    elif failure in {"wrong-target", "wrong-source"}:
        key = "target_project" if failure == "wrong-target" else "source_project"
        sql.decision["metadata"]["cross_project_handoff"][key] = "unapproved-project"
    elif failure == "unregistered-source": allowed["school-notes"] = set()
    elif failure == "unregistered-target": allowed["other-school"] = set()
    else: del sql.projects["other-school"]
    response = relay(adapter, approval=approval)
    assert response.status in {403, 404}
    assert sql.handoff is None and sql.events == []
    assert not [x for x in sql.calls if x[0] == "fetchval"]
    if failure in {"coordination-only", "no-admin-self-authored"}: assert sql.calls == []


@pytest.mark.parametrize("same", (True, False))
def test_one_use_approval_refuses_identical_and_changed_replay(setup, same):
    _, sql, _, adapter, _ = setup
    assert relay(adapter).status == 200
    response = relay(adapter, summary="PUBLIC relay" if same else "PUBLIC second relay")
    assert response.status == 409
    assert len([x for x in sql.calls if x[0] == "fetchval"]) == 1
    assert len(sql.events) == 1
