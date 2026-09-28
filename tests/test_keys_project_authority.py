"""Project key authority on a disposable, unseeded installation."""

from __future__ import annotations

import asyncio
import os
import secrets
import uuid
from dataclasses import dataclass

import asyncpg
import pytest
from cortex_v2.config import W1_INSTANCE
from cortex_v2.migrate import W1_FIXTURE_UUID_FIELDS, _seed_w1


@dataclass
class Registry:
    installation: uuid.UUID
    project: uuid.UUID
    other_project: uuid.UUID
    owner: uuid.UUID
    lead: uuid.UUID
    member: uuid.UUID
    other_lead: uuid.UUID
    sponsor_project: uuid.UUID


def _url(role: str) -> str:
    return os.environ[f"CORTEX_V2_D53_{role}_DATABASE_URL"]


async def _seed() -> Registry:
    conn = await asyncpg.connect(_url("MIGRATOR"))
    try:
        installation = uuid.uuid4()
        ids = [uuid.uuid4() for _ in range(7)]
        project, other_project, sponsor_project, owner, lead, member, other_lead = ids
        await conn.execute(
            "INSERT INTO cortex_auth.installations(installation_id,display_name) VALUES($1,'authority test')",
            installation,
        )
        for name, principal_id, kind in (
            ("owner", owner, "human"), ("lead", lead, "agent"),
            ("member", member, "service"), ("other", other_lead, "agent"),
        ):
            await conn.execute(
                "INSERT INTO cortex_auth.principals(principal_id, installation_id, principal_name, status) VALUES($1,$2,$3,'active')",
                principal_id, installation, name,
            )
            await conn.execute(
                "INSERT INTO cortex_auth.actors(actor_id,installation_id,actor_kind,display_name) VALUES($1,$2,$3,$4)",
                principal_id, installation, kind, name,
            )
            await conn.execute(
                "INSERT INTO cortex_auth.actor_bindings(actor_id,principal_id,bound_by_principal_id) VALUES($1,$1,$2)",
                principal_id, owner,
            )
        await conn.execute(
            "INSERT INTO cortex_auth.installation_owners(installation_id,principal_id) VALUES($1,$2)",
            installation, owner,
        )
        for scope_id, name in ((project, "one"), (other_project, "two"), (sponsor_project, "sponsored")):
            await conn.execute(
                "INSERT INTO cortex_core.scopes(scope_id,scope_kind,display_name) VALUES($1,'project',$2)",
                scope_id, name,
            )
            await conn.execute(
                "INSERT INTO cortex_core.scope_aliases(alias,scope_id,is_primary) VALUES($1,$2,true)",
                f"d53-{scope_id}", scope_id,
            )
            await conn.execute(
                "INSERT INTO cortex_auth.project_installations(scope_id,installation_id) VALUES($1,$2)",
                scope_id, installation,
            )
            await conn.execute(
                "INSERT INTO cortex_auth.scope_grants(principal_id,scope_id,can_read,can_write) VALUES($1,$2,true,true)",
                owner, scope_id,
            )
        for principal_id, scope_id in ((lead, project), (other_lead, other_project), (member, project)):
            await conn.execute(
                "INSERT INTO cortex_auth.scope_grants(principal_id,scope_id,can_read,can_write) VALUES($1,$2,true,true)",
                principal_id, scope_id,
            )
            await conn.execute(
                "INSERT INTO cortex_auth.memberships(scope_id,actor_id,membership_role) VALUES($1,$2,$3)",
                scope_id, principal_id, "member" if principal_id == member else "lead",
            )
        await conn.execute(
            "INSERT INTO cortex_auth.project_sponsors(project_scope_id,sponsor_principal_id,source_scope_id) VALUES($1,$2,$3)",
            sponsor_project, lead, project,
        )
        return Registry(installation, project, other_project, owner, lead, member, other_lead, sponsor_project)
    finally:
        await conn.close()


async def _app() -> asyncpg.Connection:
    return await asyncpg.connect(_url("APP"))

async def _require_manager(
    conn: asyncpg.Connection, caller: uuid.UUID, scope: uuid.UUID
) -> None:
    async with conn.transaction():
        await conn.execute("SELECT set_config('cortex.principal_id',$1,true)", str(caller))
        await conn.fetchval(
            "SELECT cortex_auth.require_project_key_manager($1,$2)", caller, scope
        )




@pytest.mark.skipif(not os.getenv("CORTEX_V2_D53_APP_DATABASE_URL"), reason="disposable pgvector DB required")
def test_lead_manages_only_own_project_keys() -> None:
    async def check() -> None:
        r = await _seed()
        conn = await _app()
        try:
            await _require_manager(conn, r.lead, r.project)
            await _require_manager(conn, r.owner, r.other_project)
            for caller, scope in ((r.lead, r.other_project), (r.member, r.project)):
                with pytest.raises(asyncpg.PostgresError) as denied:
                    await _require_manager(conn, caller, scope)
                assert denied.value.sqlstate == "PZK01"
        finally:
            await conn.close()
    asyncio.run(check())

@pytest.mark.parametrize("sponsored", (False, True))
@pytest.mark.skipif(not os.getenv("CORTEX_V2_D53_APP_DATABASE_URL"), reason="disposable pgvector DB required")
def test_read_only_lead_cannot_manage_any_project_keys(sponsored: bool) -> None:
    async def check() -> None:
        r = await _seed()
        migrator = await asyncpg.connect(_url("MIGRATOR"))
        conn = await _app()
        try:
            await migrator.execute(
                "UPDATE cortex_auth.scope_grants SET can_write=false WHERE principal_id=$1 AND scope_id=$2",
                r.lead, r.project,
            )
            with pytest.raises(asyncpg.PostgresError) as denied:
                await _require_manager(
                    conn, r.lead, r.sponsor_project if sponsored else r.project
                )
            assert denied.value.sqlstate == "PZK01"
        finally:
            await conn.close()
            await migrator.close()
    asyncio.run(check())



@pytest.mark.skipif(not os.getenv("CORTEX_V2_D53_APP_DATABASE_URL"), reason="disposable pgvector DB required")
def test_sponsor_manages_keys_but_cannot_read_sponsored_data() -> None:
    async def check() -> None:
        r = await _seed()
        conn = await _app()
        migrator = await asyncpg.connect(_url("MIGRATOR"))
        record = uuid.uuid4()
        try:
            await migrator.execute(
                """
                INSERT INTO cortex_core.memory_records(
                    scope_id,record_id,logical_record_id,record_type,revision,author_principal_id,body
                ) VALUES($1,$2,$2,'knowledge',1,$3,'private sponsored content')
                """,
                r.sponsor_project, record, r.owner,
            )
            await _require_manager(conn, r.lead, r.sponsor_project)
            for principal, visible in ((r.owner, True), (r.lead, False)):
                async with conn.transaction():
                    await conn.execute(
                        "SELECT set_config('cortex.principal_id',$1,true)", str(principal)
                    )
                    await conn.execute(
                        "SELECT set_config('cortex.read_scope_ids',$1,true)",
                        str(r.sponsor_project),
                    )
                    row = await conn.fetchrow(
                        "SELECT body FROM cortex_core.memory_records WHERE scope_id=$1 AND record_id=$2",
                        r.sponsor_project, record,
                    )
                    assert (row is not None) is visible
        finally:
            await conn.close()
            await migrator.close()
    asyncio.run(check())


@pytest.mark.parametrize("withdrawal", ("demotion", "grant_revocation", "target_deactivation", "installation_deactivation"))
@pytest.mark.skipif(not os.getenv("CORTEX_V2_D53_APP_DATABASE_URL"), reason="disposable pgvector DB required")
def test_demoted_lead_loses_sponsorship(withdrawal: str) -> None:
    async def check() -> None:
        r = await _seed()
        migrator = await asyncpg.connect(_url("MIGRATOR"))
        conn = await _app()
        try:
            if withdrawal == "demotion":
                await migrator.execute(
                    "UPDATE cortex_auth.memberships SET membership_role='member' WHERE scope_id=$1 AND actor_id=$2",
                    r.project, r.lead,
                )
            elif withdrawal == "grant_revocation":
                await migrator.execute(
                    "UPDATE cortex_auth.scope_grants SET revoked_at=now() WHERE scope_id=$1 AND principal_id=$2",
                    r.project, r.lead,
                )
            elif withdrawal == "target_deactivation":
                await migrator.execute(
                    "UPDATE cortex_core.scopes SET is_active=false WHERE scope_id=$1",
                    r.sponsor_project,
                )
            else:
                await migrator.execute(
                    "UPDATE cortex_auth.installations SET status='decommissioned' WHERE installation_id=$1",
                    r.installation,
                )
            with pytest.raises(asyncpg.PostgresError) as denied:
                await _require_manager(conn, r.lead, r.sponsor_project)
            assert denied.value.sqlstate == "PZK01"
        finally:
            await conn.close()
            await migrator.close()
    asyncio.run(check())


@pytest.mark.skipif(not os.getenv("CORTEX_V2_D53_APP_DATABASE_URL"), reason="disposable pgvector DB required")
def test_cross_installation_sponsorship_and_forged_identity_denied() -> None:
    async def check() -> None:
        r = await _seed()
        migrator = await asyncpg.connect(_url("MIGRATOR"))
        conn = await _app()
        foreign_installation, foreign_scope = uuid.uuid4(), uuid.uuid4()
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
                "INSERT INTO cortex_auth.project_installations(scope_id,installation_id) VALUES($1,$2)",
                foreign_scope, foreign_installation,
            )
            with pytest.raises(asyncpg.PostgresError) as invalid:
                await migrator.execute(
                    "INSERT INTO cortex_auth.project_sponsors(project_scope_id,sponsor_principal_id,source_scope_id) VALUES($1,$2,$3)",
                    foreign_scope, r.lead, r.project,
                )
            assert invalid.value.sqlstate == "23514"
            with pytest.raises(asyncpg.PostgresError) as denied:
                await _require_manager(conn, r.lead, foreign_scope)
            assert denied.value.sqlstate == "PZK01"
            async with conn.transaction():
                await conn.execute("SELECT set_config('cortex.principal_id',$1,true)", str(r.member))
                with pytest.raises(asyncpg.PostgresError) as forged:
                    await conn.fetchval(
                        "SELECT cortex_auth.require_project_key_manager($1,$2)", r.lead, r.project
                    )
                assert forged.value.sqlstate == "PZK01"
        finally:
            await conn.close()
            await migrator.close()
    asyncio.run(check())


@pytest.mark.skipif(not os.getenv("CORTEX_V2_D53_APP_DATABASE_URL"), reason="disposable pgvector DB required")
def test_project_has_only_one_immutable_sponsor() -> None:
    async def check() -> None:
        r = await _seed()
        conn = await asyncpg.connect(_url("MIGRATOR"))
        try:
            with pytest.raises(asyncpg.UniqueViolationError):
                await conn.execute(
                    "INSERT INTO cortex_auth.project_sponsors(project_scope_id,sponsor_principal_id,source_scope_id) VALUES($1,$2,$3)",
                    r.sponsor_project, r.other_lead, r.other_project,
                )
        finally:
            await conn.close()
    asyncio.run(check())


@pytest.mark.skipif(not os.getenv("CORTEX_V2_D53_APP_DATABASE_URL"), reason="disposable pgvector DB required")
def test_candidate_fixture_projects_keep_verified_provenance() -> None:
    async def check() -> None:
        fixture = {
            "instance_id": W1_INSTANCE,
            **{field: str(uuid.uuid4()) for field in W1_FIXTURE_UUID_FIELDS},
            **{
                field: secrets.token_bytes(32).hex()
                for field in ("owner_credential_hash", "worker_credential_hash", "recovery_hash")
            },
            **{
                field: f"d53-fixture-{uuid.uuid4().hex[:12]}"
                for field in ("project_alias", "shared_alias", "local_alias", "ungranted_alias")
            },
            "credential_generation": 1,
            "recovery_generation": 1,
        }
        conn = await asyncpg.connect(_url("MIGRATOR"))
        transaction = conn.transaction()
        await transaction.start()
        try:
            await _seed_w1(conn, fixture)
            installation = uuid.UUID(fixture["installation_id"])
            project = uuid.UUID(fixture["project_scope_id"])
            ungranted = uuid.UUID(fixture["ungranted_scope_id"])
            owner = uuid.UUID(fixture["owner_principal_id"])
            worker = uuid.UUID(fixture["worker_principal_id"])
            rows = await conn.fetch(
                "SELECT scope_id,installation_id FROM cortex_auth.project_installations "
                "WHERE scope_id=ANY($1::uuid[])",
                [project, ungranted],
            )
            assert {row["scope_id"]: row["installation_id"] for row in rows} == {
                project: installation, ungranted: installation
            }
            await conn.execute("SELECT set_config('cortex.principal_id',$1,true)", str(owner))
            assert await conn.fetchval(
                "SELECT cortex_auth.can_manage_project_keys($1,$2)", owner, project
            )
            await conn.execute(
                "INSERT INTO cortex_auth.memberships(scope_id,actor_id,membership_role) "
                "VALUES($1,$2,'lead')",
                project, uuid.UUID(fixture["worker_actor_id"]),
            )
            await conn.execute("SELECT set_config('cortex.principal_id',$1,true)", str(worker))
            assert await conn.fetchval(
                "SELECT cortex_auth.can_manage_project_keys($1,$2)", worker, project
            )
            assert not await conn.fetchval(
                "SELECT cortex_auth.can_manage_project_keys($1,$2)", worker, ungranted
            )
        finally:
            await transaction.rollback()
            await conn.close()
    asyncio.run(check())
