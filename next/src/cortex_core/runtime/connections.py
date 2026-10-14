"""RC3a connection declarations only. No driver, secret, pool or live role binding.

Role/catalogue snapshots are DATA, never PostgreSQL or custody authority. RC3b
must replace the missing owner boundary under a separately accepted grant matrix.
"""
from dataclasses import dataclass

from cortex_core.api.c11a import postgres_core_probe
from .signed_member import Refusal

RC1_PROBE = postgres_core_probe
GRANTS_UNIT = 'PG-A runtime-grants'


def _freeze(value):
    if isinstance(value, dict):
        return tuple((k,_freeze(v)) for k,v in sorted(value.items()))
    if isinstance(value, list):
        return tuple(_freeze(v) for v in value)
    return value


@dataclass(frozen=True)
class RoleData:
    logical_name: str
    sql_name: str
    installation_id: str
    policy: tuple


@dataclass(frozen=True)
class UnboundConnection:
    logical_name: str

    async def connect(self, *, connect_timeout_seconds, statement_timeout_ms):
        # Exact RC1 consumer protocol, deliberately no DSN or driver adaptation.
        raise Refusal('required_binding_gap',unit=GRANTS_UNIT)


@dataclass(frozen=True)
class ConnectionInterfaces:
    installation_id: str
    roles: tuple[RoleData,...]
    process_view: tuple[str,...]

    def for_role(self, name):
        if type(name) is not str or name not in self.process_view:
            raise Refusal('role_not_in_process_view')
        return UnboundConnection(name)


def describe_connections(admission, lock, config, *, snapshot=None):
    """Freeze already-admitted declarations; even matching observations stay UNBOUND."""
    roles=tuple(RoleData(name,row['binding']['value']['sql_name'],
                        row['binding']['value']['installation_id'],_freeze(row['policy']))
                for name,row in config['roles'].items())
    view=tuple(name for name in admission.secret_handles if name in config['roles'])
    return ConnectionInterfaces(config['installation_id'],roles,view)
