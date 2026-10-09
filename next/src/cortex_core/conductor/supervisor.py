"""Minimal installation supervisor. No request/data-path dispatch or engine relay."""

import asyncio
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

import asyncpg
from cortex_core.embeddings.pg_search import CoreUnavailable


class LeaseBusy(RuntimeError):
    code = "conflict"


class StaleLease(RuntimeError):
    code = "conflict"


class Supervisor:
    def __init__(self, pool, installation_id: UUID, *, lease_seconds=10):
        if not isinstance(installation_id, UUID) or not 0 < lease_seconds <= 60:
            raise ValueError("Sealed installation identity and bounded lease required")
        self.pool = pool
        self.installation_id = installation_id
        self.lease_seconds = lease_seconds
        self.holder = uuid4()
        self.fence = None

    @asynccontextmanager
    async def _transaction(self):
        try:
            async with self.pool.acquire(timeout=2) as conn:
                async with conn.transaction():
                    bypass = await conn.fetchval(
                        "SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname=current_user"
                    )
                    if bypass:
                        raise PermissionError("Supervisor runtime must not bypass RLS")
                    await conn.execute(
                        "SELECT set_config('cortex.installation_id',$1,true)",
                        str(self.installation_id),
                    )
                    await conn.execute("SET LOCAL statement_timeout='2s'")
                    yield conn
        except PermissionError:
            raise
        except (
            asyncpg.PostgresConnectionError,
            asyncpg.InterfaceError,
            OSError,
            TimeoutError,
        ) as exc:
            raise CoreUnavailable("Authoritative Core is unavailable") from exc

    async def acquire(self):
        async with self._transaction() as conn:
            fence = await conn.fetchval(
                """INSERT INTO coordination.supervisor_leases
                VALUES($1,$2,1,clock_timestamp()+($3 * interval '1 second'))
                ON CONFLICT(installation_id) DO UPDATE SET holder=EXCLUDED.holder,
                    fence=coordination.supervisor_leases.fence+1,
                    expires_at=clock_timestamp()+($3 * interval '1 second')
                WHERE coordination.supervisor_leases.expires_at <= clock_timestamp()
                RETURNING fence""",
                self.installation_id,
                self.holder,
                self.lease_seconds,
            )
            if fence is None:
                raise LeaseBusy("Another supervisor holds the installation lease")
        self.fence = fence
        return fence

    def _token(self):
        if self.fence is None:
            raise StaleLease("Supervisor has no active lease")
        return (self.installation_id, self.holder, self.fence)

    async def _lock_live(self, conn, token):
        # Qualification before a lock wait can survive an unchanged-row rollback.
        # Match the token only; check expiry in a separate command after locking.
        row = await conn.fetchrow(
            """SELECT expires_at FROM coordination.supervisor_leases
            WHERE installation_id=$1 AND holder=$2 AND fence=$3 FOR UPDATE""",
            *token,
        )
        if row is None or not await conn.fetchval(
            "SELECT $1::timestamptz > clock_timestamp()", row["expires_at"]
        ):
            raise StaleLease("Expired or replaced supervisor cannot change its lease")

    async def renew(self):
        token = self._token()
        async with self._transaction() as conn:
            await self._lock_live(conn, token)
            updated = await conn.fetchval(
                """UPDATE coordination.supervisor_leases
                SET expires_at=clock_timestamp()+($4 * interval '1 second')
                WHERE installation_id=$1 AND holder=$2 AND fence=$3
                  AND expires_at > clock_timestamp() RETURNING fence""",
                *token,
                self.lease_seconds,
            )
            if updated is None:
                raise StaleLease("Expired or replaced supervisor cannot renew")

    async def release(self):
        token = self._token()
        async with self._transaction() as conn:
            await self._lock_live(conn, token)
            updated = await conn.fetchval(
                """UPDATE coordination.supervisor_leases
                SET expires_at='-infinity' WHERE installation_id=$1 AND holder=$2 AND fence=$3
                  AND expires_at > clock_timestamp() RETURNING fence""",
                *token,
            )
            if updated is None:
                raise StaleLease("Expired or replaced supervisor cannot release")
        self.fence = None

    async def guarded(self, action):
        """PG-only control-intent callback. External side effects are forbidden here."""
        token = self._token()
        async with self._transaction() as conn:
            valid = await conn.fetchval(
                """SELECT fence FROM coordination.supervisor_leases
                WHERE installation_id=$1 AND holder=$2 AND fence=$3
                  AND expires_at > clock_timestamp() FOR UPDATE""",
                *token,
            )
            if valid is None:
                raise StaleLease("Stale supervisor cannot issue control intent")
            remaining = await conn.fetchval(
                """SELECT EXTRACT(EPOCH FROM (expires_at-clock_timestamp()))
                FROM coordination.supervisor_leases WHERE installation_id=$1 AND holder=$2 AND fence=$3""",
                *token,
            )
            try:
                result = await asyncio.wait_for(
                    action(conn, token[2]), timeout=max(0, float(remaining))
                )
            except TimeoutError:
                raise StaleLease(
                    "Control action outlived its lease; transaction rolled back"
                ) from None
            live = await conn.fetchval(
                """SELECT expires_at > clock_timestamp()
                FROM coordination.supervisor_leases WHERE installation_id=$1 AND holder=$2 AND fence=$3""",
                *token,
            )
            if not live:
                raise StaleLease(
                    "Control action outlived its lease; transaction rolled back"
                )
            return result

    async def health(self):
        try:
            async with self._transaction() as conn:
                row = await conn.fetchrow(
                    """SELECT fence,expires_at > clock_timestamp() AS live
                    FROM coordination.supervisor_leases WHERE installation_id=$1""",
                    self.installation_id,
                )
            return {
                "core_available": True,
                "state": "healthy" if row and row["live"] else "supervisor_down",
                "fence": row["fence"] if row else None,
            }
        except CoreUnavailable:
            # Health remains callable to report outage; no success on other routes.
            return {"core_available": False, "state": "core_unavailable", "fence": None}

    async def sample_metrics(self, metrics, backlog):
        async with self._transaction() as conn:
            size = await conn.fetchval("SELECT pg_database_size(current_database())")
        # Authoritative backlog sampler comes from Core, outside this connection.
        pending = await backlog()
        metrics.update_core(size, pending)

    async def run(self, stop: asyncio.Event):
        """External supervisor owns process restart. Loss of lease ends this loop."""
        await self.acquire()
        try:
            while not stop.is_set():
                try:
                    await asyncio.wait_for(stop.wait(), timeout=self.lease_seconds / 3)
                except TimeoutError:
                    await self.renew()
        finally:
            try:
                await self.release()
            except (StaleLease, CoreUnavailable):
                pass  # Expiry is the recovery path; never clear another holder.
