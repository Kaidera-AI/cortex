"""Disposable PG query-vector cache. Core authorization remains authoritative."""

import asyncio
from dataclasses import dataclass
import hashlib
import json
import time
from uuid import uuid4

import asyncpg

from .pg_search import CapabilityUnavailable, PostgresSearch, Scope, vector_literal


@dataclass(frozen=True)
class CacheScope(Scope):
    permission_generation: str


@dataclass(frozen=True)
class CachedEmbedding:
    vector: tuple[float, ...]
    cache_hit: bool


class QueryEmbeddingCache:
    def __init__(
        self,
        pool,
        authorize,
        embed,
        *,
        ttl_seconds=1800,
        lease_seconds=10,
        wait_seconds=12,
        max_entries=512,
    ):
        if (
            not callable(embed)
            or not 0 < ttl_seconds <= 86400
            or not 0 < lease_seconds <= 60
            or not 0 < wait_seconds <= 30
            or not 1 <= max_entries <= 10000
        ):
            raise ValueError("Invalid bounded cache policy")
        self.db = PostgresSearch(pool, authorize)
        self.embed = embed
        self.ttl_seconds = ttl_seconds
        self.lease_seconds = lease_seconds
        self.wait_seconds = wait_seconds
        self.max_entries = max_entries

    @staticmethod
    def _key(scope, identity, digest):
        if (
            not isinstance(scope, CacheScope)
            or not isinstance(scope.permission_generation, str)
            or not 1 <= len(scope.permission_generation) <= 256
        ):
            raise PermissionError("Current permission generation required")
        return (
            scope.tenant_id,
            scope.project_id,
            scope.permission_generation,
            identity,
            digest,
        )

    async def _lookup_claim(self, subject, identity, digest, owner):
        async with self.db._request(subject, isolation="read_committed") as (
            conn,
            scope,
        ):
            key = self._key(scope, identity, digest)
            # Per-scope lock serializes bounded capacity admission, NOT provider I/O.
            lock = int.from_bytes(
                hashlib.sha256(
                    (str(scope.tenant_id) + str(scope.project_id)).encode()
                ).digest()[:8],
                "big",
                signed=True,
            )
            await conn.execute("SELECT pg_advisory_xact_lock($1)", lock)
            row = await conn.fetchrow(
                """SELECT embedding::text AS vector, expires_at > clock_timestamp() AS fresh
                FROM retrieval.query_embeddings WHERE tenant_id=$1 AND project_id=$2
                AND permission_generation=$3 AND identity=$4 AND query_digest=$5""",
                *key,
            )
            if row is not None and row["fresh"] and row["vector"] is not None:
                vector = json.loads(row["vector"])
                vector_literal(vector, 768)
                return key, CachedEmbedding(tuple(vector), True), None
            await conn.execute(
                """DELETE FROM retrieval.query_embeddings WHERE tenant_id=$1 AND project_id=$2
                AND expires_at <= clock_timestamp() AND lease_until <= clock_timestamp()
                AND (permission_generation,identity,query_digest) != ($3,$4,$5)""",
                *key,
            )
            if row is None:
                count = await conn.fetchval(
                    "SELECT count(*) FROM retrieval.query_embeddings WHERE tenant_id=$1 AND project_id=$2",
                    scope.tenant_id,
                    scope.project_id,
                )
                if count >= self.max_entries:
                    raise CapabilityUnavailable("cache_capacity")
            fence = await conn.fetchval(
                """INSERT INTO retrieval.query_embeddings
                (tenant_id,project_id,permission_generation,identity,query_digest,owner,lease_until)
                VALUES($1,$2,$3,$4,$5,$6,clock_timestamp()+($7 * interval '1 second'))
                ON CONFLICT(tenant_id,project_id,permission_generation,identity,query_digest) DO UPDATE
                SET owner=EXCLUDED.owner,fence=nextval('retrieval.query_embedding_fences'),
                    lease_until=EXCLUDED.lease_until,embedding=NULL
                WHERE retrieval.query_embeddings.lease_until <= clock_timestamp()
                  AND retrieval.query_embeddings.expires_at <= clock_timestamp()
                RETURNING fence""",
                *key,
                owner,
                self.lease_seconds,
            )
            return key, None, fence

    async def _publish(self, subject, key, owner, fence, vector):
        async with self.db._request(subject) as (conn, scope):
            if self._key(scope, key[3], key[4]) != key:
                raise PermissionError("Permission generation changed during embedding")
            result = await conn.fetchval(
                """UPDATE retrieval.query_embeddings
                SET embedding=$8::vector,expires_at=clock_timestamp()+($9 * interval '1 second'),
                    lease_until='-infinity'
                WHERE tenant_id=$1 AND project_id=$2 AND permission_generation=$3 AND identity=$4
                  AND query_digest=$5 AND owner=$6 AND fence=$7 AND lease_until > clock_timestamp()
                RETURNING fence""",
                *key,
                owner,
                fence,
                vector_literal(vector, 768),
                self.ttl_seconds,
            )
            return result is not None

    async def _release(self, subject, key, owner, fence, deadline):
        # Best effort under the same current auth; an unreleasable claim expires.
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        try:
            async with asyncio.timeout(min(0.05, remaining)):
                async with self.db._request(subject) as (conn, scope):
                    if self._key(scope, key[3], key[4]) != key:
                        return
                    await conn.execute(
                        """DELETE FROM retrieval.query_embeddings
                        WHERE tenant_id=$1 AND project_id=$2 AND permission_generation=$3 AND identity=$4
                          AND query_digest=$5 AND owner=$6 AND fence=$7 AND embedding IS NULL""",
                        *key,
                        owner,
                        fence,
                    )
        except Exception:
            pass  # Lease expiry is the recovery path; never converts a request to success.

    async def get(self, subject: str, identity, query: str):
        deadline = time.monotonic() + self.wait_seconds
        try:
            async with asyncio.timeout(self.wait_seconds):
                return await self._get(subject, identity, query, deadline)
        except TimeoutError:
            raise CapabilityUnavailable("query_embedding_wait_timeout") from None

    @staticmethod
    def _check_deadline(deadline):
        if time.monotonic() >= deadline:
            raise CapabilityUnavailable("query_embedding_wait_timeout")

    async def _get(self, subject: str, identity, query: str, deadline):
        identity_key = identity.key
        if not isinstance(query, str) or not query or len(query.encode()) > 16 * 1024:
            raise ValueError("Query must be nonempty and at most 16 KiB UTF-8")
        digest = hashlib.sha256(query.encode()).hexdigest()
        owner = uuid4()
        while time.monotonic() < deadline:
            try:
                key, hit, fence = await self._lookup_claim(
                    subject, identity_key, digest, owner
                )
            except asyncpg.SerializationError:
                # Another process won the claim after this transaction's snapshot.
                # Retry with a fresh current grant/scope snapshot; no provider call.
                await asyncio.sleep(0.02)
                continue
            self._check_deadline(deadline)
            if hit is not None:
                return hit
            if fence is not None:
                try:
                    # No PG connection/transaction is retained across this await.
                    vector = await asyncio.wait_for(
                        self.embed(query, identity),
                        timeout=max(0.001, deadline - time.monotonic()),
                    )
                    vector_literal(vector, identity.dimensions)
                    if await self._publish(subject, key, owner, fence, vector):
                        self._check_deadline(deadline)
                        return CachedEmbedding(tuple(float(v) for v in vector), False)
                except asyncpg.SerializationError:
                    await self._release(subject, key, owner, fence, deadline)
                    # A concurrent winner may already have published a cached result.
                except TimeoutError:
                    await self._release(subject, key, owner, fence, deadline)
                    raise CapabilityUnavailable("provider_timeout") from None
                except BaseException:
                    await self._release(subject, key, owner, fence, deadline)
                    raise
            await asyncio.sleep(min(0.02, max(0, deadline - time.monotonic())))
        raise CapabilityUnavailable("query_embedding_wait_timeout")
