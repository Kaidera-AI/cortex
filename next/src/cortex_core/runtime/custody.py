"""J06 explicit encrypted project-vault adapter; no key creation or fallback."""
import base64
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import hmac
import math
import os
import re
import time

from cortex_core.release.codec import canonical_bytes
from .bootstrap_config import BootstrapConfig, MODE_PURPOSES, ROLE_NAMES
from .contracts import decode
from . import custody_fs
from .signed_member import Refusal

_SCHEMA = 'cortex.custody.v1'
_SEED = 'cortex.seed-custody.v1'
_KEY_ID = re.compile(r'[A-Za-z0-9_-]{1,64}\Z')
_SECRET = re.compile(rb'[0-9a-f]{64}\Z')
_INSTALLER = ContextVar('cortex_installer_custody', default=None)
_INSTALLER_PHASES = ('PRE', 'MIGRATE', 'POST', 'LEDGER', 'SEED', 'ACK', 'CLOSE')


def _unbase(value, *, maximum):
    if type(value) is not str or len(value) > maximum * 2:
        raise Refusal('custody_invalid')
    try:
        raw = base64.b64decode(value, validate=True)
    except (ValueError, TypeError):
        raise Refusal('custody_invalid') from None
    if len(raw) > maximum or base64.b64encode(raw).decode('ascii') != value:
        raise Refusal('custody_invalid')
    return raw


def _encoded(raw):
    return base64.b64encode(raw).decode('ascii')


class SeedMaterial:
    """Immutable private material, never an implicit dict/dataclass/log payload."""
    __slots__ = ('_raw', '_generation')

    def __init__(self, payload, generation):
        object.__setattr__(self, '_raw', canonical_bytes(payload))
        object.__setattr__(self, '_generation', generation)

    def __setattr__(self, name, value):
        raise AttributeError('private immutable seed material')

    def __repr__(self):
        return '<SeedMaterial private>'

    def _value(self):
        return decode(self._raw, limit=custody_fs.MAX_RECORD)

    @property
    def intent(self):
        return self._value()['intent']

    @property
    def credential(self):
        return _unbase(self._value()['credential'], maximum=4096)

    @property
    def state(self):
        return self._value()['state']

    @property
    def receipt(self):
        return self._value()['receipt']

    @property
    def generation(self):
        return self._generation


class CustodyVault:
    __slots__ = ('_config', '_key_id', '_cipher', '_directory', '_closed')

    def __init__(self, config, *, unlock_fd, key_id):
        # Install is born only inside open_installer_custody, never by widening
        # this public constructor or accepting a caller-supplied birth token.
        if (type(config) is not BootstrapConfig or config.mode not in MODE_PURPOSES
                or config.mode == 'install'
                or type(key_id) is not str or _KEY_ID.fullmatch(key_id) is None):
            raise Refusal('custody_locked')
        _unlock_vault(self, config, unlock_fd=unlock_fd, key_id=key_id)

    def __repr__(self):
        return '<CustodyVault private>'

    def _live(self):
        if self._closed:
            raise Refusal('custody_closed')
        if self._config.mode == 'install':
            binding = _INSTALLER.get()
            owner = _current_installer(self._config, self, binding[3] if binding else None)
            if owner._stage not in ('IMPORT', *_INSTALLER_PHASES):
                raise Refusal('installer_context')
        self._directory.check()

    def _reference(self, purpose):
        c = self._config
        if purpose == 'client' and c.mode == 'client':
            self._live()
            return c.client_ref, 'seed'
        if type(purpose) is str and purpose in MODE_PURPOSES[c.mode] and purpose in c.roles:
            self._live()
            return c.roles[purpose].secret_ref, purpose
        raise Refusal('purpose_not_admitted')

    def _aad(self, reference, purpose, generation):
        c = self._config
        return canonical_bytes([_SCHEMA, c.installation_id, c.tenant_id, c.project_id,
                                reference, purpose, self._key_id, generation])

    def _open(self, raw, reference, purpose):
        try:
            envelope = decode(raw, limit=custody_fs.MAX_RECORD)
            if (type(envelope) is not dict
                    or set(envelope) != {'schema','key_id','generation','nonce','ciphertext'}
                    or envelope['schema'] != _SCHEMA or envelope['key_id'] != self._key_id
                    or type(envelope['generation']) is not int
                    or not 0 <= envelope['generation'] < 2**63):
                raise Refusal('custody_invalid')
            nonce = _unbase(envelope['nonce'], maximum=12)
            ciphertext = _unbase(envelope['ciphertext'], maximum=48000)
            if len(nonce) != 12 or len(ciphertext) < 16:
                raise Refusal('custody_invalid')
            plain = self._cipher.decrypt(nonce, ciphertext,
                        self._aad(reference, purpose, envelope['generation']))
            value = decode(plain, limit=32000)
            if type(value) is not dict:
                raise Refusal('custody_invalid')
            return value, envelope['generation']
        except Exception:
            raise Refusal('custody_invalid') from None

    def _seal(self, payload, reference, purpose, generation):
        try:
            plain = canonical_bytes(payload)
            if len(plain) > 32000:
                raise Refusal('custody_invalid')
            nonce = os.urandom(12)
            ciphertext = self._cipher.encrypt(nonce, plain, self._aad(reference, purpose, generation))
            return canonical_bytes(dict(schema=_SCHEMA, key_id=self._key_id,
                generation=generation, nonce=_encoded(nonce), ciphertext=_encoded(ciphertext)))
        except (ValueError, TypeError, OverflowError):
            raise Refusal('custody_invalid') from None

    def _seed_payload(self, value, generation):
        if (type(value) is not dict or set(value) != {'schema','intent','credential','state','receipt'}
                or value['schema'] != _SEED or type(value['intent']) is not dict
                or value['state'] not in ('prepared','committed')
                or (value['state'] == 'prepared' and value['receipt'] is not None)
                or (value['state'] == 'committed' and type(value['receipt']) is not dict)):
            raise Refusal('custody_invalid')
        credential = _unbase(value['credential'], maximum=4096)
        if _SECRET.fullmatch(credential) is None:
            raise Refusal('custody_invalid')
        c = self._config
        for key, expected in [('installation_id',c.installation_id),('tenant_id',c.tenant_id),
                              ('project_id',c.project_id),('client_ref',c.client_ref),
                              ('credential_sha256',hashlib.sha256(credential).hexdigest())]:
            if value['intent'].get(key) != expected:
                raise Refusal('custody_invalid')
        return SeedMaterial(value, generation)

    @contextmanager
    def seed_record(self):
        owner = None
        if self._config.mode == 'install':
            owner = _installer_seed_owner(self)
            if owner._record_open: raise Refusal('installer_resources')
        elif self._config.mode != 'seed':
            raise Refusal('purpose_not_admitted')
        self._live()
        reference = self._config.client_ref
        if type(reference) is not str:
            raise Refusal('custody_invalid')
        if owner is not None:
            object.__setattr__(owner, '_record_open', True)
        try:
            with custody_fs.record_lock(self._directory, reference.rsplit('/',1)[1]):
                record = _SeedRecord(self, reference)
                try:
                    yield record
                finally:
                    record._active = False
        except BaseException:
            if owner is not None: _installer_fail(owner)
            raise
        finally:
            if owner is not None: object.__setattr__(owner, '_record_open', False)

    @contextmanager
    def _read_seed_receipt(self, deadline):
        """Read-only existing encrypted record; no create/commit handle escapes."""
        owner = None
        if self._config.mode == 'install':
            owner = _installer_at(self._config, self, deadline, 'ACK')
            if owner._resources is not None or owner._record_open:
                raise Refusal('installer_resources')
        elif self._config.mode != 'seed':
            raise Refusal('purpose_not_admitted')
        self._live()
        reference = self._config.client_ref
        if type(reference) is not str: raise Refusal('custody_invalid')
        if owner is not None: object.__setattr__(owner, '_record_open', True)
        try:
            with custody_fs.record_lock(self._directory, reference.rsplit('/',1)[1]):
                saved = custody_fs.read_record(self._directory, reference.rsplit('/',1)[1])
                if saved is None:
                    yield None
                else:
                    value, generation = self._open(saved[0], reference, 'seed')
                    yield self._seed_payload(value, generation)
        except BaseException:
            if owner is not None: _installer_fail(owner)
            raise
        finally:
            if owner is not None: object.__setattr__(owner, '_record_open', False)

    def resolve(self, purpose):
        reference, kind = self._reference(purpose)  # admission BEFORE record access
        result = custody_fs.read_record(self._directory, reference.rsplit('/',1)[1])
        if result is None:
            raise Refusal('custody_missing')
        value, generation = self._open(result[0], reference, kind)
        if kind == 'seed':
            material = self._seed_payload(value, generation)
            if material.state != 'committed':
                raise Refusal('custody_uncommitted')
            return material.credential
        return _credential_bytes(value)

    def import_credential(self, purpose, credential):
        reference, kind = self._reference(purpose)
        if kind == 'seed' or type(credential) is not bytes or not 1 <= len(credential) <= 4096:
            raise Refusal('purpose_not_admitted')
        self._import_role(reference, kind, credential)

    def _import_role(self, reference, kind, credential):
        # Callers have admitted operation + purpose before directory/record IO.
        # Replay authenticates privately; it cannot grant public resolve-all.
        if self._config.mode == 'install':
            binding = _INSTALLER.get()
            owner = _current_installer(self._config, self, binding[3] if binding else None)
            if (owner._stage != 'IMPORT' or not owner._busy or type(kind) is not str or kind not in ROLE_NAMES
                    or reference != self._config.roles[kind].secret_ref):
                raise Refusal('purpose_not_admitted')
        self._live()
        name = reference.rsplit('/',1)[1]
        with custody_fs.record_lock(self._directory, name):
            existing = custody_fs.read_record(self._directory, name)
            if existing is not None:
                value, _ = self._open(existing[0], reference, kind)
                if not hmac.compare_digest(_credential_bytes(value), credential):
                    raise Refusal('custody_changed')
                return
            raw = self._seal({'schema':'cortex.credential.v1','credential':_encoded(credential)}, reference, kind, 0)
            custody_fs.write_record(self._directory, name, raw, expected=None)
            # Credit is possible only after publication AND authenticated reload.
            saved = custody_fs.read_record(self._directory, name)
            if saved is None:
                raise Refusal('custody_missing')
            value, _ = self._open(saved[0], reference, kind)
            if not hmac.compare_digest(_credential_bytes(value), credential):
                raise Refusal('custody_changed')

    def close(self):
        self._closed = True
        self._cipher = None
        directory, self._directory = self._directory, None
        if directory is not None:
            directory.close()


def _unlock_vault(vault, config, *, unlock_fd, key_id):
    if (type(vault) is not CustodyVault or type(config) is not BootstrapConfig
            or config.mode not in MODE_PURPOSES
            or type(key_id) is not str or _KEY_ID.fullmatch(key_id) is None):
        raise Refusal('custody_locked')
    if config.mode == 'install':
        binding = _INSTALLER.get()
        if type(binding) is not tuple or len(binding) != 4:
            raise Refusal('installer_private')
        _bound_installer(config, vault, binding[3], opening=True)
    vault._closed, vault._directory, vault._cipher = True, None, None
    key = custody_fs.read_unlock(unlock_fd)
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        vault._cipher = AESGCM(key)
    except (ImportError, ValueError):
        raise Refusal('custody_backend_unavailable') from None
    finally:
        del key
    vault._config, vault._key_id = config, key_id
    try:
        vault._directory = custody_fs.PrivateDirectory(config.vault_root)
        vault._closed = False
    except BaseException:
        vault._cipher = None
        raise


def _credential_bytes(value):
    if set(value) != {'schema','credential'} or value['schema'] != 'cortex.credential.v1':
        raise Refusal('custody_invalid')
    raw = _unbase(value['credential'], maximum=4096)
    if not raw:
        raise Refusal('custody_invalid')
    return raw


def _installer_deadline(deadline):
    try:
        if (type(deadline) not in (int, float) or not math.isfinite(deadline)
                or deadline <= time.monotonic()):
            raise Refusal('installer_deadline')
    except OverflowError:
        raise Refusal('installer_deadline') from None


def _current_installer(config, custody, deadline):
    """Private identity/stage/context admission, before any directory or record IO.

    Tokens are refreshed to verify the *originating* Context, not merely a copied
    ContextVar value. This is trusted-code composition, not process isolation.
    """
    return _bound_installer(config, custody, deadline)


def _bound_installer(config, custody, deadline, *, opening=False):
    binding = _INSTALLER.get()
    if type(binding) is not tuple or len(binding) != 4:
        raise Refusal('installer_context')
    owner, original_config, vault, original_deadline = binding
    if (type(owner) is not InstallerCustody or type(config) is not BootstrapConfig
            or config is not original_config or config.mode != 'install'
            or type(custody) is not CustodyVault or custody is not vault
            or owner._config is not config or owner._vault is not vault
            or vault._config is not config or vault._closed != opening
            or owner._closed or owner._tainted
            or (owner._stage != 'OPENING' if opening else
                owner._stage not in ('IMPORT', 'RUN', *_INSTALLER_PHASES))
            or owner._deadline is not original_deadline
            or type(deadline) is not type(original_deadline) or deadline != original_deadline):
        raise Refusal('installer_context')
    _installer_deadline(original_deadline)
    try:
        _INSTALLER.reset(owner._token)
    except (ValueError, RuntimeError):
        raise Refusal('installer_context') from None
    object.__setattr__(owner, '_token', _INSTALLER.set(binding))
    return owner


class InstallerCustody:
    """Private finite owner of the fixed shipped Nix sequence, never callbacks."""
    __slots__ = ('_config', '_vault', '_deadline', '_stage', '_imported',
                 '_closed', '_tainted', '_busy', '_token', '_next',
                 '_resources', '_record_open', '_ack', '_operation_key')

    def __new__(cls, *args, **kwargs):
        raise Refusal('installer_private')

    def __setattr__(self, name, value):
        raise AttributeError('private installer custody')

    def __repr__(self):
        return '<InstallerCustody private>'

    def __reduce_ex__(self, protocol):
        raise Refusal('installer_private')

    def import_credential(self, purpose, credential_bytes):
        owner = _admit_installer(self)
        try:
            if (type(purpose) is not str or purpose not in ROLE_NAMES
                    or type(credential_bytes) is not bytes or not 1 <= len(credential_bytes) <= 4096):
                raise Refusal('purpose_not_admitted')
            object.__setattr__(owner, '_busy', True)
            owner._vault._import_role(owner._config.roles[purpose].secret_ref, purpose, credential_bytes)
            _current_installer(owner._config, owner._vault, owner._deadline)
            object.__setattr__(owner, '_imported', owner._imported | {purpose})
        except BaseException:
            object.__setattr__(owner, '_tainted', True)
            raise
        finally:
            object.__setattr__(owner, '_busy', False)

    def run(self, operation_key):
        owner = _admit_installer(self)
        try:
            if owner._imported != ROLE_NAMES:
                raise Refusal('installer_imports_incomplete')
            # Keep the existing seed operation contract; never execute a callback.
            from cortex_core.schema.seed import _operation
            _operation(operation_key)
            object.__setattr__(owner, '_operation_key', operation_key)
            object.__setattr__(owner, '_busy', True)
            object.__setattr__(owner, '_stage', 'RUN')
            try:
                from cortex_ship.install.provision import provision_sequence
            except ImportError:
                raise Refusal('installer_incomplete') from None
            result = provision_sequence(owner._config, custody=owner._vault,
                        deadline=owner._deadline, operation_key=operation_key)
            _current_installer(owner._config, owner._vault, owner._deadline)
            if (owner._stage != 'RUN' or owner._next != len(_INSTALLER_PHASES)
                    or owner._resources is not None or owner._record_open
                    or owner._ack is None or type(result) is not dict
                    or canonical_bytes(result) != owner._ack):
                raise Refusal('installer_incomplete')
            object.__setattr__(owner, '_stage', 'COMPLETE')
            return decode(owner._ack, limit=32000)
        except BaseException as error:
            _installer_fail(owner)
            if isinstance(error, (Refusal, KeyboardInterrupt, SystemExit)): raise
            raise Refusal('installer_sequence') from None
        finally:
            object.__setattr__(owner, '_busy', False)


def _admit_installer(owner):
    if type(owner) is not InstallerCustody:
        raise Refusal('installer_private')
    try:
        actual = _current_installer(owner._config, owner._vault, owner._deadline)
        if actual is not owner or owner._busy or owner._stage != 'IMPORT':
            raise Refusal('installer_context')
    except AttributeError:
        raise Refusal('installer_private') from None
    except BaseException:
        object.__setattr__(owner, '_tainted', True)
        raise
    return owner


def _installer_fail(owner):
    object.__setattr__(owner, '_tainted', True)
    object.__setattr__(owner, '_stage', 'ERROR')
    object.__setattr__(owner, '_ack', None)


def _installer_at(config, vault, deadline, phase):
    owner = _current_installer(config, vault, deadline)
    if not owner._busy or owner._stage != phase:
        if owner._busy: _installer_fail(owner)
        raise Refusal('installer_phase')
    return owner


@contextmanager
def _installer_phase(config, *, custody, deadline, phase):
    """Only fixed run call sites enter; no yielded authority or injected body."""
    owner = _current_installer(config, custody, deadline)
    try:
        if (not owner._busy or owner._stage != 'RUN' or type(phase) is not str
                or owner._next >= len(_INSTALLER_PHASES)
                or phase != _INSTALLER_PHASES[owner._next]
                or owner._resources is not None or owner._record_open
                or (phase == 'CLOSE' and owner._ack is None)):
            raise Refusal('installer_phase')
        object.__setattr__(owner, '_stage', phase)
        yield
        _installer_at(config, custody, deadline, phase)
        if (owner._resources is not None or owner._record_open
                or (phase == 'ACK' and owner._ack is None)):
            raise Refusal('installer_resources')
        object.__setattr__(owner, '_next', owner._next + 1)
        object.__setattr__(owner, '_stage', 'RUN')
    except BaseException:
        _installer_fail(owner)
        raise


def _installer_role_bytes(owner, purpose):
    vault = owner._vault
    vault._live()
    reference = owner._config.roles[purpose].secret_ref
    saved = custody_fs.read_record(vault._directory, reference.rsplit('/',1)[1])
    if saved is None: raise Refusal('custody_missing')
    value, _ = vault._open(saved[0], reference, purpose)
    raw = _credential_bytes(value)
    _current_installer(owner._config, vault, owner._deadline)
    return raw


def _installer_password(config, *, custody, deadline, purpose):
    """Nix's four fixed bound PRE steps, inside actual bootstrap transaction."""
    owner = _installer_at(config, custody, deadline, 'PRE')
    from .resources import BootstrapResources
    resource = owner._resources
    if (type(purpose) is not str
            or purpose not in ('migrator','installer_enrollment','api_request','probe')
            or type(resource) is not BootstrapResources or resource._closed
            or resource._custody is not custody or resource._config is not config
            or not resource._in_transaction):
        _installer_fail(owner)
        raise Refusal('purpose_not_admitted')
    resource.check_deadline()
    return _installer_role_bytes(owner, purpose)


def _installer_connect_password(config, vault, deadline, purpose):
    owner = _current_installer(config, vault, deadline)
    purposes = {'PRE': ('bootstrap',), 'MIGRATE': ('migrator',),
                'POST': ('bootstrap',), 'LEDGER': ('bootstrap',),
                'SEED': ('bootstrap','installer_enrollment','api_request'),
                'CLOSE': ('bootstrap',)}
    if (not owner._busy or owner._resources is not True or type(purpose) is not str
            or purpose not in purposes.get(owner._stage, ())):
        _installer_fail(owner)
        raise Refusal('purpose_not_admitted')
    return _installer_role_bytes(owner, purpose)


def _installer_seed_owner(vault):
    binding = _INSTALLER.get()
    owner = _installer_at(vault._config, vault, binding[3] if binding else None, 'SEED')
    from .resources import current_seed_resources
    resource = current_seed_resources(vault._config, owner._deadline)
    if resource._custody is not vault or owner._resources is not resource:
        _installer_fail(owner)
        raise Refusal('installer_resources')
    return owner


@contextmanager
def open_installer_custody(config, *, unlock_fd, key_id, deadline):
    if type(config) is not BootstrapConfig or config.mode != 'install':
        raise Refusal('installer_config')
    _installer_deadline(deadline)
    if _INSTALLER.get() is not None:
        raise Refusal('installer_nested')
    if (type(unlock_fd) is not int or unlock_fd < 3 or type(key_id) is not str
            or _KEY_ID.fullmatch(key_id) is None):
        raise Refusal('custody_locked')
    owner = object.__new__(InstallerCustody)
    vault = object.__new__(CustodyVault)
    vault._config = config
    vault._closed, vault._directory, vault._cipher = True, None, None
    for name, value in [('_config', config), ('_vault', vault), ('_deadline', deadline),
                        ('_stage', 'OPENING'), ('_imported', frozenset()),
                        ('_closed', False), ('_tainted', False), ('_busy', False),
                        ('_next', 0), ('_resources', None), ('_record_open', False),
                        ('_ack', None), ('_operation_key', None)]:
        object.__setattr__(owner, name, value)
    object.__setattr__(owner, '_token', _INSTALLER.set((owner, config, vault, deadline)))
    try:
        _unlock_vault(vault, config, unlock_fd=unlock_fd, key_id=key_id)
        object.__setattr__(owner, '_stage', 'IMPORT')
        _current_installer(config, vault, deadline)
        yield owner
    except BaseException:
        object.__setattr__(owner, '_tainted', True)
        raise
    finally:
        object.__setattr__(owner, '_closed', True)
        object.__setattr__(owner, '_stage', 'ERROR' if owner._tainted else 'EXIT')
        object.__setattr__(owner, '_imported', frozenset())
        object.__setattr__(owner, '_ack', None)
        object.__setattr__(owner, '_operation_key', None)
        cleanup_failed = False
        try:
            vault.close()
        except BaseException:
            cleanup_failed = True
        finally:
            try:
                _INSTALLER.reset(owner._token)
            except (ValueError, RuntimeError):
                cleanup_failed = True
            object.__setattr__(owner, '_token', None)
        if cleanup_failed:
            raise Refusal('installer_cleanup') from None


class _SeedRecord:
    __slots__ = ('_vault','_reference','_active','_material','_snapshot','_tainted')

    def __init__(self, vault, reference):
        self._vault, self._reference, self._active = vault, reference, True
        self._material = self._snapshot = None
        self._tainted = False

    def __repr__(self):
        return '<SeedRecord private>'

    def _live(self):
        if self._vault._config.mode == 'install':
            # Retained seed handles cannot cross a phase or active resource scope.
            _installer_seed_owner(self._vault)
        self._vault._live()
        if not self._active or self._tainted:
            raise Refusal('custody_closed')

    def load(self):
        self._live()
        result = custody_fs.read_record(self._vault._directory, self._reference.rsplit('/',1)[1])
        if result is None:
            self._material = self._snapshot = None
            return None
        value, generation = self._vault._open(result[0], self._reference, 'seed')
        self._material = self._vault._seed_payload(value, generation)
        self._snapshot = result[1]
        return self._material

    def _write(self, payload, generation, expected):
        self._live()
        material = self._vault._seed_payload(payload, generation)
        raw = self._vault._seal(payload, self._reference, 'seed', generation)
        try:
            snapshot = custody_fs.write_record(self._vault._directory,
                self._reference.rsplit('/',1)[1], raw, expected=expected)
        except BaseException:
            self._tainted = True
            raise
        self._snapshot, self._material = snapshot, material
        return material

    def create(self, *, intent, credential):
        self._live()
        if type(intent) is not dict or type(credential) is not bytes:
            raise Refusal('custody_invalid')
        if custody_fs.read_record(self._vault._directory, self._reference.rsplit('/',1)[1]) is not None:
            raise Refusal('custody_changed')
        return self._write(dict(schema=_SEED,intent=intent,credential=_encoded(credential),
                                state='prepared',receipt=None), 0, None)

    def commit(self, *, receipt):
        self._live()
        if self._material is None or type(receipt) is not dict or not receipt:
            raise Refusal('custody_invalid')
        if self._material.state == 'committed':
            if self._material.receipt != receipt:
                raise Refusal('custody_changed')
            current = custody_fs.read_record(self._vault._directory, self._reference.rsplit('/',1)[1])
            if current is None or current[1] != self._snapshot:
                raise Refusal('custody_changed')
            return self._material
        return self._write(dict(schema=_SEED,intent=self._material.intent,
            credential=_encoded(self._material.credential),state='committed',receipt=receipt),
            self._material.generation+1, self._snapshot)


@contextmanager
def open_custody(config, *, unlock_fd, key_id):
    vault = CustodyVault(config, unlock_fd=unlock_fd, key_id=key_id)
    try:
        yield vault
    finally:
        vault.close()
