"""Private owner registration/issuer and explicit hash-bound Core adoption ports."""
from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import json
import re
import secrets
from uuid import UUID, uuid4

import psycopg
from psycopg.pq import TransactionStatus

from .auth import AuthError, authorized, _ROLE_QUERY, _bind, _clear, _context_intact, _resolve

_PERMISSIONS = frozenset(('read', 'write', 'control', 'owner', 'admin'))
_AUTHORITY = {'cortex-add-agent': 'owner_or_admin', 'lead_registration_requires': 'owner_or_admin'}


class IdentityError(AuthError):
    pass


@dataclass(frozen=True)
class PrincipalIdentity:
    principal_id: UUID
    name: str
    kind: str
    adopted: bool
    roles: tuple
    generation: int


@dataclass(frozen=True)
class IssuedCredential:
    principal_id: UUID
    credential_id: UUID
    audit_id: UUID
    replayed: bool
    secret: bytes | None = field(repr=False)


@dataclass(frozen=True)
class CredentialReceipt:
    principal_id: UUID
    credential_id: UUID
    audit_id: UUID
    replayed: bool
    revoked: bool


@dataclass(frozen=True)
class AdoptionPreview:
    status: str
    count: int
    manifest_sha256: str | None
    mapping: tuple


@dataclass(frozen=True)
class AdoptionReceipt:
    manifest_sha256: str
    count: int
    audit_id: UUID
    replayed: bool


@dataclass(frozen=True)
class DonorPin:
    """Trusted server release configuration; never populated from a caller header.

    The release authority supplies the exact released SHA and original source bytes.
    Synthetic fixture pins prove mechanics only, never qualify a real adoption.
    """
    released_sha: str
    source_bytes: bytes = field(repr=False)
    released: bool = False

    @property
    def source_sha256(self):
        return hashlib.sha256(self.source_bytes).hexdigest()


def _text(value, limit):
    if not isinstance(value, str) or not 1 <= len(value) <= limit or '\x00' in value:
        raise IdentityError('invalid_input')
    try:
        value.encode('utf-8')
    except UnicodeError:
        raise IdentityError('invalid_input') from None
    return value


def _uuid(value):
    if not isinstance(value, UUID):
        raise IdentityError('invalid_input')
    return value


def _roles(values):
    if not isinstance(values, (tuple, list)) or len(values) > 32 or any(not isinstance(x, str) for x in values):
        raise IdentityError('invalid_input')
    normalized = tuple(sorted(set(x.strip().lower() for x in values)))
    if any(re.fullmatch(r'[a-z][a-z0-9_.-]{0,63}', x) is None for x in normalized):
        raise IdentityError('invalid_input')
    return normalized


def _permissions(values):
    if not isinstance(values, (tuple, list)) or not values or len(values) > 5 or any(not isinstance(x, str) or x not in _PERMISSIONS for x in values):
        raise IdentityError('invalid_input')
    return tuple(sorted(set(values)))


def _ttl(value):
    if type(value) is not int or not 60 <= value <= 2592000:
        raise IdentityError('invalid_input')
    return value


def _canonical(value):
    try:
        data = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode('utf-8')
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise IdentityError('invalid_input') from None
    if len(data) > 1024 * 1024:
        raise IdentityError('invalid_input')
    return data


def _digest(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


def lookup_bound(connection, principal_id):
    """Fresh Core identity inside an already C04-bound private transaction."""
    row = connection.execute('SELECT * FROM auth.identity_lookup(%s)', (_uuid(principal_id),)).fetchone()
    return PrincipalIdentity(row[0], row[1], row[2], row[3], tuple(row[4]), row[5]) if row else None


class Identity:
    def __init__(self, connection, credential, installation_id, project_id, *, donor_pin=None):
        self.connection = connection
        self.credential = credential
        self.installation_id = installation_id
        self.project_id = project_id
        self.donor_pin = donor_pin

    @contextmanager
    def _control(self):
        c = self.connection
        if c.closed:
            raise AuthError('core_unavailable')
        if not c.autocommit or c.info.transaction_status != TransactionStatus.IDLE:
            raise AuthError('forbidden')
        try:
            _clear(c)
            if not isinstance(self.credential, bytes) or not 1 <= len(self.credential) <= 4096:
                raise AuthError('unauthenticated')
            _uuid(self.installation_id); _uuid(self.project_id)
            arguments = (hashlib.sha256(self.credential).hexdigest(), self.installation_id, self.project_id, 'control')
            with c.transaction():
                if not c.execute(_ROLE_QUERY).fetchone()[0]:
                    raise AuthError('forbidden')
                # Serialize identity mutations before taking generation FOR SHARE locks.
                c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',
                          ('c04a-identity:' + str(self.installation_id) + ':' + str(self.project_id),))
                before = _resolve(c, arguments)
                _bind(c, before, arguments[0])
                yield before
                if not _context_intact(c, arguments):
                    raise AuthError('scope_mismatch')
                after = _resolve(c, arguments)
                stable = lambda s: (s.installation_id, s.tenant_id, s.project_id, s.principal_id, s.action)
                if stable(after) != stable(before) or after.permission_generation < before.permission_generation:
                    raise AuthError('forbidden')
        except AuthError:
            raise
        except psycopg.errors.RaiseException as error:
            code = {'identity_forbidden': 'forbidden', 'identity_conflict': 'conflict',
                    'identity_invalid_input': 'invalid_input'}.get(error.diag.message_primary)
            raise IdentityError(code or 'core_unavailable') from None
        except psycopg.errors.UniqueViolation:
            raise IdentityError('conflict') from None
        except psycopg.Error:
            raise AuthError('core_unavailable') from None
        finally:
            try:
                _clear(c)
            except psycopg.Error:
                c.close()
                raise AuthError('core_unavailable') from None

    def lookup(self, principal_id):
        _uuid(principal_id)
        with authorized(self.connection, self.credential, self.installation_id, self.project_id) as scope:
            result = lookup_bound(self.connection, principal_id)
        return result

    def _register(self, scope, kind, principal_id, name, roles, permissions, credential_id,
                  digest, ttl, attestation, request_key, request_sha):
        if kind == 'human':
            query = 'SELECT auth.identity_register_human(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)'
            args = (principal_id, name, list(roles), list(permissions), credential_id, digest, ttl,
                    attestation, request_key, request_sha)
        else:
            query = 'SELECT auth.identity_register_agent(%s,%s,%s,%s,%s,%s,%s,%s,%s)'
            args = (principal_id, name, list(roles), list(permissions), credential_id, digest, ttl,
                    request_key, request_sha)
        return self.connection.execute(query, args).fetchone()[0]

    def _registration(self, kind, principal_id, name, roles, permissions, request_key, ttl_seconds, attestation=None):
        _uuid(principal_id); _text(name, 256); _text(request_key, 256); _ttl(ttl_seconds)
        roles = _roles(roles); permissions = _permissions(permissions)
        if kind == 'human':
            _text(attestation, 512)
            if not attestation.strip():
                raise IdentityError('invalid_input')
        request_sha = _digest(dict(operation='register_' + kind, principal_id=str(principal_id), name=name,
                                  roles=roles, permissions=permissions, ttl=ttl_seconds, attestation=attestation))
        secret = secrets.token_urlsafe(32).encode('ascii')
        digest = hashlib.sha256(secret).hexdigest()
        with self._control() as scope:
            raw = self._register(scope, kind, principal_id, name, roles, permissions, uuid4(), digest,
                                 ttl_seconds, attestation, request_key, request_sha)
        return IssuedCredential(UUID(raw['principal_id']), UUID(raw['credential_id']), UUID(raw['audit_id']),
                                raw['replayed'], None if raw['replayed'] else secret)

    def register_agent(self, principal_id, name, roles, permissions, *, request_key, ttl_seconds=86400):
        return self._registration('agent', principal_id, name, roles, permissions, request_key, ttl_seconds)

    def register_human(self, principal_id, name, roles, permissions, *, owner_attestation, request_key, ttl_seconds=86400):
        return self._registration('human', principal_id, name, roles, permissions, request_key, ttl_seconds, owner_attestation)

    def legacy_add_agent(self, name, role, *, principal_id, permissions, request_key, ttl_seconds=86400):
        return self.register_agent(principal_id, name, (role,), permissions, request_key=request_key, ttl_seconds=ttl_seconds)

    def set_roles(self, principal_id, roles, *, request_key):
        _uuid(principal_id); _text(request_key, 256); roles = _roles(roles)
        request_sha = _digest(dict(operation='set_roles', principal_id=str(principal_id), roles=roles))
        with self._control():
            self.connection.execute('SELECT auth.identity_set_roles(%s,%s,%s,%s)',
                                    (principal_id, list(roles), request_key, request_sha)).fetchone()
            result = lookup_bound(self.connection, principal_id)
        return result

    def rotate(self, principal_id, old_credential_id, *, request_key, ttl_seconds=86400):
        _uuid(principal_id); _uuid(old_credential_id); _text(request_key, 256); _ttl(ttl_seconds)
        request_sha = _digest(dict(operation='rotate', principal_id=str(principal_id),
                                  old_credential_id=str(old_credential_id), ttl=ttl_seconds))
        secret = secrets.token_urlsafe(32).encode('ascii')
        with self._control():
            raw = self.connection.execute('SELECT auth.identity_rotate(%s,%s,%s,%s,%s,%s,%s)',
                (principal_id, old_credential_id, uuid4(), hashlib.sha256(secret).hexdigest(),
                 ttl_seconds, request_key, request_sha)).fetchone()[0]
        return IssuedCredential(UUID(raw['principal_id']), UUID(raw['credential_id']), UUID(raw['audit_id']),
                                raw['replayed'], None if raw['replayed'] else secret)

    def revoke(self, credential_id, *, request_key):
        _uuid(credential_id); _text(request_key, 256)
        request_sha = _digest(dict(operation='revoke', credential_id=str(credential_id)))
        with self._control():
            raw = self.connection.execute('SELECT auth.identity_revoke(%s,%s,%s)',
                                          (credential_id, request_key, request_sha)).fetchone()[0]
        return CredentialReceipt(UUID(raw['principal_id']), UUID(raw['credential_id']),
                                 UUID(raw['audit_id']), raw['replayed'], raw['revoked'])

    def _manifest(self, manifest):
        pin = self.donor_pin
        if not isinstance(pin, DonorPin) or pin.released is not True:
            return None
        if not isinstance(pin.source_bytes, bytes) or len(pin.source_bytes) > 1024 * 1024 or re.fullmatch('[0-9a-f]{40}', pin.released_sha) is None:
            raise IdentityError('invalid_input')
        data = _canonical(manifest)
        # Make a private normalized snapshot before any DB operation.
        manifest = json.loads(data)
        if not isinstance(manifest, dict) or set(manifest) != {'version', 'installation_id', 'project_id', 'donor', 'agents', 'legacy_registration_authority'}:
            raise IdentityError('invalid_input')
        if type(manifest['version']) is not int or manifest['version'] != 1 or manifest['installation_id'] != str(self.installation_id) or manifest['project_id'] != str(self.project_id):
            raise IdentityError('scope_mismatch')
        if manifest['donor'] != {'released_sha': pin.released_sha, 'source_sha256': pin.source_sha256} or manifest['legacy_registration_authority'] != _AUTHORITY:
            raise IdentityError('invalid_input')
        try:
            donor = json.loads(pin.source_bytes)
            source = {x['source_id']: x for x in donor['agents']}
        except (ValueError, TypeError, KeyError, RecursionError):
            raise IdentityError('invalid_input') from None
        if len(source) != len(donor['agents']) or not isinstance(donor['project'], str):
            raise IdentityError('invalid_input')
        agents = manifest['agents']
        if not isinstance(agents, list) or not 1 <= len(agents) <= 1000:
            raise IdentityError('invalid_input')
        seen_ids = set(); seen_names = set(); seen_sources = set()
        for item in agents:
            if not isinstance(item, dict) or set(item) != {'source_project', 'source_id', 'principal_id', 'name', 'kind', 'roles', 'permissions'}:
                raise IdentityError('invalid_input')
            original = source.get(item['source_id'])
            try:
                target = UUID(item['principal_id'])
            except (ValueError, TypeError, AttributeError):
                raise IdentityError('invalid_input') from None
            if str(target) != item['principal_id'] or item['kind'] != 'agent' or item['source_project'] != donor['project'] or original is None:
                raise IdentityError('invalid_input')
            _text(item['name'], 256)
            roles = _roles(item['roles']); permissions = _permissions(item['permissions'])
            if item['name'] != original['name'] or roles != _roles((original['role'],)) or list(roles) != item['roles'] or list(permissions) != item['permissions']:
                raise IdentityError('invalid_input')
            if target in seen_ids or item['name'] in seen_names or item['source_id'] in seen_sources:
                raise IdentityError('conflict')
            seen_ids.add(target); seen_names.add(item['name']); seen_sources.add(item['source_id'])
        return manifest, hashlib.sha256(data).hexdigest()

    def dry_run_adoption(self, manifest):
        prepared = self._manifest(manifest)
        with self._control():
            if prepared is None:
                result = AdoptionPreview('not_run', 0, None, ())
            else:
                snapshot, digest = prepared
                for item in snapshot['agents']:
                    if not self.connection.execute('SELECT auth.identity_adoption_check(%s,%s)',
                                                   (UUID(item['principal_id']), item['name'])).fetchone()[0]:
                        raise IdentityError('conflict')
                result = AdoptionPreview('ready', len(snapshot['agents']), digest, tuple(snapshot['agents']))
        return result

    def apply_adoption(self, manifest, *, approved_sha256, request_key):
        _text(request_key, 256)
        prepared = self._manifest(manifest)
        if prepared is None:
            raise IdentityError('adoption_not_ready')
        snapshot, digest = prepared
        if approved_sha256 != digest:
            raise IdentityError('conflict')
        request_sha = _digest(dict(operation='adopt', manifest_sha256=digest))
        with self._control():
            raw = self.connection.execute('SELECT auth.identity_adopt(%s::jsonb,%s,%s,%s)',
                                          (_canonical(snapshot).decode(), digest, request_key, request_sha)).fetchone()[0]
        return AdoptionReceipt(raw['manifest_sha256'], raw['count'], UUID(raw['audit_id']), raw['replayed'])
