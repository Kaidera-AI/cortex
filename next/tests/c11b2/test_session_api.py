"""Released session envelope backed by C05 record revisions and C06 events."""
import asyncio
import json
import sys
from pathlib import Path
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core_db_fixture import Fixture, WRITE_A, API, REQUEST, uid
from cortex_core.api.c11a import ConsumerGateway
from cortex_core.api.c11b import C11bRecordPort


class SessionAPI(Fixture):
    def setUp(self):
        super().setUp()
        import os
        import psycopg
        dsn = os.environ['TEST_DATABASE_URL'].replace('postgres@', API + '@')
        def connection():
            db = psycopg.connect(dsn, autocommit=True)
            db.execute(f'SET ROLE "{REQUEST}"')
            return db
        port = C11bRecordPort(connection, UUID(uid(1)), {'3': UUID(uid(3))})
        async def ready(): return True
        async def state(*_): return {'state': 'unavailable'}
        async def grant(*_): return False
        async def health(): return {'component': 'cortex', 'status': 'ok'}
        self.gateway = ConsumerGateway(core_probe=ready, principal_resolver=port.principal,
            permission_recheck=grant, capability_source=state, health=health,
            handlers={'C01-R013': port.ingest_session}, allow_legacy_idempotency=True)

    def call(self, body, *, key=WRITE_A, request_key=None):
        payload = json.dumps(body).encode()
        headers = [(b'authorization', b'Bearer ' + key), (b'x-project', b'3')]
        if request_key is not None:
            headers.append((b'idempotency-key', request_key.encode()))
        scope = {'type': 'http', 'method': 'POST', 'path': '/sessions/ingest',
                 'headers': headers, 'query_string': b''}
        messages = []
        async def receive(): return {'type': 'http.request', 'body': payload, 'more_body': False}
        async def send(value): messages.append(value)
        asyncio.run(self.gateway.app(scope, receive, send))
        status = next(x['status'] for x in messages if x['type'] == 'http.response.start')
        data = json.loads(next(x['body'] for x in messages if x['type'] == 'http.response.body'))
        return status, data

    def test_session_replay_and_replace_one_core_record(self):
        body = {'session_uuid': uid(80), 'agent': '10', 'source_path': '/synthetic/session.jsonl',
                'provider': 'codex', 'messages': [{'role': 'user', 'content': 'one'},
                                                  {'role': 'assistant', 'content': 'two'}]}
        status, first = self.call(body)
        self.assertEqual((status, first['session_id'], first['agent_id'], first['messages_inserted']),
                         (200, uid(80), uid(10), 2))
        self.assertEqual(self.call(body), (200, first))
        self.assertEqual(self.admin.execute("SELECT count(*) FROM core.records WHERE kind='session'").fetchone()[0], 1)
        changed = {**body, 'messages': [{'role': 'system', 'content': 'replacement'}]}
        status, later = self.call(changed)
        self.assertEqual((status, later['session_id'], later['messages_inserted']), (200, uid(80), 1))
        self.assertEqual(self.admin.execute("SELECT current_revision FROM core.records WHERE kind='session'").fetchone()[0], 2)

    def test_session_invalid_role_writer_and_key_conflict(self):
        body = {'session_uuid': uid(81), 'agent': '10', 'source_path': '/synthetic/second.jsonl',
                'provider': 'codex', 'messages': [{'role': 'user', 'content': 'one'}]}
        self.assertEqual(self.call({**body, 'agent': '8'})[0], 403)
        self.assertEqual(self.call({**body, 'messages': [{'role': 'invalid', 'content': 'one'}]})[0], 400)
        self.assertEqual(self.call(body, request_key='session-request')[0], 200)
        self.assertEqual(self.call({**body, 'provider': 'changed'}, request_key='session-request')[0], 409)
