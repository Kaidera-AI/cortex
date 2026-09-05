"""Roster-policy PATCH — write-side validation + merge semantics (fail-loud).

The defect: PATCH /projects/{key}/roster-policy was write-only and unvalidated —
it echoed 200 for a holder that was not on the roster (or a holder key written at
the wrong level), while /runtime and the boot-rules builder kept reporting
pm_lead=None; and it REPLACED roster_policy wholesale, so a two-key diagnostic
patch wiped approved_agents/role_assignments.

These tests drive the REAL `patch_project_roster_policy` route function through
a FakeConn that answers exactly the queries the route issues, and prove:

  * an off-roster holder is a 422 (no UPDATE lands);
  * a holder key at the roster_policy top level (instead of under `roles`) is a
    422 with guidance;
  * the default MERGE preserves unmentioned keys, mirrors the lead into both
    stored spellings, and leaves ALL read surfaces (write echo, runtime profile,
    persona/boot-rules context) reporting the SAME lead;
  * `?replace=true` restores wholesale-replace for callers that want it;
  * legacy data carrying only role_assignments.pm_lead still resolves a lead on
    every read surface (effective_pm_lead).
"""

import importlib.util
import json
from pathlib import Path

import pytest
from fastapi import HTTPException


API_MAIN_PATH = Path(__file__).resolve().parents[1] / "main.py"


def load_api_module():
    spec = importlib.util.spec_from_file_location(
        "cortex_api_main_roster_policy_patch_test", API_MAIN_PATH
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


EXISTING_POLICY = {
    "enforce_writer_roster": True,
    "roster_schema_version": "1",
    "default_writer_scope": "work",
    "system_event_writers": ["beat", "system"],
    "roles": {
        "pm_lead": "kai",
        "support_agents": ["ren"],
        "approved_agents": ["kai", "ren"],
        "role_assignments": {"pm_lead": "kai", "qa": "ren"},
    },
}

ROSTER = ["kai", "ren"]


class _FakeTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeAcquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _FakeAcquire(self.conn)


class PatchFakeConn:
    """Answers exactly the reads/writes patch_project_roster_policy issues.

    `metadata_by_project[project]` is MUTATED by the UPDATE (json round-tripped,
    like jsonb would), so a test can read back what the write actually stored.
    `updates` records every UPDATE payload so a rejected patch can be proven to
    have written nothing.
    """

    def __init__(self, metadata_by_project=None, roster_names=None):
        self.metadata_by_project = metadata_by_project or {}
        self.roster_names = list(roster_names or [])
        self.updates: list[dict] = []

    def transaction(self):
        return _FakeTransaction()

    async def execute(self, sql, *args):
        if sql.startswith("UPDATE cortex_projects SET metadata"):
            stored = json.loads(args[1])
            self.metadata_by_project[args[0]] = stored
            self.updates.append(stored)
            return "UPDATE 1"
        return "OK"  # set_config / pg_notify

    async def fetchval(self, sql, *args):
        if "INSERT INTO team_events" in sql:
            return 1
        raise AssertionError(f"Unexpected fetchval SQL: {sql}")

    async def fetchrow(self, sql, *args):
        project = args[0]
        if "FROM cortex_projects" in sql and project in self.metadata_by_project:
            if "SELECT metadata FROM cortex_projects" in sql:
                return {"metadata": json.dumps(self.metadata_by_project[project])}
            return {
                "project_key": project,
                "project_id": "00000000-0000-0000-0000-000000000001",
                "display_name": project,
                "default_agent": None,
                "repo_root": None,
                "repo_type": None,
                "status": "active",
            }
        return None  # unknown project -> require_registered_project / route 404

    async def fetch(self, sql, *args):
        if "SELECT a.name FROM agents a" in sql:
            return [{"name": name} for name in self.roster_names]
        raise AssertionError(f"Unexpected fetch SQL: {sql}")


class _FakeRequest:
    def __init__(self, token):
        self.headers = {"X-Cortex-Admin-Token": token}


def make_api(metadata_by_project=None, roster_names=None):
    module = load_api_module()
    conn = PatchFakeConn(
        metadata_by_project=metadata_by_project,
        roster_names=roster_names,
    )
    fake_pool = _FakePool(conn)
    module.pool = fake_pool
    module.pool_app = fake_pool
    module.pool_admin = fake_pool
    module.ADMIN_TOKEN = "test-admin"
    module._invalidate_roster_policy()
    return module, conn


def make_patch(module, roster_policy=None, enforce=None):
    return module.ProjectRosterPolicyPatch(
        roster_policy=roster_policy, enforce_writer_roster=enforce
    )


async def run_patch(module, conn, project, body, *, replace=False):
    return await module.patch_project_roster_policy(
        project, body, _FakeRequest("test-admin"), replace=replace
    )


# ---------------------------------------------------------------------------
# Write-side validation (fail-loud).
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_patch_rejects_off_roster_holder_422():
    api, conn = make_api(
        metadata_by_project={"proj-x": {"roster_policy": dict(EXISTING_POLICY)}},
        roster_names=ROSTER,
    )
    with pytest.raises(HTTPException) as exc:
        await run_patch(api, conn, "proj-x", make_patch(api, {"roles": {"pm_lead": "ghost"}}))
    assert exc.value.status_code == 422
    assert "ghost" in str(exc.value.detail)
    assert conn.updates == []  # nothing was written


@pytest.mark.asyncio
async def test_patch_rejects_off_roster_role_assignment_422():
    api, conn = make_api(
        metadata_by_project={"proj-x": {"roster_policy": dict(EXISTING_POLICY)}},
        roster_names=ROSTER,
    )
    with pytest.raises(HTTPException) as exc:
        await run_patch(
            api, conn, "proj-x",
            make_patch(api, {"roles": {"role_assignments": {"qa": "nobody"}}}),
        )
    assert exc.value.status_code == 422
    assert "nobody" in str(exc.value.detail)
    assert conn.updates == []


@pytest.mark.asyncio
async def test_patch_rejects_misplaced_top_level_holder_key_422():
    """A holder key at the roster_policy top level is where marlow's write went
    to die: echoed 200, seen by NO read surface. Now a loud 422 with guidance."""
    api, conn = make_api(
        metadata_by_project={"proj-x": {"roster_policy": dict(EXISTING_POLICY)}},
        roster_names=ROSTER,
    )
    with pytest.raises(HTTPException) as exc:
        await run_patch(api, conn, "proj-x", make_patch(api, {"pm_lead": "kai"}))
    assert exc.value.status_code == 422
    assert "roles" in str(exc.value.detail)
    assert conn.updates == []


# ---------------------------------------------------------------------------
# Merge semantics + read-surface agreement.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_patch_merge_preserves_unmentioned_keys_and_unifies_read_surfaces():
    """The marlow scenario, fixed: a two-key patch that names the lead via
    roles.role_assignments.pm_lead merges (nothing wiped), normalises the lead
    into both stored spellings, and every read surface reports the SAME lead."""
    api, conn = make_api(
        metadata_by_project={"proj-x": {"roster_policy": json.loads(json.dumps(EXISTING_POLICY))}},
        roster_names=ROSTER,
    )

    result = await run_patch(
        api, conn, "proj-x",
        make_patch(api, {"roles": {"role_assignments": {"pm_lead": "ren"}}}),
    )

    # 1. MERGE: unmentioned keys survived (no wholesale wipe).
    stored = conn.metadata_by_project["proj-x"]["roster_policy"]
    assert stored["system_event_writers"] == ["beat", "system"]
    assert stored["roles"]["support_agents"] == ["ren"]
    assert stored["roles"]["approved_agents"] == ["kai", "ren"]
    assert stored["roles"]["role_assignments"]["qa"] == "ren"  # merged, not replaced

    # 2. NORMALISE: the lead lands in BOTH stored spellings.
    assert stored["roles"]["pm_lead"] == "ren"
    assert stored["roles"]["role_assignments"]["pm_lead"] == "ren"

    # 3a. Surface one — the write echo reports the stored policy.
    assert result["roster_policy"]["roles"]["pm_lead"] == "ren"

    # 3b. Surface two — GET /projects/{key}/runtime (build_runtime_profile).
    runtime_metadata = json.loads(json.dumps(conn.metadata_by_project["proj-x"]))
    runtime_metadata["roots"] = [{"path": "/projects/proj-x", "kind": "primary"}]
    profile = api.build_runtime_profile(
        {
            "project_key": "proj-x",
            "repo_root": "/projects/proj-x",
            "metadata": json.dumps(runtime_metadata),
        },
        [{
            "root_path": "/projects/proj-x",
            "path_kind": "primary",
            "metadata": {"path": "/projects/proj-x", "kind": "primary"},
        }],
        [],
        None,
        None,
    )
    assert profile["roster"]["pm_lead"] == "ren"

    # 3c. Surface three — the boot-rules builder, fed exactly as the persona
    # endpoint feeds it (runtime_context["pm_lead"] = effective_pm_lead(roles)).
    runtime_context = {
        "pm_lead": api.effective_pm_lead(stored["roles"]),
        "support_agents": stored["roles"]["support_agents"],
    }
    sections = api.build_persona_sections(
        agent="ren",
        project="proj-x",
        role="qa",
        lane="qa",
        not_lane="",
        reports_to="",
        runtime_context=runtime_context,
        profile_text="",
        pending_handoffs=[],
        claimed_handoffs=[],
        recent_decisions=[],
    )
    assert "- PM lead: ren." in sections["operating_rules"]


@pytest.mark.asyncio
async def test_patch_replace_true_restores_wholesale_replace():
    api, conn = make_api(
        metadata_by_project={"proj-x": {"roster_policy": json.loads(json.dumps(EXISTING_POLICY))}},
        roster_names=ROSTER,
    )
    await run_patch(
        api, conn, "proj-x",
        make_patch(api, {"roles": {"pm_lead": "ren"}}),
        replace=True,
    )
    stored = conn.metadata_by_project["proj-x"]["roster_policy"]
    # the whole prior policy is gone; only the patch (+ the mirrored lead) remains
    assert "system_event_writers" not in stored
    assert stored["roles"]["pm_lead"] == "ren"
    assert stored["roles"]["role_assignments"] == {"pm_lead": "ren"}


@pytest.mark.asyncio
async def test_enforce_flag_patch_merges_with_existing_policy():
    api, conn = make_api(
        metadata_by_project={"proj-x": {"roster_policy": json.loads(json.dumps(EXISTING_POLICY))}},
        roster_names=ROSTER,
    )
    result = await run_patch(api, conn, "proj-x", make_patch(api, enforce=False))
    assert result["enforce_writer_roster"] is False
    stored = conn.metadata_by_project["proj-x"]["roster_policy"]
    assert stored["enforce_writer_roster"] is False
    assert stored["roles"]["pm_lead"] == "kai"  # untouched
    assert conn.metadata_by_project["proj-x"]["enforce_writer_roster"] is False


# ---------------------------------------------------------------------------
# Legacy data reconciled at read (pre-normalisation spelling).
# ---------------------------------------------------------------------------

def test_effective_pm_lead_falls_back_to_role_assignments():
    """Data written before write-side normalisation may carry the lead ONLY as
    roles.role_assignments.pm_lead. Every read surface resolves it via the ONE
    helper, so they cannot disagree over legacy blobs."""
    roles = {"role_assignments": {"pm_lead": "marlow", "qa": "ren"}}
    api, _ = make_api()
    assert api.effective_pm_lead(roles) == "marlow"
    assert api.effective_pm_lead({"pm_lead": "kai", "role_assignments": {"pm_lead": "marlow"}}) == "kai"
    assert api.effective_pm_lead({}) is None

    root = {"path": "/projects/proj-x", "kind": "primary"}
    profile = api.build_runtime_profile(
        {
            "project_key": "proj-x",
            "repo_root": root["path"],
            "metadata": {"roster_policy": {"roles": roles}, "roots": [root]},
        },
        [{"root_path": root["path"], "path_kind": "primary", "metadata": root}],
        [],
        None,
        None,
    )
    assert profile["roster"]["pm_lead"] == "marlow"
