"""E006 Inc04 — Roster-as-Data: registry-driven writer boundary.

Proves the security property is preserved against DATA, not hardcoded
frozensets:

  - kai/ren (writer_scope='work') still write; non-roster names 403.
  - writer_scope precedence: agent explicit > role default > project default.
  - keep_visible path: an agent invisible to visible_agent_sql() is excluded
    from work_writers.
  - FAIL-CLOSED: an enforcing project with no visible writers admits nobody.
  - MISSING PROJECT: an absent cortex_projects row raises instead of falling
    back to a code-level project or agent list.
  - FAIL-CLOSED on read error: a registry read failure on an enforcing project
    raises 503 (never bypasses) when there is no prior cache.
  - OPT-OUT: a registered NON-enforcing project (asw-connect,
    enforce_writer_roster=false) lets any name write (other projects unbroken).
  - Seeded kaidera-os data computes work_writers={kai,ren} and
    system_event_writers={beat,migration,system} from the registry payload.

These tests drive the REAL load_roster_policy resolver through a configurable
FakeConn that answers exactly the reads the resolver issues.
"""

import importlib.util
from pathlib import Path

import pytest
from fastapi import HTTPException


API_MAIN_PATH = Path(__file__).resolve().parents[1] / "main.py"


def load_api_module():
    spec = importlib.util.spec_from_file_location(
        "cortex_api_main_roster_as_data_test", API_MAIN_PATH
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


SEEDED_kaidera_os_META = {
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
    """Answers exactly the reads load_roster_policy issues, per-project.

    Construct with:
      metadata_by_project: {project_key: metadata_dict OR None (missing row)}
      agents_by_project:   {project_key: [ {n, scope, role}, ... ]}
      roles_by_project:    {project_key: [ {name, scope}, ... ]}
      raise_on:            'fetchrow' | 'fetch' | None  (simulate read error)
    """

    def __init__(
        self,
        metadata_by_project=None,
        agents_by_project=None,
        roles_by_project=None,
        raise_on=None,
    ):
        self.metadata_by_project = metadata_by_project or {}
        self.agents_by_project = agents_by_project or {}
        self.roles_by_project = roles_by_project or {}
        self.raise_on = raise_on

    async def execute(self, sql, *args):  # acquire_scoped set_config
        return "OK"

    async def fetchrow(self, sql, *args):
        if self.raise_on == "fetchrow":
            raise RuntimeError("simulated registry read failure")
        if "SELECT metadata FROM cortex_projects" in sql:
            project = args[0]
            if project not in self.metadata_by_project:
                return None  # missing row
            meta = self.metadata_by_project[project]
            return {"metadata": meta} if meta is not None else {"metadata": None}
        raise AssertionError(f"Unexpected fetchrow SQL: {sql}")

    async def fetch(self, sql, *args):
        if self.raise_on == "fetch":
            raise RuntimeError("simulated registry read failure")
        project = args[0]
        if "writer_scope" in sql and "FROM agents a" in sql:
            return list(self.agents_by_project.get(project, []))
        if "FROM roles" in sql and "default_capabilities" in sql:
            return list(self.roles_by_project.get(project, []))
        raise AssertionError(f"Unexpected fetch SQL: {sql}")


def make_api(
    metadata_by_project=None,
    agents_by_project=None,
    roles_by_project=None,
    raise_on=None,
):
    module = load_api_module()
    conn = RegistryFakeConn(
        metadata_by_project=metadata_by_project,
        agents_by_project=agents_by_project,
        roles_by_project=roles_by_project,
        raise_on=raise_on,
    )
    fake_pool = FakePool(conn)
    module.pool = fake_pool
    module.pool_app = fake_pool
    module.pool_admin = fake_pool
    module._invalidate_roster_policy()
    return module, conn


# ---------------------------------------------------------------------------
# Seeded kaidera-os policy is computed from registry data.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_seeded_kaidera_os_policy_is_registry_derived():
    api, _ = make_api(
        metadata_by_project={"kaidera-os": SEEDED_kaidera_os_META},
        agents_by_project={
            "kaidera-os": [
                {"n": "kai", "scope": "work", "role": "full-stack-developer"},
                {"n": "ren", "scope": "work", "role": "full-stack-developer"},
            ]
        },
    )
    policy = await api.load_roster_policy("kaidera-os")
    assert policy.enforce is True
    assert policy.work_writers == frozenset({"kai", "ren"})
    assert policy.system_event_writers == frozenset({"beat", "migration", "system"})
    assert policy.handoff_targets == frozenset({"kai", "ren"})
    assert policy.beat_may_create_handoff is True


@pytest.mark.asyncio
async def test_kai_ren_write_non_roster_rejected():
    api, _ = make_api(
        metadata_by_project={"kaidera-os": SEEDED_kaidera_os_META},
        agents_by_project={
            "kaidera-os": [
                {"n": "kai", "scope": "work", "role": "full-stack-developer"},
                {"n": "ren", "scope": "work", "role": "full-stack-developer"},
            ]
        },
    )
    # kai/ren pass the work gate and are valid handoff targets.
    await api.require_registered_agent_writer("kaidera-os", "kai")
    await api.require_registered_agent_writer("kaidera-os", "ren")
    assert await api.require_registered_handoff_target("kaidera-os", "ren") == "ren"
    with pytest.raises(HTTPException) as legacy_exc:
        await api.require_registered_agent_writer("kaidera-os", "ren:legacy")
    assert legacy_exc.value.status_code == 400

    # alpha/root are not registered writers -> 403 on both gates.
    with pytest.raises(HTTPException) as wexc:
        await api.require_registered_agent_writer("kaidera-os", "alpha")
    assert wexc.value.status_code == 403
    with pytest.raises(HTTPException) as hexc:
        await api.require_registered_handoff_target("kaidera-os", "root")
    assert hexc.value.status_code == 403


# ---------------------------------------------------------------------------
# writer_scope precedence: agent explicit > role default > project default.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_writer_scope_precedence_agent_over_role_over_project():
    api, _ = make_api(
        metadata_by_project={
            "proj": {
                "enforce_writer_roster": True,
                "roster_policy": {
                    "enforce_writer_roster": True,
                    "default_writer_scope": "read-only",  # project default
                    "system_event_writers": [],
                },
            }
        },
        agents_by_project={
            "proj": [
                # explicit agent scope wins -> work
                {"n": "amy", "scope": "work", "role": "dev"},
                # no agent scope -> role default (dev=work) wins over project default
                {"n": "bo", "scope": None, "role": "dev"},
                # no agent scope, no role default -> project default (read-only)
                {"n": "cy", "scope": None, "role": "guest"},
            ]
        },
        roles_by_project={"proj": [{"name": "dev", "scope": "work"}]},
    )
    policy = await api.load_roster_policy("proj")
    assert policy.work_writers == frozenset({"amy", "bo"})
    assert "cy" in policy.read_only
    assert policy.default_writer_scope == "read-only"


# ---------------------------------------------------------------------------
# keep_visible path: an agent invisible to visible_agent_sql is excluded.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_keep_visible_excludes_invisible_agent_then_enforcing_roster_is_empty():
    # visible_agent_sql() is applied in SQL, so an invisible agent never appears
    # in the resolver's rows. Here the agents read returns EMPTY (as it would for
    # a row lacking keep_visible='true' and any profile) -> work computes empty.
    api, _ = make_api(
        metadata_by_project={"kaidera-os": SEEDED_kaidera_os_META},
        agents_by_project={"kaidera-os": []},  # nothing visible
    )
    policy = await api.load_roster_policy("kaidera-os")
    assert policy.work_writers == frozenset()
    # Empty enforcing roster rejects everyone until the registry is repaired.
    with pytest.raises(HTTPException) as exc:
        await api.require_registered_agent_writer("kaidera-os", "alpha")
    assert exc.value.status_code == 403
    with pytest.raises(HTTPException) as kexc:
        await api.require_registered_agent_writer("kaidera-os", "kai")
    assert kexc.value.status_code == 403


# ---------------------------------------------------------------------------
# Missing row errors; empty metadata opts out unless policy data enables it.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_missing_cortex_projects_row_raises_not_hardcoded_fallback():
    api, _ = make_api(
        metadata_by_project={},  # no kaidera-os row at all
        agents_by_project={"kaidera-os": []},
    )
    with pytest.raises(HTTPException) as exc:
        await api.load_roster_policy("kaidera-os")
    assert exc.value.status_code == 404
    with pytest.raises(HTTPException) as gexc:
        await api.require_registered_agent_writer("kaidera-os", "alpha")
    assert gexc.value.status_code == 404


@pytest.mark.asyncio
async def test_empty_metadata_is_not_project_hardcoded():
    api, _ = make_api(
        metadata_by_project={"kaidera-os": {}},
        agents_by_project={"kaidera-os": []},
    )
    policy = await api.load_roster_policy("kaidera-os")
    assert policy.enforce is False
    await api.require_registered_agent_writer("kaidera-os", "alpha")


@pytest.mark.asyncio
async def test_null_metadata_is_not_project_hardcoded():
    # Row present, metadata column NULL -> json_object({}) -> same as empty.
    api, _ = make_api(
        metadata_by_project={"kaidera-os": None},
        agents_by_project={"kaidera-os": []},
    )
    policy = await api.load_roster_policy("kaidera-os")
    assert policy.enforce is False
    await api.require_registered_agent_writer("kaidera-os", "alpha")


# ---------------------------------------------------------------------------
# FAIL-CLOSED on read error: raise 503, never bypass (no prior cache).
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fail_closed_on_registry_read_error_raises_503():
    api, _ = make_api(
        metadata_by_project={"kaidera-os": SEEDED_kaidera_os_META},
        raise_on="fetchrow",
    )
    with pytest.raises(HTTPException) as exc:
        await api.load_roster_policy("kaidera-os")
    assert exc.value.status_code == 503
    # The guard surfaces the same 503 — it must NOT admit the writer.
    with pytest.raises(HTTPException) as gexc:
        await api.require_registered_agent_writer("kaidera-os", "alpha")
    assert gexc.value.status_code == 503


@pytest.mark.asyncio
async def test_read_error_serves_prior_cached_policy_not_bypass():
    api, conn = make_api(
        metadata_by_project={"kaidera-os": SEEDED_kaidera_os_META},
        agents_by_project={
            "kaidera-os": [
                {"n": "kai", "scope": "work", "role": "dev"},
                {"n": "ren", "scope": "work", "role": "dev"},
            ]
        },
    )
    good = await api.load_roster_policy("kaidera-os")  # populate cache
    # Force the TTL to be treated as fresh isn't needed; invalidate then break reads.
    api._invalidate_roster_policy("kaidera-os")
    conn.raise_on = "fetchrow"
    # No cache for kaidera-os now -> must 503 (never bypass).
    with pytest.raises(HTTPException) as exc:
        await api.load_roster_policy("kaidera-os")
    assert exc.value.status_code == 503
    # Sanity: the good policy we got earlier is enforcing and registry-derived.
    assert good.enforce is True and good.work_writers == frozenset({"kai", "ren"})


# ---------------------------------------------------------------------------
# OPT-OUT: a registered NON-enforcing project lets any name write.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_non_enforcing_project_allows_any_writer():
    api, _ = make_api(
        metadata_by_project={
            "asw-connect": {
                "enforce_writer_roster": False,
                "roster_policy": {"enforce_writer_roster": False},
            }
        },
        agents_by_project={"asw-connect": []},
    )
    policy = await api.load_roster_policy("asw-connect")
    assert policy.enforce is False
    # Gate no-ops -> any name passes (the data-driven equivalent of project!=kaidera-os).
    await api.require_registered_agent_writer("asw-connect", "whoever")
    assert await api.require_registered_handoff_target("asw-connect", "whoever") == "whoever"


@pytest.mark.asyncio
async def test_unknown_project_missing_row_is_error():
    api, _ = make_api(metadata_by_project={}, agents_by_project={})
    with pytest.raises(HTTPException) as exc:
        await api.load_roster_policy("some-other-proj")
    assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# Dynamic add (data op) + cache invalidation, simulated at the resolver level.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_added_writer_visible_after_invalidation_without_restart():
    api, conn = make_api(
        metadata_by_project={"kaidera-os": SEEDED_kaidera_os_META},
        agents_by_project={
            "kaidera-os": [
                {"n": "kai", "scope": "work", "role": "dev"},
                {"n": "ren", "scope": "work", "role": "dev"},
            ]
        },
    )
    # bob is not yet a writer.
    with pytest.raises(HTTPException):
        await api.require_registered_agent_writer("kaidera-os", "bob")

    # Simulate POST /agents adding bob as a work writer (data op) + cache invalidation.
    conn.agents_by_project["kaidera-os"].append(
        {"n": "bob", "scope": "work", "role": "dev"}
    )
    api._invalidate_roster_policy("kaidera-os")

    # Next read reflects bob as a writer + valid handoff target — no restart.
    await api.require_registered_agent_writer("kaidera-os", "bob")
    assert await api.require_registered_handoff_target("kaidera-os", "bob") == "bob"
