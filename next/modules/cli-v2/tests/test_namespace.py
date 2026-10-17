"""Version-path contract and actual command serialization, loopback fixtures only."""
from contextlib import contextmanager
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import threading
import unittest
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[4]
MODULE = ROOT/'next/modules/cli-v2'
INSTALLATION = '10000000-0000-0000-0000-000000000001'
PROJECT = '10000000-0000-0000-0000-000000000003'
TOKEN = '12' * 32  # Public fixture.
KEY = '10000000-0000-0000-0000-000000000009'
ID = '10000000-0000-0000-0000-000000000010'
RECEIPT = dict(record_id=ID, revision=1,
               event_id='10000000-0000-0000-0000-000000000011', payload_sha256='a'*64)


class VersionedNamespaceTests(unittest.TestCase):
    @contextmanager
    def scenario(self):
        requests = []
        class Recorder(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass
            def do_GET(self):
                requests.append((self.command, self.path, dict(self.headers), None))
                if self.path == '/health':
                    packet = dict(component='cortex', status='ready', installation_id=INSTALLATION)
                elif self.path.startswith('/verify/write?'):
                    packet = {'row': {'summary': 'line one\nΩ\n', 'event_type': 'commit',
                                      'files': ['same.py', 'same.py']}}
                else:
                    packet = {'source_fixture': self.path}
                raw = json.dumps(packet).encode()
                self.send_response(200); self.send_header('Content-Length', str(len(raw)))
                self.end_headers(); self.wfile.write(raw)
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                requests.append((self.command, self.path, dict(self.headers), body))
                raw = json.dumps(dict(logged=True, id=ID, verified=True, receipt=RECEIPT)).encode()
                self.send_response(200); self.send_header('Content-Length', str(len(raw)))
                self.end_headers(); self.wfile.write(raw)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Recorder)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            yield server.server_port, requests
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)
            self.assertFalse(thread.is_alive(), 'owned recorder must be reaped')

    def invoke(self, name, args, port, *, projection=True):
        entry = MODULE/('cortex-v2-' + name)
        self.assertTrue(entry.is_file(), 'v2 namespace entry must exist before invocation')
        read = None
        env = {k:v for k,v in os.environ.items() if not k.startswith('CORTEX_')}
        env.update(CORTEX_API='http://127.0.0.1:1', CORTEX_AUTH_TOKEN='legacy-untrusted',
                   CORTEX_PROJECT='legacy', CORTEX_PARENT_GOAL_ID='must-not-prepend',
                   CORTEX_IDEMPOTENCY_KEY=KEY)
        if projection:
            read, write = os.pipe()
            fields = ['cortex.client.v1', f'http://127.0.0.1:{port}', INSTALLATION,
                      PROJECT, 'helix', TOKEN, '3']
            os.write(write, b''.join(v.encode()+b'\0' for v in fields)); os.close(write)
            env['CORTEX_CLIENT_PROJECTION_FD'] = str(read)
        try:
            return subprocess.run(['/bin/bash', str(entry), *args], env=env,
                                  pass_fds=(() if read is None else (read,)),
                                  capture_output=True, text=True, timeout=10)
        finally:
            if read is not None:
                os.close(read)

    def test_v1_helper_is_exact_baseline_and_v2_is_separate(self):
        raw = (ROOT/'packages/cli/_cortex_api.sh').read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(),
                         'd78ad0c87ffdf286c69b2485d3224186decc81b37608a4ae92fe40806d84f636')
        self.assertTrue((MODULE/'_cortex_api.sh').is_file(), 'strict helper has one v2 source home')

    def test_boot_entry_preserves_agent_budget_query_and_private_authority(self):
        with self.scenario() as (port, requests):
            result = self.invoke('boot', ['FINN@legacy', '--budget', '300'], port)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual([r[1] for r in requests], ['/health', '/boot/finn?budget=300'])
            self.assertNotIn('Authorization', requests[0][2])
            self.assertEqual(requests[1][2]['Authorization'], 'Bearer '+TOKEN)
            self.assertEqual(requests[1][2]['X-Project'], 'helix')
            self.assertNotIn(TOKEN, result.stdout+result.stderr)

    def test_search_entry_sends_explicit_keyword_contract(self):
        with self.scenario() as (port, requests):
            result = self.invoke('search', ['quoted Ω'], port)
            self.assertEqual(result.returncode, 0, result.stderr)
            path = urlsplit(requests[-1][1])
            self.assertEqual(path.path, '/search')
            self.assertEqual(parse_qs(path.query), dict(q=['quoted Ω'], mode=['keyword'],
                type=['all'], hall=['project'], limit=['20'], rerank=['false'], graph=['false']))
            self.assertNotIn(TOKEN, result.stdout+result.stderr)

    def test_log_entry_preserves_strings_files_key_and_verified_readback(self):
        with self.scenario() as (port, requests):
            result = self.invoke('log', ['finn', 'commit', 'line one\nΩ\n', 'same.py', 'same.py'], port)
            self.assertEqual(result.returncode, 0, result.stderr)
            post = next(r for r in requests if r[0] == 'POST')
            self.assertEqual(post[1], '/log')
            self.assertEqual(post[3], dict(event_type='commit', summary='line one\nΩ\n',
                                          files_affected=['same.py', 'same.py']))
            self.assertEqual(post[2]['Idempotency-Key'], KEY)
            self.assertEqual(post[2]['X-Agent-Name'], 'finn')
            self.assertEqual(requests[-1][1], '/verify/write?kind=team_event&id='+ID)
            self.assertNotIn(TOKEN, result.stdout+result.stderr)

    def test_three_entries_without_private_projection_have_zero_http_effects(self):
        with self.scenario() as (port, requests):
            for name, args in [('boot', ['finn']), ('search', ['needle']),
                               ('log', ['finn', 'commit', 'note'])]:
                with self.subTest(command=name):
                    result = self.invoke(name, args, port, projection=False)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(requests, [])


if __name__ == '__main__':
    unittest.main()
