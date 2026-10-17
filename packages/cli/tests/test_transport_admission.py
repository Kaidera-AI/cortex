"""Day-2 HTTP admission negatives; synthetic loopback, no installed credit.

MIT. Finn's H-D622 scope preserves the sole packaged shell helper.
No successful Core projection is manufactured by these tests.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest


HELPER = Path(__file__).resolve().parents[1] / '_cortex_api.sh'


class TransportAdmissionTests(unittest.TestCase):
    def test_request_without_private_projection_refuses_before_http(self):
        requests = []

        class Recorder(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def respond(self):
                requests.append((self.command, self.path))
                length = int(self.headers.get('Content-Length', '0'))
                if length:
                    self.rfile.read(length)
                body = json.dumps({'project_id': '00000000-0000-0000-0000-000000000001'}).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = respond
            do_POST = respond

        server = ThreadingHTTPServer(('127.0.0.1', 0), Recorder)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory(prefix='finn-day2-admission-') as temp:
                env = {
                    'PATH': os.environ['PATH'], 'HOME': temp, 'TMPDIR': temp,
                    'CORTEX_PROJECT_ROOT': temp, 'CORTEX_AGENTS_DIR': temp,
                    'CORTEX_RUNTIME_CONFIG': str(Path(temp) / 'absent-runtime.yaml'),
                    'CORTEX_WORKSPACE_CONFIG': str(Path(temp) / 'absent-workspace.json'),
                    'CORTEX_PROJECT': 'synthetic-project',
                    'CORTEX_API_URL': f'http://127.0.0.1:{server.server_port}',
                    'CORTEX_ADMIN_TOKEN': 'synthetic-legacy-token-is-not-core-admission',
                }
                # Legacy URL/project/admin environment may not authorize a
                # daily mutation without the one Core private projection.
                result = subprocess.run(
                    ['/bin/bash', '-c',
                     'source "$1"; cortex_api_call POST /memory/write \'{"text":"synthetic"}\' first-console',
                     'finn-admission', str(HELPER)],
                    cwd=temp, env=env, capture_output=True, text=True, timeout=10,
                )
            self.assertEqual(
                (result.returncode == 0, requests, result.stdout), (False, [], ''),
                'private_projection_required: absent Core projection must refuse with no HTTP '
                f'or successful output; exit={result.returncode}, requests={requests!r}',
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive(), 'owned HTTP recorder must be reaped')


if __name__ == '__main__':
    unittest.main()
