"""Private-pipe transport refusals from Cox J06; source-only loopback controls.

MIT. Pipe bytes below are explicit synthetic doubles, never Core admission.
"""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest


HELPER = Path(__file__).resolve().parents[1] / '_cortex_api.sh'
INSTALLATION = '00000000-0000-0000-0000-000000000001'
PROJECT = '00000000-0000-0000-0000-000000000002'
TOKEN = 'a' * 64


@contextmanager
def fixture():
    requests = []

    class Recorder(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            requests.append((self.command, self.path))
            body = json.dumps({'project_id': PROJECT}).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Recorder)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix='finn-day2-projection-') as temp:
            url = f'http://127.0.0.1:{server.server_port}'
            env = {'PATH': os.environ['PATH'], 'HOME': temp, 'TMPDIR': temp,
                   'CORTEX_PROJECT_ROOT': temp, 'CORTEX_AGENTS_DIR': temp,
                   'CORTEX_RUNTIME_CONFIG': str(Path(temp) / 'missing.yaml'),
                   'CORTEX_WORKSPACE_CONFIG': str(Path(temp) / 'missing.json'),
                   'CORTEX_PROJECT': 'synthetic', 'CORTEX_API_URL': url}
            yield temp, env, requests, url
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        if thread.is_alive():
            raise RuntimeError('owned recorder did not stop')


def wire(url, **changes):
    fields = ['cortex.client.v1', url, INSTALLATION, PROJECT, 'synthetic', TOKEN, '2']
    for index, value in changes.items():
        fields[int(index)] = value
    return b''.join(value.encode() + b'\0' for value in fields)


class ProjectionRefusalTests(unittest.TestCase):
    def check_refusal(self, body=None, *, regular=False, keep_open=False, command=None):
        with fixture() as (temp, env, requests, url):
            packet = wire(url) if body is None else body(url)
            writer = None
            if regular:
                path = Path(temp) / 'projection'
                path.write_bytes(packet)
                path.chmod(0o600)
                reader = os.open(path, os.O_RDONLY)
            else:
                reader, writer = os.pipe()
                os.write(writer, packet)
                if not keep_open:
                    os.close(writer)
                    writer = None
            try:
                env['CORTEX_CLIENT_PROJECTION_FD'] = str(reader)
                result = subprocess.run(
                    ['/bin/bash', '-c', command or 'source "$1"; cortex_api_call GET /records/synthetic "" first-console',
                     'finn-projection', str(HELPER)],
                    pass_fds=(reader,), env=env, cwd=temp,
                    capture_output=True, text=True, timeout=4,
                )
                self.assertEqual(
                    (result.returncode == 0, requests, result.stdout), (False, [], ''),
                    'unsafe_private_projection: must refuse before HTTP; '
                    f'exit={result.returncode}, requests={requests!r}',
                )
                self.assertNotIn(TOKEN, result.stdout + result.stderr, 'private token escaped into output')
            finally:
                os.close(reader)
                if writer is not None:
                    os.close(writer)

    def test_unknown_protocol_refuses_before_http(self):
        self.check_refusal(lambda url: wire(url, **{'0': 'legacy.client.v1'}))

    def test_header_injection_refuses_before_http(self):
        self.check_refusal(lambda url: wire(url, **{'5': TOKEN + '\r\nInjected: yes'}))

    def test_trailing_field_refuses_before_http(self):
        self.check_refusal(lambda url: wire(url) + b'extra\0')

    def test_regular_file_projection_refuses_before_http(self):
        self.check_refusal(regular=True)

    def test_unterminated_projection_refuses_before_http(self):
        self.check_refusal(lambda url: wire(url)[:-1])

    def test_nonclosing_projection_has_bounded_refusal(self):
        self.check_refusal(keep_open=True)

    def test_legacy_curl_options_cannot_redirect_or_disable_tls(self):
        self.check_refusal(command='source "$1"; cortex_api_call GET /records/synthetic "" first-console --insecure')


if __name__ == '__main__':
    unittest.main()
