"""Postgres search adapter beneath Cox C01/C04/C11, with no HTTP authority.

The authorize callback is REQUIRED and must recheck current Core permission for
its bound operation/service. Headers are never accepted here. Configure/index
mutation methods are internal control/writer hooks, not public API operations.
Cox must bind separate read/control/writer authorization at the API boundary.
"""

import asyncio
import hashlib
import json
import math
import struct
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import wraps
from typing import Awaitable, Callable
from uuid import UUID

import asyncpg
from .projection_revision import ProjectionRevision, finish_observation, validate_target


@dataclass(frozen=True)
class Scope:
    tenant_id: UUID
    project_id: UUID


@dataclass(frozen=True)
class EmbeddingIdentity:
    provider: str
    model: str
    version: str
    dimensions: int
    preprocessing: str

    @property
    def key(self) -> str:
        if self.dimensions != 768 or not all(
            isinstance(v, str) and v.strip()
            for v in (self.provider, self.model, self.version, self.preprocessing)
        ):
            raise ValueError("Search requires a complete identity with 768 dimensions")
        return hashlib.sha256(
            json.dumps(
                [
                    self.provider,
                    self.model,
                    self.version,
                    self.dimensions,
                    self.preprocessing,
                ],
                separators=(",", ":"),
            ).encode()
        ).hexdigest()


@dataclass(frozen=True)
class Freshness:
    identity: str
    indexed_records: int
    pending_records: int

    @property
    def status(self) -> str:
        return "lagging" if self.pending_records else "current"


@dataclass(frozen=True)
class Hit:
    record_id: str
    kind: str
    source_revision: int
    distance: float


@dataclass(frozen=True)
class SearchResult:
    hits: tuple[Hit, ...]
    freshness: Freshness


class CapabilityUnavailable(RuntimeError):
    code = "capability_unavailable"
    capability = "search"

    def __init__(self, reason: str, freshness: Freshness | None = None):
        super().__init__(reason)
        self.reason = reason
        self.freshness = freshness


class CoreUnavailable(RuntimeError):
    code = "core_unavailable"


class StaleEmbedding(RuntimeError):
    code = "stale_embedding"


Authorize = Callable[[asyncpg.Connection, str], Awaitable[Scope]]


def vector_literal(vector, dimensions: int) -> str:
    if len(vector) != dimensions or any(
        isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
        for v in vector
    ):
        raise ValueError(
            "Expected a finite, nonzero vector of the configured dimensions"
        )

    def float32(value):
        try:
            return struct.unpack("!f", struct.pack("!f", value))[0]
        except (OverflowError, struct.error) as exc:
            raise ValueError("Vector exceeds float32 range") from exc

    converted = []
    norm = 0.0
    for value in vector:
        component = float32(float(value))
        if not math.isfinite(component) or (value != 0 and component == 0):
            raise ValueError("Vector component is not representable in float32")
        converted.append(component)
        # pgvector cosine accumulates products and norms in float32, not double.
        norm = float32(norm + float32(component * component))
    # Subnormal accumulators lose enough relative precision to distort cosine.
    if not math.isfinite(norm) or norm < 2.0**-126:
        raise ValueError("Vector requires a finite, normal float32 cosine norm")
    return "[" + ",".join(str(v) for v in converted) + "]"


def retry_authorized_transaction(operation):
    """Retry the WHOLE operation after rollback, including fresh Core grants."""

    @wraps(operation)
    async def wrapped(*args, **kwargs):
        for attempt in range(3):
            try:
                return await operation(*args, **kwargs)
            except asyncpg.SerializationError:
                if attempt == 2:
                    raise CapabilityUnavailable("concurrent_update") from None
                await asyncio.sleep(0.005 * (attempt + 1))

    return wrapped


class PostgresSearch:
    def __init__(self, pool: asyncpg.Pool, authorize: Authorize):
        if not callable(authorize):
            raise TypeError("Current Core authorization is mandatory")
        self.pool = pool
        self.authorize = authorize

    async def projection_revision(self, subject, target, identity, *, recheck):
        """Read actual indexed evidence for one authorized Core record."""
        record_id, required = validate_target(target, recheck)
        key = identity.key
        def observation(state, indexed=None, reason=''):
            return ProjectionRevision(state, record_id, required, indexed, key, reason=reason)
        try:
            async with self._request(subject) as (conn, scope):
                current = await conn.fetchrow(
                    'SELECT current_revision,tombstone,kind FROM core.records '
                    'WHERE tenant_id=$1 AND project_id=$2 AND id=$3',
                    scope.tenant_id, scope.project_id, record_id)
                state = await conn.fetchrow(
                    'SELECT identity,state FROM retrieval.search_state '
                    'WHERE tenant_id=$1 AND project_id=$2', scope.tenant_id, scope.project_id)
                if current is None:
                    result = observation('unavailable', reason='target_unavailable')
                elif current['tombstone']:
                    result = observation('unavailable', reason='tombstone')
                elif state is None:
                    result = observation('unavailable', reason='not_configured')
                elif state['identity'] != key:
                    result = observation('unavailable', reason='identity_mismatch')
                elif state['state'] != 'ready':
                    result = observation('unavailable', reason=state['state'])
                else:
                    row = await conn.fetchrow(
                        '''SELECT s.source_revision AS desired_revision,s.kind,
                                  v.source_revision AS indexed_revision,v.identity AS vector_identity
                             FROM retrieval.search_sources s
                             LEFT JOIN retrieval.search_vectors v
                               USING(tenant_id,project_id,record_id)
                            WHERE s.tenant_id=$1 AND s.project_id=$2 AND s.record_id=$3
                            ORDER BY s.record_id LIMIT 1''',
                        scope.tenant_id, scope.project_id, str(record_id))
                    indexed = row['indexed_revision'] if row is not None and row['vector_identity'] == key else None
                    visible = (indexed is not None and indexed >= required
                               and indexed == current['current_revision']
                               and row['desired_revision'] == current['current_revision']
                               and row['kind'] == current['kind'])
                    result = observation('visible' if visible else 'pending', indexed,
                                         '' if visible else 'index_pending')
        except CoreUnavailable:
            return observation('unavailable', reason='core_unavailable')
        except asyncpg.UndefinedTableError:
            return observation('unavailable', reason='schema_unavailable')
        except CapabilityUnavailable as exc:
            return observation('unavailable', reason=exc.reason)
        return await finish_observation(recheck, subject, scope, result)

    @asynccontextmanager
    async def _request(self, subject: str, *, isolation="repeatable_read"):
        if not subject:
            raise PermissionError("Authenticated principal required")
        try:
            async with self.pool.acquire(timeout=2) as conn:
                async with conn.transaction(isolation=isolation):
                    bypass = await conn.fetchval(
                        "SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname=current_user"
                    )
                    if bypass:
                        raise PermissionError("Search runtime must not bypass RLS")
                    scope = await self.authorize(conn, subject)
                    if (
                        not isinstance(scope, Scope)
                        or not isinstance(scope.tenant_id, UUID)
                        or not isinstance(scope.project_id, UUID)
                    ):
                        raise PermissionError(
                            "Core did not provide a valid authenticated scope"
                        )
                    await conn.execute(
                        "SELECT set_config('cortex.tenant_id',$1,true), set_config('cortex.project_id',$2,true)",
                        str(scope.tenant_id),
                        str(scope.project_id),
                    )
                    await conn.execute("SET LOCAL statement_timeout='2s'")
                    yield conn, scope
        except PermissionError:
            raise
        except (
            asyncpg.PostgresConnectionError,
            asyncpg.InterfaceError,
            OSError,
            TimeoutError,
        ) as exc:
            raise CoreUnavailable("Authoritative Core is unavailable") from exc
        except asyncpg.QueryCanceledError as exc:
            raise CapabilityUnavailable("resource_timeout") from exc

    @retry_authorized_transaction
    async def configure(self, subject: str, identity: EmbeddingIdentity, state: str):
        key = identity.key
        if state not in {"ready", "disabled", "rebuilding"}:
            raise ValueError("Invalid search state")
        async with self._request(subject) as (conn, scope):
            await conn.execute(
                """INSERT INTO retrieval.search_state VALUES($1,$2,$3,$4)
                ON CONFLICT(tenant_id,project_id) DO UPDATE SET identity=EXCLUDED.identity,state=EXCLUDED.state""",
                scope.tenant_id,
                scope.project_id,
                key,
                state,
            )

    @retry_authorized_transaction
    async def note_revision(
        self, subject: str, record_id: str, kind: str, revision: int
    ):
        """Standalone hook for tests/jobs. Core writes MUST use the transaction hook."""
        async with self._request(subject) as (conn, scope):
            await self.note_revision_in_transaction(
                conn, scope, record_id, kind, revision
            )

    @staticmethod
    async def note_revision_in_transaction(
        conn, scope: Scope, record_id: str, kind: str, revision: int
    ):
        """Call in Core's authorized record-write transaction with local RLS scope set."""
        if not conn.is_in_transaction():
            raise RuntimeError(
                "Revision notification must be atomic with the Core write"
            )
        if (
            not 1 <= len(record_id) <= 256
            or not 1 <= len(kind) <= 64
            or not 0 < revision < 2**63
        ):
            raise ValueError("Invalid record revision")
        await conn.execute(
            """INSERT INTO retrieval.search_sources VALUES($1,$2,$3,$4,$5)
            ON CONFLICT(tenant_id,project_id,record_id) DO UPDATE
            SET kind=EXCLUDED.kind,source_revision=EXCLUDED.source_revision
            WHERE retrieval.search_sources.source_revision < EXCLUDED.source_revision""",
            scope.tenant_id,
            scope.project_id,
            record_id,
            kind,
            revision,
        )

    @staticmethod
    async def _state(conn, scope, key):
        # SHARE serializes configure against in-flight reads and embedding writes.
        state = await conn.fetchrow(
            """SELECT identity,state FROM retrieval.search_state
            WHERE tenant_id=$1 AND project_id=$2 FOR SHARE""",
            scope.tenant_id,
            scope.project_id,
        )
        if state is None:
            raise CapabilityUnavailable("not_configured")
        if state["identity"] != key:
            raise CapabilityUnavailable("identity_mismatch")
        return state["state"]

    @retry_authorized_transaction
    async def store_embedding(
        self,
        subject: str,
        record_id: str,
        revision: int,
        identity: EmbeddingIdentity,
        vector,
    ):
        key = identity.key
        literal = vector_literal(vector, identity.dimensions)
        async with self._request(subject) as (conn, scope):
            await self._state(conn, scope, key)
            row = await conn.fetchrow(
                """SELECT source_revision FROM retrieval.search_sources
                WHERE tenant_id=$1 AND project_id=$2 AND record_id=$3 FOR SHARE""",
                scope.tenant_id,
                scope.project_id,
                record_id,
            )
            if row is None or row["source_revision"] != revision:
                raise StaleEmbedding("Embedding does not match current source revision")
            await conn.execute(
                """INSERT INTO retrieval.search_vectors VALUES($1,$2,$3,$4,$5,$6::vector)
                ON CONFLICT(tenant_id,project_id,record_id) DO UPDATE SET
                source_revision=EXCLUDED.source_revision,identity=EXCLUDED.identity,embedding=EXCLUDED.embedding""",
                scope.tenant_id,
                scope.project_id,
                record_id,
                revision,
                key,
                literal,
            )

    @staticmethod
    async def _freshness(conn, scope, key):
        row = await conn.fetchrow(
            """SELECT count(v.record_id) AS indexed,
            count(*) FILTER(WHERE v.record_id IS NULL) AS pending
            FROM retrieval.search_sources s LEFT JOIN retrieval.search_vectors v
            ON (v.tenant_id,v.project_id,v.record_id)=(s.tenant_id,s.project_id,s.record_id)
            AND v.source_revision=s.source_revision AND v.identity=$3
            WHERE s.tenant_id=$1 AND s.project_id=$2""",
            scope.tenant_id,
            scope.project_id,
            key,
        )
        return Freshness(key, row["indexed"], row["pending"])

    @retry_authorized_transaction
    async def search(
        self, subject: str, identity: EmbeddingIdentity, vector, *, limit: int = 20
    ):
        key = identity.key
        literal = vector_literal(vector, identity.dimensions)
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 100
        ):
            raise ValueError("Search limit must be 1..100")
        async with self._request(subject) as (conn, scope):
            state = await self._state(conn, scope, key)
            freshness = await self._freshness(conn, scope, key)
            if state != "ready":
                raise CapabilityUnavailable(state, freshness)
            # PGvector >=0.8 iterative scan avoids the default filtered-ANN underfill.
            # Bounded scan remains approximate; real-data recall belongs to Mike.
            await conn.execute(
                "SET LOCAL hnsw.iterative_scan='strict_order'; SET LOCAL hnsw.ef_search=100; SET LOCAL hnsw.max_scan_tuples=20000"
            )
            rows = await conn.fetch(
                """SELECT v.record_id,s.kind,v.source_revision,
                v.embedding <=> $4::vector AS distance FROM retrieval.search_vectors v
                JOIN retrieval.search_sources s USING(tenant_id,project_id,record_id)
                WHERE v.tenant_id=$1 AND v.project_id=$2 AND v.identity=$3
                AND v.source_revision=s.source_revision
                ORDER BY v.embedding <=> $4::vector LIMIT $5""",
                scope.tenant_id,
                scope.project_id,
                key,
                literal,
                limit,
            )
            return SearchResult(
                tuple(
                    Hit(r["record_id"], r["kind"], r["source_revision"], r["distance"])
                    for r in rows
                ),
                freshness,
            )
