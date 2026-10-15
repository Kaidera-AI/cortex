"""Private source bindings over configured library pools, not an installed owner.

SQL functions retain database authority. No public RC3a default binds this module.
"""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from decimal import Decimal
import hashlib
import json
import math
from uuid import UUID

from cortex_core.auth import AuthError, Scope, _arguments, _async_authorized
from cortex_core.embeddings.pg_search import (
    PostgresSearch, CoreUnavailable, CapabilityUnavailable, _require_asyncpg, _driver_errors,
)
from cortex_core.embeddings.query_cache import CacheScope, QueryEmbeddingCache
from cortex_core.conductor.supervisor import Supervisor, LeaseBusy, StaleLease
from cortex_core.gateway.health_metrics import CoreSample


@dataclass(frozen=True, repr=False)
class _RequestMaterial:
    credential: bytes
    expected: Scope

    def __post_init__(self):
        scope = self.expected
        if (not isinstance(scope, Scope) or scope.action != 'read'
                or not all(isinstance(v, UUID) for v in
                    (scope.installation_id, scope.tenant_id, scope.project_id, scope.principal_id))
                or type(scope.permission_generation) is not int or scope.permission_generation < 0):
            raise AuthError('forbidden')
        _arguments(self.credential, scope.installation_id, scope.project_id, scope.action)


def _cache_partition(scope):
    value = [str(scope.installation_id), str(scope.tenant_id), str(scope.project_id),
             str(scope.principal_id), scope.permission_generation, scope.action]
    return hashlib.sha256(json.dumps(value, separators=(',',':')).encode('utf-8')).hexdigest()


async def _deny(_connection, _subject):
    raise PermissionError('Legacy callback is unbound')


class _C04Search(PostgresSearch):
    def __init__(self, pool, material):
        if not isinstance(material, _RequestMaterial):
            raise TypeError('Private request material is required')
        super().__init__(pool, _deny)
        self._material = material

    @asynccontextmanager
    async def _request(self, subject, *, isolation='read_committed'):
        if isolation != 'read_committed':
            raise ValueError('C04 search requires READ COMMITTED')
        material = self._material
        expected = material.expected
        if subject != str(expected.principal_id):
            raise PermissionError('Authenticated principal changed')
        _require_asyncpg()
        try:
            async with self.pool.acquire(timeout=2) as connection:
                async with _async_authorized(connection, material.credential,
                        expected.installation_id, expected.project_id, 'read') as scope:
                    if scope != expected:
                        raise AuthError('forbidden')
                    await connection.execute("SET LOCAL statement_timeout='2s'")
                    yield connection, CacheScope(scope.tenant_id, scope.project_id, _cache_partition(scope))
        except AuthError as error:
            if error.code == 'core_unavailable':
                raise CoreUnavailable('Authoritative Core is unavailable') from None
            raise PermissionError('Current Core authorization refused') from None
        except _driver_errors('PostgresConnectionError', 'InterfaceError') + (OSError, TimeoutError):
            raise CoreUnavailable('Authoritative Core is unavailable') from None
        except _driver_errors('QueryCanceledError'):
            raise CapabilityUnavailable('resource_timeout') from None


class _C04Cache(QueryEmbeddingCache):
    def __init__(self, pool, material, embed, **options):
        super().__init__(pool, _deny, embed, **options)
        self.db = _C04Search(pool, material)


def _ttl_ms(seconds):
    if (type(seconds) not in (int, float)
            or (type(seconds) is float and not math.isfinite(seconds))):
        raise ValueError('Lease duration requires exact whole milliseconds in 1..10000')
    # Integer arithmetic on the exact decimal ratio ignores ambient Decimal precision.
    numerator, denominator = Decimal(str(seconds)).as_integer_ratio()
    milliseconds, remainder = divmod(numerator * 1000, denominator)
    if remainder or not 1 <= milliseconds <= 10000:
        raise ValueError('Lease duration requires exact whole milliseconds in 1..10000')
    return milliseconds


@asynccontextmanager
async def _port_transaction(pool):
    _require_asyncpg()
    try:
        async with pool.acquire(timeout=2) as connection:
            async with connection.transaction(isolation='read_committed'):
                await connection.execute("SET LOCAL statement_timeout='2s'")
                yield connection
    except _driver_errors('PostgresConnectionError', 'InterfaceError', 'QueryCanceledError') + (OSError, TimeoutError):
        raise CoreUnavailable('Authoritative Core is unavailable') from None
    except Exception as error:
        # Private SQL refusals carry fixed public outcomes, never dependency text.
        driver = _require_asyncpg()
        if isinstance(error, driver.PostgresError):
            raise CoreUnavailable('Authoritative Core is unavailable') from None
        raise


def _json(value):
    try:
        return json.loads(value) if isinstance(value, str) else value
    except (ValueError, TypeError):
        raise CoreUnavailable('Invalid private Core observation') from None


def _health(value):
    value = _json(value)
    if not isinstance(value, dict) or set(value) != {'state','fence'}:
        raise CoreUnavailable('Invalid private Core health')
    state, fence = value['state'], value['fence']
    if ((state == 'absent' and fence is not None)
            or (state in ('live','expired') and (type(fence) is not int or not 1 <= fence < 2**63))
            or state not in ('absent','live','expired')):
        raise CoreUnavailable('Invalid private Core health')
    return {'core_available':True, 'state':'healthy' if state == 'live' else 'supervisor_down', 'fence':fence}


async def _lease_boolean(connection, query, token):
    try:
        value = await connection.fetchval(query, *token)
    except Exception as error:
        driver = _require_asyncpg()
        if (isinstance(error, driver.PostgresError)
                and getattr(error, 'message', None) in
                    ('runtime_lease_not_live','runtime_lease_expired','runtime_lease_token_invalid')):
            raise StaleLease('Expired or replaced supervisor lease') from None
        raise
    if value is False:
        raise StaleLease('Expired or replaced supervisor lease')
    if value is not True:
        raise CoreUnavailable('Invalid private lease result')


class _LeasePorts(Supervisor):
    def __init__(self, pool, installation_id, *, lease_seconds=10):
        _ttl_ms(lease_seconds)
        super().__init__(pool, installation_id, lease_seconds=lease_seconds)

    def _transaction(self):
        return _port_transaction(self.pool)

    async def acquire(self):
        ttl = _ttl_ms(self.lease_seconds)
        async with self._transaction() as connection:
            try:
                fence = await connection.fetchval('SELECT coordination.runtime_lease_acquire($1,$2,$3)',
                                                  self.installation_id, self.holder, ttl)
            except Exception as error:
                driver = _require_asyncpg()
                if isinstance(error, driver.PostgresError) and getattr(error, 'message', None) == 'runtime_lease_busy':
                    raise LeaseBusy('Another supervisor holds the installation lease') from None
                raise
            if type(fence) is not int or not 1 <= fence < 2**63:
                raise CoreUnavailable('Invalid private lease fence')
        self.fence = fence
        return fence

    async def _lock_live(self, connection, token):
        await _lease_boolean(connection, 'SELECT coordination.runtime_lease_assert_live($1,$2,$3)', token)

    async def renew(self):
        token = self._token()
        ttl = _ttl_ms(self.lease_seconds)
        async with self._transaction() as connection:
            await _lease_boolean(connection, 'SELECT coordination.runtime_lease_renew($1,$2,$3,$4)', (*token, ttl))

    async def release(self):
        token = self._token()
        async with self._transaction() as connection:
            await _lease_boolean(connection, 'SELECT coordination.runtime_lease_release($1,$2,$3)', token)
        self.fence = None

    async def guarded(self, action):
        token = self._token()
        async with self._transaction() as connection:
            await self._lock_live(connection, token)
            try:
                result = await asyncio.wait_for(action(connection, token[2]), timeout=self.lease_seconds)
            except TimeoutError:
                raise StaleLease('Control action outlived its lease') from None
            await self._lock_live(connection, token)
        return result

    async def health(self):
        try:
            async with self._transaction() as connection:
                result = _health(await connection.fetchval('SELECT coordination.runtime_lease_health($1)', self.installation_id))
            return result
        except CoreUnavailable:
            return {'core_available':False, 'state':'core_unavailable', 'fence':None}

    async def sample_metrics(self, metrics, backlog):
        # No genuine owner/process-view contract admits an observer here yet.
        raise CoreUnavailable('Metrics observer is unbound')


class _ObserverPorts:
    def __init__(self, pool, installation_id):
        if not isinstance(installation_id, UUID):
            raise ValueError('Sealed installation identity is required')
        self.pool, self.installation_id = pool, installation_id

    async def read_health(self):
        try:
            async with _port_transaction(self.pool) as connection:
                result = _health(await connection.fetchval('SELECT coordination.runtime_lease_health($1)', self.installation_id))
            return result
        except CoreUnavailable:
            return {'core_available':False, 'state':'core_unavailable', 'fence':None}

    async def read_sample(self):
        async with _port_transaction(self.pool) as connection:
            value = _json(await connection.fetchval('SELECT coordination.runtime_observe($1)', self.installation_id))
            fields = ('database_bytes','pending_jobs','pending_embedding_jobs')
            if (not isinstance(value, dict) or set(value) != {*fields,'lease'}
                    or any(type(value[name]) is not int or value[name] < 0 for name in fields)
                    or value['pending_embedding_jobs'] > value['pending_jobs']):
                raise CoreUnavailable('Invalid private Core sample')
            _health(value['lease'])
            result = CoreSample(value['database_bytes'], value['pending_embedding_jobs'])
        return result
