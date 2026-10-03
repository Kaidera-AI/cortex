"""S8 client metadata against actual API authentication and disposable Postgres."""

from __future__ import annotations

import asyncio
import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from cortex_v2.app import create_app
from cortex_v2.clients.client import CortexClient
from cortex_v2.clients.config import ClientProfile
from cortex_v2.clients.errors import CortexApiError
from cortex_v2.clients.transport import HttpResponse
from test_keys_api_expiry import _issue_credentials
from test_keys_issuance_t21 import _settings
from test_keys_project_authority import _seed

pytestmark = pytest.mark.skipif(
    not os.getenv("CORTEX_V2_D53_APP_DATABASE_URL")
    or not os.getenv("CORTEX_V2_D53_MIGRATOR_DATABASE_URL"),
    reason="disposable pgvector database required",
)


def test_real_authenticated_api_keeps_expiry_on_success_and_denial(monkeypatch) -> None:
    pepper = secrets.token_bytes(32)
    token = secrets.token_urlsafe(32)
    registry = asyncio.run(_seed())
    expiry = datetime.now(timezone.utc) + timedelta(days=365)
    asyncio.run(_issue_credentials(pepper, [(registry.member, token, 1, expiry)]))
    _settings(monkeypatch, pepper)
    profile = ClientProfile(
        base_url="http://testserver", token=token,
        default_scope=f"d53-{registry.project}", default_read_scopes=(),
        installation_label=None, principal_label=None, source="synthetic test",
    )

    with TestClient(create_app()) as api:
        def transport(method, url, *, headers, json_body, query, timeout):
            response = api.request(
                method, urlsplit(url).path, headers=headers,
                json=json_body, params=query,
            )
            return HttpResponse(response.status_code, dict(response.headers), response.content)

        client = CortexClient(profile, transport=transport)
        success = client.call("auth.principal")
        assert success.status == 200
        assert success.key_expires_at == expiry.isoformat()
        with pytest.raises(CortexApiError) as rejected:
            client.call(
                "project.allow_create", path_params={"project": profile.default_scope},
                payload={"principal_id": str(registry.lead), "allowed": True},
                idempotency_key=uuid.uuid4().hex,
            )
        assert rejected.value.status == 403
        assert rejected.value.code == "project_create_not_allowed"
        assert rejected.value.key_expires_at == expiry.isoformat()
