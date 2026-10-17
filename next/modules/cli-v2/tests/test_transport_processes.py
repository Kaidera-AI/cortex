"""Owned-process secret/cancellation controls; synthetic HTTP only, MIT."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import threading
import unittest

from test_transport_projection import HELPER, INSTALLATION, TOKEN, wire


@contextmanager
def live_client():
    active, release = threading.Event(), threading.Event()

    class Recorder(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            if self.path == '/health':
                body = {'component': 'cortex', 'status': 'ok', 'readiness': 'ready', 'installation_id': INSTALLATION}
            else:
                active.set()
                release.wait(4)
                body = {'record': {'id': 'synthetic'}}
            raw = json.dumps(body).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            try:
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Recorder)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    reader, writer = os.pipe()
    process = None
    try:
        os.write(writer, wire(f'http://127.0.0.1:{server.server_port}', **{'6': '4'}))
        os.close(writer)
        writer = None
        with tempfile.TemporaryDirectory(prefix='finn-process-') as temp:
            env = {'PATH': os.environ['PATH'], 'HOME': temp, 'TMPDIR': temp,
                   'CORTEX_CLIENT_PROJECTION_FD': str(reader)}
            process = subprocess.Popen(
                ['/bin/bash', '-c', 'source "$1"; cortex_api_call GET /records/synthetic "" first-console',
                 'finn-process', str(HELPER)],
                pass_fds=(reader,), env=env, cwd=temp,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
            )
            yield process, active, release
    finally:
        release.set()
        if process is not None:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=3)
        os.close(reader)
        if writer is not None:
            os.close(writer)
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        if thread.is_alive():
            raise RuntimeError('owned HTTP server did not stop')


def owned_commands(root):
    commands, pending = {}, [root]
    while pending:
        pid = pending.pop()
        command = subprocess.run(['/bin/ps', 'eww', '-p', str(pid), '-o', 'command='],
                                 capture_output=True, text=True, timeout=2)
        if command.returncode == 0:
            commands[pid] = command.stdout.strip()
        children = subprocess.run(['/usr/bin/pgrep', '-P', str(pid)],
                                  capture_output=True, text=True, timeout=2)
        pending.extend(int(value) for value in children.stdout.split() if value.isdecimal())
    return commands


class TransportProcessTests(unittest.TestCase):
    def test_private_authentication_has_no_token_in_owned_argv_or_environment(self):
        with live_client() as (process, active, release):
            self.assertTrue(active.wait(2), 'credentialed source request must reach the owned recorder')
            commands = owned_commands(process.pid)
            curls = [value for value in commands.values() if value.startswith('/usr/bin/curl ')]
            self.assertEqual(len(curls), 1, 'observe the actual owned credentialed curl')
            self.assertNotIn(TOKEN, '\n'.join(commands.values()), 'credential must stay out of argv/environment')
            self.assertIn('--config /dev/fd/', curls[0])
            release.set()
            stdout, stderr = process.communicate(timeout=3)
            self.assertEqual(process.returncode, 0, stderr)
            self.assertNotIn(TOKEN.encode(), stdout + stderr)

    def test_cancelling_supervisor_reaps_owned_curl(self):
        with live_client() as (process, active, _release):
            self.assertTrue(active.wait(2), 'credentialed source request must be active')
            commands = owned_commands(process.pid)
            curls = [pid for pid, value in commands.items() if value.startswith('/usr/bin/curl ')]
            supervisors = [pid for pid, value in commands.items() if ' -S - GET /records/synthetic ' in value]
            self.assertEqual(len(curls), 1)
            self.assertEqual(len(supervisors), 1)
            os.kill(supervisors[0], signal.SIGTERM)
            stdout, stderr = process.communicate(timeout=3)
            self.assertNotEqual(process.returncode, 0, 'cancellation cannot report success')
            self.assertEqual(stdout, b'')
            self.assertNotIn(TOKEN.encode(), stdout + stderr)
            self.assertNotEqual(subprocess.run(['/bin/ps', '-p', str(curls[0]), '-o', 'pid='],
                                              capture_output=True, timeout=2).returncode, 0,
                                'owned curl must be reaped before cancellation returns')


if __name__ == '__main__':
    unittest.main()
