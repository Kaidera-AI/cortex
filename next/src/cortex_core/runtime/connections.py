"""RC3a connection declarations only. No driver, secret, pool or live role binding.

Role/catalogue snapshots are DATA, never PostgreSQL or custody authority. RC3b
must replace the missing owner boundary under a separately accepted grant matrix.
"""
from dataclasses import dataclass

from cortex_core.api.c11a import postgres_core_probe
from .config import AdmissionPlan
from .contracts import load_contracts
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
    if (type(admission) is not AdmissionPlan or admission.authority_status!='UNBOUND'
            or admission.effect_authority is not False or admission.native_qualified is not False
            or admission.lock_sha256!=config['runtime_lock_sha256']):
        raise Refusal('declaration_not_authority')
    operations=load_contracts().operations
    expected=operations['roles']
    if set(config['roles'])!=set(expected):
        raise Refusal('role_catalogue_mismatch')
    if any(not _same(row['policy'],expected[name]) for name,row in config['roles'].items()):
        raise Refusal('role_policy_mismatch')
    if snapshot is not None:
        _check_snapshot(snapshot,lock,config)
    roles=tuple(RoleData(name,row['binding']['value']['sql_name'],
                        row['binding']['value']['installation_id'],_freeze(row['policy']))
                for name,row in config['roles'].items())
    view=tuple(name for name in admission.secret_handles if name in config['roles'])
    return ConnectionInterfaces(config['installation_id'],roles,view)


def _same(left,right):
    """Exact builtin data equality; caller equality hooks are never authority."""
    if type(left) is not type(right):
        return False
    if type(right) is dict:
        return (all(type(k) is str for k in left) and set(left)==set(right)
                and all(_same(left[k],v) for k,v in right.items()))
    if type(right) in (list,tuple):
        return len(left)==len(right) and all(_same(a,b) for a,b in zip(left,right))
    return type(right) in (str,bool,int,type(None)) and left==right


def _check_snapshot(snapshot,lock,config):
    # This catches contradictory declarations only. A matching caller snapshot
    # still cannot bind a role, grant, credential, connection or service.
    if (type(snapshot) is not dict or any(type(k) is not str for k in snapshot)
            or set(snapshot)!={'installation_id','schema','embedding','roles','grant_receipt'}):
        raise Refusal('observation_shape')
    for field,expected,reason in (
        ('installation_id',config['installation_id'],'installation_mismatch'),
        ('schema',lock['current_schema'],'schema_mismatch'),
        ('embedding',config['embedding']['value'],'model_mismatch'),
        ('grant_receipt',lock['owner_ports']['role_grants']['original_receipt'],'grant_mismatch')):
        if not _same(snapshot[field],expected):
            raise Refusal(reason)
    roles=snapshot['roles']
    if type(roles) is not dict or set(roles)!=set(config['roles']):
        raise Refusal('role_catalogue_mismatch')
    for name,row in roles.items():
        if type(row) is not dict or type(row.get('policy')) is not dict:
            raise Refusal('observation_shape')
        policy=row['policy']
        if (not config['roles'][name]['policy']['lifetime'].startswith('finite-')
                and (policy.get('superuser') is not False or policy.get('bypassrls') is not False)):
            raise Refusal('bypass_role')
        if not _same(row,config['roles'][name]):
            raise Refusal('role_policy_mismatch')
