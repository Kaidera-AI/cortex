"""Real descriptor witnesses; Linux credit requires running on a Linux kernel."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

import test_health_join as actual
from test_transport_projection import wire


class PipeIdentityTests(unittest.TestCase):
    @contextmanager
    def exchange(self, kind, *, unlink=False):
        status, packet = actual.ActualHealthJoinTests().health('ready')
        self.assertEqual((status, packet['status'], packet['readiness']), (200, 'ok', 'ready'))
        requests = []

        class Recorder(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                requests.append((self.path, dict(self.headers)))
                body = json.dumps(packet if self.path == '/health' else {'source_fixture': True}).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(('127.0.0.1', 0), Recorder)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        reader = writer = None
        try:
            with tempfile.TemporaryDirectory(prefix='finn-pipe-identity-') as temp:
                body = wire(f'http://127.0.0.1:{server.server_port}', **{'2': packet['installation_id']})
                path = Path(temp) / 'projection'
                if kind == 'regular':
                    path.write_bytes(body)
                    reader = os.open(path, os.O_RDONLY)
                elif kind == 'fifo':
                    os.mkfifo(path, 0o600)
                    reader = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
                    writer = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
                else:
                    reader, writer = os.pipe()
                if writer is not None:
                    os.write(writer, body)
                    os.close(writer)
                    writer = None
                if unlink:
                    path.unlink()
                result = subprocess.run(
                    ['/bin/bash', '-c', 'source "$1"; cortex_api_call GET /records/source',
                     'finn-pipe-identity', str(actual.HELPER)],
                    env={**os.environ, 'CORTEX_CLIENT_PROJECTION_FD': str(reader)},
                    pass_fds=(reader,), capture_output=True, text=True, timeout=10)
                self.assertNotIn('a' * 64, result.stdout + result.stderr)
                yield result, requests
        finally:
            if reader is not None:
                os.close(reader)
            if writer is not None:
                os.close(writer)
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive(), 'owned recorder must stop')

    def test_actual_ready_core_health_admits_private_anonymous_pipe(self):
        with self.exchange('anonymous') as (result, requests):
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), {'source_fixture': True})
            self.assertEqual([path for path, _headers in requests], ['/health', '/records/source'])
            self.assertNotIn('Authorization', requests[0][1])
            self.assertEqual(requests[1][1]['Authorization'], 'Bearer ' + 'a' * 64)

    def test_named_fifo_refuses_before_http(self):
        # Linux proc identity also distinguishes an unlinked FIFO from a pipe.
        for unlink in ([False, True] if sys.platform.startswith('linux') else [False]):
            with self.subTest(unlink=unlink), self.exchange('fifo', unlink=unlink) as (result, requests):
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual((requests, result.stdout), ([], ''))

    def test_regular_file_refuses_before_http(self):
        for unlink in (False, True):
            with self.subTest(unlink=unlink), self.exchange('regular', unlink=unlink) as (result, requests):
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual((requests, result.stdout), ([], ''))


if __name__ == '__main__':
    unittest.main()
