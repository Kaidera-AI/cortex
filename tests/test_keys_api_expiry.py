"""Public API behavior for database-issued credential expirations."""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest
from fastapi.testclient import TestClient

import cortex_v2.app as app_module
from cortex_v2.app import create_app
from cortex_v2.identity import _translate
from cortex_v2.store import token_digest
from test_keys_issuance_t21 import _settings
from test_keys_project_authority import _seed, _url


pytestmark = pytest.mark.skipif(
    not os.getenv("CORTEX_V2_D53_APP_DATABASE_URL")
    or not os.getenv("CORTEX_V2_D53_MIGRATOR_DATABASE_URL"),
    reason="disposable pgvector database required",
)


async def _issue_credentials(
    pepper: bytes,
    credentials: list[tuple[uuid.UUID, str, int, datetime]],
) -> None:
    conn = await asyncpg.connect(_url("MIGRATOR"))
    try:
        async with conn.transaction():
            for principal_id, token, generation, expires_at in credentials:
                await conn.execute(
                    "INSERT INTO cortex_auth.credentials("
                    "credential_id,principal_id,token_hash,generation,expires_at"
                    ") VALUES($1,$2,$3,$4,$5)",
                    uuid.uuid4(), principal_id, token_digest(token, pepper),
                    generation, expires_at,
                )
    finally:
        await conn.close()


def test_authenticated_responses_include_stored_expiry_even_on_denied_and_dynamic_routes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pepper = secrets.token_bytes(32)
    owner_token, member_token = (secrets.token_urlsafe(32) for _ in range(2))
    registry = asyncio.run(_seed())
    owner_expiry = datetime.now(timezone.utc) + timedelta(days=180)
    member_expiry = owner_expiry + timedelta(seconds=1)
    asyncio.run(_issue_credentials(pepper, [
        (registry.owner, owner_token, 1, owner_expiry),
        (registry.member, member_token, 1, member_expiry),
    ]))
    _settings(monkeypatch, pepper)
    alias = f"d53-{registry.project}"

    with TestClient(create_app()) as client:
        profile = client.get(
            "/v1/auth/principal",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        assert profile.status_code == 200
        assert profile.headers["cortex-key-expires"] == owner_expiry.isoformat()

        denied = client.post(
            f"/v1/projects/{alias}:allow-create",
            json={"principal_id": str(registry.lead), "allowed": True},
            headers={
                "Authorization": f"Bearer {member_token}",
                "Idempotency-Key": uuid.uuid4().hex,
            },
        )
        assert denied.status_code == 403
        assert denied.json()["error"]["code"] == "project_create_not_allowed"
        assert denied.headers["cortex-key-expires"] == member_expiry.isoformat()

        dynamic = client.get(
            "/v1/capabilities",
            headers={
                "Authorization": f"Bearer {owner_token}",
                "X-Cortex-Scope": alias,
            },
        )
        assert dynamic.status_code == 200
        assert dynamic.headers["cortex-key-expires"] == owner_expiry.isoformat()

        health = client.get(
            "/health/live",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        assert health.status_code == 200
        assert "cortex-key-expires" not in health.headers
        invalid = client.get(
            "/v1/auth/principal",
            headers={"Authorization": f"Bearer {secrets.token_urlsafe(32)}"},
        )
        assert invalid.status_code == 401
        assert "cortex-key-expires" not in invalid.headers


def test_due_notice_starts_at_day_150_and_deduplicates_per_credential_utc_day(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    pepper = secrets.token_bytes(32)
    registry = asyncio.run(_seed())
    clock = [datetime.now(timezone.utc)]
    tokens = {name: secrets.token_urlsafe(32) for name in ("early", "due", "urgent")}
    asyncio.run(_issue_credentials(pepper, [
        (registry.owner, tokens["early"], 1, clock[0] + timedelta(days=30, microseconds=1)),
        (registry.owner, tokens["due"], 2, clock[0] + timedelta(days=30)),
        (registry.owner, tokens["urgent"], 3, clock[0] + timedelta(days=1)),
    ]))
    _settings(monkeypatch, pepper)

    class FixedClock(datetime):
        @classmethod
        def now(cls, tz: timezone | None = None) -> datetime:
            assert tz is timezone.utc
            return clock[0]

    monkeypatch.setattr(app_module, "datetime", FixedClock, raising=False)
    with caplog.at_level(logging.WARNING, logger="cortex_v2.api"):
        with TestClient(create_app()) as client:
            for name in ("early", "due", "due", "urgent", "urgent"):
                response = client.get(
                    "/v1/auth/principal",
                    headers={"Authorization": f"Bearer {tokens[name]}"},
                )
                assert response.status_code == 200
            notices = [
                record for record in caplog.records
                if record.name == "cortex_v2.api"
                and record.getMessage().startswith("observed_due_key ")
            ]
            assert len(notices) == 2
            assert all(
                f"principal_id={registry.owner}" in notice.getMessage()
                and "token_fingerprint=" in notice.getMessage()
                for notice in notices
            )
            assert len({
                notice.getMessage().split("token_fingerprint=", 1)[1].split()[0]
                for notice in notices
            }) == 2
            clock[0] += timedelta(days=1)
            assert client.get(
                "/v1/auth/principal",
                headers={"Authorization": f"Bearer {tokens['due']}"},
            ).status_code == 200
            notices = [
                record for record in caplog.records
                if record.name == "cortex_v2.api"
                and record.getMessage().startswith("observed_due_key ")
            ]
            assert len(notices) == 3

    assert not any(
        token in record.getMessage()
        or token_digest(token, pepper).hex() in record.getMessage()
        for token in tokens.values() for record in caplog.records
    )


def test_sql_key_manager_denial_translates_to_actionable_forbidden() -> None:
    registry = asyncio.run(_seed())

    async def denied():
        conn = await asyncpg.connect(_url("APP"))
        try:
            try:
                async with conn.transaction():
                    await conn.execute(
                        "SELECT set_config('cortex.principal_id',$1,true)",
                        str(registry.member),
                    )
                    await conn.fetchval(
                        "SELECT cortex_auth.require_project_key_manager($1,$2)",
                        registry.member, registry.project,
                    )
            except asyncpg.PostgresError as exc:
                assert exc.sqlstate == "PZK01"
                return _translate(exc)
            return None
        finally:
            await conn.close()

    problem = asyncio.run(denied())
    assert problem is not None
    assert problem.status == 403
    assert problem.code == "project_key_manager_required"
    assert "project" in problem.message and "lead" in problem.message


def test_issued_credential_due_notice_matches_issuance_receipt_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    pepper = secrets.token_bytes(32)
    owner_token = secrets.token_urlsafe(32)
    registry = asyncio.run(_seed())
    asyncio.run(_issue_credentials(pepper, [
        (registry.owner, owner_token, 1, datetime.now(timezone.utc) + timedelta(days=180)),
    ]))
    _settings(monkeypatch, pepper)

    with caplog.at_level(logging.WARNING, logger="cortex_v2.api"):
        with TestClient(create_app()) as client:
            issued = client.post(
                "/v1/auth/principals:enroll",
                json={
                    "principal_name": f"due-{uuid.uuid4().hex}",
                    "actor_kind": "agent",
                },
                headers={
                    "Authorization": f"Bearer {owner_token}",
                    "Idempotency-Key": uuid.uuid4().hex,
                },
            )
            assert issued.status_code == 201
            data = issued.json()["data"]

            frozen_due_time = datetime.fromisoformat(data["expires_at"]) - timedelta(days=29)

            class FixedClock(datetime):
                @classmethod
                def now(cls, tz: timezone | None = None) -> datetime:
                    assert tz is timezone.utc
                    return frozen_due_time

            monkeypatch.setattr(app_module, "datetime", FixedClock)
            authenticated = client.get(
                "/v1/auth/principal",
                headers={"Authorization": f"Bearer {data['token']}"},
            )
            assert authenticated.status_code == 200

    notices = [
        record.getMessage() for record in caplog.records
        if record.name == "cortex_v2.api"
        and record.getMessage().startswith("observed_due_key ")
    ]
    assert len(notices) == 1
    assert f"principal_id={data['principal_id']}" in notices[0]
    assert f"token_fingerprint={data['token_fingerprint']}" in notices[0]
    assert not any(
        data["token"] in record.getMessage()
        or token_digest(data["token"], pepper).hex() in record.getMessage()
        for record in caplog.records
    )
