"""Canonical health refusals through the sole helper; source fixtures only."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import subprocess
import threading
import unittest

import test_health_join as actual
from test_transport_projection import wire


class CanonicalHealthTests(unittest.TestCase):
    def packet(self):
        status, packet = actual.ActualHealthJoinTests().health('ready')
        self.assertEqual(status, 200)
        return packet

    def refusal(self, packet, *, installation, status=200, raw=None):
        requests = []

        class Recorder(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                requests.append((self.path, dict(self.headers)))
                health = self.path == '/health'
                body = (raw if raw is not None else json.dumps(packet).encode()) if health else b'{"fixture":true}'
                self.send_response(status if health else 200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(('127.0.0.1', 0), Recorder)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        reader, writer = os.pipe()
        os.write(writer, wire(f'http://127.0.0.1:{server.server_port}', **{'2': installation}))
        os.close(writer)
        try:
            result = subprocess.run(
                ['/bin/bash', '-c', 'source "$1"; cortex_api_call GET /records/fixture',
                 'finn-health-contract', str(actual.HELPER)],
                env={**os.environ, 'CORTEX_CLIENT_PROJECTION_FD': str(reader)},
                pass_fds=(reader,), capture_output=True, text=True, timeout=10)
            self.assertNotEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, '')
            self.assertEqual([r[0] for r in requests], ['/health'])
            self.assertNotIn('Authorization', requests[0][1])
        finally:
            os.close(reader)
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive(), 'owned recorder must stop')

    def test_extra_field_refuses_before_bearer(self):
        packet = self.packet()
        installation = packet['installation_id']
        for status in ('ok', 'ready'):
            with self.subTest(status=status):
                self.refusal({**packet, 'status': status, 'extra': 'unreviewed'}, installation=installation)

    def test_wrong_component_and_status_types_refuse_before_bearer(self):
        packet = self.packet()
        for field, values in (('component', ('other', None, True, ['cortex'])),
                              ('status', (None, True, ['ok']))):
            for value in values:
                with self.subTest(field=field, value=value):
                    self.refusal({**packet, field: value}, installation=packet['installation_id'])

    def test_wrong_readiness_type_refuses_before_bearer(self):
        packet = self.packet()
        for value in (None, True, 1, ['ready'], {'state': 'ready'}):
            for status in ('ok', 'ready'):
                with self.subTest(value=value, status=status):
                    self.refusal({**packet, 'status': status, 'readiness': value},
                                 installation=packet['installation_id'])

    def test_nonready_states_refuse_before_bearer(self):
        packet = self.packet()
        for state in ('enrollment_required', 'unavailable', 'starting', 'READY', ''):
            for status in ('ok', 'ready'):
                with self.subTest(state=state, status=status):
                    self.refusal({**packet, 'status': status, 'readiness': state},
                                 installation=packet['installation_id'])

    def test_legacy_status_is_never_a_readiness_alternative(self):
        packet = self.packet()
        self.refusal({**packet, 'status': 'ready'}, installation=packet['installation_id'])

    def test_duplicate_keys_and_malformed_health_refuse_before_bearer(self):
        packet = self.packet()
        raw = json.dumps(packet).encode()
        for body in (raw[:-1]+b',"readiness":"ready"}', b'[]', b'null', b'{', b'{"status":NaN}'):
            with self.subTest(body=body):
                self.refusal(packet, raw=body, installation=packet['installation_id'])

    def test_http_200_is_required_before_bearer(self):
        packet = self.packet()
        for status in (201, 204, 301, 503):
            with self.subTest(status=status):
                self.refusal(packet, status=status, installation=packet['installation_id'])

    def test_canonical_wrong_installation_refuses_before_bearer(self):
        packet = self.packet()
        for value in ('20000000-0000-0000-0000-000000000001', None, True, [packet['installation_id']]):
            with self.subTest(value=value):
                self.refusal({**packet, 'installation_id': value}, installation=packet['installation_id'])


if __name__ == '__main__':
    unittest.main()
