"""X3 fixed resolver policy over protected owner ports (installed supplier UNBOUND).

Fixtures replace _installed_inputs only. Python objects/observations are not OS
custody or effect authority; no public supplier injection, secret store, driver,
file IO, transport, recovery journal or runtime binding switch is provided here.
Necessary transient driver authentication buffers are private, not zeroized by
claim. P1 accepts nonsecret original descriptors, never credential preimages.
"""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
import math
from uuid import UUID
from weakref import WeakKeyDictionary

from .signed_member import Refusal
from cortex_core.driver_logging import private_driver_logs

PORT = 'cortex.runtime.secret-resolver.v1'
MISSING_UNIT = 'X3 protected resolver/custodian and native material supplier'
_REASONS = frozenset(('required_binding_gap invalid_handle invalid_reference invalid_deadline '
    'deadline_expired purpose_not_in_view authority_changed resolver_unavailable '
    'carrier_mismatch cleanup_unproved session_mismatch invalid_connection_policy').split())


class SecretRefusal(Refusal):
    def __init__(self, reason):
        reason = reason if type(reason) is str and reason in _REASONS else 'resolver_unavailable'
        self.result = 'HOLD' if reason == 'cleanup_unproved' else 'BLOCKED'
        super().__init__(reason, unit=MISSING_UNIT if reason == 'required_binding_gap' else None)


@dataclass(frozen=True)
class _Grant:
    """Protected supplier observation DATA, not a caller-creatable grant."""
    reference: str
    revision: str
    installation: str
    purpose: str
    mode: str
    process: tuple
    epoch: str
    generation: int
    expires: float
    original: str


@dataclass(frozen=True)
class _CarrierState:
    resource: str
    identity: tuple
    process: tuple
    uid: str
    mode: int
    directory_mode: int
    links: int
    private: bool
    regular: bool
    present: bool
    consumers: int
    phase: str


@dataclass(frozen=True)
class _SQLPolicy:
    driver: str
    host: str
    port: int
    database: str
    user: str
    ca_path: str
    server_identity: str
    ssl_context: object
    login_oid: int
    binding_revision: int


@dataclass(frozen=True)
class _ConnectAttempt:
    operation: str
    attempt: str
    resource: str
    process: tuple


@dataclass(frozen=True)
class _SessionState:
    installation: str
    database: str
    server_ip: str
    ip_san: str
    login_oid: int
    binding_revision: int
    tls: bool
    hba: bool
    idle: bool


def _installed_inputs():
    # One fixed owner transport/custodian composition. No candidate can select it.
    # X1/X2/X7 must independently admit actual process/clock/custody originals.
    raise SecretRefusal('required_binding_gap')


def _inputs():
    try: return _installed_inputs()
    except SecretRefusal: raise
    except Exception: raise SecretRefusal('resolver_unavailable') from None


def _require(condition, reason):
    if not condition: raise SecretRefusal(reason)


def _atom(value):
    return type(value) is str and 0 < len(value.encode('utf-8')) <= 256


def _uuid(value):
    try: return type(value) is str and str(UUID(value)) == value
    except (ValueError, AttributeError): return False


def _deadline(value):
    _require(type(value) in (float, int) and math.isfinite(value), 'invalid_deadline')
    _require(value > asyncio.get_running_loop().time(), 'deadline_expired')
    return value


async def _bounded(awaitable, deadline):
    try:
        _deadline(deadline)
    except SecretRefusal:
        if hasattr(awaitable, 'close'): awaitable.close()
        raise
    try:
        with private_driver_logs():
            async with asyncio.timeout_at(deadline):
                result = await awaitable
        _deadline(deadline)  # a supplier may swallow cancellation; late success is never current
        return result
    except TimeoutError:
        raise SecretRefusal('deadline_expired') from None
    except SecretRefusal:
        raise
    except Exception:
        raise SecretRefusal('resolver_unavailable') from None


def _grant(value, reference, purpose, installation, mode, deadline):
    _require(type(value) is _Grant, 'authority_changed')
    _require(all(_atom(getattr(value, x)) for x in
        ('reference','revision','installation','purpose','mode','epoch','original')),
        'authority_changed')
    _require(type(value.process) is tuple and len(value.process) == 6
        and all(_atom(x) for x in value.process)
        and type(value.generation) is int and 0 < value.generation < 2**63,
        'authority_changed')
    _require((value.reference,value.purpose,value.installation,value.mode) ==
             (reference,purpose,installation,mode), 'authority_changed')
    _deadline(value.expires)
    return value


def _handle_port():
    records = WeakKeyDictionary()

    class SecretHandle:
        __slots__ = ('__weakref__',)
        def __new__(cls): raise TypeError('Only the fixed resolver issues a handle')
        def __repr__(self): return '<SecretHandle private>'
        def __reduce_ex__(self, protocol): raise TypeError('Secret handles cannot be copied or serialized')
        @property
        def reference(self): return checked(self)[1].reference
        @property
        def revision(self): return checked(self)[1].revision
        def close(self): records.pop(self, None)

    def checked(handle):
        _require(type(handle) is SecretHandle and handle in records, 'invalid_handle')
        return records[handle]

    def seal(inputs, grant, deadline):
        handle = object.__new__(SecretHandle)
        records[handle] = [inputs, grant, min(grant.expires,deadline)]
        return handle

    return SecretHandle, checked, seal


SecretHandle, _handle, _seal = _handle_port()
del _handle_port


class SecretResolver:
    """No constructor arguments or public material export/binding operation."""
    async def resolve(self, reference_uuid, purpose, installation_uuid, consumer_mode, absolute_deadline):
        # Missing fixed authority precedes any candidate lookup, just like R01.
        inputs = _inputs()
        deadline = _deadline(absolute_deadline)
        _require(_uuid(reference_uuid) and _uuid(installation_uuid), 'invalid_reference')
        from .contracts import load_contracts
        views = load_contracts().operations['secret_handles_by_mode']
        allowed = views.get(consumer_mode, ()) if type(consumer_mode) is str else ()
        # Hosted selection itself remains independently guarded by existing E0.
        allowed = (*allowed, 'provider') if consumer_mode == 'api' else allowed
        if consumer_mode == 'enrollment-owner-client': allowed = ('installer_enrollment',)
        _require(type(purpose) is str and purpose in allowed, 'purpose_not_in_view')
        observed = await _bounded(inputs.observe(reference_uuid,purpose,installation_uuid,
                                                consumer_mode,deadline),deadline)
        grant = _grant(observed,reference_uuid,purpose,installation_uuid,consumer_mode,deadline)
        # Actual peer/current-authority verification is the protected supplier's
        # obligation. An _Grant or equal fixture receipt does not implement it.
        return _seal(inputs,grant,deadline)

    async def check(self, handle, absolute_deadline):
        await self._current(handle,absolute_deadline)

    async def _current(self, handle, absolute_deadline):
        record = _handle(handle)
        inputs, original, expiry = record
        deadline = min(_deadline(absolute_deadline), expiry)
        _deadline(deadline)
        _require(_inputs() is inputs, 'authority_changed')
        observed = await _bounded(inputs.observe(original.reference,original.purpose,
            original.installation,original.mode,deadline),deadline)
        current = _grant(observed,original.reference,original.purpose,
                         original.installation,original.mode,deadline)
        _require(all(getattr(current,k) == getattr(original,k) for k in
            ('reference','revision','installation','purpose','mode','process','epoch',
             'generation','original')), 'authority_changed')
        record[2] = min(expiry,current.expires)
        return inputs, original, min(deadline,record[2])

    @asynccontextmanager
    async def _material(self, handle, deadline):
        inputs, grant, bound = await self._current(handle,deadline)
        material = await _bounded(inputs.read(grant,bound),bound)
        try:
            _require(type(material) is str and 0 < len(material.encode()) <= 65536,
                     'resolver_unavailable')
            await self.check(handle,bound)  # revision may change during lookup
            yield material
            await self.check(handle,bound)  # no stale success publication
        finally:
            material = None  # scoped reference release, not guaranteed erasure

    async def carrier(self, handle, absolute_deadline):
        inputs, grant, bound = await self._current(handle,absolute_deadline)
        _require((grant.purpose,grant.mode) == ('bootstrap','core-init'), 'purpose_not_in_view')
        state = await _bounded(inputs.reserve_carrier(grant,bound),bound)
        carrier = _Carrier(inputs,grant,state,min(absolute_deadline,_handle(handle)[2]),handle)
        # Reserve is material-effect-free. An invalid original is not permission
        # to create material or to dispatch cleanup against an unadmitted target.
        carrier._valid(state,present=False)
        try:
            async with self._material(handle,bound) as material:
                await _bounded(inputs.create_carrier(state,material,bound),bound)
            await carrier.check()
            return carrier
        except BaseException:
            # The original reserved identity survives partial-create/cancel.
            await carrier.close()
            raise


class _Carrier:
    path = '/run/cortex/bootstrap/initdb-password'
    def __init__(self, inputs, grant, state, deadline, handle):
        self._inputs,self._grant,self._state,self._deadline = inputs,grant,state,deadline
        self._closed = False
        self._handle = handle
    def __repr__(self): return '<PrivateCarrier original custody descriptor>'
    @property
    def descriptor(self):
        s,g = self._state,self._grant
        # Original reservation DATA only; this is never a live restore witness.
        return dict(original=g.original,reference=g.reference,revision=g.revision,
            installation=g.installation,purpose=g.purpose,mode=g.mode,epoch=g.epoch,
            generation=g.generation,process=g.process,expires=self._deadline,
            resource=s.resource,identity=s.identity,uid=s.uid,file_mode=s.mode,
            directory_mode=s.directory_mode,links=s.links,private=s.private,
            regular=s.regular,reserved_present=s.present,phase=s.phase)
    def _valid(self, state, *, present):
        original = self._state
        _require(type(state) is _CarrierState and type(original) is _CarrierState,
                 'carrier_mismatch')
        _require(_atom(state.resource) and type(state.identity) is tuple
            and len(state.identity) == 3 and all(_atom(x) for x in state.identity)
            and state.resource == original.resource and state.identity == original.identity
            and state.process == self._grant.process and state.uid == self._grant.process[2]
            and type(state.mode) is int and state.mode == 0o600
            and type(state.directory_mode) is int and state.directory_mode == 0o700
            and type(state.links) is int and state.links == 1
            and state.private is True and state.regular is True
            and state.present is present and type(state.consumers) is int
            and state.consumers >= 0 and (present or state.consumers == 0)
            and state.phase == original.phase == 'S02', 'carrier_mismatch')
    async def check(self):
        _require(not self._closed, 'carrier_mismatch')
        await SecretResolver().check(self._handle,self._deadline)
        state = await _bounded(self._inputs.observe_carrier(self._state,self._deadline),self._deadline)
        self._valid(state,present=True)
        await SecretResolver().check(self._handle,self._deadline)
    async def close(self):
        if self._closed: return
        try:
            # Cleanup uses the original recorded scope, never fresh resolution.
            await _bounded(self._inputs.close_carrier(self._state,self._deadline),self._deadline)
            state = await _bounded(self._inputs.observe_carrier(self._state,self._deadline),self._deadline)
            self._valid(state,present=False)
            _require(state.consumers == 0,'cleanup_unproved')
        except BaseException:
            raise SecretRefusal('cleanup_unproved') from None
        self._closed = True
