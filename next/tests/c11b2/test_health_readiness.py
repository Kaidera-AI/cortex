"""Frozen CX-GAP-2b real-PG health/readiness contract; product is absent for RED."""

import asyncio
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
from threading import Thread
import urllib.error
import urllib.request
from uuid import UUID

import psycopg

from core_db_fixture import Fixture, REQUEST, uid
from cortex_core.api import c11a
from cortex_core.auth import AuthError, authorized


INSTALLER = 'kaidera-runtime-core-installer'
FUNCTION = 'auth.enrollment_readiness(uuid)'
STATE_TYPE = 'auth.enrollment_readiness_state'
SECRET = b'synthetic-readiness-console'
DIGEST = hashlib.sha256(SECRET).hexdigest()


class HealthReadiness(Fixture):
    def setUp(self):
        super().setUp()
        self.admin.execute('INSERT INTO core.installations(id) VALUES (%s)', (uid(30),))
        self.admin.execute('INSERT INTO core.tenants(id,installation_id) VALUES (%s,%s)',
                           (uid(31), uid(30)))
        self.admin.execute("INSERT INTO core.projects(tenant_id,id,name) VALUES (%s,%s,'default')",
                           (uid(31), uid(32)))
        self.core_state = 'healthy'
        self.reader_error = False
        self.reader_calls = []

    def reader_present(self):
        self.assertIsNotNone(self.admin.execute('SELECT to_regprocedure(%s)',
                                                (FUNCTION,)).fetchone()[0],
                             '2b private enum reader is deliberately absent before 006')

    def enroll(self):
        dsn = os.environ['TEST_DATABASE_URL'].replace('postgres@', INSTALLER + '@')
        with psycopg.connect(dsn, autocommit=True) as db:
            result = db.execute('SELECT auth.first_identity(%s,%s,%s,%s,%s,%s,%s)',
                tuple(uid(n) for n in (30, 31, 32, 33, 34)) +
                (DIGEST, 'synthetic-readiness-first')).fetchone()[0]
        self.assertFalse(result['replayed'])

    def adapter(self, installation=30, reader=None, timeout_seconds=1.0):
        cls = getattr(c11a, 'EnrollmentHealth', None)
        self.assertIsNotNone(cls, '2b reviewed-Core/receipt health adapter is absent before build')

        async def core_health():
            return {'core_available': self.core_state != 'core_unavailable',
                    'state': self.core_state}

        async def private_reader(bound_installation):
            self.reader_calls.append(bound_installation)
            if self.reader_error:
                raise RuntimeError('synthetic private read failure')
            return self.request.execute('SELECT auth.enrollment_readiness(%s)::text',
                                        (bound_installation,)).fetchone()[0]

        return cls(core_health, reader or private_reader, UUID(uid(installation)),
                   timeout_seconds=timeout_seconds)

    def gateway(self, health):
        async def ready():
            return self.core_state != 'core_unavailable'

        async def unused(*_args):
            raise AssertionError('health must not invoke an authenticated gateway port')

        return c11a.ConsumerGateway(core_probe=ready, principal_resolver=unused,
            permission_recheck=unused, capability_source=unused, health=health, handlers={})

    def call(self, gateway):
        async def run():
            messages = []

            async def receive():
                return {'type': 'http.request', 'body': b'', 'more_body': False}

            async def send(value):
                messages.append(value)

            await gateway.app({'type': 'http', 'method': 'GET', 'path': '/health',
                               'headers': [], 'query_string': b''}, receive, send)
            status = next(x['status'] for x in messages if x['type'] == 'http.response.start')
            body = json.loads(next(x['body'] for x in messages if x['type'] == 'http.response.body'))
            return status, body

        return asyncio.run(run())

    def status_only_podman_probe(self, gateway):
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                status, body = fixture.call(gateway)
                payload = json.dumps(body).encode()
                self.send_response(status)
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, _format, *_args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            try:
                with urllib.request.urlopen(
                    f'http://127.0.0.1:{server.server_port}/health', timeout=3) as response:
                    response.read()
                    return response.status
            except urllib.error.HTTPError as error:
                return error.code
        finally:
            server.shutdown()
            thread.join(timeout=3)
            server.server_close()

    def test_fresh_unenrolled_is_200_and_status_only_probe_passes(self):
        gateway = self.gateway(self.adapter())
        status, body = self.call(gateway)
        self.assertEqual(status, 200)
        self.assertEqual(body, {'component': 'cortex', 'status': 'ok',
                                'readiness': 'enrollment_required'})
        self.assertEqual(self.status_only_podman_probe(gateway), 200)
        self.assertEqual(self.reader_calls, [UUID(uid(30)), UUID(uid(30))])

    def test_status_only_probe_rejects_non200_unenrolled_mutant_without_new_reader(self):
        async def synthetic_unenrolled_health():
            return {'component': 'cortex', 'status': 'ok',
                    'readiness': 'enrollment_required'}

        gateway = self.gateway(synthetic_unenrolled_health)
        self.assertEqual(self.call(gateway)[0], 200)
        self.assertEqual(self.status_only_podman_probe(gateway), 200)

    def test_committed_first_identity_becomes_ready_without_secret_in_public_body(self):
        self.enroll()
        gateway = self.gateway(self.adapter())
        status, body = self.call(gateway)
        self.assertEqual(status, 200)
        self.assertEqual(body, {'component': 'cortex', 'status': 'ok', 'readiness': 'ready'})
        self.assertNotIn(DIGEST, json.dumps(body))
        self.assertEqual(self.status_only_podman_probe(gateway), 200)

    def test_core_down_is_503_and_does_not_consult_receipt_reader(self):
        self.core_state = 'core_unavailable'

        async def forbidden_reader(_installation):
            self.fail('Core-down health consulted the receipt reader')

        status, body = self.call(self.gateway(self.adapter(reader=forbidden_reader)))
        self.assertEqual(status, 503)
        self.assertEqual(body, {'component': 'cortex', 'status': 'unavailable',
                                'readiness': 'unavailable'})
        self.assertEqual(self.reader_calls, [])

    def test_reader_failure_keeps_core_up_http_200_but_never_claims_ready(self):
        self.reader_error = True
        status, body = self.call(self.gateway(self.adapter()))
        self.assertEqual(status, 200)
        self.assertEqual(body, {'component': 'cortex', 'status': 'degraded',
                                'readiness': 'unavailable'})

    def test_reader_deadline_keeps_core_up_http_200_without_false_ready(self):
        async def slow(_installation):
            await asyncio.sleep(0.05)
            return 'ready'

        status, body = self.call(self.gateway(self.adapter(reader=slow, timeout_seconds=0.005)))
        self.assertEqual(status, 200)
        self.assertEqual(body, {'component': 'cortex', 'status': 'degraded',
                                'readiness': 'unavailable'})

    def test_supervisor_down_is_degraded_but_independent_readiness(self):
        self.core_state = 'supervisor_down'

        async def fresh(_installation):
            return 'enrollment_required'

        status, body = self.call(self.gateway(self.adapter(reader=fresh)))
        self.assertEqual(status, 200)
        self.assertEqual(body, {'component': 'cortex', 'status': 'degraded',
                                'readiness': 'enrollment_required'})

    def test_private_reader_returns_only_enum_and_has_narrow_acl(self):
        self.reader_present()
        state = self.admin.execute('SELECT to_regtype(%s)', (STATE_TYPE,)).fetchone()[0]
        self.assertIsNotNone(state)
        owner, security_definer, config, result_type = self.admin.execute(
            'SELECT pg_get_userbyid(proowner),prosecdef,proconfig,pg_get_function_result(oid) '
            'FROM pg_proc WHERE oid=%s::regprocedure', (FUNCTION,)).fetchone()
        self.assertEqual(owner, 'kaidera-runtime-core-verifier')
        self.assertTrue(security_definer)
        self.assertIn('search_path=pg_catalog, auth, core, pg_temp', config)
        self.assertEqual(result_type, STATE_TYPE)
        definition = self.admin.execute('SELECT pg_get_functiondef(%s::regprocedure)',
                                        (FUNCTION,)).fetchone()[0]
        self.assertNotIn('auth.credentials', definition)
        self.assertNotIn('key_digest', definition)
        self.assertFalse(self.admin.execute(
            "SELECT EXISTS(SELECT 1 FROM aclexplode(proacl) a "
            "WHERE a.grantee=0 AND a.privilege_type='EXECUTE') "
            'FROM pg_proc WHERE oid=%s::regprocedure', (FUNCTION,)).fetchone()[0])
        self.assertTrue(self.admin.execute('SELECT has_function_privilege(%s,%s,%s)',
                                         (REQUEST, FUNCTION, 'EXECUTE')).fetchone()[0])
        self.assertFalse(self.admin.execute('SELECT has_table_privilege(%s,%s,%s)',
                                          (REQUEST, 'auth.first_identity_receipts', 'SELECT')).fetchone()[0])
        self.assertFalse(self.admin.execute('SELECT has_function_privilege(%s,%s,%s)',
                                          (INSTALLER, FUNCTION, 'EXECUTE')).fetchone()[0])
        cursor = self.request.execute('SELECT auth.enrollment_readiness(%s)::text', (uid(30),))
        self.assertEqual(len(cursor.description), 1)
        self.assertEqual(cursor.fetchone(), ('enrollment_required',))
        self.enroll()
        self.assertEqual(self.request.execute('SELECT auth.enrollment_readiness(%s)::text',
                                              (uid(30),)).fetchone(), ('ready',))

    def test_a_receipt_in_one_installation_never_readies_another(self):
        self.enroll()
        self.admin.execute('INSERT INTO core.installations(id) VALUES (%s)', (uid(60),))
        self.admin.execute('INSERT INTO core.tenants(id,installation_id) VALUES (%s,%s)',
                           (uid(61), uid(60)))
        self.admin.execute("INSERT INTO core.projects(tenant_id,id,name) VALUES (%s,%s,'default')",
                           (uid(61), uid(62)))
        status_a, body_a = self.call(self.gateway(self.adapter(installation=30)))
        status_b, body_b = self.call(self.gateway(self.adapter(installation=60)))
        self.assertEqual((status_a, body_a['readiness']), (200, 'ready'))
        self.assertEqual((status_b, body_b['readiness']), (200, 'enrollment_required'))

    def test_invalid_receipt_grant_linkage_never_claims_ready(self):
        self.enroll()
        self.admin.execute("INSERT INTO core.projects(tenant_id,id,name) VALUES (%s,%s,'side')",
                           (uid(31), uid(37)))
        self.admin.execute('UPDATE auth.first_identity_receipts SET project_id=%s '
                           'WHERE installation_id=%s', (uid(37), uid(30)))
        status, body = self.call(self.gateway(self.adapter()))
        self.assertEqual(status, 200)
        self.assertEqual(body, {'component': 'cortex', 'status': 'degraded',
                                'readiness': 'unavailable'})

    def test_preexisting_identity_without_receipt_is_unavailable_not_new_enrollment(self):
        self.admin.execute("INSERT INTO auth.principals(tenant_id,id,name) "
                           "VALUES (%s,%s,'synthetic prior principal')", (uid(31), uid(42)))
        status, body = self.call(self.gateway(self.adapter()))
        self.assertEqual(status, 200)
        self.assertEqual(body, {'component': 'cortex', 'status': 'degraded',
                                'readiness': 'unavailable'})

    def test_revoked_first_key_stays_refused_despite_ready_enrollment_marker(self):
        self.enroll()
        self.admin.execute('UPDATE auth.credentials SET revoked_at=clock_timestamp() '
                           'WHERE tenant_id=%s AND id=%s', (uid(31), uid(34)))
        with self.assertRaises(AuthError):
            with authorized(self.request, SECRET, UUID(uid(30)), UUID(uid(32))):
                pass
        status, body = self.call(self.gateway(self.adapter()))
        self.assertEqual((status, body['readiness']), (200, 'ready'))

    def test_openkai_health_exchange_stays_unauthenticated_with_readiness(self):
        fixture = json.loads((Path(__file__).resolve().parents[2] /
                              'contracts/fixtures/openkai/route-exchanges.json').read_text())
        health = next(x for x in fixture['cases'] if x['id'] == 'route-C01-R002_get_health')
        self.assertEqual(health['request']['headers'], {})
        self.assertEqual(health['success']['status'], 200)
        self.assertEqual(health['success']['body'].get('readiness'), 'enrollment_required')
