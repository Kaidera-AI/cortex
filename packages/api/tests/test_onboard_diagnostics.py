import importlib.util
from datetime import UTC, datetime
from pathlib import Path

import pytest


API_MAIN_PATH = Path(__file__).resolve().parents[1] / "main.py"


def load_api_module():
    spec = importlib.util.spec_from_file_location(
        "cortex_api_main_onboard_diagnostics_test",
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


class OnboardConn:
    async def execute(self, sql, *args):
        if "set_config('cortex.project'" in sql:
            return "SELECT 1"
        raise AssertionError(f"Unexpected execute SQL: {sql}")

    async def fetch(self, sql, *args):
        if "FROM agents a" in sql and "agent_profiles ap" in sql and "ORDER BY agent_name" in sql:
            return [{"agent_name": "kai"}, {"agent_name": "ren"}]
        if "FROM decisions" in sql and "Cortex v2 onboarding complete" in sql:
            return [
                {
                    "agent_name": "kai@kaidera-os",
                    "count": 1,
                    "latest_at": datetime(2026, 5, 31, 12, 0, tzinfo=UTC),
                }
            ]
        if "status = 'claimed'" in sql:
            return [
                {
                    "compound_id": "abc12345@kaidera-os",
                    "claimed_by": "ren@kaidera-os",
                    "priority": "high",
                    "minutes": 45,
                    "summary": "stale handoff",
                }
            ]
        if "FROM handoffs h" in sql and "handoff_completed" in sql:
            return []
        if "WITH roster AS" in sql and "agent_diaries" in sql:
            return [
                {
                    "agent_name": "ren",
                    "claimed_today": 2,
                    "diary_today": 0,
                }
            ]
        raise AssertionError(f"Unexpected fetch SQL: {sql}")


@pytest.mark.asyncio
async def test_onboard_diagnostics_agent_status_and_closure():
    api = load_api_module()
    conn = OnboardConn()
    pool = FakePool(conn)
    api.pool = pool
    api.pool_app = pool
    api.pool_admin = pool

    result = await api.onboard_diagnostics(
        agent="kai",
        closure=True,
        x_project="kaidera-os",
    )

    assert result["project"] == "kaidera-os"
    assert result["agents"] == [
        {
            "agent": "kai",
            "onboarded": True,
            "count": 1,
            "latest_at": "2026-05-31T12:00:00+00:00",
        }
    ]
    assert result["closure"]["stale_handoffs"][0]["compound_id"] == "abc12345@kaidera-os"
    assert result["closure"]["completed_without_lifecycle_report"] == []
    assert result["closure"]["agents_missing_diary"][0]["agent_name"] == "ren"
