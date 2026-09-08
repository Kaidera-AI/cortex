"""Flagged registry rows cannot acquire startup context through retained profiles.

Repair-plan item 5, approved at 4780865579 by the owner (2026-09-07).
All storage and event delivery below are finite in-memory fakes. The caller uses
a deliberately non-enforcing policy: an unrelated writer gate must not make the
new eligibility negatives pass before the actual bug is fixed.
"""

from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

from fastapi import HTTPException
import pytest
from starlette.requests import Request


API_PATH = Path(__file__).resolve().parents[1] / "main.py"
ENDPOINTS = ("boot", "bootstrap", "persona")


class Registry:
    def __init__(self, *, capabilities, profile=True, project="kaidera-os", row=True):
        self.project = project
        self.agent = "ren-cx"
        self.rows = {}
        self.profiles = {}
        self.context_reads = []
        self.profile_reads = []
        self.events = []
        self.closed = 0
        self.fail_registry = False
        if row:
            self.rows[(project, self.agent)] = {
                "name": self.agent,
                "agent_name": self.agent,
                "role": "cpo",
                "model": "test-model",
                "capabilities": deepcopy(capabilities),
            }
        if profile:
            self.profiles[(project, self.agent)] = {
                "agent_name": self.agent,
                "role": "legacy-role",
                "profile_kind": "identity",
                "profile_text": "Retained historical identity text.",
                "metadata": {},
                "updated_at": datetime(2026, 9, 7, tzinfo=timezone.utc),
            }

    async def fetchrow(self, sql, *args):
        if "FROM agents" in sql:
            assert "project = $1" in sql and "lower(name) = $2" in sql
            if self.fail_registry:
                raise RuntimeError("synthetic registry read failure")
            return deepcopy(self.rows.get(tuple(args[:2])))
        if "FROM agent_profiles" in sql:
            assert "project = $1" in sql and "lower(agent_name) = $2" in sql
            self.profile_reads.append(tuple(args[:2]))
            return deepcopy(self.profiles.get(tuple(args[:2])))
        if "SELECT default_agent FROM cortex_projects" in sql:
            self.context_reads.append("default-agent")
            return {"default_agent": "kai"}
        raise AssertionError("unexpected synthetic fetchrow")

    async def fetch(self, sql, *args):
        assert sql.lstrip().startswith("SELECT")
        assert args and args[0] in {"kaidera-os", "other-project"}
        self.context_reads.append(sql)
        if "SELECT role, capabilities" in sql:
            row = self.rows.get(tuple(args[:2]))
            return [{"role": row["role"], "capabilities": row["capabilities"]}] if row else []
        return []

    async def fetchval(self, sql, *args):
        assert sql.lstrip().startswith("SELECT")
        self.context_reads.append(sql)
        return False

    async def execute(self, *_args):
        raise AssertionError("startup eligibility must not mutate synthetic storage")


@pytest.fixture
def api(monkeypatch):
    spec = importlib.util.spec_from_file_location("cortex_persona_eligibility_test", API_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    async def project(key):
        return {"project_key": key, "display_name": key, "repo_root": "/synthetic"}

    async def policy(_key):
        return SimpleNamespace(
            enforce=False, work_writers=frozenset(), read_only=frozenset(),
            system_event_writers=frozenset(), roles={},
        )

    async def metadata(_key):
        return {}

    monkeypatch.setattr(module, "require_registered_project", project)
    monkeypatch.setattr(module, "load_roster_policy", policy)
    monkeypatch.setattr(module, "fetch_project_metadata", metadata)
    return module


def wire(api, monkeypatch, registry):
    @asynccontextmanager
    async def scoped(project):
        assert project in {"kaidera-os", "other-project"}
        try:
            yield registry
        finally:
            registry.closed += 1

    async def event(conn, **kwargs):
        assert conn is registry
        registry.events.append(deepcopy(kwargs))

    monkeypatch.setattr(api, "acquire_scoped", scoped)
    monkeypatch.setattr(api, "emit_team_event", event)


async def start(api, endpoint, *, project="kaidera-os"):
    if endpoint == "boot":
        request = Request({"type": "http", "query_string": b"budget=500"})
        return await api.boot("ren-cx", request, x_project=project, query=None)
    if endpoint == "bootstrap":
        return await api.bootstrap("ren-cx", x_project=project)
    return await api.get_agent_persona("ren-cx", x_project=project)


FLAGGED = (
    {"visibility": "history-only", "keep_visible": True},
    {"visibility": "active", "keep_visible": True, "transient": True},
    {"visibility": "active", "keep_visible": True, "transient": "true"},
    json.dumps({"visibility": "history-only", "transient": True}),
)


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ENDPOINTS)
@pytest.mark.parametrize("profile", [False, True], ids=["role-derived", "retained-profile"])
@pytest.mark.parametrize("capabilities", FLAGGED, ids=["history", "transient", "legacy-true", "json-flags"])
async def test_flagged_persona_refused_before_profile_context_or_events(api, monkeypatch, endpoint, profile, capabilities):
    registry = Registry(capabilities=capabilities, profile=profile)
    original = deepcopy((registry.rows, registry.profiles))
    wire(api, monkeypatch, registry)

    with pytest.raises(HTTPException) as error:
        await start(api, endpoint)

    assert error.value.status_code == 403
    assert registry.profile_reads == []
    assert registry.context_reads == []
    assert registry.events == []
    assert registry.closed == 1
    assert (registry.rows, registry.profiles) == original


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ENDPOINTS)
@pytest.mark.parametrize("transient", [False, "false", None], ids=["false", "legacy-false", "null"])
async def test_active_role_derived_persona_and_codex_harness_preserved(api, monkeypatch, endpoint, transient):
    registry = Registry(capabilities={"keep_visible": True, "transient": transient, "harness": "codex"}, profile=False)
    wire(api, monkeypatch, registry)
    result = await start(api, endpoint)

    assert registry.closed == 1
    assert registry.context_reads
    if endpoint == "boot":
        assert set(result) == {"boot", "surface_version", "persona"}
        assert result["persona"]["role"] == "cpo"
    elif endpoint == "bootstrap":
        assert result["agent"] == "ren-cx"
        assert "ren-cx@kaidera-os, cpo" in result["text"]
    else:
        assert result["role"] == "cpo"
        assert result["harness"]["default"] == "codex"
    assert len(registry.events) == (1 if endpoint == "bootstrap" else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ENDPOINTS)
async def test_profile_only_legacy_compatibility_unchanged(api, monkeypatch, endpoint):
    registry = Registry(capabilities={}, row=False)
    wire(api, monkeypatch, registry)
    result = await start(api, endpoint)
    assert "legacy-role" in json.dumps(result)
    assert registry.closed == 1


@pytest.mark.asyncio
async def test_boot_current_registry_role_still_wins_over_stale_profile(api, monkeypatch):
    registry = Registry(capabilities={})
    wire(api, monkeypatch, registry)
    result = await start(api, "boot")
    assert result["persona"]["role"] == "cpo"
    assert "Role: cpo." in result["boot"]
    assert "Role: legacy-role." not in result["boot"]


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ENDPOINTS)
async def test_same_name_active_elsewhere_does_not_override_retirement(api, monkeypatch, endpoint):
    registry = Registry(capabilities={"visibility": "history-only"})
    registry.rows[("other-project", "ren-cx")] = {
        **registry.rows[("kaidera-os", "ren-cx")], "capabilities": {"keep_visible": True},
    }
    wire(api, monkeypatch, registry)
    active = await start(api, endpoint, project="other-project")
    assert "ren-cx@other-project" in json.dumps(active)
    registry.context_reads.clear()
    registry.profile_reads.clear()
    registry.events.clear()

    with pytest.raises(HTTPException) as error:
        await start(api, endpoint)

    assert error.value.status_code == 403
    assert registry.context_reads == registry.profile_reads == registry.events == []


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ENDPOINTS)
async def test_failed_registry_lookup_cannot_fall_back_to_historical_profile(api, monkeypatch, endpoint):
    registry = Registry(capabilities={})
    registry.fail_registry = True
    wire(api, monkeypatch, registry)
    with pytest.raises(RuntimeError, match="synthetic registry read failure"):
        await start(api, endpoint)
    assert registry.profile_reads == registry.context_reads == registry.events == []
    assert registry.closed == 1
