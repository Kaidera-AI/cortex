"""RED-first released R121 envelope and C04-scoped committed session IDs."""

import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from uuid import UUID, NAMESPACE_URL, uuid5

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core_db_fixture import API, Fixture, READ_A, READ_B, REQUEST, WRITE_A, uid
from cortex_core.api.c11a import ConsumerGateway
from cortex_core.api.c11b import C11bRecordPort
from cortex_core.records import Records


class SessionIdsRead(unittest.TestCase):
    reset = Fixture.reset
    auth = Fixture.auth

    def setUp(self):
        Fixture.setUp(self)
        self.admin.execute('INSERT INTO auth.project_grants VALUES (%s,%s,%s,%s)',
                           (uid(2), uid(4), uid(8), ['read']))
        self.admin.execute('INSERT INTO auth.project_grants VALUES (%s,%s,%s,%s)',
                           (uid(2), uid(4), uid(10), ['read', 'write']))
        dsn = os.environ['TEST_DATABASE_URL'].replace('postgres@', API + '@')

        def connection():
            db = psycopg.connect(dsn, autocommit=True)
            db.execute(f'SET ROLE "{REQUEST}"')
            return db

        self.connection = connection
        self.ports = {}
        self.gateways = {}
        for installation in (1, 20):
            port = C11bRecordPort(connection, UUID(uid(installation)),
                                  {'3': UUID(uid(3)), '4': UUID(uid(4))})
            handlers = {'C01-R013': port.ingest_session}
            if callable(getattr(port, 'session_ids', None)):
                handlers['C01-R121'] = port.session_ids
            async def ready(): return True
            async def capability(*_): return {'state': 'unavailable'}
            async def recheck(*_): return True
            async def health(): return {'component': 'cortex', 'status': 'ok'}
            self.ports[installation] = port
            self.gateways[installation] = ConsumerGateway(
                core_probe=ready, principal_resolver=port.principal,
                permission_recheck=recheck, capability_source=capability,
                health=health, handlers=handlers,
                allow_legacy_idempotency=True)

    def call(self, method='GET', *, body=None, key=READ_A, project='3',
             query=b'', installation=1):
        path = '/sessions/ingested-ids' if method == 'GET' else '/sessions/ingest'
        headers = [(b'authorization', b'Bearer ' + key)]
        if project is not None:
            headers.append((b'x-project', project.encode()))
        if method == 'POST':
            headers.append((b'idempotency-key', f'session-{body["session_uuid"]}'.encode()))
        scope = {'type': 'http', 'method': method, 'path': path,
                 'headers': headers, 'query_string': query}
        payload = b'' if body is None else json.dumps(body).encode()
        messages = []
        async def receive():
            return {'type': 'http.request', 'body': payload, 'more_body': False}
        async def send(value):
            messages.append(value)
        asyncio.run(self.gateways[installation].app(scope, receive, send))
        status = next(row['status'] for row in messages
                      if row['type'] == 'http.response.start')
        response = json.loads(next(row['body'] for row in messages
                                   if row['type'] == 'http.response.body'))
        return status, response

    def seed(self, number, project='3'):
        body = {'session_uuid': uid(number), 'agent': '10',
                'source_path': f'/synthetic/{project}/{number}.jsonl',
                'provider': 'codex', 'messages': []}
        status, response = self.call('POST', body=body, key=WRITE_A, project=project)
        self.assertEqual(status, 200, response)
        return body['session_uuid']

    def test_released_cli_exact_project_ids_envelope_and_both_selectors(self):
        # Published KOS v0.2.007 and signed-unpublished 0.2.009
        # .agents/scripts/cortex-ingest-all SHA-256 79a57504... lines 48-49.
        self.seed(82)
        self.seed(80)
        expected = {'project': '3', 'ids': [uid(80), uid(82)]}
        for project, query in (('3', b''), (None, b'project=3'),
                               ('3', b'project=3')):
            with self.subTest(project=project, query=query):
                status, body = self.call(project=project, query=query)
                self.assertEqual(status, 200, body)
                self.assertEqual(body, expected)

    def test_same_tenant_other_project_and_other_tenant_do_not_leak_ids(self):
        self.seed(83, project='3')
        self.seed(84, project='4')
        self.assertEqual(self.call(project='4'),
                         (200, {'project': '4', 'ids': [uid(84)]}))
        self.assertEqual(self.call(key=READ_B, installation=20),
                         (200, {'project': '3', 'ids': []}))

    def test_missing_mismatched_or_duplicate_selector_refuses(self):
        for project, query in ((None, b''), ('3', b'project=4'),
                               (None, b'project=3&project=4')):
            with self.subTest(project=project, query=query):
                status, body = self.call(project=project, query=query)
                self.assertEqual(status, 400, body)
                self.assertEqual(body['error']['code'], 'invalid_input')

    def test_tombstoned_session_is_not_returned(self):
        session_id = self.seed(85)
        record_id = uuid5(NAMESPACE_URL, f'cortex.session:{uid(2)}:{uid(3)}:{session_id}')
        Records(self.request, WRITE_A, UUID(uid(1)), UUID(uid(3))).delete(
            record_id, 1, 'delete-session-85')
        status, body = self.call()
        self.assertEqual(status, 200, body)
        self.assertEqual(body, {'project': '3', 'ids': []})

    def test_request_role_cannot_select_private_session_sources(self):
        with self.assertRaises(psycopg.errors.InsufficientPrivilege):
            with self.request.transaction():
                self.request.execute('SELECT session_uuid FROM coordination.session_sources')

    def test_read_function_privileges_and_unbound_context(self):
        row = self.admin.execute('''SELECT pg_get_userbyid(p.proowner),p.prosecdef,p.proconfig,
            has_function_privilege('public','coordination.c11b4_session_ids()','EXECUTE'),
            has_function_privilege(%s,'coordination.c11b4_session_ids()','EXECUTE'),
            has_table_privilege(%s,'coordination.session_sources','SELECT')
            FROM pg_proc p WHERE p.oid='coordination.c11b4_session_ids()'::regprocedure''',
            (REQUEST, REQUEST)).fetchone()
        self.assertEqual(row[0], 'kaidera-runtime-core-verifier')
        self.assertIs(row[1], True)
        self.assertIn('search_path=pg_catalog, auth, core, coordination, pg_temp', row[2])
        self.assertEqual(row[3:], (False, True, False))
        with self.assertRaises(psycopg.errors.InsufficientPrivilege):
            with self.request.transaction():
                self.request.execute('SELECT * FROM coordination.c11b4_session_ids()')
        with self.assertRaises(psycopg.errors.InsufficientPrivilege):
            with self.request.transaction():
                self.request.execute("SELECT set_config('cortex.credential_digest',%s,true),"
                                     "set_config('cortex.installation_id',%s,true),"
                                     "set_config('cortex.project_id',%s,true),"
                                     "set_config('cortex.action','read',true)",
                                     (hashlib.sha256(READ_A).hexdigest(), uid(1), uid(3)))
                self.request.execute('SELECT * FROM coordination.c11b4_session_ids()')

    def test_late_revocation_and_permission_change_refuse_before_success(self):
        for change in ('revoke', 'generation'):
            with self.subTest(change=change):
                self.seed(86 if change == 'revoke' else 87)
                port = self.ports[1]
                original = getattr(port, '_session_ids', None)
                def change_after_read(principal, scope):
                    result = original(principal, scope)
                    if change == 'revoke':
                        self.admin.execute('UPDATE auth.credentials SET revoked_at=clock_timestamp() '
                                           'WHERE key_digest=%s',
                                           (hashlib.sha256(READ_A).hexdigest(),))
                    else:
                        self.admin.execute("UPDATE auth.project_grants SET permissions=ARRAY['read','write'] "
                                           'WHERE tenant_id=%s AND project_id=%s AND principal_id=%s',
                                           (uid(2), uid(3), uid(8)))
                    return result
                if original is None:
                    status, body = self.call()
                else:
                    with patch.object(port, '_session_ids', change_after_read):
                        status, body = self.call()
                self.assertEqual(status, 403, body)
                self.assertEqual(body['error']['code'], 'forbidden')
                if change == 'revoke':
                    self.admin.execute('UPDATE auth.credentials SET revoked_at=NULL '
                                       'WHERE key_digest=%s',
                                       (hashlib.sha256(READ_A).hexdigest(),))
