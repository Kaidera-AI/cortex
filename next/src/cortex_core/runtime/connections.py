"""RC3a declarations and X3 physical-connect policy; installed ports UNBOUND.

Role/catalogue/session observations are DATA, never PostgreSQL or custody
authority. X3 adds no driver, pool, secret store or live factory selection.
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


class ResolverConnection:
    """X3 source boundary, preserving RC1's async connect protocol.

    No installed factory selects this adapter yet. The one fixed protected X3
    supplier owns driver choice, original TLS/HBA/role policy, actual session
    observations and retirement. No driver imported or caller kwargs accepted.
    X5's later pool owner must invoke check on checkout/use/after C04 exit before
    publication, and retire on failure; C04 human authorization remains separate.
    """
    def __init__(self, reference, purpose, installation, mode, absolute_deadline):
        from weakref import WeakKeyDictionary
        self._request = (reference,purpose,installation,mode,absolute_deadline)
        self._sessions = WeakKeyDictionary()

    async def connect(self, *, connect_timeout_seconds, statement_timeout_ms):
        import asyncio
        import math
        from . import secret_resolver as secrets
        from cortex_core.driver_logging import private_driver_logs
        resolver = secrets.SecretResolver()
        secrets._require(type(connect_timeout_seconds) in (int,float)
            and math.isfinite(connect_timeout_seconds) and 0 < connect_timeout_seconds <= 2
            and type(statement_timeout_ms) is int and 1 <= statement_timeout_ms <= 2000,
            'invalid_deadline')
        handle = await resolver.resolve(*self._request)
        connection = None
        inputs = None
        connecting = False
        attempt = None
        deadline = self._request[-1]
        try:
            inputs,grant,bound = await resolver._current(handle,deadline)
            deadline = min(bound,asyncio.get_running_loop().time()+connect_timeout_seconds)
            try:
                with private_driver_logs(): policy = inputs.sql_policy(grant)
            except Exception: raise secrets.SecretRefusal('resolver_unavailable') from None
            secrets._require(type(policy) is secrets._SQLPolicy and type(policy.driver) is str and policy.driver in ('psycopg','asyncpg'),
                             'invalid_connection_policy')
            from .config import _private_address
            try: _private_address(policy.host)
            except (Refusal,TypeError,ValueError): raise secrets.SecretRefusal('invalid_connection_policy') from None
            secrets._require(policy.server_identity == policy.host and type(policy.port) is int
                and policy.port == 5432 and all(secrets._atom(x) for x in
                (policy.database,policy.user,policy.ca_path)) and policy.ca_path.startswith('/')
                and type(policy.login_oid) is int and policy.login_oid > 0
                and type(policy.binding_revision) is int and policy.binding_revision > 0,
                'invalid_connection_policy')
            async with resolver._material(handle,deadline) as password:
                if policy.driver == 'psycopg':
                    kwargs = dict(conninfo='',host=policy.host,hostaddr=policy.host,port=policy.port,
                        dbname=policy.database,user=policy.user,password=password,
                        sslmode='verify-full',sslrootcert=policy.ca_path,gssencmode='disable',
                        autocommit=True,connect_timeout=max(1,math.ceil(connect_timeout_seconds)),
                        options=f'-c statement_timeout={statement_timeout_ms}')
                else:
                    context = policy.ssl_context
                    _strict_context(context)
                    async def current_password():
                        # The driver may invoke this again; never return cached bytes.
                        async with resolver._material(handle,deadline) as fresh: return fresh
                    kwargs = dict(dsn=None,host=policy.host,port=policy.port,database=policy.database,
                        user=policy.user,password=current_password,ssl=context,
                        timeout=connect_timeout_seconds,
                        server_settings={'statement_timeout':str(statement_timeout_ms)})
                # The protected supplier reserves the exact original owned attempt
                # BEFORE driver dispatch; lost results reconcile this ticket only.
                attempt = await secrets._bounded(inputs.reserve_connect(grant,policy,deadline),deadline)
                secrets._require(type(attempt) is secrets._ConnectAttempt
                    and all(secrets._atom(x) for x in (attempt.operation,attempt.attempt,attempt.resource))
                    and attempt.process == grant.process, 'authority_changed')
                await resolver.check(handle,deadline)
                _current_sql_policy(inputs,grant,policy)
                connecting = True
                connection = await secrets._bounded(inputs.connect(policy,kwargs,deadline,attempt),deadline)
                kwargs = None
                observation = await secrets._bounded(
                    inputs.observe_session(connection,grant,policy,deadline),deadline)
                _session_state(observation,grant,policy)
            _current_sql_policy(inputs,grant,policy)
            # Registry holds no credential; expiry remains the original owner bound,
            # not the shorter connect-only timeout. Current use rechecks the handle.
            self._sessions[connection] = (inputs,handle,policy,self._request[-1])
            return connection
        except BaseException as error:
            handle.close()  # fence callbacks even when retirement remains unknown
            if connection is not None and inputs is not None:
                await _retire_resolved(inputs,connection,self._request[-1])
            elif connecting:
                try:
                    absent = await secrets._bounded(inputs.abort_connect(attempt,self._request[-1]),self._request[-1])
                    secrets._require(absent is True,'cleanup_unproved')
                except BaseException:
                    raise secrets.SecretRefusal('cleanup_unproved') from None
            if isinstance(error,(secrets.SecretRefusal,asyncio.CancelledError)): raise
            raise secrets.SecretRefusal('resolver_unavailable') from None

    async def check(self, connection, *, deadline):
        from . import secret_resolver as secrets
        record = self._sessions.get(connection)
        secrets._require(record is not None,'invalid_handle')
        inputs,handle,policy,original_deadline = record
        try:
            _,grant,bound = await secrets.SecretResolver()._current(handle,min(deadline,original_deadline))
            _current_sql_policy(inputs,grant,policy)
            state = await secrets._bounded(inputs.observe_session(connection,grant,policy,bound),bound)
            _session_state(state,grant,policy)
            await secrets.SecretResolver().check(handle,bound)
            _current_sql_policy(inputs,grant,policy)
        except BaseException:
            await self.retire(connection,deadline=original_deadline)
            raise

    async def retire(self, connection, *, deadline):
        from . import secret_resolver as secrets
        record = self._sessions.get(connection)
        secrets._require(record is not None,'invalid_handle')
        inputs,handle,_,original_deadline = record
        handle.close()
        await _retire_resolved(inputs,connection,min(deadline,original_deadline))
        self._sessions.pop(connection,None)


def _strict_context(context):
    import ssl
    from . import secret_resolver as secrets
    secrets._require(type(context) is ssl.SSLContext and context.check_hostname
        and context.verify_mode == ssl.CERT_REQUIRED
        and context.hostname_checks_common_name is False,'invalid_connection_policy')


def _current_sql_policy(inputs, grant, policy):
    from . import secret_resolver as secrets
    from cortex_core.driver_logging import private_driver_logs
    try:
        with private_driver_logs(): current = inputs.sql_policy(grant)
    except Exception: raise secrets.SecretRefusal('resolver_unavailable') from None
    secrets._require(type(current) is secrets._SQLPolicy and all(
        _same(getattr(current,key),getattr(policy,key)) for key in
        ('driver','host','port','database','user','ca_path','server_identity',
         'login_oid','binding_revision')), 'authority_changed')
    if current.driver == 'asyncpg':
        _strict_context(current.ssl_context)
        _strict_context(policy.ssl_context)


def _session_state(state, grant, policy):
    from . import secret_resolver as secrets
    secrets._require(type(state) is secrets._SessionState
        and (state.installation,state.database,state.server_ip,state.ip_san,
             state.login_oid,state.binding_revision) ==
            (grant.installation,policy.database,policy.host,policy.server_identity,
             policy.login_oid,policy.binding_revision)
        and type(state.login_oid) is int and type(state.binding_revision) is int
        and state.tls is True and state.hba is True and state.idle is True,
        'session_mismatch')


async def _retire_resolved(inputs, connection, deadline):
    from . import secret_resolver as secrets
    try:
        absent = await secrets._bounded(inputs.retire(connection,deadline),deadline)
        secrets._require(absent is True,'cleanup_unproved')
    except BaseException:
        raise secrets.SecretRefusal('cleanup_unproved') from None
