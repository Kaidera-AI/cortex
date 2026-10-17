"""Day-1 foundation negatives; these never qualify an installed proof.

Finn-owned assertions from the accepted DAY-1 / INDEX J04 and J11 contracts.
Repository MIT licence. Baseline: Cortex 7a1078213ac2497d6219e7778c7866001f97f30b.
No supplier, vault store, credential or admission is manufactured here.
The loopback recorder observes the existing helper's transport effects only.
"""
import importlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest


ROOT = Path(__file__).resolve().parents[5]
HELPER = ROOT / 'packages/cli/_cortex_api.sh'


class SeedFoundationTests(unittest.TestCase):
    def test_installed_binding_requires_private_custody(self):
        # A missing seed module is an ERROR, never a causal BODY kill. Once the
        # owned entry point exists, this assertion requires a typed refusal.
        seed = importlib.import_module('cortex_core.schema.seed')
        with self.assertRaises(Exception) as observed:
            seed.seed_runtime_bindings(None, 'synthetic-day1-operation', time.monotonic() + 5)
        self.assertTrue(
            isinstance(getattr(observed.exception, 'reason', None), str),
            'installed_binding: missing private config/custody must produce a typed refusal',
        )

    def test_three_command_memory_missing_projection_has_zero_http_effects(self):
        # This is a missing-admission negative, not a fake successful memory API.
        # Legacy project/API environment variables cannot replace Core config.
        requests = []

        class Recorder(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                requests.append((self.command, self.path))
                body = json.dumps({'project_id': '00000000-0000-0000-0000-000000000001'}).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(('127.0.0.1', 0), Recorder)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory(prefix='finn-day1-negative-') as temp:
                env = {
                    'PATH': os.environ['PATH'],
                    'HOME': temp,
                    'TMPDIR': temp,
                    'CORTEX_PROJECT_ROOT': temp,
                    'CORTEX_AGENTS_DIR': temp,
                    'CORTEX_RUNTIME_CONFIG': str(Path(temp) / 'absent-runtime.yaml'),
                    'CORTEX_WORKSPACE_CONFIG': str(Path(temp) / 'absent-workspace.json'),
                    'CORTEX_PROJECT': 'synthetic-project',
                    'CORTEX_API_URL': f'http://127.0.0.1:{server.server_port}',
                }
                # Sourcing alone must not discover/probe v1 before the sole
                # private Core client projection is available. No real service
                # or database is contacted, and no successful write is faked.
                result = subprocess.run(
                    ['/bin/bash', '-c', 'source "$1"', 'finn-negative', str(HELPER)],
                    env=env, cwd=temp, capture_output=True, text=True, timeout=10,
                )
            self.assertEqual(
                requests, [],
                'three_command_memory: missing Core client projection must refuse before any HTTP request; '
                f'helper exit={result.returncode}, observed={requests!r}',
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive(), 'owned recorder must be reaped')


if __name__ == '__main__':
    unittest.main()
