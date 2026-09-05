"""Typo-proof agent-name guard (roster-aware did-you-mean).

Covers suggest_agent_name + the registry-driven write/handoff roster gates. The
/boot endpoint guard that uses these is verified live against the rebuilt API.

E006 Inc04: the write/handoff gates are now async + registry-driven
(load_roster_policy reads cortex_projects.metadata + agents/roles). These tests
drive the guards through a registry-aware FakeConn so the SAME did-you-mean /
allow / opt-out assertions pass THROUGH the data path, proving behaviour is
preserved against DATA rather than hardcoded frozensets.
"""

import importlib.util
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

API_MAIN_PATH = Path(__file__).resolve().parents[1] / "main.py"


def load_api_module():
    spec = importlib.util.spec_from_file_location("cortex_api_main_name_guard_test", API_MAIN_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["cortex_api_main_name_guard_test"] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


api = load_api_module()


# ---------------------------------------------------------------------------
# Registry fixtures: a FakeConn that answers load_roster_policy's reads.
#
#   - kaidera-os    -> enforcing, work writers {kai, ren}, system {beat,migration,system}
#   - asw-connect -> NON-enforcing (enforce_writer_roster=false) -> gate no-ops
#   - any other   -> no cortex_projects row -> resolver default (fail-closed only
#                    for the kaidera-os seed; other projects opt out)
# ---------------------------------------------------------------------------

_kaidera_os_META = {
    "enforce_writer_roster": True,
    "roster_policy": {
        "enforce_writer_roster": True,
        "roster_schema_version": "1",
        "default_writer_scope": "work",
        "system_event_writers": ["beat", "migration", "system"],
        "beat_may_create_handoff": True,
        "handoff_targets": "writers",
        "suggest_cutoff": 0.6,
    },
}

_ASW_META = {
    # Registered, real, NON-enforcing project (the opt-out path).
    "enforce_writer_roster": False,
    "roster_policy": {"enforce_writer_roster": False},
}

_PROJECT_META = {
    "kaidera-os": _kaidera_os_META,
    "asw-connect": _ASW_META,
}

_PROJECT_AGENTS = {
    "kaidera-os": [
        {"n": "kai", "scope": "work", "role": "full-stack-developer"},
        {"n": "ren", "scope": "work", "role": "full-stack-developer"},
    ],
}


class FakeAcquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return FakeAcquire(self.conn)


class RegistryFakeConn:
    """Answers exactly the reads load_roster_policy issues."""

    async def execute(self, sql, *args):  # acquire_scoped set_config
        return "OK"

    async def fetchrow(self, sql, *args):
        if "SELECT metadata FROM cortex_projects" in sql:
            project = args[0]
            meta = _PROJECT_META.get(project)
            return {"metadata": meta} if meta is not None else None
        raise AssertionError(f"Unexpected fetchrow SQL: {sql}")

    async def fetch(self, sql, *args):
        project = args[0]
        if "writer_scope" in sql and "FROM agents a" in sql:
            return list(_PROJECT_AGENTS.get(project, []))
        if "FROM roles" in sql and "default_capabilities" in sql:
            return []
        raise AssertionError(f"Unexpected fetch SQL: {sql}")


@pytest.fixture
def registry_api():
    conn = RegistryFakeConn()
    fake_pool = FakePool(conn)
    api.pool = fake_pool
    api.pool_app = fake_pool
    api.pool_admin = fake_pool
    api._invalidate_roster_policy()  # clear any cached policy between tests
    yield api
    api._invalidate_roster_policy()


# ---------------------------------------------------------------------------
# suggest_agent_name — membership-agnostic, no DB. Kept as-is + cutoff case.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "candidate,expected",
    [
        ("kia", "kai"),      # transposition
        ("kaii", "kai"),     # doubled char
        ("rne", "ren"),      # transposition
        ("renn", "ren"),     # doubled char
        ("kai@kaidera-os", None),  # exact display identity -> excluded
        ("ren", None),       # exact
        ("phoenix", None),   # genuinely novel agent
        ("sophia", None),    # genuinely novel agent
        ("", None),          # empty
    ],
)
def test_suggest_agent_name(candidate, expected):
    assert api.suggest_agent_name(candidate, ["kai", "ren"]) == expected


def test_suggest_agent_name_empty_roster():
    assert api.suggest_agent_name("kia", []) is None


def test_suggest_agent_name_explicit_cutoff():
    # Default cutoff (0.6) suggests kai for kia; a strict cutoff rejects it.
    assert api.suggest_agent_name("kia", ["kai", "ren"], cutoff=0.6) == "kai"
    assert api.suggest_agent_name("kia", ["kai", "ren"], cutoff=0.95) is None


def test_validate_agent_name_rejects_retired_colon_identity():
    with pytest.raises(HTTPException) as exc:
        api.validate_agent_name("oryx:????")

    assert exc.value.status_code == 400
    assert "Colon-suffixed Cortex identity" in exc.value.detail


def test_agent_base_for_project_rejects_mismatched_display_project():
    with pytest.raises(HTTPException) as exc:
        api.agent_base_for_project("sam@kaidera", "marketing", field_name="to_agent")

    assert exc.value.status_code == 400
    assert "belongs to project 'kaidera'" in exc.value.detail
    assert "X-Project is 'marketing'" in exc.value.detail


def test_agent_base_for_project_accepts_matching_display_project():
    assert api.agent_base_for_project("Sam@Marketing", "marketing") == "sam"


def test_validate_registry_agent_name_strips_matching_project_suffix():
    assert api.validate_registry_agent_name("Sam@Marketing", "marketing") == "sam"


@pytest.mark.parametrize("name", ["claude-subagent-deadbeef", "the", "--help"])
def test_validate_registry_agent_name_rejects_ephemeral_and_fragments(name):
    with pytest.raises(HTTPException):
        api.validate_registry_agent_name(name, "kaidera-os")


# ---------------------------------------------------------------------------
# Write / handoff gates — now async + registry-driven, same assertions.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_kaidera_os_writer_gate_suggests_closest(registry_api):
    with pytest.raises(HTTPException) as exc:
        await registry_api.require_registered_agent_writer("kaidera-os", "kia")
    assert exc.value.status_code == 403
    assert "Did you mean 'kai'?" in exc.value.detail


@pytest.mark.asyncio
async def test_kaidera_os_writer_gate_allows_registered(registry_api):
    # kai/ren are registered writers; beat is allowed as a system event writer.
    await registry_api.require_registered_agent_writer("kaidera-os", "kai")
    await registry_api.require_registered_agent_writer("kaidera-os", "ren")
    await registry_api.require_registered_agent_writer(
        "kaidera-os", "beat", scope="system-event"
    )


@pytest.mark.asyncio
async def test_kaidera_os_writer_gate_skips_other_projects(registry_api):
    # The hard roster gate applies only to ENFORCING projects; a registered but
    # non-enforcing project (asw-connect, enforce_writer_roster=false) is not
    # blocked here — the data-driven equivalent of the old project!=kaidera-os skip.
    await registry_api.require_registered_agent_writer("asw-connect", "whoever")


@pytest.mark.asyncio
async def test_handoff_target_gate_suggests_closest(registry_api):
    with pytest.raises(HTTPException) as exc:
        await registry_api.require_registered_handoff_target("kaidera-os", "rne")
    assert exc.value.status_code == 403
    assert "Did you mean 'ren'?" in exc.value.detail


@pytest.mark.asyncio
async def test_handoff_target_gate_allows_registered(registry_api):
    assert await registry_api.require_registered_handoff_target("kaidera-os", "kai") == "kai"
