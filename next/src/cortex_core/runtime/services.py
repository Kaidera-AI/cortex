"""One fixed C11b/Core binding. SQL runs on the bounded main event-loop thread.

The sole Nix validator/package and live role receipts are admission dependencies;
source-only composition tests do not qualify their implementation or policy.
"""
from contextvars import ContextVar
import re
import time
from uuid import UUID

from cortex_core.api.c11a import ConsumerGateway, C05Committed, _packet, _respond
from cortex_core.api.c11b import C11bRecordPort
from cortex_core.api.contract import BoundRegistry, export_bound_openapi
from cortex_core.auth import AuthError
from .catalog import _service
from .resources import CoreResources, remaining_seconds
from .signed_member import Refusal

_REQUEST=ContextVar('cortex_option_a_request',default=None)
_AUTHORITY=frozenset({b'authorization',b'x-project',b'x-project-id',b'x-agent-name',b'idempotency-key'})
_IDENTITY=('principal_id','tenant_id','project_id','permission_generation')


def _committed(value,key):
    if (type(value) is not C05Committed or value.committed is not True or value.request_key!=key
            or type(value.receipt) is not dict
            or set(value.receipt)!={'record_id','revision','event_id','payload_sha256'}):
        raise Refusal('service_commit')
    receipt=value.receipt
    try:
        for name in ('record_id','event_id'):
            raw=receipt[name]
            if type(raw) is not str or str(UUID(raw))!=raw or UUID(raw).int==0:raise ValueError
        if (type(receipt['revision']) is not int or not 0<receipt['revision']<2**63
                or type(receipt['payload_sha256']) is not str
                or re.fullmatch('[0-9a-f]{64}',receipt['payload_sha256']) is None):raise ValueError
        if value.response is not None and (type(value.response) is not dict or 'receipt' in value.response):raise ValueError
    except (ValueError,TypeError,AttributeError):raise Refusal('service_commit') from None
    response={**(value.response or {}),'receipt':receipt}
    return C05Committed(value.request_key,receipt,True,response)


class _Port(C11bRecordPort):
    # Only wrappers change: actual parsers, authorization, SQL and C05/C06 remain
    # in the existing inherited synchronous primitives, never a cancelled worker.
    async def principal(self,scope):return self._principal(scope)
    async def write_memory(self,principal,scope,key):return _committed(self._write_memory(principal,scope,key),key)
    async def ingest_session(self,principal,scope,key):return _committed(self._ingest_session(principal,scope,key),key)
    async def read_record(self,principal,scope,record_id):return self._read_record(principal,scope,record_id)
    async def session_ids(self,principal,scope,key):
        await self._current_read(principal,scope)
        ids=self._session_ids(principal,scope)
        await self._current_read(principal,scope)
        return {'project':scope['_c11b_project_key'],'ids':ids}


class _BoundServices:
    def __init__(self,resources):
        if type(resources) is not CoreResources:raise Refusal('resources_unbound')
        resources._live()
        self._resources=resources;self._started=False
        self.port=_Port(self._connection,UUID(resources._config.installation_id),
            {name:UUID(value) for name,value in resources._config.project_names.items()})
        self.gateway=ConsumerGateway(core_probe=self._probe,principal_resolver=self.port.principal,
            permission_recheck=self._permission,capability_source=self._capability,health=self._health,
            handlers={'C01-R102':self.port.write_memory,'C01-R013':self.port.ingest_session,
                'C01-R121':self.port.session_ids},record_reader=self.port.read_record,
            allow_legacy_idempotency=True)
        host=resources._config.publish['host'];host='['+host+']' if ':' in host else host
        self._contract=BoundRegistry(self.gateway,
            listener=f'http://{host}:{resources._config.publish["port"]}',
            request_seconds=min(10,resources._config.limits['request_seconds']))

    def export_bound_openapi(self,*,source_commit,version,profile,listener):
        if not self._started:raise Refusal('contract_not_started')
        return export_bound_openapi(self._contract,source_commit=source_commit,
            version=version,profile=profile,listener=listener)

    def _current(self):
        current=_REQUEST.get()
        if current is None or current[0] is not self:raise Refusal('resources_unbound')
        remaining_seconds(current[1]);return current

    def _connection(self):
        current=self._current()
        return self._resources.request_connection('api_request',deadline=current[1])

    def start(self):
        if self._started:raise Refusal('service_started')
        try:
            self._contract.check()
            from cortex_core.schema.readiness import check_schema_ready
            with self._resources.transaction('api_request',_read_only=True,_repeatable=False) as connection:
                if check_schema_ready(connection) is not True:raise Refusal('service_startup')
            observation=self._resources.observe_service_catalog()
            try:
                from cortex_ship.install.roles import validate_catalog
            except ImportError:raise Refusal('catalog_validator_unavailable') from None
            try:
                validated=validate_catalog(self._resources._config,observation,phase='serve')
                if type(validated) is not dict:raise ValueError
            except Exception:raise Refusal('catalog_refused') from None
            if observation['service']!={'installation_matches':True,'enrollment_readiness':'ready'}:
                raise Refusal('service_startup')
            with self._resources.transaction('probe',_read_only=True,_repeatable=False) as connection:
                if connection.execute('SELECT 1').fetchone()!=(1,):raise Refusal('service_startup')
            self._resources.check_deadline();self._started=True
        except Refusal as error:
            if error.reason in ('catalog_validator_unavailable','catalog_refused'):raise
            raise Refusal('service_startup') from None
        except Exception:raise Refusal('service_startup') from None

    async def _probe(self):
        if not self._started:return False
        try:
            current=self._current()
            with self._resources.request_connection('probe',deadline=current[1]) as connection:
                return connection.execute('SELECT 1').fetchone()==(1,)
        except Exception:return False

    async def _health(self):
        value={'component':'cortex','status':'unavailable','readiness':'unavailable',
            'installation_id':self._resources._config.installation_id}
        if not await self._probe():return value
        try:
            with self._connection() as connection:
                with connection.transaction():
                    connection.execute('SET TRANSACTION ISOLATION LEVEL READ COMMITTED READ ONLY')
                    state=_service(connection,self._resources._config)
            if state['installation_matches'] is not True:return value
            readiness=state['enrollment_readiness']
            return {**value,'status':'ok' if readiness!='unavailable' else 'unavailable','readiness':readiness}
        except Exception:return value

    async def _permission(self,principal,capability):
        current=self._current()
        fresh=await self.port.principal(current[2])
        if any(fresh[key]!=principal[key] for key in _IDENTITY):raise AuthError('forbidden')
        return True

    async def _capability(self,name,principal):
        # No configured UUID or optional dependency fabricates capability health.
        self._current()
        return {'state':'unavailable'}

    async def __call__(self,scope,receive,send):
        if scope.get('type')!='http':
            return await _respond(send,404,_packet('not_found',retryable=False))
        deadline=time.monotonic()+min(10,self._resources._config.limits['request_seconds'])
        scope={**scope,'_cortex_deadline':deadline};token=_REQUEST.set((self,deadline,scope))
        started=False
        async def owned_send(message):
            nonlocal started
            if message.get('type')=='http.response.start':started=True
            await send(message)
        try:
            seen=set()
            for name,value in scope.get('headers') or []:
                if name.lower() in _AUTHORITY:
                    # ASGI headers must be lowercase; do not let an alternative
                    # spelling bypass either duplicate admission or C11b lookup.
                    if name!=name.lower() or name in seen:
                        return await _respond(owned_send,400,_packet('invalid_input',retryable=False))
                    seen.add(name)
            row=next((row for row,pattern in self.gateway.routes if row['method']==scope.get('method')
                and pattern.fullmatch(scope.get('path') or '')),None)
            if (row is not None and row['effect']=='write' and row['id'] in self.gateway.handlers
                    and not dict(scope.get('headers') or []).get(b'idempotency-key')):
                return await _respond(owned_send,400,_packet('idempotency_key_required',retryable=False))
            await self.gateway.app(scope,receive,owned_send)
        except Refusal:
            if started:raise
            await _respond(owned_send,503,_packet('core_unavailable'))
        finally:_REQUEST.reset(token)


def bind_core_services(resources):
    return _BoundServices(resources)
