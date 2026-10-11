"""R010/R011/R012 frozen ingest envelope through real C04/C05/C06 PostgreSQL."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import sys
import unittest
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core_db_fixture import Fixture, WRITE_A, API, REQUEST, uid
from cortex_core.api.c11a import C05Committed, ConsumerGateway
from cortex_core.api.c11b import C11bRecordPort
from cortex_core.auth import AuthError


CASES = (
    ('/knowledge/ingest', 'C01-R010',
     {'content': 'first knowledge', 'source_file': '/synthetic/k.md',
      'category': 'imported', 'section': 'K'}, 'content'),
    ('/lessons/ingest', 'C01-R011',
     {'summary': 'L', 'detail': 'first lesson', 'category': 'imported',
      'agent_name': '10'}, 'detail'),
    ('/decisions/ingest', 'C01-R012',
     {'summary': 'D', 'rationale': 'first decision', 'category': 'imported',
      'agent_name': '10'}, 'rationale'),
)


class IngestAPI(Fixture):
    def setUp(self):
        super().setUp()
        dsn = os.environ['TEST_DATABASE_URL'].replace('postgres@', API + '@')

        def connection():
            db = psycopg.connect(dsn, autocommit=True)
            db.execute(f'SET ROLE "{REQUEST}"')
            return db

        self.connection = connection
        self.base_port = C11bRecordPort(connection, UUID(uid(1)),
                                         {'3': UUID(uid(3)), '4': UUID(uid(4))})
        self.core_ready = True

    def gateway(self, *, bound=True, handlers=None):
        if handlers is None and bound:
            try:
                from cortex_core.api.c11_ingest import C11IngestPort
            except ImportError:
                self.fail('R010/R011/R012 adapter is absent')
            port = C11IngestPort(self.connection, UUID(uid(1)),
                                 {'3': UUID(uid(3)), '4': UUID(uid(4))})
            handlers = {'C01-R010': port.ingest_knowledge,
                        'C01-R011': port.ingest_lesson,
                        'C01-R012': port.ingest_decision}
        async def ready(): return self.core_ready
        async def state(*_): return {'state': 'unavailable'}
        async def grant(*_): return False
        async def health(): return {'component': 'cortex', 'status': 'ok'}
        return ConsumerGateway(core_probe=ready, principal_resolver=self.base_port.principal,
            permission_recheck=grant, capability_source=state, health=health,
            handlers=handlers if handlers is not None else {})

    def call(self, gateway, path, body, *, key=WRITE_A, request_key='ingest-1',
             project='3', raw=False):
        payload = body if isinstance(body, bytes) else json.dumps(body, separators=(',', ':')).encode()
        headers = [(b'x-project', project.encode())]
        if key is not None:
            headers.append((b'authorization', b'Bearer ' + key))
        if request_key is not None:
            headers.append((b'idempotency-key', request_key.encode()))
        scope = {'type': 'http', 'method': 'POST', 'path': path, 'headers': headers,
                 'query_string': b''}
        messages = []
        async def receive(): return {'type': 'http.request', 'body': payload, 'more_body': False}
        async def send(message): messages.append(message)
        asyncio.run(gateway.app(scope, receive, send))
        status = next(x['status'] for x in messages if x['type'] == 'http.response.start')
        data = b''.join(x.get('body', b'') for x in messages if x['type'] == 'http.response.body')
        return (status, data) if raw else (status, json.loads(data))

    def assert_ack(self, ack, status, revision):
        self.assertEqual(set(ack), {'id', 'status', 'created', 'updated', 'embedded', 'receipt'})
        self.assertEqual((ack['status'], ack['created'], ack['updated'], ack['embedded']),
                         (status, status == 'created', status == 'updated', False))
        self.assertEqual(UUID(ack['id']).__str__(), ack['id'])
        self.assertEqual(set(ack['receipt']), {'record_id', 'revision', 'event_id'})
        self.assertEqual((ack['receipt']['record_id'], ack['receipt']['revision']),
                         (ack['id'], revision))
        if status == 'unchanged':
            self.assertIsNone(ack['receipt']['event_id'])
        else:
            self.assertEqual(str(UUID(ack['receipt']['event_id'])), ack['receipt']['event_id'])

    def counts(self, record_id):
        return (self.admin.execute('SELECT count(*) FROM core.record_revisions WHERE record_id=%s',
                                   (record_id,)).fetchone()[0],
                self.admin.execute('SELECT count(*) FROM coordination.outbox WHERE aggregate_id=%s',
                                   (record_id,)).fetchone()[0])

    def test_all_three_routes_created_unchanged_updated_exact_envelope(self):
        gateway = self.gateway()
        for path, route_id, body, changed_field in CASES:
            with self.subTest(route=route_id):
                first_status, first = self.call(gateway, path, body, request_key=route_id + '-create')
                self.assertEqual(first_status, 200)
                self.assert_ack(first, 'created', 1)
                self.assertEqual(self.call(gateway, path, body,
                                           request_key=route_id + '-create', raw=True)[1],
                                 json.dumps(first, sort_keys=True, separators=(',', ':')).encode())
                self.assertEqual(self.counts(first['id']), (1, 1))
                same_status, same = self.call(gateway, path, body, request_key=route_id + '-same')
                self.assertEqual(same_status, 200)
                self.assert_ack(same, 'unchanged', 1)
                self.assertEqual(self.call(gateway, path, body,
                                           request_key=route_id + '-same', raw=True)[1],
                                 json.dumps(same, sort_keys=True, separators=(',', ':')).encode())
                self.assertEqual(same['id'], first['id'])
                self.assertEqual(self.counts(first['id']), (1, 1))
                self.assertEqual(self.admin.execute(
                    'SELECT outcome FROM coordination.idempotency WHERE request_key=%s',
                    (route_id + '-same',)).fetchone()[0], 'observed')
                changed = {**body, changed_field: 'changed', 'on_conflict': 'update'}
                newer_status, newer = self.call(gateway, path, changed,
                                                 request_key=route_id + '-update')
                self.assertEqual(newer_status, 200)
                self.assert_ack(newer, 'updated', 2)
                self.assertEqual(self.call(gateway, path, changed,
                                           request_key=route_id + '-update', raw=True)[1],
                                 json.dumps(newer, sort_keys=True, separators=(',', ':')).encode())
                self.assertEqual(newer['id'], first['id'])
                self.assertEqual(self.counts(first['id']), (2, 2))

    def test_write_only_credential_can_create_observe_and_update(self):
        self.admin.execute('UPDATE auth.project_grants SET permissions=%s WHERE tenant_id=%s '
                           'AND project_id=%s AND principal_id=%s',
                           (['write'], uid(2), uid(3), uid(10)))
        gateway = self.gateway()
        path, _, body, changed_field = CASES[0]
        status, first = self.call(gateway, path, body, request_key='write-only-create')
        self.assertEqual(status, 200)
        self.assert_ack(first, 'created', 1)
        status, same = self.call(gateway, path, body, request_key='write-only-noop')
        self.assertEqual(status, 200)
        self.assert_ack(same, 'unchanged', 1)
        changed = {**body, changed_field: 'write-only changed', 'on_conflict': 'update'}
        status, later = self.call(gateway, path, changed, request_key='write-only-update')
        self.assertEqual(status, 200)
        self.assert_ack(later, 'updated', 2)
        self.assertEqual(self.counts(first['id']), (2, 2))

    def test_private_noop_guard_rejects_forged_digest_without_receipt_or_event(self):
        gateway = self.gateway()
        path, _, body, _ = CASES[0]
        _, first = self.call(gateway, path, body, request_key='guard-create')
        request = json.dumps(['ingest.observe', first['id'], 'knowledge',
                              '0' * 64, 1, 'conflict'], separators=(',', ':'))
        receipt = {'record_id': first['id'], 'revision': 1, 'event_id': None,
                   'status': 'unchanged', 'tombstone': False,
                   'payload_sha256': '0' * 64, 'ingest_kind': 'knowledge',
                   'ingest_mode': 'conflict', 'request_json': request}
        with self.assertRaises(AuthError) as caught:
            with self.auth(WRITE_A, action='write') as scope:
                self.request.execute('''INSERT INTO coordination.idempotency
                    (tenant_id,project_id,principal_id,request_key,request_sha256,outcome,receipt)
                    VALUES (%s,%s,%s,%s,%s,'observed',%s)''',
                    (scope.tenant_id, scope.project_id, scope.principal_id,
                     'forged-noop', hashlib.sha256(request.encode()).hexdigest(),
                     Jsonb(receipt)))
        self.assertEqual(caught.exception.code, 'core_unavailable')
        self.assertEqual(self.admin.execute(
            'SELECT count(*) FROM coordination.idempotency WHERE request_key=%s',
            ('forged-noop',)).fetchone()[0], 0)
        self.assertEqual(self.counts(first['id']), (1, 1))

    def test_same_key_replay_is_byte_equivalent_after_fresh_auth(self):
        gateway = self.gateway()
        path, _, body, _ = CASES[0]
        first = self.call(gateway, path, body, request_key='lost-response', raw=True)
        replay = self.call(gateway, path, body, request_key='lost-response', raw=True)
        self.assertEqual(first, replay)
        self.assertEqual(first[0], 200)
        record_id = json.loads(first[1])['id']
        self.assertEqual(self.counts(record_id), (1, 1))
        self.admin.execute('UPDATE auth.credentials SET revoked_at=clock_timestamp() WHERE key_digest=%s',
                           (hashlib.sha256(WRITE_A).hexdigest(),))
        denied, response = self.call(gateway, path, body, request_key='lost-response')
        self.assertEqual(denied, 403)
        self.assertEqual(response['error']['code'], 'forbidden')
        self.assertNotIn('status', response)
        self.assertEqual(self.counts(record_id), (1, 1))

    def test_concurrent_same_key_is_one_revision_and_event(self):
        gateway = self.gateway()
        path, _, body, _ = CASES[1]
        with ThreadPoolExecutor(max_workers=2) as pool:
            replies = list(pool.map(lambda _: self.call(gateway, path, body,
                                                        request_key='race-one', raw=True),
                                    range(2)))
        self.assertEqual(replies[0], replies[1])
        self.assertEqual(replies[0][0], 200)
        self.assertEqual(self.counts(json.loads(replies[0][1])['id']), (1, 1))

    def test_changed_request_same_key_conflicts_without_partial_write(self):
        gateway = self.gateway()
        for path, route_id, body, changed_field in CASES:
            with self.subTest(route=route_id):
                _, first = self.call(gateway, path, body, request_key=route_id + '-key')
                before = self.counts(first['id'])
                changed = {**body, changed_field: 'different', 'on_conflict': 'update'}
                status, error = self.call(gateway, path, changed, request_key=route_id + '-key')
                self.assertEqual(status, 409)
                self.assertEqual(error['error']['code'], 'conflict')
                self.assertNotIn('status', error)
                self.assertEqual(self.counts(first['id']), before)
                status, error = self.call(gateway, path, changed, request_key=route_id + '-new')
                self.assertEqual(status, 200)
                self.assert_ack(error, 'updated', 2)

    def test_conflict_mode_and_injected_late_failure_leave_no_partial_write(self):
        from unittest.mock import patch
        gateway = self.gateway()
        path, _, body, field = CASES[1]
        _, first = self.call(gateway, path, body, request_key='initial')
        changed = {**body, field: 'different'}
        status, error = self.call(gateway, path, changed, request_key='conflict-mode')
        self.assertEqual(status, 409)
        self.assertEqual(error['error']['code'], 'conflict')
        self.assertEqual(self.counts(first['id']), (1, 1))
        with patch('cortex_core.records._save_request', side_effect=RuntimeError('late rollback')):
            status, error = self.call(gateway, path, {**changed, 'on_conflict': 'update'},
                                      request_key='late-failure')
        self.assertEqual(status, 503)
        self.assertEqual(error['error']['code'], 'core_unavailable')
        self.assertNotIn('status', error)
        self.assertEqual(self.counts(first['id']), (1, 1))

    def test_full_error_table_and_no_success_shaped_error(self):
        gateway = self.gateway()
        path, _, body, _ = CASES[2]
        cases = (
            ({'key': None}, 401, 'credential_required', False),
            ({'project': '4'}, 403, 'forbidden', False),
            ({'request_key': None}, 400, 'idempotency_key_required', False),
            ({'body': b'{bad-json'}, 400, 'invalid_input', False),
            ({'body': {**body, 'on_conflict': 'ignore'}}, 400, 'invalid_input', False),
            ({'body': {**body, 'on_conflict': []}}, 400, 'invalid_input', False),
            ({'body': {**body, 'agent_name': '8'}}, 403, 'forbidden', False),
        )
        for changes, expected, code, retryable in cases:
            with self.subTest(code=code, changes=list(changes)):
                status, error = self.call(gateway, path, changes.get('body', body),
                                          **{key: value for key, value in changes.items()
                                             if key != 'body'})
                self.assertEqual(status, expected)
                self.assertEqual((error['error']['code'], error['error']['retryable']),
                                 (code, retryable))
                self.assertNotIn('status', error)
                self.assertNotIn('receipt', error)
        status, error = self.call(self.gateway(bound=False), path, body)
        self.assertEqual((status, error['error']['code'], error['error']['reason'],
                          error['error']['retryable']),
                         (503, 'capability_unavailable', 'adapter_unbound', False))
        self.assertNotIn('status', error)
        self.core_ready = False
        status, error = self.call(gateway, path, body)
        self.assertEqual((status, error['error']['code'], error['error']['retryable']),
                         (503, 'core_unavailable', True))
        self.assertNotIn('status', error)
        self.core_ready = True
        async def bad_commit(_principal, _scope, key):
            return C05Committed(key, {'record_id': uid(50)}, True,
                                {'status': 'created', 'id': uid(50)})
        status, error = self.call(self.gateway(handlers={'C01-R012': bad_commit}), path, body)
        self.assertEqual(status, 503)
        self.assertEqual(error['error']['code'], 'core_unavailable')
        self.assertNotIn('status', error)
        self.assertEqual(self.admin.execute("SELECT count(*) FROM core.records WHERE kind='decision'").fetchone()[0], 0)

    def test_old_cli_without_credential_fails_before_body_or_replay(self):
        gateway = self.gateway()
        path, _, body, _ = CASES[0]
        self.assertEqual(self.call(gateway, path, body, request_key='known')[0], 200)
        status, error = self.call(gateway, path, b'{bad-json', key=None, request_key='known')
        self.assertEqual(status, 401)
        self.assertEqual(error['error']['code'], 'credential_required')
        self.assertNotIn('status', error)


if __name__ == '__main__':
    unittest.main()
