"""RC3a service-factory interfaces: no services, connections or effect authority.

Uses RC2 original-member admission and current C11 contracts. Handler/provider
objects are checked as interface inputs only; none are invoked or retained.
"""
from dataclasses import dataclass
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
        # Reuse the reviewed RC1/C11 composer with closed local unbound ports.
        return await EnrollmentHealth(_core_unbound,_enrollment_unbound,
            UUID(self.installation_id),timeout_seconds=.2)()


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
    load_routes()
    connections=describe_connections(admission,lock,config,snapshot=snapshot)
    return FactoryInterfaces(config['installation_id'],connections,
                             tuple(binding.name for binding in handlers))
