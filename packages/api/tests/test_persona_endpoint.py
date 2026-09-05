import importlib.util
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi import HTTPException


API_MAIN_PATH = Path(__file__).resolve().parents[1] / "main.py"


def load_api_module():
    spec = importlib.util.spec_from_file_location(
        "cortex_api_main_persona_endpoint_test",
        API_MAIN_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


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


class FakeConn:
    async def execute(self, sql, *args):
        return "OK"

    async def fetchrow(self, sql, *args):
        # E006 Inc04: the registry resolver reads only `metadata` from cortex_projects.
        if "SELECT metadata FROM cortex_projects" in sql:
            return {
                "metadata": {
                    "enforce_writer_roster": True,
                    "roster_policy": {
                        "enforce_writer_roster": True,
                        "roster_schema_version": "1",
                        "default_writer_scope": "work",
                        "system_event_writers": ["beat", "migration", "system"],
                        "beat_may_create_handoff": True,
                        "handoff_targets": "writers",
                        "suggest_cutoff": 0.6,
                        "roles": {
                            "pm_lead": "kai",
                            "support_agents": ["ren"],
                            "approved_agents": ["kai", "ren"],
                            "role_assignments": {"pm_lead": "kai", "qa": "ren"},
                        },
                        "persona": {
                            "active_epic": "E005_LOCAL_CORTEX_RELIABILITY_SECURITY",
                            "active_increment": "03_04_api_migration_write_fidelity",
                            "policy_refs": [
                                ".agents/config/autonomy-policy.json",
                                "Program/Release_v0.1.0/E003_KAIDERA_OS_AUTONOMY_CONTROL_PLANE/PM_AUTONOMY_TICK_PROTOCOL.md",
                                "Program/Release_v0.1.0/E005_LOCAL_CORTEX_RELIABILITY_SECURITY/INCREMENTS/03.md",
                                "Program/Release_v0.1.0/E005_LOCAL_CORTEX_RELIABILITY_SECURITY/INCREMENTS/04.md",
                            ],
                            "skills": ["Kai-led PM/CPO cadence"],
                            "reports_to": "cto for hard gates; peer review",
                            "non_roster_rule": "Non-roster names are signal/system-only where explicitly allowed; they do not own or receive kaidera-os work.",
                            "hard_gates": ["no cross-project access without CORTEX_CTO_OVERRIDE"],
                        },
                    },
                }
            }
        if "FROM cortex_projects" in sql and "project_key" in sql:
            return {
                "project_key": "kaidera-os",
                "project_id": "11111111-1111-4111-8111-111111111111",
                "display_name": "kaidera-os",
                "default_agent": "kai",
                "repo_root": "/tmp/kaidera-os",
                "repo_type": "repo",
                "status": "active",
            }
        if "SELECT default_agent FROM cortex_projects" in sql:
            return {"default_agent": "kai"}
        if "FROM agent_profiles" in sql:
            project, agent = args
            if project == "kaidera-os" and agent in {"kai", "ren"}:
                return {
                    "agent_name": agent,
                    "role": "full-stack-developer",
                    "profile_kind": "identity",
                    "profile_text": "Registry-backed kaidera-os profile",
                    "metadata": {},
                    "updated_at": datetime(2026, 5, 30, tzinfo=timezone.utc),
                }
            return None
        if "FROM agents" in sql:
            project, agent = args
            if project == "kaidera-os" and agent in {"kai", "ren"}:
                return {
                    "name": agent,
                    "role": "full-stack-developer",
                    "model": "gpt-5.5" if agent == "kai" else "gemini-3.1-pro-preview",
                    "capabilities": {"keep_visible": True, "writer_scope": "work"},
                }
            return None
        raise AssertionError(f"Unexpected fetchrow SQL: {sql}")

    async def fetch(self, sql, *args):
        # E006 Inc04: registry resolver reads (agents writer_scope + role defaults).
        if "writer_scope" in sql and "FROM agents a" in sql:
            project = args[0]
            if project == "kaidera-os":
                return [
                    {"n": "kai", "scope": "work", "role": "full-stack-developer"},
                    {"n": "ren", "scope": "work", "role": "full-stack-developer"},
                ]
            return []
        if "FROM roles" in sql and "default_capabilities" in sql:
            return []
        if "SELECT role, capabilities" in sql and "FROM agents" in sql:
            project, agent = args
            if project == "kaidera-os" and agent in {"kai", "ren"}:
                return [{"role": "full-stack-developer", "capabilities": {}}]
            return []
        if "SELECT DISTINCT role" in sql:
            return [{"role": "full-stack-developer"}]
        if "FROM handoffs" in sql:
            if "status = 'pending'" in sql:
                return [
                    {
                        "id": "afcef461-1806-41e3-955c-2009fa349652",
                        "priority": "high",
                        "summary": "Ren evidence review",
                    }
                ]
            return []
        if "FROM decisions" in sql:
            return [
                {
                    "agent_name": "ren@kaidera-os",
                    "summary": "[REN-E003-ALPHA-GUARD-PM-TICK-REVIEW-2026-05-30] Accepted",
                }
            ]
        raise AssertionError(f"Unexpected fetch SQL: {sql}")


@pytest.fixture
def cortex_api():
    module = load_api_module()
    fake_pool = FakePool(FakeConn())
    module.pool = fake_pool
    module.pool_app = fake_pool
    module.pool_admin = fake_pool
    return module


@pytest.mark.asyncio
async def test_kaidera_os_persona_injects_active_runtime_context(cortex_api):
    result = await cortex_api.get_agent_persona("kai", x_project="kaidera-os")

    assert result["schema"] == "cortex.persona.v1"
    assert result["agent"] == "kai"
    assert result["project"] == "kaidera-os"
    assert "project_hex" not in result
    assert result["agent_identity"] == "kai@kaidera-os"
    assert result["runtime_context"]["active_epic"] == "E005_LOCAL_CORTEX_RELIABILITY_SECURITY"
    assert result["runtime_context"]["active_increment"] == "03_04_api_migration_write_fidelity"
    assert result["runtime_context"]["approved_agents"] == ["kai", "ren"]
    assert ".agents/config/autonomy-policy.json" in result["runtime_context"]["policy_refs"]
    assert "Non-roster names are signal/system-only" in result["runtime_context"]["non_roster_rule"]
    assert result["runtime_context"]["pm_lead"] == "kai"
    assert result["runtime_context"]["support_agents"] == ["ren"]
    assert "## Runtime Skill Context" in result["additionalContext"]
    assert "PM_AUTONOMY_TICK_PROTOCOL.md" in result["additionalContext"]
    assert "E005_LOCAL_CORTEX_RELIABILITY_SECURITY/INCREMENTS/03.md" in result["additionalContext"]
    assert "E005_LOCAL_CORTEX_RELIABILITY_SECURITY/INCREMENTS/04.md" in result["additionalContext"]
    assert "Kai-led PM/CPO cadence" in result["additionalContext"]
    assert "Registry-backed kaidera-os profile" in result["additionalContext"]


@pytest.mark.asyncio
async def test_kaidera_os_persona_rejects_non_roster_agent(cortex_api):
    with pytest.raises(HTTPException) as excinfo:
        await cortex_api.get_agent_persona("alpha", x_project="kaidera-os")

    assert excinfo.value.status_code == 403
    assert "not a registered runtime persona for project 'kaidera-os'" in excinfo.value.detail
