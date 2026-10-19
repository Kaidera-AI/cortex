"""Storage-A/DFP-03 contract: exercise real HTTP routes, never a live service.

Provider catalogue/native admission remain Pike's gate. This suite proves that
storage supplies the exact selected data or refuses; it cannot launch anything.
"""
import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

API = Path(__file__).resolve().parents[1]


def selection(**updates):
    values = dict(harness="codex", provider="selected-fixture", model="selected-model",
                  effort="high", fast=False, permission="workspace", autonomy="interactive",
                  router_mode="off", allowed=[])
    values.update(updates)
    return {key: {"mode": "set", "value": value} for key, value in values.items()}


class MemoryConnection:
    """Small SQL-boundary fixture; assertions reject unexpected source behavior."""
    def __init__(self):
        self.rows = {(p, "worker"): {"name": "worker", "role": "developer", "capabilities": {}}
                     for p in ("fixture-a", "fixture-b")}
        self.events = []
        self.writes = []
        self.lock = asyncio.Lock()
        self.fail_audit = False

    @asynccontextmanager
    async def transaction(self):
        # Nested event transactions don't re-acquire the outer row transaction.
        outer = not self.lock.locked()
        if outer:
            await self.lock.acquire()
        before = deepcopy((self.rows, self.events, self.writes))
        try:
            yield self
        except BaseException:
            self.rows, self.events, self.writes = before
            raise
        finally:
            if outer:
                self.lock.release()

    async def fetchrow(self, sql, *args):
        if "FROM agents" in sql:
            return deepcopy(self.rows.get(tuple(args[:2])))
        raise AssertionError(sql)

    async def execute(self, sql, *args):
        if "INSERT INTO agents" in sql:
            self.writes.append((args[2], args[0]))
            return "INSERT 0 1"
        if "UPDATE agents" in sql:
            project, worker, capabilities = args[:3]
            self.rows[(project, worker)]["capabilities"] = json.loads(capabilities)
            self.writes.append((project, worker))
            return "UPDATE 1"
        if "pg_notify" in sql or "set_config" in sql:
            return "SELECT 1"
        raise AssertionError(sql)

    async def fetchval(self, sql, *args):
        if "INSERT INTO team_events" in sql:
            if self.fail_audit:
                raise RuntimeError("injected audit failure")
            self.events.append(dict(project=args[0], actor=args[1], kind=args[2], detail=json.loads(args[4])))
            return len(self.events)
        raise AssertionError(sql)


@pytest.fixture
def runtime(monkeypatch):
    spec = importlib.util.spec_from_file_location("cortex_run_profile_test", API / "main.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    db = MemoryConnection()
    module.ADMIN_TOKEN = "fixture-owner-token"

    @asynccontextmanager
    async def acquire(project, **kwargs):
        assert project in {"fixture-a", "fixture-b"}
        yield db

    async def registered(project):
        assert project in {"fixture-a", "fixture-b"}

    monkeypatch.setattr(module, "acquire_scoped", acquire)
    monkeypatch.setattr(module, "require_registered_project", registered)
    return module, db


def client(module, project="fixture-a", owner=True):
    headers = {"X-Project": project, "X-Agent-Name": "spoofed-actor"}
    if owner:
        headers["X-Cortex-Admin-Token"] = "fixture-owner-token"
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=module.app), base_url="http://fixture", headers=headers)


async def put(c, fields=None, revision=0, **more):
    return await c.put("/agents/worker/run-profile", json={"expected_revision": revision,
                       "fields": selection() if fields is None else fields, **more})


@pytest.mark.asyncio
async def test_unset_refuses_without_model_provider_or_ambient_defaults(runtime, monkeypatch):
    module, db = runtime
    monkeypatch.setenv("OPENAI_MODEL", "ambient-model")
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-never-used")
    db.rows[("fixture-a", "worker")]["model"] = "legacy-model"
    async with client(module) as c:
        r = await c.get("/agents/worker/run-profile")
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "unconfigured"
    assert {"model", "provider"} <= set(r.json()["detail"]["missing"])
    assert db.writes == db.events == []


@pytest.mark.asyncio
async def test_exact_selection_false_off_and_server_attribution_roundtrip(runtime):
    module, db = runtime
    async with client(module) as c:
        r = await put(c)
        assert r.status_code == 200, r.text
        saved = r.json()
        read = await c.get("/agents/worker/run-profile")
    assert read.json() == saved
    assert saved["worker_id"] == "worker@fixture-a"
    assert saved["revision"] == 1
    for key, value in selection().items():
        assert saved["fields"][key]["value"] == value["value"]
        assert saved["fields"][key]["version"] == 1
        assert saved["fields"][key]["set_by"] == {"actor": "local-owner", "source": "user"}
    assert len(db.events) == 1
    assert db.events[0]["actor"] == "local-owner"
    assert db.events[0]["detail"]["after"] == saved


@pytest.mark.asyncio
async def test_catalogue_context_suffix_is_preserved_without_normalizing_model(runtime):
    module, _ = runtime
    async with client(module) as c:
        response = await put(c, selection(harness="claude-code", model="claude-opus-4-8[1m]"))
        assert response.status_code == 200, response.text
        saved = (await c.get("/agents/worker/run-profile")).json()
    assert saved["fields"]["model"]["value"] == "claude-opus-4-8[1m]"


@pytest.mark.asyncio
async def test_cas_preserves_fields_and_rejects_stale_write(runtime):
    module, db = runtime
    async with client(module) as c:
        first = await put(c)
        assert first.status_code == 200, first.text
        second = await put(c, {"effort": {"mode": "set", "value": "low"}}, 1)
        assert second.status_code == 200, second.text
        stale = await put(c, revision=1)
        assert stale.status_code == 409, stale.text
        assert stale.json()["detail"]["code"] == "conflict"
        current = (await c.get("/agents/worker/run-profile")).json()
    assert current["revision"] == 2
    assert current["fields"]["effort"]["version"] == 2
    assert current["fields"]["model"] == first.json()["fields"]["model"]
    assert len(db.events) == len(db.writes) == 2


@pytest.mark.asyncio
async def test_label_changes_no_transport_identity_or_other_project(runtime):
    module, db = runtime
    db.rows[("fixture-a", "worker")]["runtime_state"] = {"claim": "in-flight", "outbox": "pending"}
    async with client(module) as c:
        before = await put(c)
        assert before.status_code == 200, before.text
        r = await put(c, {}, 1, display_label="Renamed Worker")
        assert r.status_code == 200, r.text
        assert r.json()["worker_id"] == before.json()["worker_id"]
        assert r.json()["display_label"] == "Renamed Worker"
        assert (await c.get("/agents/Renamed%20Worker/run-profile")).status_code in {400, 404, 422}
    assert set(db.rows) == {("fixture-a", "worker"), ("fixture-b", "worker")}
    assert db.rows[("fixture-b", "worker")]["capabilities"] == {}
    assert db.rows[("fixture-a", "worker")]["runtime_state"] == {"claim": "in-flight", "outbox": "pending"}


@pytest.mark.asyncio
async def test_worker_and_spoofed_header_cannot_change_permission(runtime):
    module, db = runtime
    async with client(module, owner=False) as c:
        r = await put(c, selection(permission="full"))
    assert r.status_code == 403, r.text
    assert db.writes == db.events == []


@pytest.mark.parametrize("payload", [
    {"fields": selection(), "expected_revision": -1},
    {"fields": selection(), "expected_revision": True},
    {"fields": selection(), "expected_revision": 0, "set_by": "forged"},
    {"fields": {"provider": {"mode": "set", "value": ""}}, "expected_revision": 0},
    {"fields": {"provider": {"mode": "set", "value": None}}, "expected_revision": 0},
    {"fields": {"provider": {"mode": "set", "value": "https://secret.invalid/key"}}, "expected_revision": 0},
    {"fields": {"fast": {"mode": "set", "value": "false"}}, "expected_revision": 0},
    {"fields": {"permission": {"mode": "set", "value": "administrator"}}, "expected_revision": 0},
    {"fields": {"model": {"mode": "set", "value": "model", "version": 900}}, "expected_revision": 0},
    {"fields": {"provider": {"mode": "inherit", "value": "fallback"}}, "expected_revision": 0},
    {"fields": {"unknown": {"mode": "set", "value": "anything"}}, "expected_revision": 0},
    {"fields": {}, "expected_revision": 0},
])
@pytest.mark.asyncio
async def test_invalid_data_never_writes(runtime, payload):
    module, db = runtime
    async with client(module) as c:
        r = await c.put("/agents/worker/run-profile", json=payload)
    assert r.status_code == 422, r.text
    assert db.writes == db.events == []


@pytest.mark.asyncio
async def test_partial_configuration_refuses_and_can_be_completed_with_cas(runtime):
    module, db = runtime
    async with client(module) as c:
        r = await put(c, {"model": selection()["model"]})
        assert r.status_code == 200, r.text
        unset = await c.get("/agents/worker/run-profile")
        assert unset.status_code == 409
        assert "provider" in unset.json()["detail"]["missing"]
        rest = selection()
        rest.pop("model")
        assert (await put(c, rest, 1)).status_code == 200
        read = await c.get("/agents/worker/run-profile")
        assert read.status_code == 200
        assert read.json()["fields"]["model"]["version"] == 1


@pytest.mark.asyncio
async def test_audit_failure_rolls_back_profile_ack(runtime):
    module, db = runtime
    db.fail_audit = True
    async with client(module) as c:
        with pytest.raises(RuntimeError, match="injected audit failure"):
            await put(c)
    assert db.rows[("fixture-a", "worker")]["capabilities"] == {}
    assert db.writes == db.events == []


@pytest.mark.parametrize("protected", ["run_profile", "display_name"])
@pytest.mark.asyncio
async def test_registration_cannot_bypass_profile_cas(runtime, protected, monkeypatch):
    module, db = runtime
    async def writer(*args, **kwargs):
        pass
    monkeypatch.setattr(module, "require_registered_agent_writer", writer)
    monkeypatch.setattr(module, "upsert_role_record", writer)
    async with client(module) as c:
        r = await c.post("/agents", json={"name": "worker", "role": "developer", "capabilities": {protected: {}}})
    assert r.status_code == 422, r.text
    assert db.writes == db.events == []


@pytest.mark.asyncio
async def test_foreign_scoped_identity_never_resolves_by_label(runtime):
    module, db = runtime
    async with client(module) as c:
        r = await c.get("/agents/worker@fixture-b/run-profile")
    assert r.status_code in {400, 403}, r.text
    assert db.writes == db.events == []


@pytest.mark.asyncio
async def test_project_registration_cannot_seed_bypass_profile(runtime):
    module, db = runtime
    async with client(module) as c:
        r = await c.post("/projects", json={"project_key": "fixture-a", "repo_root": "/fixture/a",
            "agents": [{"name": "worker", "capabilities": {"run_profile": {"revision": 99}}}]})
    assert r.status_code == 422, r.text
    assert db.writes == db.events == []


@pytest.mark.asyncio
async def test_corrupt_existing_profile_refuses_instead_of_overwrite(runtime):
    module, db = runtime
    db.rows[("fixture-a", "worker")]["capabilities"]["run_profile"] = {"revision": "bad"}
    async with client(module) as c:
        read = await c.get("/agents/worker/run-profile")
        write = await put(c)
    assert read.status_code == write.status_code == 409
    assert read.json()["detail"]["code"] == write.json()["detail"]["code"] == "invalid_profile"
    assert db.writes == db.events == []


@pytest.mark.asyncio
async def test_modes_do_not_turn_inherit_or_auto_into_fallback_settings(runtime):
    module, db = runtime
    async with client(module) as c:
        r = await put(c, {"provider": {"mode": "inherit"}, "model": {"mode": "auto"}})
        assert r.status_code == 200, r.text
        assert r.json()["fields"]["provider"]["value"] is None
        assert r.json()["fields"]["model"]["value"] is None
        unset = await c.get("/agents/worker/run-profile")
        assert unset.status_code == 409
    assert len(db.writes) == 1


@pytest.mark.parametrize("field", ["permission", "autonomy", "router_mode", "allowed"])
@pytest.mark.asyncio
async def test_router_cannot_choose_authority_fields(runtime, field):
    module, db = runtime
    async with client(module) as c:
        r = await put(c, {field: {"mode": "auto"}})
    assert r.status_code == 422
    assert db.writes == db.events == []


@pytest.mark.asyncio
async def test_malformed_vault_profile_refuses_before_import_storage(runtime):
    module, db = runtime
    async with client(module) as c:
        r = await c.post("/admin/projects/fixture-a/import", json={
            "format": "kaidera.cortex-project.v1",
            "source_project": {"project_key": "fixture-a", "project_id": "00000000-0000-0000-0000-000000000001"},
            "tables": [{"schema_name": "public", "table_name": "agents", "rows": [{"name": "worker", "capabilities": {"run_profile": {"schema_version": 1, "revision": -1}}}]}]})
    assert r.status_code == 422, r.text
    assert db.writes == db.events == []


@pytest.mark.parametrize("model,values", [
    ("LogRequest", {"event_type": "run_profile_changed", "summary": "forged"}),
    ("SyncEvent", {"event_type": "run_profile_changed", "summary": "forged", "agent_name": "worker"}),
])
def test_public_event_writers_cannot_forge_profile_audit(runtime, model, values):
    from pydantic import ValidationError
    module, db = runtime
    with pytest.raises(ValidationError):
        getattr(module, model)(**values)
