"""Plain OpenAPI schemas attached to the actual fixed Core gateway registry.

This exports source bindings, not evidence that SQL, an installation or a
release identity is qualified. The host must explicitly supply that identity.
Unbound inherited routes document refusals and never advertise success.
"""
import copy
import hashlib
import re

from cortex_core.release.codec import canonical_bytes
from .c11a import _compile_path, _json_bytes


class ContractRefusal(ValueError):
    code = 'capability_unavailable'
    capability = 'openapi'

    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def _object(properties, required=(), *, extra=False):
    return dict(type='object', properties=properties, required=list(required),
                additionalProperties=extra)


STRING = dict(type='string')
UUID = dict(type='string', format='uuid')
INTEGER = dict(type='integer')
BOOLEAN = dict(type='boolean')
RECEIPT = _object(dict(record_id=UUID, revision=dict(type='integer', minimum=1),
    event_id=UUID, payload_sha256=dict(type='string', pattern='^[0-9a-f]{64}$')),
    ('record_id','revision','event_id','payload_sha256'))
SCHEMAS = {
    'Error': _object(dict(error=_object(dict(code=STRING, retryable=BOOLEAN,
        capability=STRING, reason=STRING, message=STRING, state=STRING),
        ('code','retryable'))), ('error',)),
    'Health': _object(dict(component=dict(type='string', const='cortex'),
        status=dict(type='string', enum=['ok','unavailable']), readiness=STRING,
        installation_id=UUID, reason=STRING), ('component','status')),
    'MemoryInput': _object(dict(section=STRING, content=STRING, category=STRING,
        source=dict(type=['string','null'], maxLength=512)), ('section','content')),
    'MemoryWrite': _object(dict(id=UUID, action=STRING, status=STRING,
        created=BOOLEAN, updated=BOOLEAN, embedded=dict(type='boolean', const=False),
        receipt=RECEIPT), ('id','action','status','created','updated','embedded','receipt')),
    'Record': _object(dict(id=UUID, kind=STRING, revision=dict(type='integer', minimum=1),
        payload_sha256=dict(type='string', pattern='^[0-9a-f]{64}$'),
        section=STRING, content=STRING, category=STRING, source=STRING),
        ('id','kind','revision','payload_sha256')),
    'SessionInput': _object(dict(session_uuid=UUID, agent=STRING, task=dict(type=['string','null']),
        source_path=STRING, provider=STRING, cwd=dict(type=['string','null']),
        git_branch=dict(type=['string','null']), source_kind=dict(type=['string','null']),
        metadata=dict(type=['object','null']), messages=dict(type='array', maxItems=10000,
        items=_object(dict(role=dict(type='string', enum=['user','assistant','human','agent','system']),
            content=dict(type='string', maxLength=65536), ts=dict(type=['string','null']),
            metadata=dict(type=['object','null'])), ('role','content')))),
        ('session_uuid','agent','source_path','provider')),
    'SessionWrite': _object(dict(session_id=UUID, agent_id=UUID,
        messages_inserted=dict(type='integer', minimum=0), receipt=RECEIPT),
        ('session_id','agent_id','messages_inserted','receipt')),
    'SessionIds': _object(dict(project=STRING, ids=dict(type='array', items=UUID)), ('project','ids')),
    'OpenAPI': _object(dict(openapi=dict(type='string', const='3.1.0'),
        info=dict(type='object'), paths=dict(type='object'), components=dict(type='object'),
        servers=dict(type='array', items=dict(type='object')),
        **{'x-cortex-binding':dict(type='object'), 'x-cortex-contract-sha256':STRING}),
        ('openapi','info','paths','components','servers','x-cortex-binding','x-cortex-contract-sha256')),
}
OPENAPI_ROW = dict(id='CORE-OPENAPI', method='GET', path='/openapi.json',
    effect='read', capability=None, disposition='add', observed=False, openkai_production=False)
FIXED_HANDLERS = frozenset({'C01-R102','C01-R013','C01-R121'})


def _response(schema, description, *, links=None):
    value = dict(description=description,
        content={'application/json':dict(schema={'$ref':'#/components/schemas/'+schema})})
    if links:
        value['links'] = links
    return value


def _parameter(name, location, *, required=True, schema=None):
    return dict(name=name, **{'in':location}, required=required, schema=schema or STRING)


def _operation(row, *, request_seconds):
    rid = row['id']
    public = rid == 'C01-R002' or row['effect'] == 'retired_sql'
    protected = [dict(BearerAuth=[], ProjectHeader=[])]
    if rid == 'C01-R121':
        protected.append(dict(BearerAuth=[], ProjectQuery=[]))
    operation = dict(operationId=rid, security=[] if public else protected,
        parameters=[_parameter(name,'path',schema=UUID if rid=='C11-D1' else STRING)
            for name in re.findall(r'{([^{}]+)}',row['path'])],
        responses={}, **{'x-cortex-effect':row['effect'],
            'x-cortex-binding-state':'unbound', 'x-cortex-body-limit-bytes':1048576,
            'x-cortex-default-deadline-seconds':request_seconds})
    responses = operation['responses']
    for status in ('400','503'):
        responses[status] = _response('Error','Typed admission or Core refusal')
    if not public:
        responses['401'] = _response('Error','Credential required')
        if rid != 'C11-D1':
            responses['403'] = _response('Error','Credential or configured project forbidden')
    if row['effect'] == 'retired_sql':
        operation['x-cortex-binding-state'] = 'retired'
        responses['410'] = _response('Error','Retired route; no SQL operation')
    success = {'C01-R002':'Health', 'C01-R102':'MemoryWrite',
        'C01-R013':'SessionWrite', 'C01-R121':'SessionIds',
        'C11-D1':'Record', 'CORE-OPENAPI':'OpenAPI'}
    if rid in success:
        operation['x-cortex-binding-state'] = 'bound'
        responses['200'] = _response(success[rid],'Actual bound handler response')
    if rid == 'C01-R002':
        responses['503'] = _response('Health','Core unavailable')
    if rid == 'C11-D1':
        responses['404'] = _response('Error','Absent or private record')
        responses['410'] = _response('Error','Record is a tombstone')
    if rid in {'C01-R102','C01-R013'}:
        responses['409'] = _response('Error','Committed request intent conflicts')
        operation['parameters'].append(_parameter('Idempotency-Key','header'))
        schema = 'MemoryInput' if rid=='C01-R102' else 'SessionInput'
        operation['requestBody'] = dict(required=True,
            content={'application/json':dict(schema={'$ref':'#/components/schemas/'+schema})})
        expression = '$response.body#/id' if rid=='C01-R102' else '$response.body#/receipt/record_id'
        responses['200']['links'] = dict(read=dict(operationId='C11-D1', parameters=dict(id=expression)))
        if rid == 'C01-R102':
            operation['parameters'].append(_parameter('X-Agent-Name','header'))
        else:
            operation['x-cortex-body-limit-bytes'] = 8*1048576
    if rid == 'C01-R121':
        operation['parameters'] += [_parameter('X-Project','header',required=False),
            _parameter('project','query',required=False)]
        operation['description'] = 'One matching configured project selector: header or query; duplicates/conflicts refuse.'
    return operation


class BoundRegistry:
    """Enrich the dispatch rows in place; retain one actual gateway registry."""

    def __init__(self, gateway, *, listener, request_seconds):
        if (set(gateway.handlers) != FIXED_HANDLERS or gateway.record_reader is None
                or any(row['id']=='CORE-OPENAPI' for row,pattern in gateway.routes)):
            raise ContractRefusal('contract_bindings_incomplete')
        self.gateway = gateway
        self.listener = listener
        self.schemas = copy.deepcopy(SCHEMAS)
        self.identity = self.document = self.raw = None
        # These are the exact rows and callable map the existing dispatcher uses.
        gateway.routes.append((dict(OPENAPI_ROW),_compile_path(OPENAPI_ROW['path'])))
        gateway.handlers['CORE-OPENAPI'] = self._serve
        for row,pattern in gateway.routes:
            row['contract'] = _operation(row,request_seconds=request_seconds)
        from jsonschema import Draft202012Validator
        for schema in self.schemas.values():
            Draft202012Validator.check_schema(schema)
        self._validate()
        self.snapshot = self._snapshot()

    def _validate(self):
        # This is registry admission, not a second OpenAPI parser/validator.
        # JSON Schema syntax is checked by the existing stock implementation.
        def references(value):
            if isinstance(value,dict):
                for key,child in value.items():
                    if key=='$ref' and (not isinstance(child,str) or
                            not child.startswith('#/components/schemas/') or
                            child.removeprefix('#/components/schemas/') not in self.schemas):
                        raise ContractRefusal('contract_schemas_incomplete')
                    references(child)
            elif isinstance(value,list):
                for child in value:references(child)
        seen=set()
        for row,pattern in self.gateway.routes:
            key=(row['method'],row['path']);operation=row['contract']
            responses=operation.get('responses',{})
            if (key in seen or operation.get('operationId')!=row['id'] or
                    not {'400','503'}<=responses.keys() or
                    not operation.get('security') and row['id']!='C01-R002'
                    and row['effect']!='retired_sql' or
                    row['id'] in {'C01-R102','C01-R013'} and not operation.get('requestBody')):
                raise ContractRefusal('contract_schemas_incomplete')
            seen.add(key)
            for response in responses.values():
                schema=response.get('content',{}).get('application/json',{}).get('schema')
                if not isinstance(schema,dict) or not schema:
                    raise ContractRefusal('contract_schemas_incomplete')
            references(operation)
        references(self.schemas)

    def _snapshot(self):
        rows = [dict(row=row, pattern=pattern.pattern, flags=pattern.flags)
                for row,pattern in self.gateway.routes]
        sources = (id(self.gateway.health), id(self.gateway.record_reader),
            id(self.gateway.core_probe), id(self.gateway.principal_resolver),
            id(self.gateway.permission_recheck))
        options=(self.gateway.allow_legacy_idempotency,self.gateway.parse_search_body,
            id(self.gateway.search_admission),id(self.gateway.capability_source))
        return (canonical_bytes(dict(rows=rows, schemas=self.schemas)), options,
            tuple(sorted((key,id(callback)) for key,callback in self.gateway.handlers.items())), sources)

    def check(self):
        try:
            same = self._snapshot() == self.snapshot
        except (ValueError,TypeError,RecursionError):
            same = False
        if not same:
            raise ContractRefusal('contract_registry_changed')
        if self.raw is not None and _json_bytes(self.document)!=self.raw:
            raise ContractRefusal('contract_registry_changed')

    async def _serve(self, principal, scope, request_key):
        self.check()
        if self.raw is None:
            raise ContractRefusal('contract_identity_unbound')
        return self.document


def export_bound_openapi(registry, *, source_commit, version, profile, listener):
    """Return the same bytes as authenticated GET/openapi.json after owner binding.

    Identity is an explicit host composition input, never read from HTTP, Git,
    environment or a second configuration file. Supplying it is not provenance
    verification. The caller must retain its actual source/release receipt.
    """
    if type(registry) is not BoundRegistry:
        raise ContractRefusal('contract_bindings_incomplete')
    registry.check()
    if (type(source_commit) is not str or not re.fullmatch('[0-9a-f]{40}',source_commit)
            or source_commit=='0'*40 or type(version) is not str
            or not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+',version)
            or profile!='A' or type(listener) is not str or listener!=registry.listener):
        raise ContractRefusal('contract_identity_invalid')
    identity = dict(source_commit=source_commit,version=version,profile=profile,listener=listener)
    if registry.identity is not None:
        if identity != registry.identity:
            raise ContractRefusal('contract_identity_changed')
        return registry.raw
    paths = {}
    for row,pattern in registry.gateway.routes:
        item = paths.setdefault(row['path'],{})
        if row['method'].lower() in item:
            raise ContractRefusal('contract_bindings_incomplete')
        item[row['method'].lower()] = copy.deepcopy(row['contract'])
    document = dict(openapi='3.1.0',info=dict(title='Cortex bound Core API',version=version),
        servers=[dict(url=listener)],paths=paths,components=dict(schemas=copy.deepcopy(registry.schemas),
            securitySchemes=dict(BearerAuth=dict(type='http',scheme='bearer'),
                ProjectHeader=dict(type='apiKey',name='X-Project',**{'in':'header'}),
                ProjectQuery=dict(type='apiKey',name='project',**{'in':'query'}))),
        **{'x-cortex-binding':identity,
            'x-cortex-duplicate-authority-headers':'refused before dict conversion'})
    document['x-cortex-contract-sha256'] = hashlib.sha256(canonical_bytes(document)).hexdigest()
    raw = _json_bytes(document)
    registry.identity,registry.document,registry.raw = identity,document,raw
    return raw
