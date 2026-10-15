"""Private Core authorization port; results are accepted only after context exit."""
from contextlib import contextmanager, asynccontextmanager
import asyncio
import sys
from dataclasses import dataclass
import hashlib
from uuid import UUID

import psycopg
from psycopg.pq import TransactionStatus

_CONTEXT = ('cortex.credential_digest', 'cortex.installation_id', 'cortex.project_id',
            'cortex.action', 'cortex.tenant_id', 'cortex.principal_id', 'cortex.permission_generation')
_LOCAL_ROLE = 'SET LOCAL ROLE "kaidera-runtime-core-request"'
_ROLE_QUERY = """SELECT
    pg_has_role(current_user,'kaidera-runtime-core-request','MEMBER')
    AND NOT pg_has_role(session_user,'kaidera-runtime-core-verifier','MEMBER')
    AND NOT EXISTS (
        SELECT 1 FROM pg_roles r
        WHERE (r.rolname IN (current_user,session_user) OR pg_has_role(session_user,r.oid,'MEMBER'))
        AND (r.rolsuper OR r.rolbypassrls OR r.rolcreatedb OR r.rolcreaterole OR r.rolreplication))
    AND NOT EXISTS (
        SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname IN ('core','auth','coordination','retrieval')
        AND pg_has_role(session_user,c.relowner,'MEMBER'))
    AND NOT EXISTS (
        SELECT 1 FROM pg_namespace n WHERE n.nspname IN ('core','auth','coordination','retrieval')
        AND pg_has_role(session_user,n.nspowner,'MEMBER'))"""


class AuthError(RuntimeError):
    """Only a fixed code crosses the boundary; never dependency text or credentials."""
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class Scope:
    installation_id: UUID
    tenant_id: UUID
    project_id: UUID
    principal_id: UUID
    permission_generation: int
    action: str

    @property
    def cache_partition(self):
        return (self.installation_id, self.tenant_id, self.project_id,
                self.principal_id, self.permission_generation, self.action)


def _arguments(credential, installation_id, project_id, action):
    if not isinstance(credential, bytes) or not 1 <= len(credential) <= 4096:
        raise AuthError('unauthenticated')
    if not isinstance(installation_id, UUID) or not isinstance(project_id, UUID):
        raise AuthError('scope_mismatch')
    if action not in ('read', 'write', 'control'):
        raise AuthError('forbidden')
    return hashlib.sha256(credential).hexdigest(), installation_id, project_id, action


def _bound_values(scope, digest):
    return [digest, str(scope.installation_id), str(scope.project_id), scope.action,
            str(scope.tenant_id), str(scope.principal_id), str(scope.permission_generation)]


def _accept_exit(intact, unchanged):
    if not intact:
        raise AuthError('scope_mismatch')
    if not unchanged:
        raise AuthError('forbidden')


def _clear(connection):
    connection.execute("SELECT set_config(name,'',false) FROM unnest(%s::text[]) AS names(name)",
                       (list(_CONTEXT),))


def _resolve(connection, arguments):
    row = connection.execute('SELECT * FROM auth.resolve_scope(%s,%s,%s,%s)', arguments).fetchone()
    if row is None:
        raise AuthError('forbidden')
    return Scope(*row)


def _bind(connection, scope, digest):
    if not connection.execute('SELECT auth.bind_scope(%s,%s,%s,%s)',
                              (digest,scope.installation_id,scope.project_id,scope.action)).fetchone()[0]:
        raise AuthError('forbidden')
    values = _bound_values(scope, digest)
    connection.execute("SELECT set_config(name,value,true) FROM unnest(%s::text[],%s::text[]) AS bound(name,value)",
                       (list(_CONTEXT), values))


def _context_intact(connection, arguments):
    digest, installation, project, action = arguments
    actual = connection.execute("""SELECT current_setting('cortex.credential_digest',true),
        current_setting('cortex.installation_id',true),current_setting('cortex.project_id',true),
        current_setting('cortex.action',true)""").fetchone()
    return actual == (digest, str(installation), str(project), action)


@contextmanager
def authorized(connection, credential, installation_id, project_id, action='read'):
    """Own a request transaction, derive scope, then revalidate before committing it.

    The caller keeps its result private until this context has exited successfully.
    Direct clients never receive this connection or its database credential.
    """
    if connection.closed:
        raise AuthError('core_unavailable')
    if not connection.autocommit or connection.info.transaction_status != TransactionStatus.IDLE:
        raise AuthError('forbidden')
    try:
        _clear(connection)
        arguments = _arguments(credential, installation_id, project_id, action)
        digest = arguments[0]
        with connection.transaction():
            connection.execute(_LOCAL_ROLE)
            if not connection.execute(_ROLE_QUERY).fetchone()[0]:
                raise AuthError('forbidden')
            scope = _resolve(connection, arguments)
            _bind(connection, scope, digest)
            yield scope
            # Preserve the existing independent mutation seams at the sync boundary.
            if not _context_intact(connection, arguments):
                _accept_exit(False, False)
            if _resolve(connection, arguments) != scope:
                _accept_exit(True, False)
    except AuthError:
        raise
    except psycopg.Error:
        raise AuthError('core_unavailable') from None
    finally:
        try:
            _clear(connection)
        except psycopg.Error:
            connection.close()
            raise AuthError('core_unavailable') from None


def _async_errors(*names):
    # The owning adapter imports its driver at acquisition, never at module load.
    driver = sys.modules.get('asyncpg')
    return tuple(getattr(driver, name) for name in names) if driver is not None else ()


async def _async_clear(connection):
    await connection.execute("SELECT set_config(name,'',false) FROM unnest($1::text[]) AS names(name)",
                             list(_CONTEXT))


async def _async_resolve(connection, arguments):
    row = await connection.fetchrow('SELECT * FROM auth.resolve_scope($1,$2,$3,$4)', *arguments)
    if row is None:
        raise AuthError('forbidden')
    return Scope(*(row[name] for name in ('installation_id','tenant_id','project_id',
                                        'principal_id','permission_generation','action')))


async def _async_bind(connection, scope, digest):
    if not await connection.fetchval('SELECT auth.bind_scope($1,$2,$3,$4)',
                                    digest, scope.installation_id, scope.project_id, scope.action):
        raise AuthError('forbidden')
    await connection.execute("SELECT set_config(name,value,true) FROM "
                             "unnest($1::text[],$2::text[]) AS bound(name,value)",
                             list(_CONTEXT), _bound_values(scope, digest))


async def _async_context_intact(connection, arguments):
    digest, installation, project, action = arguments
    actual = await connection.fetchrow("""SELECT current_setting('cortex.credential_digest',true),
        current_setting('cortex.installation_id',true),current_setting('cortex.project_id',true),
        current_setting('cortex.action',true)""")
    return actual is not None and tuple(actual) == (digest, str(installation), str(project), action)


@asynccontextmanager
async def _async_authorized(connection, credential, installation_id, project_id, action='read'):
    """Private asyncpg C04 loop on one exclusively borrowed connection.

    Results remain private through transaction exit and the seven-field clear.
    Physical pool release/reset and native retirement belong to the runtime owner.
    """
    if connection.is_closed():
        raise AuthError('core_unavailable')
    if connection.is_in_transaction():
        raise AuthError('forbidden')
    try:
        await _async_clear(connection)
        arguments = _arguments(credential, installation_id, project_id, action)
        async with connection.transaction(isolation='read_committed'):
            await connection.execute(_LOCAL_ROLE)
            if not await connection.fetchval(_ROLE_QUERY):
                raise AuthError('forbidden')
            scope = await _async_resolve(connection, arguments)
            await _async_bind(connection, scope, arguments[0])
            yield scope
            intact = await _async_context_intact(connection, arguments)
            _accept_exit(intact, (await _async_resolve(connection, arguments) == scope) if intact else False)
    except AuthError:
        raise
    except _async_errors('InsufficientPrivilegeError'):
        raise AuthError('forbidden') from None
    except _async_errors('PostgresConnectionError', 'InterfaceError') + (OSError, TimeoutError):
        raise AuthError('core_unavailable') from None
    finally:
        try:
            await _async_clear(connection)
        except (Exception, asyncio.CancelledError) as error:
            # A failed clear cannot publish a result or become a retryable commit.
            try:
                connection.terminate()
            except Exception:
                pass  # Abort was attempted; native retirement is not established here.
            if isinstance(error, asyncio.CancelledError):
                raise
            raise AuthError('core_unavailable') from None
