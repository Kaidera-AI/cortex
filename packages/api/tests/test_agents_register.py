"""Live tests for project/agent/role onboarding through Cortex API."""

from __future__ import annotations

import os
import json
import uuid
from pathlib import Path

import httpx
import pytest

asyncpg = pytest.importorskip("asyncpg")


CORTEX_API = os.environ.get("CORTEX_API_URL", "http://localhost:8501")
PG_DSN_APP = os.environ.get(
    "CORTEX_TEST_PG_DSN_APP", ""
).strip()
TEST_PROJECT = os.environ.get("CORTEX_TEST_PROJECT", "").strip()
REPO_ROOT = Path(__file__).resolve().parents[2]


def _api_alive() -> bool:
    try:
        r = httpx.get(f"{CORTEX_API}/health", timeout=2.0)
        return r.status_code == 200 and r.json().get("status") == "healthy"
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not TEST_PROJECT or not PG_DSN_APP or not _api_alive(),
    reason="CORTEX_TEST_PROJECT, CORTEX_TEST_PG_DSN_APP, and a reachable cortex-api are required",
)


@pytest.fixture
def http_client():
    with httpx.Client(
        base_url=CORTEX_API,
        timeout=15.0,
        headers={
            "X-Project": TEST_PROJECT,
            "X-Agent-Name": "kai",
            "X-Cortex-Admin-Token": os.environ.get(
                "CORTEX_ADMIN_TOKEN", "cortex-local-admin"
            ),
        },
    ) as client:
        yield client


def _cleanup_project(client: httpx.Client, project: str):
    client.post(
        "/admin/sql/exec",
        json={
            "sql": f"""
            DELETE FROM team_events WHERE project = '{project}';
            DELETE FROM messages WHERE project = '{project}';
            DELETE FROM agent_sessions WHERE project = '{project}';
            DELETE FROM session_sources WHERE project = '{project}';
            DELETE FROM agents WHERE project = '{project}';
            DELETE FROM roles WHERE project = '{project}';
            DELETE FROM cortex_project_paths WHERE project_key = '{project}';
            DELETE FROM cortex_projects WHERE project_key = '{project}';
            """
        },
    )


def _cleanup_agent(client: httpx.Client, project: str, agent: str, role: str):
    client.post(
        "/admin/sql/exec",
        json={
            "sql": f"""
            DELETE FROM team_events WHERE project = '{project}' AND agent_name = '{agent}';
            DELETE FROM agents WHERE project = '{project}' AND name = '{agent}';
            DELETE FROM roles WHERE project = '{project}' AND name = '{role}';
            """
        },
    )


def _json_cell(value):
    return json.loads(value) if isinstance(value, str) else value


@pytest.mark.asyncio
async def test_app_user_cannot_run_roles_ddl_but_api_registers_agent(http_client):
    """Runtime app user has no DDL, while API registration remains data-only."""
    try:
        conn = await asyncpg.connect(PG_DSN_APP)
    except Exception as exc:
        pytest.skip(f"cannot connect as cortex_app: {exc}")

    try:
        await conn.execute("SELECT set_config('cortex.project', $1, false)", TEST_PROJECT)
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await conn.execute("ALTER TABLE roles ENABLE ROW LEVEL SECURITY")
    finally:
        await conn.close()

    suffix = uuid.uuid4().hex[:8]
    agent = f"kai-ddl-{suffix}"
    role = f"field-engineer-{suffix}"
    try:
        response = http_client.post(
            "/agents",
            headers={"X-Agent-Name": agent},
            json={
                "role": role,
                "capabilities": {"primary": ["field-debugging"]},
                "role_description": "Temporary DDL-boundary test role",
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["agent"] == agent
        assert response.json()["role"] == role

        query = http_client.post(
            "/admin/sql/query",
            json={
                "sql": (
                    "SELECT a.name, a.role, r.name AS role_name "
                    "FROM agents a JOIN roles r "
                    "ON r.project = a.project AND r.name = a.role "
                    f"WHERE a.project = '{TEST_PROJECT}' AND a.name = '{agent}'"
                )
            },
        )
        assert query.status_code == 200, query.text
        assert query.json()["rows"] == [[agent, role, role]]
    finally:
        _cleanup_agent(http_client, TEST_PROJECT, agent, role)


def test_project_api_registers_arbitrary_project_agent_role(http_client):
    """Project onboarding can create arbitrary project/agent/role records."""
    suffix = uuid.uuid4().hex[:8]
    project = f"kai-agent-{suffix}"
    agent = f"pilot-{suffix}"
    role = f"mission-lead-{suffix}"
    try:
        response = http_client.post(
            "/projects",
            json={
                "project_key": project,
                "display_name": "Kai Agent Register Test",
                "repo_root": f"/tmp/{project}",
                "default_agent": agent,
                "agents": [
                    {
                        "name": agent,
                        "role": role,
                        "capabilities": {"primary": ["planning"]},
                    }
                ],
            },
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["project_key"] == project
        assert data["default_agent"] == agent
        assert data["agents"] == [{"name": agent, "role": role, "model": None}]

        runtime = http_client.get(f"/projects/{project}/runtime", headers={"X-Project": project})
        assert runtime.status_code == 200, runtime.text
        runtime_data = runtime.json()
        assert runtime_data["project_key"] == project
        assert runtime_data["project_id"] == data["project_id"]
        assert "project_hex" not in runtime_data
        assert runtime_data["beat"]["agent_id"] == f"beat@{project}"
        assert "legacy_agent_id" not in runtime_data["beat"]
        assert runtime_data["beat"]["env"]["CORTEX_PROJECT"] == project
        assert "CORTEX_PROJECT_HEX" not in runtime_data["beat"]["env"]
        assert runtime_data["agents"][0]["runtime_id"] == f"{agent}@{project}"
        assert "legacy_runtime_id" not in runtime_data["agents"][0]

        roster = http_client.get("/roster", headers={"X-Project": project})
        assert roster.status_code == 200, roster.text
        agents = roster.json().get("agents", [])
        assert any(row["name"] == agent and row["role"] == role for row in agents)
    finally:
        _cleanup_project(http_client, project)


def test_register_agent_persists_runtime_state_outside_capabilities(http_client):
    """Agent registration writes operational state to agents runtime columns."""
    suffix = uuid.uuid4().hex[:8]
    agent = f"kai-state-{suffix}"
    role = f"runtime-lead-{suffix}"
    try:
        response = http_client.post(
            "/agents",
            headers={"X-Agent-Name": agent},
            json={
                "role": role,
                "capabilities": {"primary": ["registration"]},
                "role_description": "Temporary runtime-state test role",
            },
        )
        assert response.status_code == 200, response.text

        seed = http_client.post(
            "/admin/sql/exec",
            json={
                "sql": (
                    "UPDATE agents "
                    "SET runtime_state = runtime_state || '{\"preserved\": true}'::jsonb "
                    f"WHERE project = '{TEST_PROJECT}' AND name = '{agent}'"
                )
            },
        )
        assert seed.status_code == 200, seed.text

        second = http_client.post(
            "/agents",
            headers={"X-Agent-Name": agent},
            json={
                "role": role,
                "capabilities": {"secondary": ["rerun"]},
            },
        )
        assert second.status_code == 200, second.text

        query = http_client.post(
            "/admin/sql/query",
            json={
                "sql": (
                    "SELECT status, runtime_state, capabilities "
                    "FROM agents "
                    f"WHERE project = '{TEST_PROJECT}' AND name = '{agent}'"
                )
            },
        )
        assert query.status_code == 200, query.text
        status, runtime_state, capabilities = query.json()["rows"][0]
        runtime_state = _json_cell(runtime_state)
        capabilities = _json_cell(capabilities)

        assert status == "available"
        assert runtime_state["agent"] == agent
        assert runtime_state["role_profile"] == role
        assert runtime_state["project"] == TEST_PROJECT
        assert runtime_state["preserved"] is True
        assert "last_registered_at" in runtime_state
        assert capabilities["primary"] == ["registration"]
        assert capabilities["secondary"] == ["rerun"]
        assert "status" not in capabilities
        assert "runtime_state" not in capabilities

        events = http_client.post(
            "/admin/sql/query",
            json={
                "sql": (
                    "SELECT event_type, summary, detail "
                    "FROM team_events "
                    f"WHERE project = '{TEST_PROJECT}' AND agent_name = '{agent}@{TEST_PROJECT}' "
                    "ORDER BY id DESC LIMIT 1"
                )
            },
        )
        assert events.status_code == 200, events.text
        event_type, summary, detail = events.json()["rows"][0]
        detail = _json_cell(detail)
        assert event_type == "agent_registered"
        assert summary == f"Registered {agent} as {role}"
        assert detail["status"] == "available"
    finally:
        _cleanup_agent(http_client, TEST_PROJECT, agent, role)


def test_runtime_state_is_project_scoped_for_same_agent_name(http_client):
    """The same agent name can register independently in two projects."""
    suffix = uuid.uuid4().hex[:8]
    project_a = f"state-a-{suffix}"
    project_b = f"state-b-{suffix}"
    agent = f"pilot-{suffix}"
    role_a = f"lead-a-{suffix}"
    role_b = f"lead-b-{suffix}"
    try:
        for project in (project_a, project_b):
            response = http_client.post(
                "/projects",
                json={
                    "project_key": project,
                    "display_name": "Kai Runtime State Isolation Test",
                    "repo_root": f"/tmp/{project}",
                },
            )
            assert response.status_code == 200, response.text

        register_a = http_client.post(
            "/agents",
            headers={"X-Project": project_a, "X-Agent-Name": agent},
            json={"role": role_a, "capabilities": {"project": ["a"]}},
        )
        assert register_a.status_code == 200, register_a.text

        register_b = http_client.post(
            "/agents",
            headers={"X-Project": project_b, "X-Agent-Name": agent},
            json={"role": role_b, "capabilities": {"project": ["b"]}},
        )
        assert register_b.status_code == 200, register_b.text

        query = http_client.post(
            "/admin/sql/query",
            json={
                "sql": (
                    "SELECT project, role, status, runtime_state "
                    "FROM agents "
                    f"WHERE name = '{agent}' AND project IN ('{project_a}', '{project_b}') "
                    "ORDER BY project"
                )
            },
        )
        assert query.status_code == 200, query.text
        rows = query.json()["rows"]
        assert len(rows) == 2
        by_project = {row[0]: row for row in rows}
        assert by_project[project_a][1] == role_a
        assert by_project[project_b][1] == role_b
        assert by_project[project_a][2] == "available"
        assert by_project[project_b][2] == "available"
        assert _json_cell(by_project[project_a][3])["project"] == project_a
        assert _json_cell(by_project[project_b][3])["project"] == project_b

        roster_a = http_client.get("/roster", headers={"X-Project": project_a})
        roster_b = http_client.get("/roster", headers={"X-Project": project_b})
        assert roster_a.status_code == 200, roster_a.text
        assert roster_b.status_code == 200, roster_b.text
        roster_a_agents = roster_a.json()["agents"]
        roster_b_agents = roster_b.json()["agents"]
        assert roster_a_agents[0]["name"] == agent
        assert roster_a_agents[0]["role"] == role_a
        assert roster_a_agents[0]["model"] is None
        assert roster_b_agents[0]["name"] == agent
        assert roster_b_agents[0]["role"] == role_b
        assert roster_b_agents[0]["model"] is None
        if "writer_scope" in roster_a_agents[0]:
            assert roster_a_agents[0]["writer_scope"] == "work"
        if "writer_scope" in roster_b_agents[0]:
            assert roster_b_agents[0]["writer_scope"] == "work"
    finally:
        _cleanup_project(http_client, project_a)
        _cleanup_project(http_client, project_b)


def test_register_agent_handler_no_longer_writes_redis_roster_state():
    source = (REPO_ROOT / "api" / "main.py").read_text()
    block = source.split("async def register_agent(", 1)[1].split(
        "# ---------------------------------------------------------------------------\n# POST /analysis/session/",
        1,
    )[0]
    assert ".hset(" not in block
    assert ".xadd(" not in block
    assert "emit_team_event(" in block


def test_agent_runtime_state_migration_shape():
    migration = (
        REPO_ROOT
        / "data"
        / "migrations"
        / "2026-05-17-agent-runtime-state-jsonb.sql"
    ).read_text()
    assert "ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'available'" in migration
    assert "ADD COLUMN IF NOT EXISTS runtime_state JSONB NOT NULL DEFAULT '{}'::jsonb" in migration
    assert "CREATE INDEX IF NOT EXISTS idx_agents_project_status" in migration
