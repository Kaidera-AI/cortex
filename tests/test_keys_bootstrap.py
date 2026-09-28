"""First-install behavior against a fresh disposable Cortex database."""

from __future__ import annotations

import asyncio
import os
import secrets
import time
import uuid
from datetime import datetime, timedelta

import asyncpg
import pytest
from fastapi.testclient import TestClient

from cortex_v2 import config, identity
from cortex_v2.app import create_app, create_bootstrap_app
from cortex_v2.store import ApiProblem


def test_production_profile_has_full_migrations_without_fixture() -> None:
    profile = config.INSTANCE_PROFILES[config.PRODUCTION_INSTANCE]
    assert profile.deployment_class == "production"
    assert profile.migrations == config.FULL_V2_MIGRATIONS


def test_public_http_never_exposes_bootstrap() -> None:
    client = TestClient(create_app(), raise_server_exceptions=False)
    response = client.post(
        "/v1/auth/bootstrap",
        json={"setup_code": "x" * 43, "installation_name": "X", "owner_name": "Y"},
    )
    assert response.status_code == 404


def test_expired_or_misbound_setup_code_is_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    installation = uuid.uuid4()
    token = "q" * 43
    monkeypatch.setattr(
        identity, "read_secret_path",
        lambda name: f"{token}\n0\n{installation}\n".encode(),
    )
    with pytest.raises(ApiProblem) as expired:
        identity.verify_bootstrap_code(token, installation)
    assert expired.value.status == 403

    monkeypatch.setattr(
        identity, "read_secret_path",
        lambda name: f"{token}\n{int(time.time())}\n{installation}\n".encode(),
    )
    with pytest.raises(ApiProblem) as misbound:
        identity.verify_bootstrap_code(token, uuid.uuid4())
    assert misbound.value.status == 403


@pytest.mark.skipif(
    not os.getenv("CORTEX_V2_D53_TEST_DATABASE_URL")
    or not os.getenv("CORTEX_V2_D53_MIGRATOR_DATABASE_URL"),
    reason="requires a fresh, disposable, unseeded Cortex database",
)
def test_private_bootstrap_issues_authentic_owner_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code, installation_id, pepper = (
        secrets.token_urlsafe(32), uuid.uuid4(), secrets.token_bytes(32)
    )
    configured_id = str(installation_id)
    monkeypatch.setenv("CORTEX_V2_SANDBOX_INSTANCE", config.PRODUCTION_INSTANCE)
    monkeypatch.setattr(
        config.Settings, "from_env",
        classmethod(
            lambda cls: cls(
                os.environ["CORTEX_V2_D53_TEST_DATABASE_URL"],
                pepper, config.PRODUCTION_INSTANCE, installation_id,
            )
        ),
    )
    monkeypatch.setattr(
        identity, "read_secret_path",
        lambda name: f"{code}\n{int(time.time())}\n{configured_id}\n".encode(),
    )
    request = {
        "setup_code": code,
        "installation_name": "First installation",
        "owner_name": "owner",
    }
    with TestClient(create_bootstrap_app()) as private:
        wrong = private.post("/v1/auth/bootstrap", json={**request, "setup_code": "z" * 43})
        assert wrong.status_code == 403
        assert wrong.json()["error"]["code"] == "bootstrap_denied"
        response = private.post("/v1/auth/bootstrap", json=request)
        assert response.status_code == 201
        data = response.json()["data"]
        assert data["installation_id"] == configured_id
        assert data["recovery_generation"] == 1
        assert data["owner_token"] != data["recovery_token"]
        assert data["owner_principal_id"] != data["installation_id"]
        assert data["expires_at"] is not None
        second = private.post("/v1/auth/bootstrap", json=request)
        assert second.status_code == 403
        assert second.json()["error"]["code"] == "bootstrap_denied"

    with TestClient(create_app()) as public:
        authenticated = public.get(
            "/v1/auth/principal", headers={"Authorization": f"Bearer {data['owner_token']}"}
        )
        assert authenticated.status_code == 200
        assert authenticated.json()["data"]["principal_id"] == data["owner_principal_id"]
        assert public.post("/v1/auth/bootstrap", json=request).status_code == 404

    async def assert_database_guard() -> None:
        conn = await asyncpg.connect(os.environ["CORTEX_V2_D53_TEST_DATABASE_URL"])
        try:
            with pytest.raises(asyncpg.PostgresError) as rejected:
                await conn.fetchrow(
                    "SELECT * FROM cortex_auth.bootstrap_installation($1, $2, $3, $4, $5)",
                    uuid.uuid4(), "Second installation", "new owner", os.urandom(32),
                    os.urandom(32),
                )
            assert rejected.value.sqlstate == "42501"
        finally:
            await conn.close()
        migrator = await asyncpg.connect(
            os.environ["CORTEX_V2_D53_MIGRATOR_DATABASE_URL"]
        )
        try:
            stored = await migrator.fetchrow(
                "SELECT expires_at,created_at FROM cortex_auth.credentials "
                "WHERE credential_id=$1",
                uuid.UUID(data["credential_id"]),
            )
            assert stored is not None
            assert datetime.fromisoformat(data["expires_at"]) == stored["expires_at"]
            assert stored["expires_at"] - stored["created_at"] == timedelta(days=180)
        finally:
            await migrator.close()

    asyncio.run(assert_database_guard())
