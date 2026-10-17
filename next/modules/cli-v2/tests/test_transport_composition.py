"""Actual Core client/custody joined to the helper; SOURCE ONLY fixtures.

The committed encrypted record is staged with public fixture bytes, without SQL
enrollment. HTTP is a loopback recorder, except the error packet comes from the
existing gateway. Neither fixture grants installed, SQL, or native credit.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

from cortex_core.api.c11a import _error_status
from cortex_core.auth import AuthError
from cortex_core.runtime.bootstrap_config import decode_config
from cortex_core.runtime.custody import open_custody
from cortex_core.runtime.tests.test_bootstrap_config import sample
from cortex_core.runtime.tests.test_custody import intent, receipt, KEY, SECRET

HELPER = Path(__file__).resolve().parents[1] / '_cortex_api.sh'


class CoreClientCompositionTests(unittest.TestCase):
    def exchange(self, *, committed=True, key=KEY, project=None, wrong_installation=False,
                 packet=None, status=200, method='GET'):
        requests = []
        with tempfile.TemporaryDirectory(prefix='finn-core-client-') as temp:
            root = Path(temp).resolve()
            root.chmod(0o700)
            (root / 'config').mkdir(mode=0o700)
            (root / 'secrets').mkdir(mode=0o700)
            (root / 'secrets/cortex').mkdir(mode=0o700)
            value = sample(root)
            value['limits']['request_seconds'] = 2
            alternate = '20000000-0000-0000-0000-000000000003'
            value['project_names']['alternate'] = alternate

            class Recorder(BaseHTTPRequestHandler):
                def log_message(self, *_args):
                    pass

                def respond(self):
                    requests.append((self.path, dict(self.headers)))
                    health = self.path == '/health'
                    body = ({'component': 'cortex', 'status': 'ok', 'readiness': 'ready',
                             'installation_id': alternate if wrong_installation else value['installation_id']}
                            if health else packet if packet is not None else {'record': {'id': 'fixture'}})
                    raw = json.dumps(body).encode()
                    self.send_response(200 if health else status)
                    self.send_header('Content-Length', str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)

                do_GET = respond
                do_POST = respond

            server = ThreadingHTTPServer(('127.0.0.1', 0), Recorder)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                value['publish']['port'] = server.server_port
                path = root / 'config/cortex.json'
                path.write_text(json.dumps(value))
                path.chmod(0o600)
                config = decode_config(json.dumps(value).encode(), mode='seed')
                unlock, writer = os.pipe()
                os.write(writer, KEY)
                os.close(writer)
                try:
                    with open_custody(config, unlock_fd=unlock, key_id='public-fixture-key') as vault:
                        with vault.seed_record() as record:
                            expected = intent(value)
                            record.create(intent=expected, credential=SECRET)
                            if committed:
                                record.commit(receipt=receipt(expected))
                finally:
                    os.close(unlock)
                encrypted = (root / 'secrets/cortex/client').read_bytes()
                self.assertNotIn(SECRET, encrypted, 'fixture must use actual encrypted custody')
                reader, writer = os.pipe()
                unlock, key_writer = os.pipe()
                os.write(key_writer, key)
                os.close(key_writer)
                env = {**os.environ, 'HOME': temp, 'TMPDIR': temp,
                       'CORTEX_API_URL': 'http://127.0.0.1:1', 'CORTEX_PROJECT': 'legacy-wrong',
                       'CORTEX_IDEMPOTENCY_KEY': 'stable-fixture-operation'}
                args = [sys.executable, '-B', '-m', 'cortex_core.runtime.operator', 'config', 'client',
                        '--config', str(path), '--unlock-fd', str(unlock),
                        '--key-id', 'public-fixture-key', '--projection-fd', str(writer)]
                if project is not None:
                    args += ['--project', project]
                try:
                    producer = subprocess.run(args, pass_fds=(unlock, writer), env=env,
                                              capture_output=True, text=True, timeout=5)
                    os.close(writer)
                    writer = None
                    env['CORTEX_CLIENT_PROJECTION_FD'] = str(reader)
                    consumer = subprocess.run(
                        ['/bin/bash', '-c', 'source "$1"; cortex_api_call "$2" /records/fixture \'{}\' first-console',
                         'finn-core-client', str(HELPER), method], pass_fds=(reader,), env=env,
                        capture_output=True, text=True, timeout=5)
                    self.assertEqual(producer.stdout, '')
                    self.assertNotIn(SECRET.decode(), producer.stderr + consumer.stdout + consumer.stderr)
                    return producer, consumer, requests, value
                finally:
                    os.close(unlock)
                    os.close(reader)
                    if writer is not None:
                        os.close(writer)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
                self.assertFalse(thread.is_alive(), 'owned recorder must stop')

    def test_actual_encrypted_committed_client_reaches_existing_helper(self):
        producer, consumer, requests, value = self.exchange()
        self.assertEqual(producer.returncode, 0, producer.stderr)
        self.assertEqual(consumer.returncode, 0, consumer.stderr)
        self.assertEqual(json.loads(consumer.stdout), {'record': {'id': 'fixture'}})
        self.assertEqual([r[0] for r in requests], ['/health', '/records/fixture'])
        self.assertNotIn('Authorization', requests[0][1])
        self.assertEqual(requests[1][1]['Authorization'], 'Bearer ' + SECRET.decode())
        self.assertEqual(requests[1][1]['X-Project-Id'], value['project_id'])
        self.assertEqual(requests[1][1]['X-Project'], 'sample')

    def test_actual_client_selected_project_overrides_legacy_environment(self):
        producer, consumer, requests, _ = self.exchange(project='alternate')
        self.assertEqual((producer.returncode, consumer.returncode), (0, 0), producer.stderr + consumer.stderr)
        self.assertEqual(requests[-1][1]['X-Project'], 'alternate')
        self.assertEqual(requests[-1][1]['X-Project-Id'], '20000000-0000-0000-0000-000000000003')

    def test_prepared_record_cannot_supply_projection_or_http(self):
        producer, consumer, requests, _ = self.exchange(committed=False)
        self.assertNotEqual(producer.returncode, 0)
        self.assertNotEqual(consumer.returncode, 0)
        self.assertEqual((requests, consumer.stdout), ([], ''))
        self.assertEqual(json.loads(producer.stderr)['error']['reason'], 'custody_uncommitted')

    def test_wrong_unlock_cannot_supply_projection_or_http(self):
        producer, consumer, requests, _ = self.exchange(key=b'x' * 32)
        self.assertNotEqual(producer.returncode, 0)
        self.assertNotEqual(consumer.returncode, 0)
        self.assertEqual((requests, consumer.stdout), ([], ''))
        self.assertEqual(json.loads(producer.stderr)['error']['reason'], 'custody_invalid')

    def test_actual_projected_identity_refuses_wrong_health_before_bearer(self):
        producer, consumer, requests, _ = self.exchange(wrong_installation=True)
        self.assertEqual(producer.returncode, 0, producer.stderr)
        self.assertNotEqual(consumer.returncode, 0)
        self.assertEqual([r[0] for r in requests], ['/health'])
        self.assertNotIn('Authorization', requests[0][1])

    def test_existing_gateway_unauthenticated_packet_keeps_typed_401_message(self):
        status, packet = _error_status(AuthError('unauthenticated'))
        self.assertEqual(status, 401)
        self.assertEqual(packet['error']['code'], 'credential_required')
        producer, consumer, _, _ = self.exchange(status=status, packet=packet)
        self.assertEqual(producer.returncode, 0, producer.stderr)
        self.assertEqual(consumer.returncode, 22, consumer.stderr)
        self.assertEqual(consumer.stdout, '')
        self.assertEqual(json.loads(consumer.stderr), packet)

    def test_message_is_bounded_by_utf8_bytes(self):
        packet = {'error': {'code': 'credential_required', 'retryable': False, 'message': '\u00e9' * 129}}
        _, consumer, _, _ = self.exchange(status=401, packet=packet)
        self.assertNotEqual(consumer.returncode, 22)
        self.assertNotEqual(consumer.returncode, 0)
        self.assertEqual(consumer.stdout, '')

    def test_unknown_error_field_still_refuses(self):
        packet = {'error': {'code': 'forbidden', 'retryable': False, 'driver': 'fixture-only'}}
        _, consumer, _, _ = self.exchange(status=403, packet=packet)
        self.assertNotEqual(consumer.returncode, 22)
        self.assertNotEqual(consumer.returncode, 0)
        self.assertEqual(consumer.stdout, '')


if __name__ == '__main__':
    unittest.main()
