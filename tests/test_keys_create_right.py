"""Revocable project-create authority on a disposable registry."""

from __future__ import annotations

import asyncio
import os
import secrets
import uuid

import asyncpg
import pytest
from fastapi.testclient import TestClient

from cortex_v2 import config
from cortex_v2.app import create_app
from cortex_v2.store import token_digest
from test_keys_project_authority import Registry, _app, _seed, _url


async def _require_create(
    conn: asyncpg.Connection, caller: uuid.UUID, source_scope: uuid.UUID
) -> None:
    async with conn.transaction():
        await conn.execute("SELECT set_config('cortex.principal_id',$1,true)", str(caller))
        await conn.fetchval("SELECT cortex_auth.require_project_create($1,$2)", caller, source_scope)


async def _allow_create(
    conn: asyncpg.Connection, owner: uuid.UUID, alias: str, target: uuid.UUID, allowed: bool
) -> None:
    async with conn.transaction():
        await conn.execute("SELECT set_config('cortex.principal_id',$1,true)", str(owner))
        await conn.fetchval(
            "SELECT cortex_auth.allow_project_create($1,$2,$3,$4)",
            owner, alias, target, allowed,
        )

@pytest.mark.skipif(not os.getenv("CORTEX_V2_D53_APP_DATABASE_URL"), reason="disposable pgvector DB required")
def test_create_right_default_deny_owner_only_grant_and_revoke() -> None:
    async def check() -> None:
        r = await _seed()
        conn = await _app()
        migrator = await asyncpg.connect(_url("MIGRATOR"))
        try:
            await _require_create(conn, r.owner, r.project)
            with pytest.raises(asyncpg.PostgresError) as missing:
                await _require_create(conn, r.lead, r.project)
            assert missing.value.sqlstate == "PZC01"
            with pytest.raises(asyncpg.PostgresError) as no_right:
                await _allow_create(conn, r.owner, f"d53-{r.project}", r.lead, False)
            assert no_right.value.sqlstate == "P0002"
            await _allow_create(conn, r.owner, f"d53-{r.project}", r.lead, True)
            await _require_create(conn, r.lead, r.project)
            await _allow_create(conn, r.owner, f"d53-{r.project}", r.lead, False)
            with pytest.raises(asyncpg.PostgresError) as revoked:
                await _require_create(conn, r.lead, r.project)
            assert revoked.value.sqlstate == "PZC01"
            with pytest.raises(asyncpg.PostgresError) as member:
                await _require_create(conn, r.member, r.project)
            assert member.value.sqlstate == "PZC01"
            actions = await migrator.fetch(
                "SELECT action_type,caller_principal_id,target_scope_id,"
                "target_principal_id,detail->>'allowed' AS allowed "
                "FROM cortex_auth.privileged_actions "
                "WHERE action_type='allow_project_create' AND target_scope_id=$1",
                r.project,
            )
            assert len(actions) == 2
            assert {
                (row["action_type"], row["caller_principal_id"], row["target_scope_id"],
                 row["target_principal_id"], row["allowed"])
                for row in actions
            } == {
                ("allow_project_create", r.owner, r.project, r.lead, "true"),
                ("allow_project_create", r.owner, r.project, r.lead, "false"),
            }
        finally:
            await conn.close()
            await migrator.close()
    asyncio.run(check())


@pytest.mark.skipif(not os.getenv("CORTEX_V2_D53_APP_DATABASE_URL"), reason="disposable pgvector DB required")
def test_grant_does_not_authorize_every_agent_lead() -> None:
    async def check() -> None:
        r = await _seed()
        conn, migrator = await _app(), await asyncpg.connect(_url("MIGRATOR"))
        try:
            await migrator.execute(
                "INSERT INTO cortex_auth.scope_grants(principal_id,scope_id,can_read,can_write) "
                "VALUES($1,$2,true,true)",
                r.other_lead, r.project,
            )
            await migrator.execute(
                "INSERT INTO cortex_auth.memberships(scope_id,actor_id,membership_role) "
                "VALUES($1,$2,'lead')",
                r.project, r.other_lead,
            )
            await _allow_create(conn, r.owner, f"d53-{r.project}", r.lead, True)
            await _require_create(conn, r.lead, r.project)
            with pytest.raises(asyncpg.PostgresError) as denied:
                await _require_create(conn, r.other_lead, r.project)
            assert denied.value.sqlstate == "PZC01"
            await _allow_create(conn, r.owner, f"d53-{r.other_project}", r.other_lead, True)
            with pytest.raises(asyncpg.PostgresError) as no_right_here:
                await _allow_create(conn, r.owner, f"d53-{r.project}", r.other_lead, False)
            assert no_right_here.value.sqlstate == "P0002"
            await _require_create(conn, r.other_lead, r.other_project)
            await _allow_create(conn, r.owner, f"d53-{r.project}", r.other_lead, True)
            await _require_create(conn, r.other_lead, r.project)
            await _allow_create(conn, r.owner, f"d53-{r.project}", r.lead, False)
            with pytest.raises(asyncpg.PostgresError) as revoked:
                await _require_create(conn, r.lead, r.project)
            assert revoked.value.sqlstate == "PZC01"
            await _require_create(conn, r.other_lead, r.project)
            for target in (r.member, uuid.uuid4()):
                with pytest.raises(asyncpg.PostgresError) as invalid_target:
                    await _allow_create(conn, r.owner, f"d53-{r.project}", target, True)
                assert invalid_target.value.sqlstate == "PZC01"
            await _allow_create(conn, r.owner, f"d53-{r.project}", r.other_lead, False)
            with pytest.raises(asyncpg.PostgresError) as revoked:
                await _require_create(conn, r.other_lead, r.project)
            assert revoked.value.sqlstate == "PZC01"
            await _require_create(conn, r.other_lead, r.other_project)
        finally:
            await conn.close()
            await migrator.close()
    asyncio.run(check())


@pytest.mark.skipif(not os.getenv("CORTEX_V2_D53_APP_DATABASE_URL"), reason="disposable pgvector DB required")
def test_demoted_lead_cannot_use_stale_create_right() -> None:
    async def check() -> None:
        r = await _seed()
        app, migrator = await _app(), await asyncpg.connect(_url("MIGRATOR"))
        try:
            await _allow_create(app, r.owner, f"d53-{r.project}", r.lead, True)
            await migrator.execute(
                "UPDATE cortex_auth.memberships SET membership_role='member' WHERE scope_id=$1 AND actor_id=$2",
                r.project, r.lead,
            )
            with pytest.raises(asyncpg.PostgresError) as denied:
                await _require_create(app, r.lead, r.project)
            assert denied.value.sqlstate == "PZC01"
            await _allow_create(app, r.owner, f"d53-{r.project}", r.lead, False)
            await _allow_create(app, r.owner, f"d53-{r.project}", r.lead, False)
            assert await migrator.fetchval(
                "SELECT count(*) FROM cortex_auth.privileged_actions "
                "WHERE action_type='allow_project_create' AND target_scope_id=$1 "
                "AND target_principal_id=$2 AND detail->>'allowed'='false' "
                "AND detail->>'affected'='0'",
                r.project, r.lead,
            ) == 1
            await migrator.execute(
                "UPDATE cortex_auth.memberships SET membership_role='lead' "
                "WHERE scope_id=$1 AND actor_id=$2",
                r.project, r.lead,
            )
            with pytest.raises(asyncpg.PostgresError) as revoked:
                await _require_create(app, r.lead, r.project)
            assert revoked.value.sqlstate == "PZC01"
            await _allow_create(app, r.owner, f"d53-{r.project}", r.lead, True)
            await _require_create(app, r.lead, r.project)
            await migrator.execute(
                "UPDATE cortex_auth.principals SET status='revoked' WHERE principal_id=$1",
                r.lead,
            )
            await _allow_create(app, r.owner, f"d53-{r.project}", r.lead, False)
            assert await migrator.fetchval(
                "SELECT revoked_at IS NOT NULL FROM cortex_auth.project_create_rights "
                "WHERE project_scope_id=$1 AND principal_id=$2",
                r.project, r.lead,
            )
        finally:
            await app.close()
            await migrator.close()
    asyncio.run(check())

@pytest.mark.skipif(not os.getenv("CORTEX_V2_D53_APP_DATABASE_URL"), reason="disposable pgvector DB required")
def test_foreign_installation_owner_cannot_grant_create_right() -> None:
    async def check() -> None:
        r = await _seed()
        conn = await _app()
        migrator = await asyncpg.connect(_url("MIGRATOR"))
        foreign_installation, foreign_scope = uuid.uuid4(), uuid.uuid4()
        alias = f"d53-{foreign_scope}"
        try:
            await migrator.execute(
                "INSERT INTO cortex_auth.installations(installation_id,display_name) VALUES($1,'foreign')",
                foreign_installation,
            )
            await migrator.execute(
                "INSERT INTO cortex_core.scopes(scope_id,scope_kind,display_name) VALUES($1,'project','foreign')",
                foreign_scope,
            )
            await migrator.execute(
                "INSERT INTO cortex_core.scope_aliases(alias,scope_id,is_primary) VALUES($1,$2,true)",
                alias, foreign_scope,
            )
            await migrator.execute(
                "INSERT INTO cortex_auth.project_installations(scope_id,installation_id) VALUES($1,$2)",
                foreign_scope, foreign_installation,
            )
            with pytest.raises(asyncpg.PostgresError) as denied:
                await _allow_create(conn, r.owner, alias, r.lead, True)
            assert denied.value.sqlstate == "PZC01"
            foreign_lead = uuid.uuid4()
            await migrator.execute(
                "INSERT INTO cortex_auth.principals(principal_id,installation_id,principal_name,status) "
                "VALUES($1,$2,'foreign lead','active')",
                foreign_lead, foreign_installation,
            )
            await migrator.execute(
                "INSERT INTO cortex_auth.actors(actor_id,installation_id,actor_kind,display_name) "
                "VALUES($1,$2,'agent','foreign lead')",
                foreign_lead, foreign_installation,
            )
            await migrator.execute(
                "INSERT INTO cortex_auth.actor_bindings(actor_id,principal_id,bound_by_principal_id) "
                "VALUES($1,$1,$1)",
                foreign_lead,
            )
            await migrator.execute(
                "INSERT INTO cortex_auth.scope_grants(principal_id,scope_id,can_read,can_write) "
                "VALUES($1,$2,true,true)",
                foreign_lead, r.project,
            )
            await migrator.execute(
                "INSERT INTO cortex_auth.memberships(scope_id,actor_id,membership_role) "
                "VALUES($1,$2,'lead')",
                r.project, foreign_lead,
            )
            with pytest.raises(asyncpg.PostgresError) as foreign_target:
                await _allow_create(conn, r.owner, f"d53-{r.project}", foreign_lead, True)
            assert foreign_target.value.sqlstate == "PZC01"
            count = await migrator.fetchval(
                "SELECT count(*) FROM cortex_auth.privileged_actions "
                "WHERE action_type='allow_project_create' AND target_scope_id=$1",
                foreign_scope,
            )
            assert count == 0
        finally:
            await conn.close()
            await migrator.close()
    asyncio.run(check())



@pytest.mark.skipif(not os.getenv("CORTEX_V2_D53_APP_DATABASE_URL"), reason="disposable pgvector DB required")
def test_member_with_forged_scope_header_cannot_allow_create(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pepper = secrets.token_bytes(32)
    owner_token, member_token = secrets.token_urlsafe(32), secrets.token_urlsafe(32)

    async def arrange() -> Registry:
        r = await _seed()
        migrator = await asyncpg.connect(_url("MIGRATOR"))
        try:
            for principal, token in ((r.owner, owner_token), (r.member, member_token)):
                await migrator.execute(
                    "INSERT INTO cortex_auth.credentials(credential_id,principal_id,token_hash,generation,expires_at) VALUES($1,$2,$3,1,transaction_timestamp()+interval '180 days')",
                    uuid.uuid4(), principal, token_digest(token, pepper),
                )
        finally:
            await migrator.close()
        return r

    r = asyncio.run(arrange())
    monkeypatch.setenv("CORTEX_V2_SANDBOX_INSTANCE", config.PRODUCTION_INSTANCE)
    monkeypatch.setattr(
        config.Settings, "from_env",
        classmethod(lambda cls: cls(_url("APP"), pepper, config.PRODUCTION_INSTANCE)),
    )
    alias = f"d53-{r.project}"
    with TestClient(create_app()) as client:
        owner_key = uuid.uuid4().hex
        granted = client.post(
            f"/v1/projects/{alias}:allow-create",
            json={"principal_id": str(r.lead), "allowed": True},
            headers={"Authorization": f"Bearer {owner_token}", "Idempotency-Key": owner_key},
        )
        assert granted.status_code == 200
        assert granted.json()["data"]["operation"] == "project.allow_create"
        assert granted.json()["data"]["project"] == alias
        assert granted.json()["data"]["principal_id"] == str(r.lead)
        assert granted.json()["data"]["allowed"] is True
        reused_for_other_target = client.post(
            f"/v1/projects/{alias}:allow-create",
            json={"principal_id": str(r.other_lead), "allowed": True},
            headers={"Authorization": f"Bearer {owner_token}", "Idempotency-Key": owner_key},
        )
        assert reused_for_other_target.status_code == 409
        assert reused_for_other_target.json()["error"]["code"] == "idempotency_key_reused"
        forged = client.post(
            f"/v1/projects/{alias}:allow-create",
            json={"principal_id": str(r.lead), "allowed": True},
            headers={
                "Authorization": f"Bearer {member_token}",
                "Idempotency-Key": uuid.uuid4().hex,
                "X-Cortex-Scope": alias,
            },
        )
        assert forged.status_code == 403
        assert forged.json()["error"]["code"] == "project_create_not_allowed"
        wrong_project = client.post(
            f"/v1/projects/d53-{uuid.uuid4()}:allow-create",
            json={"principal_id": str(r.lead), "allowed": True},
            headers={
                "Authorization": f"Bearer {owner_token}",
                "Idempotency-Key": uuid.uuid4().hex,
                "X-Cortex-Scope": alias,
            },
        )
        assert wrong_project.status_code == 403
        assert wrong_project.json()["error"]["code"] == "project_create_not_allowed"
        wrong_target = client.post(
            f"/v1/projects/{alias}:allow-create",
            json={"principal_id": str(r.member), "allowed": True},
            headers={
                "Authorization": f"Bearer {owner_token}",
                "Idempotency-Key": uuid.uuid4().hex,
            },
        )
        assert wrong_target.status_code == 403
        assert wrong_target.json()["error"]["code"] == "project_create_not_allowed"
        no_right = client.post(
            f"/v1/projects/{alias}:allow-create",
            json={"principal_id": str(r.other_lead), "allowed": False},
            headers={
                "Authorization": f"Bearer {owner_token}",
                "Idempotency-Key": uuid.uuid4().hex,
            },
        )
        assert no_right.status_code == 404
        assert no_right.json()["error"]["code"] == "registry_target_not_found"
