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


def route_inventory():
    """Preserve all88, including routes without one of RC3a's five labels."""
    return tuple(RouteInterface(tuple(row.items()),
        'retired_410' if row['disposition'] == 'retire' else 'required_service_join')
        for row in load_routes())


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
        return value if loop.time() < deadline else 'unavailable'
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
