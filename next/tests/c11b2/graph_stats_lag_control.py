"""Review control: index becomes lagging after the gateway readiness check."""

import asyncio
import json
import sys
import types

# This control does not open PostgreSQL. Supply import-only psycopg shapes so
# the real gateway and read-port methods can run in the host Python environment.
psycopg = types.ModuleType('psycopg')
psycopg.Error = type('Error', (Exception,), {})
psycopg.errors = types.SimpleNamespace(UniqueViolation=type('UniqueViolation', (Exception,), {}))
pq = types.ModuleType('psycopg.pq')
pq.TransactionStatus = types.SimpleNamespace(IDLE=0)
types_module = types.ModuleType('psycopg.types')
json_module = types.ModuleType('psycopg.types.json')
json_module.Jsonb = object
sys.modules.update({'psycopg': psycopg, 'psycopg.pq': pq,
                    'psycopg.types': types_module, 'psycopg.types.json': json_module})

from cortex_core.api.c11a import ConsumerGateway
from cortex_core.api.c11b2 import C11b2ReadPort


class LaggingGraph:
    async def stats(self, _principal):
        return {'entity_count': 0, 'freshness': {'state': 'lagging', 'pending_records': 1}}

    async def repository_stats(self, _principal):
        return {'total_nodes': 0, 'freshness': {'state': 'lagging', 'pending_records': 1}}


class ReadPort(C11b2ReadPort):
    def __init__(self):
        pass

    def _ports(self, _principal):
        return None, LaggingGraph()

    async def _current(self, principal):
        return principal


async def call(path):
    port = ReadPort()

    async def ready():
        return True

    async def principal(_scope):
        return {'principal_id': 'synthetic-principal', 'project_id': 'synthetic-project'}

    async def capability(_name, _principal):
        return {'state': 'ready', 'model_id': 'm', 'generation': 'g',
                'freshness': {'complete': True, 'applied_cursor': '1'}}

    async def recheck(_principal, _name):
        return True

    async def health():
        return {'component': 'cortex', 'status': 'ok'}

    app = ConsumerGateway(core_probe=ready, principal_resolver=principal,
                          permission_recheck=recheck, capability_source=capability,
                          health=health,
                          handlers={'C01-R024': port.graph_stats,
                                    'C01-R034': port.graph_repository_stats})
    sent = []

    async def receive():
        return {'type': 'http.request', 'body': b'', 'more_body': False}

    async def send(value):
        sent.append(value)

    await app.app({'type': 'http', 'method': 'GET', 'path': path,
                   'headers': [(b'authorization', b'Bearer synthetic')],
                   'query_string': b''}, receive, send)
    status = next(x['status'] for x in sent if x['type'] == 'http.response.start')
    body = json.loads(next(x['body'] for x in sent if x['type'] == 'http.response.body'))
    assert status == 503 and body['error']['code'] == 'capability_unavailable', (path, status, body)


async def main():
    failures = []
    for path in ('/cortex-graph/stats', '/graph/stats'):
        try:
            await call(path)
        except AssertionError as error:
            failures.append(str(error))
    assert not failures, failures


asyncio.run(main())
