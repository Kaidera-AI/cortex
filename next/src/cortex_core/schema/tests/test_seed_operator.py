"""Actual seed/operator/client/helper source join; DB and HTTP are fixtures.

The sole seed callable and Core dispatcher/custody run unchanged. No production
authority is injected. These controls supply no PostgreSQL or installed permit.
"""
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import subprocess
import threading
import unittest

from cortex_core.runtime import operator
from cortex_core.runtime.tests.test_bootstrap_config import sample
import test_seed


class SeedOperatorCompositionTests(unittest.TestCase):
    def config_file(self, case, port=8501):
        root = Path(case.config.vault_root)
        (root/'config').mkdir(mode=0o700, exist_ok=True)
        value = sample(root)
        value['roles']['installer_enrollment']['login'] = 'kaidera-runtime-core-installer'
        value['publish']['port'] = port
        path = root/'config/cortex.json'
        path.write_text(json.dumps(value)); path.chmod(0o600)
        return path

    def invoke(self, case, path, *, client=False):
        unlock, writer = os.pipe()
        os.write(writer, bytes(range(32))); os.close(writer)  # PUBLIC source fixture
        read = output = None
        args = (['config', 'client'] if client else ['seed']) + [
            '--config', str(path), '--unlock-fd', str(unlock), '--key-id', 'unit-public-key']
        if client:
            read, output = os.pipe()
            args += ['--projection-fd', str(output)]
        else:
            args += ['--operation-key', case.operation]
        out, err = io.StringIO(), io.StringIO()
        try:
            with redirect_stdout(out), redirect_stderr(err):
                code = operator.main(args)
            raw = os.read(read, 8192) if client else None
            return code, out.getvalue(), err.getvalue(), raw
        finally:
            os.close(unlock)
            if read is not None:
                os.close(read)  # Core owns/closes the projection writer.

    def test_actual_seed_caller_reloads_committed_exact_receipt_after_cleanup(self):
        controls = test_seed.DurableSeedTests()
        with controls.scenario() as c:
            path = self.config_file(c)
            first = self.invoke(c, path)
            self.assertEqual(first[0], 0, 'actual seed operator must succeed: ' + first[2])
            material = controls.material(c)
            self.assertIsNotNone(material, 'actual seed must produce durable custody')
            self.assertEqual(material.state, 'committed')
            packet = json.loads(first[1])
            self.assertEqual(packet['receipt'], material.receipt)
            self.assertEqual(len(packet['receipt']), 8)
            self.assertEqual(first[2], '')
            self.assertEqual([e[1] for e in c.db.events if e[0] == 'close'],
                             ['api_request', 'installer_enrollment', 'bootstrap'])
            before = (material.intent, material.credential, material.receipt, material.generation)
            second = self.invoke(c, path)
            self.assertEqual(second[:3], first[:3])
            after = controls.material(c)
            self.assertEqual((after.intent, after.credential, after.receipt, after.generation), before)
            self.assertEqual(c.db.enrollments, 1)
            self.assertNotIn(material.credential.decode(), first[1] + first[2])

    def test_actual_seed_readiness_failure_has_no_success_or_client_projection(self):
        controls = test_seed.DurableSeedTests()
        with controls.scenario() as c:
            path = self.config_file(c)
            c.db.readiness = 'unavailable'
            code, out, err, _ = self.invoke(c, path)
            self.assertEqual(code, 2)
            self.assertEqual(out, '')
            material = controls.material(c)
            self.assertIsNotNone(material, 'readiness failure must retain actual PREPARED intent')
            self.assertEqual(material.state, 'prepared')
            self.assertEqual(json.loads(err)['error']['reason'], 'seed_readiness')
            code, out, err, raw = self.invoke(c, path, client=True)
            self.assertEqual((code, out, raw), (2, '', b''))
            self.assertEqual(json.loads(err)['error']['reason'], 'custody_uncommitted')

    def test_actual_seed_then_private_client_pipe_reaches_existing_http_helper(self):
        requests = []
        installation = '10000000-0000-0000-0000-000000000001'
        class Recorder(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass
            def do_GET(self):
                requests.append((self.path, dict(self.headers)))
                packet = ({'component': 'cortex', 'status': 'ok', 'readiness': 'ready', 'installation_id': installation}
                          if self.path == '/health' else {'record': {'id': 'source-fixture'}})
                raw = json.dumps(packet).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers(); self.wfile.write(raw)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Recorder)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            controls = test_seed.DurableSeedTests()
            with controls.scenario() as c:
                path = self.config_file(c, server.server_port)
                code, out, err, _ = self.invoke(c, path)
                self.assertEqual(code, 0, 'actual seed must precede client/helper: ' + err)
                code, public, err, wire = self.invoke(c, path, client=True)
                self.assertEqual((code, public, err), (0, '', ''))
                read, write = os.pipe(); os.write(write, wire); os.close(write)
                try:
                    env = {**os.environ, 'CORTEX_CLIENT_PROJECTION_FD': str(read)}
                    result = subprocess.run(['/bin/bash', '-c',
                        'source "$1"; cortex_api_call GET /records/source',
                        'finn-seed-client', str(test_seed.HELPER)], pass_fds=(read,),
                        env=env, capture_output=True, text=True, timeout=10)
                finally:
                    os.close(read)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout), {'record': {'id': 'source-fixture'}})
                self.assertEqual([r[0] for r in requests], ['/health', '/records/source'])
                self.assertNotIn('Authorization', requests[0][1])
                secret = controls.material(c).credential.decode()
                self.assertEqual(requests[1][1]['Authorization'], 'Bearer ' + secret)
                self.assertNotIn(secret, out + err + result.stdout + result.stderr)
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)
            self.assertFalse(thread.is_alive(), 'owned loopback recorder must be reaped')


if __name__ == '__main__':
    unittest.main()
