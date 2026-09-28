"""Fresh owner enrollment persists a database-issued credential lifetime."""

from __future__ import annotations

import asyncio
import os
import secrets
import uuid
from datetime import datetime, timedelta

import asyncpg
import pytest
from fastapi.testclient import TestClient

from cortex_v2 import config
from cortex_v2.app import create_app
from cortex_v2.store import token_digest
from test_keys_project_authority import _seed, _url


async def _owner(pepper: bytes, owner_token: str) -> uuid.UUID:
    registry = await _seed()
    conn = await asyncpg.connect(_url("MIGRATOR"))
    try:
        await conn.execute(
            "INSERT INTO cortex_auth.credentials(credential_id,principal_id,token_hash,"
            "generation,expires_at) VALUES($1,$2,$3,1,now()+interval '180 days')",
            uuid.uuid4(), registry.owner, token_digest(owner_token, pepper),
        )
    finally:
        await conn.close()
    return registry.owner


def _settings(monkeypatch: pytest.MonkeyPatch, pepper: bytes) -> None:
    monkeypatch.setenv("CORTEX_V2_SANDBOX_INSTANCE", config.PRODUCTION_INSTANCE)
    monkeypatch.setattr(
        config.Settings, "from_env",
        classmethod(lambda cls: cls(_url("APP"), pepper, config.PRODUCTION_INSTANCE)),
    )


async def _stored_credential(credential_id: str) -> asyncpg.Record:
    conn = await asyncpg.connect(_url("MIGRATOR"))
    try:
        return await conn.fetchrow(
            "SELECT c.expires_at, c.created_at, c.principal_id "
            "FROM cortex_auth.credentials AS c WHERE c.credential_id=$1",
            uuid.UUID(credential_id),
        )
    finally:
        await conn.close()


@pytest.mark.skipif(
    not os.getenv("CORTEX_V2_D53_APP_DATABASE_URL"),
    reason="disposable pgvector database required",
)
def test_owner_enrollment_receipt_matches_database_180_day_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pepper = secrets.token_bytes(32)
    owner_token = secrets.token_urlsafe(32)

    owner_id = asyncio.run(_owner(pepper, owner_token))
    _settings(monkeypatch, pepper)
    with TestClient(create_app()) as client:
        rejected_name = f"s4-short-{uuid.uuid4().hex}"
        rejected_key = uuid.uuid4().hex
        rejected = client.post(
            "/v1/auth/principals:enroll",
            json={
                "principal_name": rejected_name,
                "actor_kind": "agent",
                "expires_in_seconds": 1,
            },
            headers={
                "Authorization": f"Bearer {owner_token}",
                "Idempotency-Key": rejected_key,
            },
        )
        assert rejected.status_code == 422
        assert rejected.json()["error"]["code"] == "invalid_request"

        async def inspect_rejection() -> tuple[int, int]:
            conn = await asyncpg.connect(_url("MIGRATOR"))
            try:
                issued = await conn.fetchval(
                    "SELECT count(*) FROM cortex_auth.principals "
                    "WHERE principal_name=$1 AND installation_id=("
                    "SELECT installation_id FROM cortex_auth.principals WHERE principal_id=$2)",
                    rejected_name, owner_id,
                )
                receipts = await conn.fetchval(
                    "SELECT count(*) FROM cortex_core.command_receipts "
                    "WHERE principal_id=$1 AND operation='auth.enroll_principal' "
                    "AND idempotency_key=$2",
                    owner_id, rejected_key,
                )
                return issued, receipts
            finally:
                await conn.close()

        assert asyncio.run(inspect_rejection()) == (0, 0)
        response = client.post(
            "/v1/auth/principals:enroll",
            json={"principal_name": "s4-agent", "actor_kind": "agent"},
            headers={
                "Authorization": f"Bearer {owner_token}",
                "Idempotency-Key": uuid.uuid4().hex,
            },
        )
        assert response.status_code == 201
        data = response.json()["data"]
        assert data["expires_at"] is not None
        assert client.get(
            "/v1/auth/principal",
            headers={"Authorization": f"Bearer {data['token']}"},
        ).status_code == 200

    stored = asyncio.run(_stored_credential(data["credential_id"]))
    assert stored is not None
    assert stored["principal_id"] != owner_id
    assert datetime.fromisoformat(data["expires_at"]) == stored["expires_at"]
    assert stored["expires_at"] - stored["created_at"] == timedelta(days=180)


@pytest.mark.skipif(
    not os.getenv("CORTEX_V2_D53_APP_DATABASE_URL"),
    reason="disposable pgvector database required",
)
def test_rotated_replacement_receipt_matches_database_180_day_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pepper = secrets.token_bytes(32)
    owner_token = secrets.token_urlsafe(32)
    owner_id = asyncio.run(_owner(pepper, owner_token))
    _settings(monkeypatch, pepper)
    with TestClient(create_app()) as client:
        rejected_key = uuid.uuid4().hex
        rejected = client.post(
            "/v1/auth/credentials:rotate",
            json={"expires_in_seconds": 1},
            headers={
                "Authorization": f"Bearer {owner_token}",
                "Idempotency-Key": rejected_key,
            },
        )
        assert rejected.status_code == 422
        assert rejected.json()["error"]["code"] == "invalid_request"

        async def inspect_rejection() -> tuple[int, int]:
            conn = await asyncpg.connect(_url("MIGRATOR"))
            try:
                issued = await conn.fetchval(
                    "SELECT count(*) FROM cortex_auth.credentials WHERE principal_id=$1",
                    owner_id,
                )
                receipts = await conn.fetchval(
                    "SELECT count(*) FROM cortex_core.command_receipts "
                    "WHERE principal_id=$1 AND operation='auth.rotate_credential' "
                    "AND idempotency_key=$2",
                    owner_id, rejected_key,
                )
                return issued, receipts
            finally:
                await conn.close()

        assert asyncio.run(inspect_rejection()) == (1, 0)
        response = client.post(
            "/v1/auth/credentials:rotate",
            json={},
            headers={
                "Authorization": f"Bearer {owner_token}",
                "Idempotency-Key": uuid.uuid4().hex,
            },
        )
        assert response.status_code == 201
        data = response.json()["data"]
        assert data["expires_at"] is not None
        assert client.get(
            "/v1/auth/principal",
            headers={"Authorization": f"Bearer {data['token']}"},
        ).status_code == 200

    stored = asyncio.run(_stored_credential(data["credential_id"]))
    assert stored is not None
    assert stored["principal_id"] == owner_id
    assert datetime.fromisoformat(data["expires_at"]) == stored["expires_at"]
    assert stored["expires_at"] - stored["created_at"] == timedelta(days=180)


@pytest.mark.skipif(
    not os.getenv("CORTEX_V2_D53_APP_DATABASE_URL"),
    reason="disposable pgvector database required",
)
def test_single_query_auth_detail_classifies_expiry_without_exposing_revoked_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pepper = secrets.token_bytes(32)
    owner_token = secrets.token_urlsafe(32)

    async def check() -> dict[str, str]:
        owner_id = await _owner(pepper, owner_token)
        migrator = await asyncpg.connect(_url("MIGRATOR"))
        app = await asyncpg.connect(_url("APP"))
        issued = {
            name: secrets.token_urlsafe(32)
            for name in ("expired", "revoked", "legacy", "unknown")
        }
        try:
            expired_at = await migrator.fetchval("SELECT now() - interval '1 day'")
            for generation, name in enumerate(("expired", "revoked", "legacy"), 2):
                await migrator.execute(
                    "INSERT INTO cortex_auth.credentials("
                    "credential_id,principal_id,token_hash,generation,expires_at,revoked_at"
                    ") VALUES($1,$2,$3,$4,$5,$6)",
                    uuid.uuid4(), owner_id, token_digest(issued[name], pepper),
                    generation, None if name == "legacy" else expired_at,
                    expired_at if name == "revoked" else None,
                )

            async def status(token: str) -> asyncpg.Record | None:
                return await app.fetchrow(
                    "SELECT principal_id,installation_id,expires_at,is_expired "
                    "FROM cortex_auth.authenticate($1)",
                    token_digest(token, pepper),
                )

            live = await status(owner_token)
            assert live is not None
            assert live["principal_id"] == owner_id
            assert live["expires_at"] is not None and live["is_expired"] is False
            expired = await status(issued["expired"])
            assert expired is not None
            assert expired["expires_at"] == expired_at
            assert expired["is_expired"] is True
            assert await status(issued["revoked"]) is None
            assert await status(issued["unknown"]) is None
            legacy = await status(issued["legacy"])
            assert legacy is not None
            assert legacy["expires_at"] is None
            assert legacy["is_expired"] is False
            with pytest.raises(asyncpg.PostgresError) as forbidden:
                await app.fetchval("SELECT count(*) FROM cortex_auth.credentials")
            assert forbidden.value.sqlstate == "42501"
        finally:
            await app.close()
            await migrator.close()
        return issued

    issued = asyncio.run(check())
    _settings(monkeypatch, pepper)
    with TestClient(create_app()) as client:
        assert client.get(
            "/v1/auth/principal",
            headers={"Authorization": f"Bearer {owner_token}"},
        ).status_code == 200
        for name in ("expired", "revoked", "unknown"):
            response = client.get(
                "/v1/auth/principal",
                headers={"Authorization": f"Bearer {issued[name]}"},
            )
            assert response.status_code == 401
            assert response.json()["error"]["code"] == "invalid_credential"


@pytest.mark.skipif(
    not os.getenv("CORTEX_V2_D53_SANDBOX_APP_DATABASE_URL")
    or not os.getenv("CORTEX_V2_D53_SANDBOX_MIGRATOR_DATABASE_URL"),
    reason="separate disposable 0001-only sandbox database required",
)
def test_0001_only_sandbox_authenticates_without_full_auth_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pepper = secrets.token_bytes(32)
    token = secrets.token_urlsafe(32)
    installation_id, principal_id = uuid.uuid4(), uuid.uuid4()
    app_url = os.environ["CORTEX_V2_D53_SANDBOX_APP_DATABASE_URL"]

    async def arrange() -> None:
        conn = await asyncpg.connect(
            os.environ["CORTEX_V2_D53_SANDBOX_MIGRATOR_DATABASE_URL"]
        )
        try:
            await conn.execute(
                "INSERT INTO cortex_auth.principals("
                "principal_id,installation_id,principal_name,status"
                ") VALUES($1,$2,'sandbox owner','active')",
                principal_id, installation_id,
            )
            await conn.execute(
                "INSERT INTO cortex_auth.credentials("
                "credential_id,principal_id,token_hash,generation,expires_at"
                ") VALUES($1,$2,$3,1,now()+interval '180 days')",
                uuid.uuid4(), principal_id, token_digest(token, pepper),
            )
        finally:
            await conn.close()

    asyncio.run(arrange())
    monkeypatch.setenv("CORTEX_V2_SANDBOX_INSTANCE", config.SANDBOX_INSTANCE)
    monkeypatch.setattr(
        config.Settings, "from_env",
        classmethod(lambda cls: cls(app_url, pepper, config.SANDBOX_INSTANCE)),
    )
    with TestClient(create_app()) as client:
        response = client.get(
            "/v1/auth/principal",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200
        assert response.json()["data"]["principal_id"] == str(principal_id)
        assert response.json()["data"]["installation_id"] == str(installation_id)
