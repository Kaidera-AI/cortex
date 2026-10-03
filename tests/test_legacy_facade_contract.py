"""L2 first RED: today's project reader, using new v2 credentials only.

Contract frozen from the Console's get_projects_strict and the existing API's
list_projects. This is a RED freeze, not a completed facade or release gate.
"""

from __future__ import annotations

import asyncio
import os
import secrets
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from cortex_v2.app import create_app
from test_keys_api_expiry import _issue_credentials
from test_keys_issuance_t21 import _settings
from test_keys_project_authority import _seed

PROJECT_FIELDS = frozenset({
    "project_key", "project_id", "display_name", "default_agent", "status",
    "parent_project_key", "repo_root", "created_at", "updated_at",
    "agent_count", "profile_count", "roots",
})
pytestmark = pytest.mark.skipif(
    not os.getenv("CORTEX_V2_D53_APP_DATABASE_URL")
    or not os.getenv("CORTEX_V2_D53_MIGRATOR_DATABASE_URL"),
    reason="disposable pgvector database required",
)


@pytest.mark.parametrize("token", (
    None, "ctx1_" + "0" * 32 + "." + "q" * 43, "q" * 43,
))
def test_projects_requires_new_valid_v2_credential(monkeypatch, token) -> None:
    _settings(monkeypatch, secrets.token_bytes(32))
    headers = {"X-Project": "other-project", "X-Agent-Name": "owner"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    with TestClient(create_app()) as client:
        response = client.get("/projects", headers=headers)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_credential"


def test_projects_returns_real_legacy_fields_with_member_scope_only(monkeypatch) -> None:
    registry = asyncio.run(_seed())
    pepper, token = secrets.token_bytes(32), secrets.token_urlsafe(32)
    asyncio.run(_issue_credentials(pepper, [
        (registry.member, token, 1, datetime.now(timezone.utc) + timedelta(days=365)),
    ]))
    _settings(monkeypatch, pepper)
    with TestClient(create_app()) as client:
        headers = {
            "Authorization": f"Bearer {token}",
            # Neither identity nor project header may elevate the bearer.
            "X-Agent-Name": "owner", "X-Project": f"d53-{registry.other_project}",
        }
        assert client.get("/v1/auth/principal", headers=headers).status_code == 200
        response = client.get("/projects", headers=headers)
    assert response.status_code == 200
    projects = response.json()["projects"]
    assert isinstance(projects, list)
    assert len(projects) == 1
    row = projects[0]
    assert PROJECT_FIELDS <= row.keys()
    assert row["project_key"] == f"d53-{registry.project}"
    assert row["project_id"] == str(registry.project)
    assert row["display_name"] == "one"
    assert row["status"] == "active"
    assert isinstance(row["roots"], list)
