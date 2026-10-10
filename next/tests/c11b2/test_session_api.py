"""Released session envelope backed by C05 record revisions and C06 events."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
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
        port = C11bRecordPort(connection, UUID(uid(1)),
                              {'3': UUID(uid(3)), '4': UUID(uid(4))})
        async def ready(): return True
        async def state(*_): return {'state': 'unavailable'}
        async def grant(*_): return False
        async def health(): return {'component': 'cortex', 'status': 'ok'}
        self.gateway = ConsumerGateway(core_probe=ready, principal_resolver=port.principal,
            permission_recheck=grant, capability_source=state, health=health,
            handlers={'C01-R013': port.ingest_session}, allow_legacy_idempotency=True)

    def call(self, body, *, key=WRITE_A, request_key=None, project='3'):
        payload = json.dumps(body).encode()
        headers = [(b'authorization', b'Bearer ' + key), (b'x-project', project.encode())]
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

    def test_source_path_is_unique_across_authorized_projects(self):
        self.admin.execute("INSERT INTO auth.project_grants VALUES (%s,%s,%s,ARRAY['read','write'])",
                           (uid(2), uid(4), uid(10)))
        body = {'session_uuid': uid(82), 'agent': '10',
                'source_path': '/synthetic/shared.jsonl', 'provider': 'codex', 'messages': []}
        self.assertEqual(self.call(body)[0], 200)
        status, error = self.call({**body, 'session_uuid': uid(83)}, project='4')
        self.assertEqual(status, 409)
        self.assertEqual(error['error']['code'], 'conflict')
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM core.records WHERE kind='session' AND project_id=%s",
            (uid(4),)).fetchone()[0], 0)

    def test_racing_project_claims_commit_only_one_session(self):
        self.admin.execute("INSERT INTO auth.project_grants VALUES (%s,%s,%s,ARRAY['read','write'])",
                           (uid(2), uid(4), uid(10)))
        path = '/synthetic/racing.jsonl'
        def submit(item):
            project, session = item
            return self.call({'session_uuid': uid(session), 'agent': '10',
                              'source_path': path, 'provider': 'codex', 'messages': []},
                             project=project)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(submit, [('3', 87), ('4', 88)]))
        self.assertEqual(sorted(status for status, _ in results), [200, 409])
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM core.records WHERE kind='session'").fetchone()[0], 1)
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM coordination.session_sources").fetchone()[0], 1)

    def test_iso_timestamps_and_bounded_large_batch(self):
        body = {'session_uuid': uid(84), 'agent': '10',
                'source_path': '/synthetic/large.jsonl', 'provider': 'codex',
                'messages': [{'role': 'user', 'content': 'a' * 2048,
                              'ts': '2026-10-10T20:14:00Z'} for _ in range(600)]}
        status, result = self.call(body)
        self.assertEqual(status, 200)
        self.assertEqual(result['messages_inserted'], 600)
        invalid = {**body, 'session_uuid': uid(85),
                   'messages': [{'role': 'user', 'content': 'bad', 'ts': '2026-99-99'}]}
        status, error = self.call(invalid)
        self.assertEqual((status, error['error']['code']), (400, 'invalid_input'))
        oversized = {**body, 'session_uuid': uid(86),
                     'messages': [{'role': 'user', 'content': 'x' * 65536} for _ in range(140)]}
        self.assertEqual(self.call(oversized)[0], 400)

    def test_non_iso_separator_refuses_without_core_or_source_writes(self):
        tables = ('core.records', 'core.record_revisions',
                  'coordination.outbox', 'coordination.session_sources')
        before = tuple(self.admin.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
                       for table in tables)
        body = {'session_uuid': uid(89), 'agent': '10',
                'source_path': '/synthetic/non-iso-separator.jsonl',
                'provider': 'codex',
                'messages': [{'role': 'user', 'content': 'bad',
                              'ts': '2026-10-10Q20:14:00'}]}
        status, error = self.call(body)
        self.assertEqual(status, 400)
        self.assertEqual(error['error']['code'], 'invalid_input')
        after = tuple(self.admin.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
                      for table in tables)
        self.assertEqual(after, before)

    def test_valid_iso_offsets_and_fractions_are_preserved(self):
        for number, timestamp in enumerate(('2026-10-10T20:14:00Z',
                                            '2026-10-10T20:14:00+02:00',
                                            '2026-10-10T20:14:00.123456-03:30'), 90):
            body = {'session_uuid': uid(number), 'agent': '10',
                    'source_path': f'/synthetic/valid-iso-{number}.jsonl',
                    'provider': 'codex',
                    'messages': [{'role': 'user', 'content': 'valid', 'ts': timestamp}]}
            status, result = self.call(body)
            self.assertEqual((status, result['messages_inserted']), (200, 1))
