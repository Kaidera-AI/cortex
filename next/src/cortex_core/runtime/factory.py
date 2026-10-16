"""RC3a service-factory interfaces: no services, connections or effect authority.

Uses RC2 original-member admission and current C11 contracts. Handler/provider
objects are checked as interface inputs only; none are invoked or retained.
"""
from dataclasses import dataclass
import asyncio
import importlib
import math
from uuid import UUID

from cortex_core.api.c11a import EnrollmentHealth, load_routes
from .config import RUNTIME_LOCK_MEMBER, admit_config
from .contracts import MAX_CONFIG_BYTES, MAX_LOCK_BYTES, decode
from .connections import ConnectionInterfaces, GRANTS_UNIT, describe_connections
from .signed_member import Refusal, read_member, request_member


@dataclass(frozen=True)
class HandlerBinding:
    name: str
    route_id: str | None
    callback: object


async def _core_unbound():
    return {'core_available':False,'state':'core_unavailable'}


async def _enrollment_unbound(_installation):
    return 'unavailable'


@dataclass(frozen=True)
class RouteInterface:
    """Exact existing contract DATA; a route row never grants effect authority."""
    metadata: tuple
    binding_gap: str
    authority_status: str = 'UNBOUND'

    @property
    def route_id(self): return dict(self.metadata)['id']

    @property
    def openkai_production(self): return dict(self.metadata)['openkai_production']


# Accepted X5 v2 dispositions are gap DATA, never an executable route registry.
_ROUTE_GAPS = {
    'X6_SEPARATE_GUARD': 'C01-R001'.split(),
    'NAMED_JOIN_REQUIRED': 'C01-R002 C01-R006 C01-R013 C01-R021 C01-R022 C01-R023 C01-R024 C01-R025 C01-R030 C01-R034 C01-R037 C01-R102 C01-R121 C11-D1'.split(),
    'READ_POSITIVE_GAP': 'C01-R003 C01-R004 C01-R005 C01-R017 C01-R018 C01-R020 C01-R027 C01-R042 C01-R043 C01-R047 C01-R064 C01-R065 C01-R081 C01-R082 C01-R088 C01-R089 C01-R091 C01-R092 C01-R093 C01-R096 C01-R099 C01-R100 C01-R101 C01-R105 C01-R106 C01-R107 C01-R111 C01-R115 C01-R116 C01-R118 C01-R119 C01-R120 C01-R122 C01-R123'.split(),
    'WRITE_POSITIVE_GAP': 'C01-R008 C01-R010 C01-R011 C01-R012 C01-R015 C01-R016 C01-R026 C01-R035 C01-R036 C01-R044 C01-R045 C01-R046 C01-R048 C01-R052 C01-R055 C01-R056 C01-R058 C01-R078 C01-R079 C01-R097 C01-R098 C01-R103'.split(),
    'OWNER_TRANSITION_GAP': 'C01-R019 C01-R077 C01-R080 C01-R087 C01-R108 C01-R109 C01-R117 C01-R124'.split(),
    'MEDIA_REFUSAL_PROPOSED': 'C01-R028 C01-R029'.split(),
    'CONTRACT_UNAVAILABLE': 'C01-R072 C01-R073 C01-R074'.split(),
    'STREAM_CONTRACT_GAP': 'C01-R075 C01-R076'.split(),
    'RETIRED_410': 'C01-R125 C01-R126'.split(),
}

def route_inventory():
    """Preserve all88, including routes without one of RC3a's five labels."""
    gaps = {route: category for category, routes in _ROUTE_GAPS.items() for route in routes}
    rows = load_routes()
    if set(gaps) != {row['id'] for row in rows}:
        raise Refusal('handler_route_mismatch')
    return tuple(RouteInterface(tuple(row.items()), gaps[row['id']]) for row in rows)


def _enrollment_reader(readiness, resolve_scope):
    """Join X12's sole existing reader. Missing X12 stays unavailable until merged.

    No enrollment SQL, parser, receipt verifier or credential path is duplicated.
    Reconstruct per observation so an old reader cannot cache an owner delivery.
    """
    async def state(installation):
        try:
            module = importlib.import_module('cortex_core.runtime.enrollment')
        except ModuleNotFoundError as error:
            if error.name != 'cortex_core.runtime.enrollment': raise
            return 'unavailable'
        try:
            return await module.enrollment_state_reader(readiness, resolve_scope)(installation)
        except Refusal:
            return 'unavailable'
    return state


# Pending/cancellation-swallowing health work remains owned, never a late result.
# S3/X6 must include this set in exact shutdown ownership before installed use.
_health_work = set()


async def _installed_health(core_health, enrollment_state, installation, *, deadline):
    """Reuse C11's composer within ONE original E and fence supervisor loss."""
    unavailable = {'component':'cortex','status':'unavailable','readiness':'unavailable'}
    loop = asyncio.get_running_loop()
    if (type(deadline) not in (int, float) or not math.isfinite(deadline)
            or deadline <= loop.time()):
        return unavailable
    async def current_core():
        core = await core_health()
        if (loop.time() >= deadline or type(core) is not dict
                or core.get('state') == 'supervisor_down'):
            return {'core_available':False,'state':'core_unavailable'}
        return core
    async def current_enrollment(installation_id):
        if loop.time() >= deadline: return 'unavailable'
        value = await enrollment_state(installation_id)
        # Enrollment waited: re-observe the existing Core/supervisor port under
        # the same E before accepting readiness. A prior roundtrip is not current.
        core = await current_core()
        if (loop.time() >= deadline or type(core) is not dict
                or core.get('state') != 'healthy' or core.get('core_available') is not True):
            return 'unavailable'
        return value
    composer = EnrollmentHealth(current_core, current_enrollment, installation,
        timeout_seconds=min(10, deadline-loop.time()))
    task = loop.create_task(composer())
    _health_work.add(task)
    def finished(done):
        _health_work.discard(done)
        if not done.cancelled(): done.exception()
    task.add_done_callback(finished)
    try:
        done, _ = await asyncio.wait({task}, timeout=max(0, deadline-loop.time()))
        if not done or loop.time() >= deadline:
            task.cancel()
            return unavailable
        return task.result()
    except asyncio.CancelledError:
        task.cancel()
        raise


@dataclass(frozen=True)
class FactoryInterfaces:
    installation_id: str
    connections: ConnectionInterfaces
    handler_names: tuple[str,...]
    dependencies: tuple[str,...] = (GRANTS_UNIT,'PG-B runtime-ports',
                                  'PG-C reviewed runtime contract revision',
                                  'PG-D C04 connection adapters')
    enrollment_states: tuple[str,...] = ('enrollment_required','ready','unavailable')

    @property
    def authority_status(self):
        return 'UNBOUND'

    @property
    def effect_authority(self):
        return False

    def require_services(self):
        raise Refusal('required_binding_gap',unit=GRANTS_UNIT)

    async def health(self):
        # Default descriptor still invokes neither installed nor caller ports.
        return await _installed_health(_core_unbound,_enrollment_unbound,
            UUID(self.installation_id), deadline=asyncio.get_running_loop().time()+.2)


def describe_factory(member, config_raw, *, aggregate_id, observed_target,
                     handlers, snapshot=None, provider=None, resolver=None):
    """Return DATA after RC2 admission; no callable input or connection is used."""
    if member is None:
        member=request_member(aggregate_id,RUNTIME_LOCK_MEMBER)
    admission=admit_config(member,config_raw,aggregate_id=aggregate_id,
                          observed_target=observed_target,mode='api')
    lock=decode(read_member(member,aggregate_id=aggregate_id,
                           member_name=RUNTIME_LOCK_MEMBER),limit=MAX_LOCK_BYTES)
    config=decode(config_raw,limit=MAX_CONFIG_BYTES)
    routes=load_routes()
    _check_handlers(handlers,routes)
    connections=describe_connections(admission,lock,config,snapshot=snapshot)
    return FactoryInterfaces(config['installation_id'],connections,
                             tuple(binding.name for binding in handlers))


def _check_handlers(handlers,routes):
    expected={'write':'C01-R102','read':'C11-D1','search':'C01-R022',
              'status':'C01-R030','enrollment':None}
    if type(handlers) is not tuple or any(type(x) is not HandlerBinding for x in handlers):
        raise Refusal('missing_handler')
    names=[x.name for x in handlers]
    route_ids=[x.route_id for x in handlers if x.route_id is not None]
    if (any(type(n) is not str for n in names+route_ids)
            or len(set(names))!=len(names) or len(set(route_ids))!=len(route_ids)):
        raise Refusal('handler_collision')
    if set(names)!=set(expected) or any(not callable(x.callback) for x in handlers):
        raise Refusal('missing_handler')
    current={row['id'] for row in routes}
    if any(x.route_id!=expected[x.name] or x.route_id is not None and x.route_id not in current
           for x in handlers):
        raise Refusal('handler_route_mismatch')
