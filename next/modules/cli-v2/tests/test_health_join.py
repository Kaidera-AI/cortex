"""Actual bound Core health -> sole helper; explicit SQL/policy/HTTP fixtures.

The health packet is produced by the real Core service, never recreated by the
HTTP recorder. This source join grants no installed or real PostgreSQL credit.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import threading
import unittest

from cortex_core.runtime.tests import test_services

HELPER = Path(__file__).resolve().parents[1]/'_cortex_api.sh'
TOKEN = '12'*32  # Public fixture, not an enrolled credential.


class ActualHealthJoinTests(unittest.TestCase):
    def health(self, state):
        control = test_services.ServiceTests()
        control.setUp()
        try:
            with control.opened() as owned, control.bound(owned) as bound:
                bound.start()
                control.rows['auth.enrollment_readiness'] = [(state,)]
                status, packet = control.request(bound, 'GET', '/health', headers=[])
                return status, packet
        finally:
            control.doCleanups()

    def exchange(self, status, packet):
        requests = []
        class Recorder(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass
            def do_GET(self):
                requests.append((self.path, dict(self.headers)))
                health = self.path == '/health'
                raw = json.dumps(packet if health else {'source_fixture': True}).encode()
                self.send_response(status if health else 200)
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers(); self.wfile.write(raw)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Recorder)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        read, write = os.pipe()
        fields = ['cortex.client.v1', f'http://127.0.0.1:{server.server_port}',
                  packet['installation_id'], '10000000-0000-0000-0000-000000000003',
                  'sample', TOKEN, '3']
        os.write(write, b''.join(s.encode()+b'\0' for s in fields)); os.close(write)
        try:
            result = subprocess.run(['/bin/bash', '-c',
                'source "$1"; cortex_api_call GET /records/source', 'finn-health', str(HELPER)],
                env={**os.environ, 'CORTEX_CLIENT_PROJECTION_FD': str(read)}, pass_fds=(read,),
                capture_output=True, text=True, timeout=10)
            self.assertNotIn(TOKEN, result.stdout+result.stderr)
            return result, requests
        finally:
            os.close(read); server.shutdown(); server.server_close(); thread.join(timeout=2)
            self.assertFalse(thread.is_alive(), 'owned recorder must be reaped')

    def test_actual_ready_core_health_admits_exact_private_identity(self):
        status, packet = self.health('ready')
        self.assertEqual((status, packet['status'], packet['readiness']), (200, 'ok', 'ready'))
        result, requests = self.exchange(status, packet)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([r[0] for r in requests], ['/health', '/records/source'])
        self.assertNotIn('Authorization', requests[0][1])
        self.assertEqual(requests[1][1]['Authorization'], 'Bearer '+TOKEN)

    def test_actual_enrollment_required_core_health_never_receives_bearer(self):
        status, packet = self.health('enrollment_required')
        self.assertEqual((status, packet['status'], packet['readiness']),
                         (200, 'ok', 'enrollment_required'))
        result, requests = self.exchange(status, packet)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual([r[0] for r in requests], ['/health'])
        self.assertNotIn('Authorization', requests[0][1])

    def test_missing_readiness_cannot_be_inferred_from_ok_or_legacy_ready(self):
        status, packet = self.health('ready')
        del packet['readiness']
        for legacy_status in ('ok', 'ready'):
            with self.subTest(status=legacy_status):
                packet['status'] = legacy_status
                result, requests = self.exchange(status, packet)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual([r[0] for r in requests], ['/health'])
                self.assertNotIn('Authorization', requests[0][1])


if __name__ == '__main__':
    unittest.main()
