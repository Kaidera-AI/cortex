"""Proposed A result wire controls; synthetic server, zero installed credit.

MIT. Cox must accept the health/receipt join before these represent integration.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest

from test_transport_projection import HELPER, INSTALLATION, PROJECT, TOKEN, wire


class TransportResultTests(unittest.TestCase):
    def exchange(self, *, body=None, status=200, health=None, method='GET', key='same-operation',
                 headers=None, delay=0, url_suffix=''):
        requests = []
        if body is None:
            body = b'{"record":{"id":"synthetic"}}'
        if health is None:
            health = {'component': 'cortex', 'installation_id': INSTALLATION, 'status': 'ready'}

        class Recorder(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def respond(self):
                import time
                length = int(self.headers.get('Content-Length', '0'))
                data = self.rfile.read(length)
                requests.append((self.path, dict(self.headers), data))
                is_health = self.path == '/health'
                response = json.dumps(health).encode() if is_health else body
                self.send_response(200 if is_health else status)
                for name, value in (headers or {}).items():
                    self.send_header(name, value)
                self.send_header('Content-Length', str(len(response)))
                self.end_headers()
                if not is_health:
                    time.sleep(delay)
                try:
                    self.wfile.write(response)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            do_GET = respond
            do_POST = respond

        server = ThreadingHTTPServer(('127.0.0.1', 0), Recorder)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        reader, writer = os.pipe()
        try:
            os.write(writer, wire(f'http://127.0.0.1:{server.server_port}' + url_suffix))
            os.close(writer)
            writer = None
            with tempfile.TemporaryDirectory(prefix='finn-results-') as temp:
                env = {'PATH': os.environ['PATH'], 'HOME': temp, 'TMPDIR': temp,
                       'CORTEX_CLIENT_PROJECTION_FD': str(reader),
                       'CORTEX_IDEMPOTENCY_KEY': key, 'CORTEX_PROJECT': 'wrong-legacy-project',
                       'CORTEX_API_URL': 'http://127.0.0.1:1'}
                result = subprocess.run(
                    ['/bin/bash', '-c', 'source "$1"; cortex_api_call "$2" /records/synthetic \'{"text":"synthetic"}\' first-console',
                     'finn-result', str(HELPER), method],
                    pass_fds=(reader,), env=env, cwd=temp, capture_output=True,
                    text=True, timeout=5,
                )
            self.assertNotIn(TOKEN, result.stdout + result.stderr, 'credential appeared in output')
            return result, requests
        finally:
            os.close(reader)
            if writer is not None:
                os.close(writer)
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive(), 'owned recorder must stop')

    def test_admitted_read_uses_projection_and_authenticates_only_after_health(self):
        result, requests = self.exchange()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {'record': {'id': 'synthetic'}})
        self.assertEqual([r[0] for r in requests], ['/health', '/records/synthetic'])
        self.assertNotIn('Authorization', requests[0][1])
        self.assertEqual(requests[1][1]['Authorization'], 'Bearer ' + TOKEN)
        self.assertEqual(requests[1][1]['X-Project'], 'synthetic')
        self.assertEqual(requests[1][1]['X-Project-Id'], PROJECT)

    def test_wrong_installation_never_receives_credential(self):
        result, requests = self.exchange(health={'component': 'cortex', 'status': 'ready', 'installation_id': PROJECT})
        self.assertNotEqual(result.returncode, 0, 'installation mismatch must refuse')
        self.assertEqual(result.stdout, '')
        self.assertEqual([r[0] for r in requests], ['/health'])
        self.assertNotIn('Authorization', requests[0][1])

    def test_successful_mutation_requires_committed_receipt_and_stable_key(self):
        receipt = {'record_id': INSTALLATION, 'revision': 1, 'event_id': PROJECT, 'payload_sha256': 'b' * 64}
        result, requests = self.exchange(method='POST', body=json.dumps({'receipt': receipt}).encode())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['receipt'], receipt)
        self.assertEqual(requests[-1][1]['Idempotency-Key'], 'same-operation')
        self.assertEqual(requests[-1][1]['X-Agent-Name'], 'first-console')

    def test_mutation_without_key_has_zero_http_effects(self):
        result, requests = self.exchange(method='POST', key='')
        self.assertEqual((result.returncode == 0, requests, result.stdout), (False, [], ''))

    def test_2xx_without_receipt_is_not_mutation_success(self):
        result, _ = self.exchange(method='POST', body=b'{"status":"committed"}')
        self.assertNotEqual(result.returncode, 0, '2xx alone is not a committed receipt')
        self.assertEqual(result.stdout, '')

    def test_redirect_is_not_followed(self):
        result, requests = self.exchange(status=307, headers={'Location': '/credential-sink'})
        self.assertNotEqual(result.returncode, 0, 'redirect must refuse')
        self.assertEqual(result.stdout, '')
        self.assertNotIn('/credential-sink', [r[0] for r in requests])

    def test_html_is_not_success(self):
        result, _ = self.exchange(body=b'<html>unrelated service</html>')
        self.assertNotEqual(result.returncode, 0, 'HTML must refuse')
        self.assertEqual(result.stdout, '')

    def test_duplicate_json_keys_are_not_success(self):
        result, _ = self.exchange(body=b'{"record":1,"record":2}')
        self.assertNotEqual(result.returncode, 0, 'ambiguous JSON must refuse')
        self.assertEqual(result.stdout, '')

    def test_oversized_response_is_bounded(self):
        result, _ = self.exchange(body=b'{"data":"' + b'x' * 1048576 + b'"}')
        self.assertNotEqual(result.returncode, 0, 'oversized response must refuse')
        self.assertEqual(result.stdout, '')

    def test_core_refusal_is_preserved(self):
        error = {'code': 'denied', 'reason': 'read_only', 'retryable': False}
        result, _ = self.exchange(status=403, body=json.dumps({'error': error}).encode())
        self.assertEqual(result.returncode, 22)
        self.assertEqual(result.stdout, '')
        self.assertEqual(json.loads(result.stderr), {'error': error})

    def test_timeout_does_not_retry_mutation(self):
        result, requests = self.exchange(method='POST', delay=2.5)
        self.assertNotEqual(result.returncode, 0, 'total command deadline must refuse')
        self.assertEqual(result.stdout, '')
        self.assertEqual([r[0] for r in requests], ['/health', '/records/synthetic'])

    def test_projection_url_with_path_refuses_before_http(self):
        result, requests = self.exchange(url_suffix='/@evil')
        self.assertEqual((result.returncode == 0, requests, result.stdout), (False, [], ''))


if __name__ == '__main__':
    unittest.main()
