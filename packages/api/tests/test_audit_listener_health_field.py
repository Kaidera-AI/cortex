"""The real /health handler exposes listener readiness without flapping health."""
import ast
import copy
from pathlib import Path
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient


SOURCE = Path(__file__).resolve().parents[1] / 'main.py'


def isolated_health():
    tree = ast.parse(SOURCE.read_text())
    function = copy.deepcopy(next(node for node in tree.body
                                  if isinstance(node, ast.AsyncFunctionDef) and node.name == 'health'))
    function.decorator_list = []

    class Connection:
        async def fetchval(self, query):
            if query == 'SELECT 1':
                return 1
            if "cortex_meta" in query:
                return 'fixture-schema'
            if 'pg_notification_queue_usage' in query:
                return 0
            raise AssertionError(f'unexpected health query: {query}')

    class Pool:
        def acquire(self):
            return self

        async def __aenter__(self):
            return Connection()

        async def __aexit__(self, *_):
            return False

    async def platform_config():
        return {'embedding_provider': 'local', 'rerank_enabled': False}

    namespace = {
        'pool_admin': Pool(),
        'event_backend_uses_postgres': lambda: True,
        'load_cortex_platform_config_cached': platform_config,
        'CORTEX_PLATFORM_DEFAULTS': {'embedding_provider': 'local', 'rerank_enabled': False},
        'LOCAL_SEARCH_PROVIDER': 'local',
        'CORTEX_EVENT_BACKEND': 'postgres',
        'CORTEX_API_VERSION': 'fixture-api',
        'CORTEX_SURFACE_VERSION': 'fixture-surface',
        'RLS_ENFORCED': None,
        'event_listener_ready': False,
    }
    exec(compile(ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[])),
                 str(SOURCE), 'exec'), namespace)
    return namespace


class ListenerHealthField(unittest.TestCase):
    def test_listener_down_then_recovered_is_a_boolean_body_field_only(self):
        namespace = isolated_health()
        app = FastAPI()
        app.get('/health')(namespace['health'])
        client = TestClient(app)
        for ready in (False, True):
            namespace['event_listener_ready'] = ready
            response = client.get('/health')
            self.assertEqual(response.status_code, 200)
            body = response.json()
            self.assertEqual(body['status'], 'healthy')
            self.assertEqual(body['postgres'], 'connected')
            self.assertIs(body.get('event_listener_ready'), ready, body)
            self.assertIs(type(body['event_listener_ready']), bool)


if __name__ == '__main__':
    unittest.main()
