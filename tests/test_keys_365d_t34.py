"""T34 issuance and append-only upgrade on a fresh disposable database.

The bootstrap/recovery calls below use synthetic hashes inside rolled-back
transactions. They never use an installation's real credentials or recovery.
Run this module before persistent installation fixtures on the same database.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import uuid
from datetime import timedelta
from pathlib import Path

import asyncpg
import pytest

from cortex_v2 import config
from cortex_v2.key_lifetime import due_state
from test_keys_project_authority import _url

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"
T34_MIGRATION = "0014_keys_365d_t34.sql"
pytestmark = pytest.mark.skipif(
    not os.getenv("CORTEX_V2_D53_MIGRATOR_DATABASE_URL"),
    reason="fresh disposable pgvector database required",
)


async def _bootstrap(conn: asyncpg.Connection) -> tuple[asyncpg.Record, bytes]:
    recovery_hash = secrets.token_bytes(32)
    receipt = await conn.fetchrow(
        "SELECT * FROM cortex_auth.bootstrap_installation($1,$2,$3,$4,$5)",
        uuid.uuid4(), "T34 disposable installation", "T34 owner",
        secrets.token_bytes(32), recovery_hash,
    )
    await conn.execute(
        "SELECT set_config('cortex.principal_id',$1,true)",
        str(receipt["owner_principal_id"]),
    )
    return receipt, recovery_hash


@pytest.mark.parametrize("issuer", ("bootstrap", "enroll", "rotate", "recover"))
def test_all_new_issuers_use_database_365_days(issuer: str) -> None:
    async def check() -> None:
        conn = await asyncpg.connect(_url("MIGRATOR"))
        transaction = conn.transaction()
        await transaction.start()
        try:
            bootstrap, recovery_hash = await _bootstrap(conn)
            owner = bootstrap["owner_principal_id"]
            if issuer == "bootstrap":
                receipt = bootstrap
            elif issuer == "enroll":
                receipt = await conn.fetchrow(
                    "SELECT * FROM cortex_auth.enroll_principal($1,$2,$3,$4)",
                    owner, "T34 agent", "agent", secrets.token_bytes(32),
                )
            elif issuer == "rotate":
                receipt = await conn.fetchrow(
                    "SELECT * FROM cortex_auth.rotate_credential($1,$1,$2)",
                    owner, secrets.token_bytes(32),
                )
            else:
                receipt = await conn.fetchrow(
                    "SELECT * FROM cortex_auth.recover_owner($1,$2,$3)",
                    recovery_hash, secrets.token_bytes(32), secrets.token_bytes(32),
                )
            stored = await conn.fetchrow(
                "SELECT created_at,expires_at FROM cortex_auth.credentials "
                "WHERE credential_id=$1", receipt["credential_id"],
            )
            assert receipt["expires_at"] == stored["expires_at"]
            assert stored["expires_at"] - stored["created_at"] == timedelta(days=365)
            assert due_state(stored["expires_at"], stored["created_at"] + timedelta(days=334)) is None
            assert due_state(stored["expires_at"], stored["created_at"] + timedelta(days=335)) == "due"
            assert due_state(stored["expires_at"], stored["created_at"] + timedelta(days=365)) == "expired"
        finally:
            await transaction.rollback()
            await conn.close()

    asyncio.run(check())


def test_0014_upgrade_preserves_historical_180_and_null_rows() -> None:
    async def check() -> None:
        conn = await asyncpg.connect(_url("MIGRATOR"))
        transaction = conn.transaction()
        await transaction.start()
        try:
            # Restore 0013's functions inside this rolled-back transaction to
            # exercise the actual 180 -> 365 upgrade with genuine old issuance.
            old_sql = (MIGRATIONS / "0013_keys_issuance_t21.sql").read_text()
            for signature in (
                "bootstrap_installation(uuid,text,text,bytea,bytea)",
                "enroll_principal(uuid,text,text,bytea)",
                "rotate_credential(uuid,uuid,bytea)",
                "authenticate(bytea)",
                "recover_owner(bytea,bytea,bytea)",
            ):
                await conn.execute(f"DROP FUNCTION cortex_auth.{signature}")
            # 0013's DROP statements name pre-0013 signatures; the originals
            # remain immutable and only this test copy omits those statements.
            old_sql = "\n".join(
                line for line in old_sql.splitlines()
                if not line.startswith("DROP FUNCTION ")
            )
            await conn.execute(old_sql)
            old, _ = await _bootstrap(conn)
            owner = old["owner_principal_id"]
            await conn.execute(
                "INSERT INTO cortex_auth.credentials(credential_id,principal_id,"
                "token_hash,generation,expires_at) VALUES($1,$2,$3,2,NULL)",
                uuid.uuid4(), owner, secrets.token_bytes(32),
            )
            before = await conn.fetch(
                "SELECT * FROM cortex_auth.credentials ORDER BY credential_id"
            )
            assert len(before) == 2
            assert any(row["expires_at"] is None for row in before)
            finite = next(row for row in before if row["expires_at"] is not None)
            assert finite["expires_at"] - finite["created_at"] == timedelta(days=180)

            await conn.execute((MIGRATIONS / T34_MIGRATION).read_text())
            after = await conn.fetch(
                "SELECT * FROM cortex_auth.credentials ORDER BY credential_id"
            )
            assert after == before
            new = await conn.fetchrow(
                "SELECT * FROM cortex_auth.enroll_principal($1,$2,$3,$4)",
                owner, "T34 after upgrade", "agent", secrets.token_bytes(32),
            )
            stored = await conn.fetchrow(
                "SELECT created_at,expires_at FROM cortex_auth.credentials "
                "WHERE credential_id=$1", new["credential_id"],
            )
            assert new["expires_at"] == stored["expires_at"]
            assert stored["expires_at"] - stored["created_at"] == timedelta(days=365)
            assert config.FULL_V2_MIGRATIONS[-1] == T34_MIGRATION
        finally:
            await transaction.rollback()
            await conn.close()

    asyncio.run(check())
