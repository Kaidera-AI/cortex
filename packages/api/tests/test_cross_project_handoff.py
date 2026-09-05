"""CTO-approved cross-project handoff relay tests."""

import importlib.util
from pathlib import Path
from uuid import UUID

import pytest


API_MAIN_PATH = Path(__file__).resolve().parents[1] / "main.py"
MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "data"
    / "migrations"
    / "2026-07-15-cross-project-handoff-relay.sql"
)
FULL_SCHEMA_PATH = (
    Path(__file__).resolve().parents[2] / "data" / "cortex-schema-full.sql"
)
APPROVAL_ID = "e60b027e-4007-44f1-8009-8b6ee7c36291"


def load_api_module():
    spec = importlib.util.spec_from_file_location(
        "cortex_api_cross_project_handoff_test",
        API_MAIN_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _stub_request(admin_token: str = "unit-test-admin"):
    """The route takes a FastAPI Request first; SEC-01 admin-gates it."""
    from starlette.requests import Request

    return Request({
        "type": "http",
        "method": "POST",
        "path": "/handoffs/cross-project",
        "raw_path": b"/handoffs/cross-project",
        "query_string": b"",
        "headers": [(b"x-cortex-admin-token", admin_token.encode())],
    })


class AsyncContext:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, exc_type, exc, traceback):
        return False


@pytest.mark.asyncio
async def test_cross_project_route_keeps_source_and_target_rosters_separate(
    monkeypatch,
):
    api = load_api_module()
    api.ADMIN_TOKEN = "unit-test-admin"
    calls = []
    captured = {}

    async def require_project(project):
        calls.append(("project", project))

    async def require_writer(project, agent, *, scope="work"):
        calls.append(("writer", project, agent, scope))

    async def require_target(project, agent):
        calls.append(("target", project, agent))
        return "scribe"

    async def require_approval(**kwargs):
        calls.append(("approval", kwargs))
        return APPROVAL_ID

    async def persist(**kwargs):
        captured.update(kwargs)
        return {
            "id": "handoff-id",
            "status": "pending",
            "verified": True,
            "deduped": False,
        }

    monkeypatch.setattr(api, "require_registered_project", require_project)
    monkeypatch.setattr(api, "require_registered_agent_writer", require_writer)
    monkeypatch.setattr(api, "require_registered_handoff_target", require_target)
    monkeypatch.setattr(api, "require_cross_project_handoff_approval", require_approval)
    monkeypatch.setattr(api, "persist_handoff_record", persist)

    body = api.CrossProjectHandoffCreate(
        target_project="kaidera",
        from_role="cpo",
        to_role="knowledge-keeper",
        to_agent="scribe@kaidera",
        priority="high",
        summary="Update the Kaidera OS website",
        evidence={"release_ready": False},
    )
    result = await api.create_cross_project_handoff(
        _stub_request(),
        body,
        x_agent="ren@kaidera-os",
        x_project="kaidera-os",
        x_cto_override=APPROVAL_ID,
    )

    assert ("writer", "kaidera-os", "ren", "work-handoff") in calls
    assert ("target", "kaidera", "scribe@kaidera") in calls
    assert captured["project"] == "kaidera"
    assert captured["from_agent"] == "ren@kaidera-os"
    assert captured["to_agent"] == "scribe@kaidera"
    assert captured["single_use_approval_id"] == APPROVAL_ID
    assert captured["body"].evidence["release_ready"] is False
    assert captured["body"].evidence["cross_project_relay"] == {
        "schema_version": 1,
        "approval_decision_id": APPROVAL_ID,
        "source_project": "kaidera-os",
        "source_agent": "ren@kaidera-os",
        "target_project": "kaidera",
        "target_agent": "scribe@kaidera",
    }
    assert result["source_project"] == "kaidera-os"
    assert result["target_project"] == "kaidera"


@pytest.mark.asyncio
async def test_cross_project_route_requires_explicit_target_agent():
    api = load_api_module()
    api.ADMIN_TOKEN = "unit-test-admin"
    body = api.CrossProjectHandoffCreate(
        target_project="kaidera",
        to_role="knowledge-keeper",
        summary="Update the website",
    )

    with pytest.raises(api.HTTPException) as exc_info:
        await api.create_cross_project_handoff(
            _stub_request(),
            body,
            x_agent="ren",
            x_project="kaidera-os",
            x_cto_override=APPROVAL_ID,
        )

    assert exc_info.value.status_code == 400
    assert "explicit to_agent" in str(exc_info.value.detail)


@pytest.mark.asyncio
async def test_legacy_cto_decision_authorizes_only_its_exact_relay(monkeypatch):
    api = load_api_module()
    decision = {
        "agent_name": "ren@kaidera-os",
        "summary": (
            "CTO authorized ren@kaidera-os to create one cross-project handoff "
            "in project kaidera for scribe@kaidera to update the website."
        ),
        "metadata": {},
    }

    class DecisionConn:
        async def fetchrow(self, sql, *args):
            assert args == ("kaidera-os", APPROVAL_ID)
            return decision

    monkeypatch.setattr(
        api, "acquire_scoped", lambda project: AsyncContext(DecisionConn())
    )

    approved = await api.require_cross_project_handoff_approval(
        approval_decision_id=APPROVAL_ID,
        source_project="kaidera-os",
        source_agent="ren",
        target_project="kaidera",
        target_agent="scribe",
    )
    assert approved == APPROVAL_ID

    with pytest.raises(api.HTTPException) as exc_info:
        await api.require_cross_project_handoff_approval(
            approval_decision_id=APPROVAL_ID,
            source_project="kaidera-os",
            source_agent="ren",
            target_project="kaidera",
            target_agent="bob",
        )
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_cross_project_approval_is_single_use(monkeypatch):
    api = load_api_module()

    class Transaction:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    class HandoffConn:
        def transaction(self):
            return Transaction()

        async def execute(self, sql, *args):
            return "SELECT 1"

        async def fetchrow(self, sql, *args):
            assert "cross_project_relay" in sql
            return {"id": "existing-handoff", "status": "completed"}

        async def fetchval(self, sql, *args):
            raise AssertionError("a consumed approval must not insert another handoff")

    async def no_duplicate(*args, **kwargs):
        return None

    monkeypatch.setattr(
        api, "acquire_scoped", lambda project: AsyncContext(HandoffConn())
    )
    monkeypatch.setattr(api, "find_equal_open_handoff", no_duplicate)

    body = api.HandoffCreate(
        from_role="cpo",
        to_role="knowledge-keeper",
        to_agent="scribe@kaidera",
        summary="A different relay using the same approval",
        evidence={
            "cross_project_relay": {
                "approval_decision_id": APPROVAL_ID,
            }
        },
    )
    with pytest.raises(api.HTTPException) as exc_info:
        await api.persist_handoff_record(
            project="kaidera",
            from_agent="ren@kaidera-os",
            to_agent="scribe@kaidera",
            body=body,
            single_use_approval_id=APPROVAL_ID,
        )

    assert exc_info.value.status_code == 409
    assert "already consumed" in str(exc_info.value.detail)


@pytest.mark.asyncio
async def test_cross_project_lifecycle_event_uses_destination_system(monkeypatch):
    api = load_api_module()
    emitted = {}

    class Transaction:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    class HandoffConn:
        def transaction(self):
            return Transaction()

        async def execute(self, sql, *args):
            return "SELECT 1"

        async def fetchrow(self, sql, *args):
            return None

        async def fetchval(self, sql, *args):
            return UUID("11111111-1111-4111-8111-111111111111")

    async def no_duplicate(*args, **kwargs):
        return None

    async def verify(*args, **kwargs):
        return None

    async def emit(*args, **kwargs):
        emitted.update(kwargs)
        return 1

    monkeypatch.setattr(
        api, "acquire_scoped", lambda project: AsyncContext(HandoffConn())
    )
    monkeypatch.setattr(api, "find_equal_open_handoff", no_duplicate)
    monkeypatch.setattr(api, "verify_handoff_persisted", verify)
    monkeypatch.setattr(api, "emit_handoff_lifecycle_event", emit)

    body = api.HandoffCreate(
        from_role="cpo",
        to_role="knowledge-keeper",
        to_agent="scribe@kaidera",
        summary="Update the website",
        evidence={
            "cross_project_relay": {
                "approval_decision_id": APPROVAL_ID,
            }
        },
    )
    result = await api.persist_handoff_record(
        project="kaidera",
        from_agent="ren@kaidera-os",
        to_agent="scribe@kaidera",
        body=body,
        single_use_approval_id=APPROVAL_ID,
    )

    assert result["id"] == "11111111-1111-4111-8111-111111111111"
    assert emitted["actor"] == "system@kaidera"
    assert emitted["handoff"]["from_agent"] == "ren@kaidera-os"


def test_identity_trigger_preserves_only_validated_cross_project_sender():
    migration = MIGRATION_PATH.read_text(encoding="utf-8")
    full_schema = FULL_SCHEMA_PATH.read_text(encoding="utf-8")

    for source in (migration, full_schema):
        assert "NEW.evidence->'cross_project_relay'" in source
        assert "relay_source_project = cp.project_key" in source
        assert "relay_target_project <> cp.project_key" in source
        assert "relay_source_agent <> cortex_identity_display" in source
        assert "relay_target_agent <> cortex_identity_display" in source
        assert "source_cp.project_key" in source
        assert "Cross-project relay requires a destination agent" in source
