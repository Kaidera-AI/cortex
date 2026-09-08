"""Cortex opaque credentials; no HTTP, registry creation, or startup side effects.

Adapter contract (all operations async):
    ServiceAuthStore(lambda: admin_pool)
    authenticate(raw) -> AuthContext                  # authoritative read, no cache
    observe_consumer_use(context)                     # AFTER a successful real call
    issue(*, authority, project_id, agent_id, installation_id, scopes, recover=False)
    create_setup_grant(*, authority, purpose='bootstrap') -> SetupGrant
    consume_setup_grant(raw, *, authority, resolve_identity, installation_id,
                        scopes, purpose='bootstrap', revoke_existing=True)
    rotate(token_id, *, authority) -> IssuedToken
    acknowledge_rotation(token_id, *, authority) -> AuthContext
    revoke(token_id, *, authority) -> bool
    disable_grant(principal_id, *, authority)
    disable_principal(principal_id, *, authority)
    set_grant_scopes(principal_id, scopes, *, authority)
    list_tokens(*, authority, principal_id=None) -> list[dict]  # metadata only
    due_rotations(*, authority) -> list[dict]
    invalidate_restored_generations(*, authority) -> int

Issuance returns IssuedToken(raw_token, context); only the SHA-256 digest enters
SQL. resolve_identity(conn) must return canonical (project_id, agent_id), using
the supplied transaction/connection. Bootstrap consumes its grant atomically
with that callback, registration, and issuance. Callbacks must not commit or
perform external side effects.

LOCAL_OWNER is an in-process capability, NOT authentication or wire data. Only
the adapter that has verified the private owner-control transport may pass it.
Never expose these mutation methods on the ordinary network listener merely
because a client supplies a flag, scope, token, or installation ID. Delegated
controller policy is intentionally not implemented by this owner-only store.

observe_consumer_use is called by trusted response middleware after an actual
successful, non-auth operation by the intended consumer. whoami, token helper
probes, rejected calls and untrusted headers cannot establish this evidence.
Owner acknowledgement additionally confirms intended-consumer reload; the
store refuses acknowledgement without server-observed candidate use.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import re
import secrets
from typing import Callable, Iterable
from uuid import UUID, uuid4

import asyncpg


SCOPES = frozenset({
    "memory:read", "memory:write", "runtime:read", "coordination:read",
    "coordination:write", "ingest:write", "registry:read", "registry:write",
    "instance:admin", "tokens:manage",
})
TOKEN_LIFETIME = timedelta(days=30)
ROTATE_AFTER = timedelta(days=21)
MAX_OVERLAP = timedelta(hours=24)
SETUP_LIFETIME = timedelta(minutes=10)
LOCAL_OWNER = object()
_TOKEN_PATTERN = re.compile(r"\Actx1_([0-9a-f]{32})\.([A-Za-z0-9_-]{43})\Z")
_SETUP_PATTERN = re.compile(r"\Actxs1_([0-9a-f]{32})\.([A-Za-z0-9_-]{43})\Z")
_DUMMY_DIGEST = bytes(32)


class AuthInvalid(Exception):
    """Absent, malformed, expired, revoked or otherwise unusable credential (401)."""


class AuthForbidden(Exception):
    """Valid identity but insufficient authority or incompatible operation (403)."""


class AuthStoreUnavailable(Exception):
    """Security store unavailable or incompatible: close the service (503)."""


@dataclass(frozen=True, slots=True)
class AuthContext:
    principal_id: UUID
    project_id: UUID
    project_key: str
    agent: str
    installation_id: str
    token_id: UUID
    scopes: frozenset[str]
    expires_at: datetime
    generation: int
    agent_id: UUID
    actor_id: UUID

    def require(self, *scopes: str) -> None:
        if not set(scopes) <= self.scopes:
            raise AuthForbidden("Required permission is not granted")


@dataclass(frozen=True, slots=True)
class IssuedToken:
    raw_token: str = field(repr=False)
    context: AuthContext


@dataclass(frozen=True, slots=True)
class SetupGrant:
    raw_token: str = field(repr=False)
    grant_id: UUID
    expires_at: datetime
    purpose: str


def _owner(authority) -> None:
    if authority is not LOCAL_OWNER:
        raise AuthForbidden("Verified local owner control is required")


def _expected_instance(state, expected) -> None:
    # The owner transport always supplies this guard. It is checked while the
    # state row is locked, not merely during an earlier discovery request.
    if expected is not None and str(state.get("instance_id")) != str(_uuid(expected)):
        raise AuthForbidden("Security instance changed; deliberate pairing is required")


def _scopes(value: Iterable[str], *, empty: bool = False) -> list[str]:
    if isinstance(value, (str, bytes)):
        raise AuthForbidden("Scopes must be an explicit permission collection")
    try:
        result = frozenset(value)
    except (TypeError, ValueError):
        raise AuthForbidden("Invalid permission collection") from None
    if not result <= SCOPES or (not result and not empty):
        raise AuthForbidden("Unknown or empty permissions")
    return sorted(result)


def _uuid(value) -> UUID:
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        raise AuthForbidden("Canonical UUID identity is required") from None


def _installation(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,199}", value):
        raise AuthForbidden("Invalid installation namespace")
    return value


def _credential(*, setup=False):
    public_id = uuid4()
    raw = f"{'ctxs1' if setup else 'ctx1'}_{public_id.hex}.{secrets.token_urlsafe(32)}"
    return public_id, raw, hashlib.sha256(raw.encode("ascii")).digest()


def _parse(raw, *, setup=False):
    pattern = _SETUP_PATTERN if setup else _TOKEN_PATTERN
    if not isinstance(raw, str) or not (match := pattern.fullmatch(raw)):
        raise AuthInvalid("Invalid credential")
    return UUID(hex=match.group(1)), hashlib.sha256(raw.encode("ascii")).digest()


_TOKEN_SELECT = """
SELECT t.*, p.project_id, p.agent_id, p.actor_id, p.installation_id,
       p.disabled_at AS principal_disabled, g.disabled_at AS grant_disabled,
       g.scopes AS grant_scopes, s.generation AS current_generation,
       cp.project_key, cp.status AS project_status, a.name AS agent_name,
       a.status AS agent_status, a.project_id AS agent_project_id,
       a.actor_id AS agent_actor_id, ca.project_id AS actor_project_id,
       ca.status AS actor_status
FROM cortex_auth.tokens t
JOIN cortex_auth.principals p ON p.id=t.principal_id
JOIN cortex_auth.grants g ON g.principal_id=p.id
JOIN cortex_auth.state s ON s.singleton
JOIN public.cortex_projects cp ON cp.id=p.project_id
JOIN public.agents a ON a.id=p.agent_id
JOIN public.cortex_actors ca ON ca.id=p.actor_id
WHERE t.id=$1
"""


def _context(row, now: datetime, *, allow_expired=False) -> AuthContext:
    if row is None:
        raise AuthInvalid("Invalid credential")
    if (
        row["revoked_at"] is not None
        or row["principal_disabled"] is not None
        or row["grant_disabled"] is not None
        or row["generation"] != row["current_generation"]
        or row["project_status"] != "active"
        or row["actor_status"] not in {"active", "system"}
        or row["agent_status"] in {"disabled", "deleted", "archived", "retired"}
        or row["agent_project_id"] != row["project_id"]
        or row["actor_project_id"] != row["project_id"]
        or row["agent_actor_id"] != row["actor_id"]
        or row["issued_at"] > now
        or (not allow_expired and row["expires_at"] <= now)
        or (not allow_expired and row["overlap_deadline"] is not None and row["overlap_deadline"] <= now)
    ):
        raise AuthInvalid("Invalid credential")
    # Unknown stored permissions are a schema/configuration fault, not a bypass.
    try:
        ceiling = frozenset(_scopes(row["scopes"]))
        grant = frozenset(_scopes(row["grant_scopes"], empty=True))
    except AuthForbidden:
        raise AuthStoreUnavailable("Invalid security store state") from None
    return AuthContext(
        principal_id=row["principal_id"], project_id=row["project_id"],
        project_key=row["project_key"], agent=row["agent_name"],
        installation_id=row["installation_id"], token_id=row["id"],
        scopes=ceiling & grant, expires_at=row["expires_at"],
        generation=row["generation"], agent_id=row["agent_id"], actor_id=row["actor_id"],
    )


class ServiceAuthStore:
    def __init__(self, pool_provider: Callable, *, clock: Callable | None = None):
        self._pool_provider = pool_provider
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def _now(self):
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise AuthStoreUnavailable("Security clock must be timezone-aware")
        return value.astimezone(timezone.utc)

    async def instance_identity(self, *, authority) -> str:
        """Authoritative, owner-only pairing metadata; never initializes state."""
        _owner(authority)
        async with self._connection() as conn:
            row = await conn.fetchrow("SELECT * FROM cortex_auth.state WHERE singleton")
            try:
                return str(UUID(str(row["instance_id"])))
            except (KeyError, TypeError, ValueError):
                raise AuthStoreUnavailable("Security instance is unavailable") from None

    @asynccontextmanager
    async def _connection(self, *, write=False):
        try:
            pool = self._pool_provider()
            if pool is None:
                raise AuthStoreUnavailable("Security store is unavailable")
            async with pool.acquire() as conn:
                if write:
                    async with conn.transaction():
                        # Serialize infrequent lifecycle changes, not data reads.
                        state = await conn.fetchrow("SELECT * FROM cortex_auth.state WHERE singleton FOR UPDATE")
                        if state is None:
                            raise AuthStoreUnavailable("Security state is missing")
                        yield conn, state
                else:
                    yield conn
        except (AuthInvalid, AuthForbidden, AuthStoreUnavailable):
            raise
        except (asyncpg.PostgresError, asyncpg.InterfaceError, OSError, TimeoutError):
            # Never expose driver diagnostics, SQL parameters, or credential bytes.
            raise AuthStoreUnavailable("Security store is unavailable") from None

    async def _audit(self, conn, action, generation, principal_id=None, token_id=None):
        await conn.execute(
            "INSERT INTO cortex_auth.audit(occurred_at,action,principal_id,token_id,generation) VALUES($1,$2,$3,$4,$5)",
            self._now(), action, principal_id, token_id, generation,
        )

    async def authenticate(self, raw) -> AuthContext:
        token_id, digest = _parse(raw)
        async with self._connection() as conn:
            row = await conn.fetchrow(_TOKEN_SELECT, token_id)
            if row is None and not await conn.fetchval("SELECT EXISTS(SELECT 1 FROM cortex_auth.state WHERE singleton)"):
                raise AuthStoreUnavailable("Security state is missing")
        stored = bytes(row["token_digest"]) if row is not None else _DUMMY_DIGEST
        if not hmac.compare_digest(digest, stored):
            raise AuthInvalid("Invalid credential")
        return _context(row, self._now())

    async def observe_consumer_use(self, context: AuthContext) -> None:
        """Trusted adapter hook; never invoke for a helper/auth-only probe."""
        if not isinstance(context, AuthContext):
            raise AuthForbidden("Verified request context is required")
        # Common case is read-only. Do not serialize every successful memory
        # request on the lifecycle lock or repeatedly update a last-used row.
        async with self._connection() as conn:
            row = await conn.fetchrow(_TOKEN_SELECT, context.token_id)
            current = _context(row, self._now())
            if current.principal_id != context.principal_id or current.generation != context.generation:
                raise AuthInvalid("Credential changed during the request")
            if row["consumer_used_at"] is not None:
                return
        async with self._connection(write=True) as (conn, state):
            row = await conn.fetchrow(_TOKEN_SELECT, context.token_id)
            current = _context(row, self._now())
            if current.principal_id != context.principal_id or current.generation != context.generation:
                raise AuthInvalid("Credential changed during the request")
            if row["consumer_used_at"] is None:
                await conn.execute("UPDATE cortex_auth.tokens SET consumer_used_at=$2 WHERE id=$1", context.token_id, self._now())
                await self._audit(conn, "consumer_used", state["generation"], current.principal_id, current.token_id)

    async def _principal(self, conn, project_id, agent_id, installation_id, scopes):
        identity = await conn.fetchrow(
            """SELECT a.id, a.actor_id FROM public.agents a
               JOIN public.cortex_projects cp ON cp.id=a.project_id
               JOIN public.cortex_actors ca ON ca.id=a.actor_id AND ca.project_id=cp.id
               WHERE a.id=$1 AND cp.id=$2 AND cp.status='active'
                 AND ca.status IN ('active','system')
                 AND a.status NOT IN ('disabled','deleted','archived','retired')""",
            agent_id, project_id,
        )
        if identity is None:
            raise AuthForbidden("An active canonical project and agent are required")
        principal = await conn.fetchrow(
            """SELECT p.*, g.disabled_at AS grant_disabled, g.scopes AS grant_scopes
               FROM cortex_auth.principals p JOIN cortex_auth.grants g ON g.principal_id=p.id
               WHERE installation_id=$1 AND project_id=$2 AND agent_id=$3""",
            installation_id, project_id, agent_id,
        )
        if principal is not None:
            if principal["disabled_at"] is not None or principal["grant_disabled"] is not None:
                raise AuthForbidden("Disabled grants cannot be re-enabled by enrollment or recovery")
            if principal["actor_id"] != identity["actor_id"]:
                raise AuthForbidden("Canonical identity changed; owner review is required")
            if not set(scopes) <= set(principal["grant_scopes"]):
                raise AuthForbidden("Requested permissions exceed the current grant")
            return principal["id"], False
        principal_id, now = uuid4(), self._now()
        await conn.execute(
            """INSERT INTO cortex_auth.principals(id,project_id,agent_id,actor_id,installation_id,created_at)
               VALUES($1,$2,$3,$4,$5,$6)""",
            principal_id, project_id, agent_id, identity["actor_id"], installation_id, now,
        )
        await conn.execute(
            "INSERT INTO cortex_auth.grants(principal_id,scopes,created_at,updated_at) VALUES($1,$2,$3,$3)",
            principal_id, scopes, now,
        )
        return principal_id, True

    async def _mint(self, conn, state, principal_id, scopes, *, predecessor=None):
        token_id, raw, digest = _credential()
        now = self._now()
        await conn.execute(
            """INSERT INTO cortex_auth.tokens(id,principal_id,family_id,generation,token_digest,
                       scopes,issued_at,expires_at,rotate_after,predecessor_id)
               VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)""",
            token_id, principal_id, predecessor["family_id"] if predecessor else uuid4(),
            state["generation"], digest, scopes, now, now + TOKEN_LIFETIME, now + ROTATE_AFTER,
            predecessor["id"] if predecessor else None,
        )
        row = await conn.fetchrow(_TOKEN_SELECT, token_id)
        context = _context(row, now)
        await self._audit(conn, "token_issued", state["generation"], principal_id, token_id)
        return IssuedToken(raw, context)

    async def _issue(self, conn, state, project_id, agent_id, installation_id, scopes, *, recover=False, revoke_existing=True):
        principal_id, created = await self._principal(conn, project_id, agent_id, installation_id, scopes)
        exists = await conn.fetchval("SELECT EXISTS(SELECT 1 FROM cortex_auth.tokens WHERE principal_id=$1)", principal_id)
        if exists and not recover:
            raise AuthForbidden("Already enrolled; reuse the saved credential or request owner recovery")
        if recover and revoke_existing:
            await conn.execute("UPDATE cortex_auth.tokens SET revoked_at=COALESCE(revoked_at,$2) WHERE principal_id=$1", principal_id, self._now())
        if created:
            await self._audit(conn, "principal_created", state["generation"], principal_id)
        if recover:
            await self._audit(conn, "owner_recovery", state["generation"], principal_id)
        return await self._mint(conn, state, principal_id, scopes)

    async def issue(self, *, authority, project_id, agent_id, installation_id, scopes, recover=False) -> IssuedToken:
        _owner(authority)
        project_id, agent_id = _uuid(project_id), _uuid(agent_id)
        installation_id, scopes = _installation(installation_id), _scopes(scopes)
        async with self._connection(write=True) as (conn, state):
            return await self._issue(conn, state, project_id, agent_id, installation_id, scopes, recover=recover)

    async def create_setup_grant(self, *, authority, purpose="bootstrap", expected_instance_id=None) -> SetupGrant:
        _owner(authority)
        if purpose not in {"bootstrap", "recovery"}:
            raise AuthForbidden("Unknown setup purpose")
        async with self._connection(write=True) as (conn, state):
            _expected_instance(state, expected_instance_id)
            if (purpose == "bootstrap") != (state["initialized_at"] is None):
                raise AuthForbidden("Setup purpose does not match initialized state")
            token_id, raw, digest = _credential(setup=True)
            now = self._now()
            await conn.execute("UPDATE cortex_auth.setup_grants SET revoked_at=$1 WHERE consumed_at IS NULL AND revoked_at IS NULL", now)
            await conn.execute(
                "INSERT INTO cortex_auth.setup_grants(id,purpose,token_digest,generation,created_at,expires_at) VALUES($1,$2,$3,$4,$5,$6)",
                token_id, purpose, digest, state["generation"], now, now + SETUP_LIFETIME,
            )
            await self._audit(conn, f"{purpose}_grant_issued", state["generation"])
            return SetupGrant(raw, token_id, now + SETUP_LIFETIME, purpose)

    async def consume_setup_grant(self, raw, *, authority, resolve_identity, installation_id, scopes,
                                  purpose="bootstrap", revoke_existing=True, expected_instance_id=None) -> IssuedToken:
        _owner(authority)
        grant_id, digest = _parse(raw, setup=True)
        installation_id, scopes = _installation(installation_id), _scopes(scopes)
        async with self._connection(write=True) as (conn, state):
            _expected_instance(state, expected_instance_id)
            grant = await conn.fetchrow("SELECT * FROM cortex_auth.setup_grants WHERE id=$1 FOR UPDATE", grant_id)
            stored = bytes(grant["token_digest"]) if grant is not None else _DUMMY_DIGEST
            if not hmac.compare_digest(digest, stored) or grant is None:
                raise AuthInvalid("Invalid setup credential")
            now = self._now()
            if (grant["purpose"] != purpose or grant["generation"] != state["generation"]
                    or grant["consumed_at"] is not None or grant["revoked_at"] is not None
                    or grant["expires_at"] <= now or grant["created_at"] > now):
                raise AuthInvalid("Invalid setup credential")
            if (purpose == "bootstrap") != (state["initialized_at"] is None):
                raise AuthForbidden("Setup purpose does not match initialized state")
            project_id, agent_id = await resolve_identity(conn)
            issued = await self._issue(conn, state, _uuid(project_id), _uuid(agent_id), installation_id,
                                       scopes, recover=purpose == "recovery", revoke_existing=revoke_existing)
            await conn.execute("UPDATE cortex_auth.setup_grants SET consumed_at=$2 WHERE id=$1", grant_id, now)
            if purpose == "bootstrap":
                await conn.execute("UPDATE cortex_auth.state SET initialized_at=$1 WHERE singleton", now)
            await self._audit(conn, f"{purpose}_consumed", state["generation"], issued.context.principal_id, issued.context.token_id)
            return issued

    async def rotate(self, token_id, *, authority) -> IssuedToken:
        _owner(authority)
        token_id = _uuid(token_id)
        async with self._connection(write=True) as (conn, state):
            row = await conn.fetchrow(_TOKEN_SELECT, token_id)
            # Owner renewal may recover from sleep past expiry; a data token cannot.
            context = _context(row, self._now(), allow_expired=True)
            if row["predecessor_id"] is not None and row["acknowledged_at"] is None:
                raise AuthForbidden("An unacknowledged candidate cannot be rotated")
            candidate = await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM cortex_auth.tokens WHERE predecessor_id=$1 AND revoked_at IS NULL)", token_id,
            )
            if candidate:
                raise AuthForbidden("Rotation already has a candidate; recover or revoke the orphan")
            if not context.scopes:
                raise AuthForbidden("No active permissions remain")
            now = self._now()
            if row["rotation_started_at"] is None:
                await conn.execute(
                    "UPDATE cortex_auth.tokens SET rotation_started_at=$2,overlap_deadline=$3 WHERE id=$1",
                    token_id, now, min(now + MAX_OVERLAP, row["expires_at"]),
                )
            issued = await self._mint(conn, state, context.principal_id, sorted(context.scopes), predecessor=row)
            await self._audit(conn, "rotation_started", state["generation"], context.principal_id, issued.context.token_id)
            return issued

    async def acknowledge_rotation(self, token_id, *, authority) -> AuthContext:
        _owner(authority)
        token_id = _uuid(token_id)
        async with self._connection(write=True) as (conn, state):
            row = await conn.fetchrow(_TOKEN_SELECT, token_id)
            context = _context(row, self._now())
            if row["predecessor_id"] is None:
                raise AuthForbidden("Credential is not a rotation candidate")
            if row["consumer_used_at"] is None:
                raise AuthForbidden("Intended consumer has not made a verified real call")
            if row["acknowledged_at"] is None:
                now = self._now()
                await conn.execute("UPDATE cortex_auth.tokens SET acknowledged_at=$2 WHERE id=$1", token_id, now)
                await conn.execute("UPDATE cortex_auth.tokens SET revoked_at=COALESCE(revoked_at,$2) WHERE id=$1", row["predecessor_id"], now)
                await self._audit(conn, "rotation_acknowledged", state["generation"], context.principal_id, token_id)
            return context

    async def revoke(self, token_id, *, authority) -> bool:
        _owner(authority)
        token_id = _uuid(token_id)
        async with self._connection(write=True) as (conn, state):
            row = await conn.fetchrow("SELECT principal_id,revoked_at FROM cortex_auth.tokens WHERE id=$1", token_id)
            if row is None:
                raise AuthForbidden("Unknown credential identifier")
            if row["revoked_at"] is not None:
                return False
            await conn.execute("UPDATE cortex_auth.tokens SET revoked_at=$2 WHERE id=$1", token_id, self._now())
            await self._audit(conn, "token_revoked", state["generation"], row["principal_id"], token_id)
            return True

    async def _disable(self, principal_id, authority, *, principal=False):
        _owner(authority)
        principal_id = _uuid(principal_id)
        async with self._connection(write=True) as (conn, state):
            exists = await conn.fetchval("SELECT EXISTS(SELECT 1 FROM cortex_auth.principals WHERE id=$1)", principal_id)
            if not exists:
                raise AuthForbidden("Unknown principal")
            now = self._now()
            if principal:
                await conn.execute("UPDATE cortex_auth.principals SET disabled_at=COALESCE(disabled_at,$2) WHERE id=$1", principal_id, now)
            await conn.execute("UPDATE cortex_auth.grants SET disabled_at=COALESCE(disabled_at,$2),updated_at=$2 WHERE principal_id=$1", principal_id, now)
            await conn.execute("UPDATE cortex_auth.tokens SET revoked_at=COALESCE(revoked_at,$2) WHERE principal_id=$1", principal_id, now)
            await self._audit(conn, "principal_disabled" if principal else "grant_disabled", state["generation"], principal_id)

    async def disable_grant(self, principal_id, *, authority):
        await self._disable(principal_id, authority)

    async def disable_principal(self, principal_id, *, authority):
        await self._disable(principal_id, authority, principal=True)

    async def set_grant_scopes(self, principal_id, scopes, *, authority):
        _owner(authority)
        principal_id, scopes = _uuid(principal_id), _scopes(scopes, empty=True)
        async with self._connection(write=True) as (conn, state):
            row = await conn.fetchrow(
                "SELECT p.disabled_at,g.disabled_at AS grant_disabled FROM cortex_auth.principals p JOIN cortex_auth.grants g ON g.principal_id=p.id WHERE p.id=$1",
                principal_id,
            )
            if row is None or row["disabled_at"] is not None or row["grant_disabled"] is not None:
                raise AuthForbidden("Unknown or disabled grant")
            await conn.execute("UPDATE cortex_auth.grants SET scopes=$2,updated_at=$3 WHERE principal_id=$1", principal_id, scopes, self._now())
            await self._audit(conn, "grant_scopes_changed", state["generation"], principal_id)

    async def list_tokens(self, *, authority, principal_id=None):
        _owner(authority)
        principal_id = _uuid(principal_id) if principal_id is not None else None
        async with self._connection() as conn:
            rows = await conn.fetch(
                """SELECT id,principal_id,family_id,generation,scopes,issued_at,expires_at,rotate_after,
                          revoked_at,predecessor_id,overlap_deadline,consumer_used_at,acknowledged_at
                   FROM cortex_auth.tokens WHERE ($1::uuid IS NULL OR principal_id=$1) ORDER BY issued_at,id""", principal_id,
            )
        return [dict(row) for row in rows]

    async def due_rotations(self, *, authority):
        _owner(authority)
        async with self._connection() as conn:
            rows = await conn.fetch(
                """SELECT t.id,t.principal_id,t.expires_at,t.rotate_after FROM cortex_auth.tokens t
                   JOIN cortex_auth.principals p ON p.id=t.principal_id
                   JOIN cortex_auth.grants g ON g.principal_id=p.id
                   JOIN cortex_auth.state s ON s.singleton AND s.generation=t.generation
                   WHERE t.revoked_at IS NULL AND p.disabled_at IS NULL AND g.disabled_at IS NULL
                     AND t.rotate_after <= $1 AND t.rotation_started_at IS NULL
                     AND (t.predecessor_id IS NULL OR t.acknowledged_at IS NOT NULL)
                   ORDER BY t.rotate_after,t.id""", self._now(),
            )
        return [dict(row) for row in rows]

    async def invalidate_restored_generations(self, *, authority) -> int:
        """Explicit restricted-restore hook. NEVER call during ordinary startup."""
        _owner(authority)
        async with self._connection(write=True) as (conn, state):
            generation = state["generation"] + 1
            now = self._now()
            await conn.execute("UPDATE cortex_auth.state SET generation=$1 WHERE singleton", generation)
            await conn.execute("UPDATE cortex_auth.tokens SET revoked_at=COALESCE(revoked_at,$1)", now)
            await conn.execute("UPDATE cortex_auth.setup_grants SET revoked_at=COALESCE(revoked_at,$1)", now)
            await self._audit(conn, "restore_invalidated", generation)
            return generation
