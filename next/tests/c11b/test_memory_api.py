"""Released /memory shape and C05 admission through a disposable real PG."""

import asyncio
import hashlib
import json
import sys
from pathlib import Path
from uuid import UUID
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core_db_fixture import Fixture, WRITE_A, READ_A, READ_B, OWNER_A, REQUEST, API, uid
from cortex_core.api.c11a import ConsumerGateway
from cortex_core.api.c11b import C11bRecordPort
from cortex_core.records import Records


class MemoryAPI(Fixture):
    def setUp(self):
        super().setUp()
        import os
        import psycopg
        dsn = os.environ['TEST_DATABASE_URL'].replace('postgres@', API + '@')
        def connection():
            db = psycopg.connect(dsn, autocommit=True)
            db.execute(f'SET ROLE "{REQUEST}"')
            return db
        self.port = C11bRecordPort(connection, UUID(uid(1)), {'3': UUID(uid(3))})
        async def ready(): return True
        async def state(*_): return {'state': 'unavailable'}
        async def grant(*_): return False
        async def health(): return {'component': 'cortex', 'status': 'ok'}
        self.gateway = ConsumerGateway(core_probe=ready,
            principal_resolver=self.port.principal, permission_recheck=grant,
            capability_source=state, health=health,
            handlers={'C01-R102': self.port.write_memory},
            record_reader=self.port.read_record,
            record_credential_active=self.port.credential_active,
            allow_legacy_idempotency=True)

    def call(self, method, path, key, body=None, *, agent='10', project='3',
             request_key=None, raw=False):
        payload = json.dumps(body, separators=(',', ':')).encode() if body is not None else b''
        headers = [(b'authorization', b'Bearer ' + key),
                   (b'x-project', project.encode()), (b'x-agent-name', agent.encode())]
        if request_key is not None:
            headers.append((b'idempotency-key', request_key if isinstance(request_key, bytes)
                            else request_key.encode()))
        scope = {'type': 'http', 'method': method, 'path': path, 'headers': headers,
                 'query_string': b'', 'client': ('127.0.0.1', 10001)}
        messages = []
        async def receive(): return {'type': 'http.request', 'body': payload, 'more_body': False}
        async def send(message): messages.append(message)
        asyncio.run(self.gateway.app(scope, receive, send))
        start = next(x for x in messages if x['type'] == 'http.response.start')
        data = b''.join(x.get('body', b'') for x in messages if x['type'] == 'http.response.body')
        if raw:
            return start['status'], data, tuple(start['headers'])
        return start['status'], json.loads(data)

    def test_d1_committed_read_exposes_exact_revision_and_digest(self):
        body = {'section': 'decisions', 'content': 'committed D1 payload',
                'category': 'operational', 'source': 'openkai/d1/one'}
        status, ack = self.call('POST', '/memory', WRITE_A, body)
        self.assertEqual(status, 200)
        expected = Records(self.request, READ_A, UUID(uid(1)), UUID(uid(3))).get(UUID(ack['id']))
        status, read = self.call('GET', '/records/' + ack['id'], READ_A)
        self.assertEqual(status, 200)
        self.assertEqual(read['revision'], expected.revision)
        self.assertEqual(read.get('payload_sha256'), expected.payload_sha256)

    def test_d1_authenticated_out_of_scope_matches_unknown_id(self):
        body = {'section': 'decisions', 'content': 'private D1 payload',
                'source': 'openkai/d1/private'}
        status, ack = self.call('POST', '/memory', WRITE_A, body)
        self.assertEqual(status, 200)
        other_key = b'synthetic-only-read-other-project'
        self.admin.execute("INSERT INTO auth.principals(tenant_id,id,name) VALUES (%s,%s,'d1-other')",
                           (uid(2), uid(16)))
        self.admin.execute('INSERT INTO auth.project_grants VALUES (%s,%s,%s,%s)',
                           (uid(2), uid(4), uid(16), ['read']))
        self.admin.execute('INSERT INTO auth.credentials(tenant_id,id,principal_id,key_digest) VALUES (%s,%s,%s,%s)',
                           (uid(2), uid(116), uid(16), hashlib.sha256(other_key).hexdigest()))
        hidden = self.call('GET', '/records/' + ack['id'], other_key, raw=True)
        missing = self.call('GET', '/records/' + uid(1234), READ_A, raw=True)
        self.assertEqual(hidden, missing, 'out-of-scope and unknown must have identical status, body and headers')
        self.assertEqual(hidden[0], 404)

    def test_d1_authorized_tombstone_is_gone(self):
        body = {'section': 'decisions', 'content': 'D1 tombstone',
                'source': 'openkai/d1/tombstone'}
        status, ack = self.call('POST', '/memory', WRITE_A, body)
        self.assertEqual(status, 200)
        deleted = Records(self.request, WRITE_A, UUID(uid(1)), UUID(uid(3))).delete(
            UUID(ack['id']), 1, 'd1-delete')
        self.assertEqual(deleted.revision, 2)
        status, result = self.call('GET', '/records/' + ack['id'], READ_A)
        self.assertEqual(status, 410)
        self.assertEqual(result['error']['code'], 'gone')

    def test_d1_revoked_and_forged_credentials_remain_unauthenticated(self):
        body = {'section': 'decisions', 'content': 'D1 credential',
                'source': 'openkai/d1/credential'}
        status, ack = self.call('POST', '/memory', WRITE_A, body)
        self.assertEqual(status, 200)
        self.admin.execute('UPDATE auth.credentials SET revoked_at=clock_timestamp() WHERE key_digest=%s',
                           (hashlib.sha256(READ_A).hexdigest(),))
        for key in (READ_A, b'synthetic-forged-d1-credential'):
            with self.subTest(key=key):
                status, result = self.call('GET', '/records/' + ack['id'], key)
                self.assertEqual((status, result['error']['code']),
                                 (401, 'credential_required'))

    def test_d1_revoked_and_forged_match_unknown_without_existence_leak(self):
        body = {'section': 'decisions', 'content': 'D1 refusal',
                'source': 'openkai/d1/refusal'}
        status, ack = self.call('POST', '/memory', WRITE_A, body)
        self.assertEqual(status, 200)
        unknown = self.call('GET', '/records/' + uid(1234), READ_A, raw=True)
        self.assertEqual(unknown[0], 404)
        self.admin.execute('UPDATE auth.credentials SET revoked_at=clock_timestamp() WHERE key_digest=%s',
                           (hashlib.sha256(READ_A).hexdigest(),))
        for key in (READ_A, b'synthetic-forged-d1-credential'):
            with self.subTest(key=key):
                self.assertEqual(self.call('GET', '/records/' + ack['id'], key, raw=True),
                                 unknown, 'refusal and unknown must match status, body and headers')
        status, result = self.call('GET', '/records/' + ack['id'], b'')
        self.assertEqual((status, result['error']['code']), (401, 'credential_required'))

    def test_released_shape_commit_read_and_headerless_replay(self):
        body = {'section': 'decisions', 'content': 'one exact memory',
                'category': 'operational', 'source': 'openkai/decision/one'}
        first_status, first = self.call('POST', '/memory', WRITE_A, body)
        self.assertEqual(first_status, 200)
        self.assertEqual(set(first), {'id', 'action', 'status', 'created', 'updated', 'embedded'})
        self.assertEqual((first['action'], first['status'], first['created'], first['updated'],
                          first['embedded']), ('created', 'created', True, False, False))
        saved = Records(self.request, READ_A, UUID(uid(1)), UUID(uid(3))).get(UUID(first['id']))
        self.assertEqual(self.call('GET', '/records/' + first['id'], READ_A),
                         (200, {'id': first['id'], 'kind': 'memory', 'revision': 1,
                                'payload_sha256': saved.payload_sha256,
                                'section': 'decisions', 'content': 'one exact memory',
                                'category': 'operational', 'source': 'openkai/decision/one'}))
        self.assertEqual(self.call('POST', '/memory', WRITE_A, body), (200, first))
        self.assertEqual(self.admin.execute('SELECT count(*) FROM core.record_revisions WHERE record_id=%s',
                                            (first['id'],)).fetchone()[0], 1)
        self.assertEqual(self.admin.execute('SELECT count(*) FROM coordination.outbox WHERE aggregate_id=%s',
                                            (first['id'],)).fetchone()[0], 1)

    def test_explicit_key_conflict_update_and_old_retry(self):
        body = {'section': 'decisions', 'content': 'first', 'source': 'openkai/decision/two'}
        status, first = self.call('POST', '/memory', WRITE_A, body, request_key='logical-1')
        self.assertEqual(status, 200)
        changed = {**body, 'content': 'second'}
        self.assertEqual(self.call('POST', '/memory', WRITE_A, changed, request_key='logical-1')[0], 409)
        status, second = self.call('POST', '/memory', WRITE_A, changed, request_key='logical-2')
        self.assertEqual((status, second['id'], second['action']), (200, first['id'], 'updated'))
        self.assertEqual(self.call('POST', '/memory', WRITE_A, body, request_key='logical-1'),
                         (200, first))
        self.assertEqual(self.admin.execute('SELECT count(*) FROM core.record_revisions WHERE record_id=%s',
                                            (first['id'],)).fetchone()[0], 2)

    def test_forged_writer_revocation_and_cross_tenant_id(self):
        body = {'section': 'decisions', 'content': 'secret', 'source': 'openkai/decision/three'}
        self.assertEqual(self.call('POST', '/memory', WRITE_A, body, agent='8')[0], 403)
        status, created = self.call('POST', '/memory', WRITE_A, body)
        self.assertEqual(status, 200)
        self.assertEqual(self.call('GET', '/records/' + created['id'], READ_B)[0], 401)
        self.admin.execute('UPDATE auth.credentials SET revoked_at=clock_timestamp() WHERE key_digest=%s',
                           (hashlib.sha256(WRITE_A).hexdigest(),))
        self.assertEqual(self.call('POST', '/memory', WRITE_A, body)[0], 403)
        self.assertEqual(self.call('GET', '/records/' + created['id'], WRITE_A)[0], 401)

    def test_released_tokenless_cli_shape_is_refused_by_c04(self):
        body = {'section': 'decisions', 'content': 'CLI memory', 'category': 'operational'}
        before = self.admin.execute('SELECT count(*) FROM core.records WHERE kind=%s',
                                    ('memory',)).fetchone()[0]
        status, response = self.call('POST', '/memory', b'', body)
        self.assertEqual((status, response['error']['code']), (401, 'credential_required'))
        self.assertIn('upgrade', response['error']['message'].lower())
        self.assertEqual(self.admin.execute('SELECT count(*) FROM core.records WHERE kind=%s',
                                            ('memory',)).fetchone()[0], before)

    def test_same_key_concurrent_requests_one_revision_and_default_source(self):
        body = {'section': 'learnings', 'content': 'parallel write', 'category': 'operational'}
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda _: self.call('POST', '/memory', WRITE_A, body,
                                                           request_key='parallel'), range(2)))
        self.assertEqual(responses[0], responses[1])
        self.assertEqual(responses[0][0], 200)
        record_id = responses[0][1]['id']
        self.assertEqual(self.call('GET', '/records/' + record_id, READ_A)[1]['source'], 'manual:10')
        self.assertEqual(self.admin.execute('SELECT count(*) FROM core.record_revisions WHERE record_id=%s',
                                            (record_id,)).fetchone()[0], 1)

    def test_private_request_lookup_is_principal_scoped(self):
        body = {'section': 'learnings', 'content': 'bound to writer ten',
                'source': 'openkai/learning/scope'}
        self.assertEqual(self.call('POST', '/memory', WRITE_A, body, request_key='scoped-key')[0], 200)
        owner = Records(self.request, OWNER_A, UUID(uid(1)), UUID(uid(3)))
        self.assertIsNone(owner.lookup_request('scoped-key'))

    def test_invalid_key_and_forged_project_refuse_before_write(self):
        body = {'section': 'learnings', 'content': 'never written', 'source': 'invalid-key'}
        before = self.admin.execute('SELECT count(*) FROM coordination.outbox').fetchone()[0]
        self.assertEqual(self.call('POST', '/memory', WRITE_A, body, request_key=b'\xff')[0], 400)
        self.assertEqual(self.call('POST', '/memory', WRITE_A, body, project='other')[0], 403)
        self.assertEqual(self.admin.execute('SELECT count(*) FROM coordination.outbox').fetchone()[0], before)
