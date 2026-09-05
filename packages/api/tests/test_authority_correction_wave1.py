from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from starlette.requests import Request


API_MAIN_PATH = Path(__file__).resolve().parents[1] / "main.py"
HANDOFF_ID = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"


def load_module():
    spec = importlib.util.spec_from_file_location(
        "cortex_api_authority_correction_wave1",
        API_MAIN_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def actor_request(*, agent: str | None = "kai", legacy_agent: str | None = None) -> Request:
    request = Request({"type": "http", "headers": []})
    claims = {"project": "project-alpha"}
    if agent is not None:
        claims["agent"] = agent
    if legacy_agent is not None:
        claims["agent_name"] = legacy_agent
    request.state.jwt_claims = claims
    return request


def admin_request(token: str = "test-admin-token") -> Request:
    request = Request(
        {
            "type": "http",
            "headers": [(b"x-cortex-admin-token", token.encode("utf-8"))],
        }
    )
    request.state.jwt_claims = {}
    return request


class FakeTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeAcquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeConn:
    def __init__(self):
        self.executed: list[tuple[str, tuple]] = []
        self.bound: list[tuple[str, tuple]] = []

    def transaction(self):
        return FakeTransaction()

    async def execute(self, sql, *args):
        self.executed.append((sql, args))
        return "UPDATE 1"

    async def fetchrow(self, sql, *args):
        self.bound.append((sql, args))
        return {
            "id": "binding-id",
            "project": args[0],
            "subject_kind": args[1],
            "subject": args[2],
            "skill_slug": args[3],
            "binding_type": args[4],
            "priority": args[5],
            "version_pin": args[6],
            "created_at": "2026-08-29T00:00:00Z",
        }


@pytest.fixture
def api(monkeypatch):
    module = load_module()
    conn = FakeConn()
    calls = {"projects": [], "writers": [], "events": []}

    async def require_registered_project(project):
        calls["projects"].append(project)
        return {"project_key": project}

    async def require_registered_agent_writer(project, agent, scope="work"):
        calls["writers"].append((project, agent, scope))

    async def compound_agent(agent, project):
        return f"{agent}@{project}"

    async def emit_event(_conn, **kwargs):
        calls["events"].append(kwargs)

    monkeypatch.setattr(module, "require_registered_project", require_registered_project)
    monkeypatch.setattr(module, "require_registered_agent_writer", require_registered_agent_writer)
    monkeypatch.setattr(module, "compound_agent", compound_agent)
    monkeypatch.setattr(module, "emit_handoff_lifecycle_event", emit_event)
    monkeypatch.setattr(module, "acquire_scoped", lambda _project: FakeAcquire(conn))
    return module, conn, calls


@pytest.mark.asyncio
async def test_skill_binding_rejects_body_project_escape_before_authorization(api):
    module, conn, calls = api

    with pytest.raises(module.HTTPException) as exc:
        await module.bind_skill(
            "open-code-review",
            module.SkillBind(subject_kind="role", subject="developer", project="project-beta"),
            actor_request(),
            x_agent="kai",
            x_project="project-alpha",
        )

    assert exc.value.status_code == 403
    assert "Body project does not match" in exc.value.detail
    assert calls["projects"] == []
    assert calls["writers"] == []
    assert conn.bound == []


@pytest.mark.asyncio
async def test_skill_binding_stays_in_authenticated_project(api):
    module, conn, calls = api

    result = await module.bind_skill(
        "open-code-review",
        module.SkillBind(subject_kind="role", subject="developer", project="project-alpha"),
        actor_request(),
        x_agent="kai",
        x_project="project-alpha",
    )

    assert result["project"] == "project-alpha"
    assert calls["projects"] == ["project-alpha"]
    assert calls["writers"] == [("project-alpha", "kai", "work")]
    assert conn.bound[0][1][0] == "project-alpha"


def test_bearer_uses_agent_claim_and_rejects_conflicting_alias(api):
    module, _conn, _calls = api
    module.require_jwt_actor_match(actor_request(agent="kai"), "kai", "project-alpha")

    with pytest.raises(module.HTTPException) as mismatch:
        module.require_jwt_actor_match(
            actor_request(agent="kai", legacy_agent="ren"),
            "kai",
            "project-alpha",
        )
    assert mismatch.value.status_code == 403
    assert "claims disagree" in mismatch.value.detail

    with pytest.raises(module.HTTPException) as missing:
        module.require_jwt_actor_match(
            actor_request(agent=None),
            "kai",
            "project-alpha",
        )
    assert missing.value.status_code == 403
    assert "does not identify" in missing.value.detail


def handoff_row(*, status="claimed", claimed_by="kai@project-alpha"):
    return {
        "id": HANDOFF_ID,
        "status": status,
        "kind": "task",
        "from_agent": "ren@project-alpha",
        "from_role": "cpo",
        "to_role": "developer",
        "to_agent": "kai",
        "priority": "high",
        "summary": "authority correction",
        "claimed_by": claimed_by,
        "claimed_at": "2026-08-29T00:00:00Z",
        "retry_count": 0,
        "terminal_reason": None,
    }


def install_handoff_row(monkeypatch, module, row):
    async def resolve(_conn, *, project, handoff_id):
        assert project == "project-alpha"
        assert handoff_id == "aaaaaaaa"
        return row

    async def locked(_conn, *, project, handoff_id):
        assert project == "project-alpha"
        assert handoff_id == HANDOFF_ID
        return row

    monkeypatch.setattr(module, "resolve_unique_handoff_for_mutation", resolve)
    monkeypatch.setattr(module, "locked_handoff_row", locked)


@pytest.mark.asyncio
@pytest.mark.parametrize("route_name", ["release_handoff", "abandon_handoff", "fail_handoff"])
async def test_terminal_routes_reject_non_claimant(api, monkeypatch, route_name):
    module, conn, calls = api
    install_handoff_row(monkeypatch, module, handoff_row(claimed_by="ren@project-alpha"))

    route = getattr(module, route_name)
    with pytest.raises(module.HTTPException) as exc:
        await route(
            "aaaaaaaa",
            request=actor_request(agent="kai"),
            body=module.HandoffTerminate(reason="not mine"),
            x_agent="kai",
            x_project="project-alpha",
        )

    assert exc.value.status_code == 403
    assert "current claimant" in exc.value.detail
    assert conn.executed == []
    assert calls["events"] == []


@pytest.mark.asyncio
async def test_terminal_route_rejects_pending_handoff(api, monkeypatch):
    module, conn, calls = api
    install_handoff_row(monkeypatch, module, handoff_row(status="pending", claimed_by=None))

    with pytest.raises(module.HTTPException) as exc:
        await module.abandon_handoff(
            "aaaaaaaa",
            request=actor_request(agent="kai"),
            body=module.HandoffTerminate(reason="skip claim"),
            x_agent="kai",
            x_project="project-alpha",
        )

    assert exc.value.status_code == 409
    assert "not in 'claimed' state" in exc.value.detail
    assert conn.executed == []
    assert calls["events"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("route_name", ["release_handoff", "abandon_handoff", "fail_handoff"])
async def test_terminal_routes_bind_update_to_locked_claimant(api, monkeypatch, route_name):
    module, conn, calls = api
    install_handoff_row(monkeypatch, module, handoff_row())

    result = await getattr(module, route_name)(
        "aaaaaaaa",
        request=actor_request(agent="kai"),
        body=module.HandoffTerminate(reason="owned transition"),
        x_agent="kai",
        x_project="project-alpha",
    )

    assert result
    assert len(conn.executed) == 1
    sql, args = conn.executed[0]
    assert "split_part(COALESCE(claimed_by" in sql
    assert args[3] is False
    assert args[4] == "kai"
    assert calls["events"][0]["actor"] == "kai@project-alpha"


@pytest.mark.asyncio
@pytest.mark.parametrize("route_name", ["release_handoff", "abandon_handoff", "fail_handoff"])
async def test_terminal_routes_allow_explicit_admin_recovery_authority(
    api, monkeypatch, route_name
):
    module, conn, calls = api
    monkeypatch.setattr(module, "ADMIN_TOKEN", "test-admin-token")
    install_handoff_row(monkeypatch, module, handoff_row(claimed_by="ren@project-alpha"))

    result = await getattr(module, route_name)(
        "aaaaaaaa",
        request=admin_request(),
        body=module.HandoffTerminate(reason="operator recovery"),
        x_agent="",
        x_project="project-alpha",
    )

    assert result
    _sql, args = conn.executed[0]
    assert args[3] is True
    assert args[4] == ""
    assert calls["writers"] == []
    assert calls["events"][0]["actor"] == "cortex-admin@project-alpha"


@pytest.mark.asyncio
async def test_terminal_route_rejects_missing_claimant_and_admin(api, monkeypatch):
    module, conn, calls = api
    install_handoff_row(monkeypatch, module, handoff_row())

    with pytest.raises(module.HTTPException) as exc:
        await module.release_handoff(
            "aaaaaaaa",
            request=actor_request(agent=None),
            body=module.HandoffTerminate(reason="anonymous"),
            x_agent="",
            x_project="project-alpha",
        )

    assert exc.value.status_code == 403
    assert "claimant or a valid admin token" in exc.value.detail
    assert conn.executed == []
    assert calls["events"] == []


@pytest.mark.asyncio
async def test_terminal_route_rejects_bearer_header_actor_mismatch(api, monkeypatch):
    module, conn, calls = api
    install_handoff_row(monkeypatch, module, handoff_row())

    with pytest.raises(module.HTTPException) as exc:
        await module.fail_handoff(
            "aaaaaaaa",
            request=actor_request(agent="ren"),
            body=module.HandoffTerminate(reason="forged header"),
            x_agent="kai",
            x_project="project-alpha",
        )

    assert exc.value.status_code == 403
    assert "does not match" in exc.value.detail
    assert conn.executed == []
    assert calls["events"] == []
