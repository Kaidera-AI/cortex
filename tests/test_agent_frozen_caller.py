"""Execute captured caller and new shim with the source bridge, never issued keys.

This verifies source execution, not a built binary, converted data or installed ACL.
"""
import json
import os
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from test_agent_project_shim import PROJECT

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def project_server():
    records, state = [], {'status': 200}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def do_GET(self):
            records.append({'method': self.command, 'path': self.path,
                            'selected_member': self.headers.get('Authorization') == 'Bearer ' + 'A' * 43,
                            'scope': self.headers.get('X-Cortex-Scope')})
            data = {'projects': [PROJECT]} if state['status'] == 200 else {
                'error': {'code': 'selected_member_denied', 'message': 'denied'}}
            body = json.dumps(data).encode()
            self.send_response(state['status'])
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f'http://127.0.0.1:{server.server_port}', records, state
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)
    assert not thread.is_alive()


def runners(tmp_path, origin):
    directory = tmp_path / 'caller'
    directory.mkdir()
    for source, name in ((ROOT / 'tests/fixtures/legacy-cortex-projects', 'legacy-projects'),
                         (ROOT / 'scripts/agent-shims/cortex-projects', 'cortex-projects'),
                         (ROOT / 'scripts/agent-shims/_cortex_api.sh', '_cortex_api.sh')):
        shutil.copyfile(source, directory / name)
    runner = directory / 'cortex-agent'
    runner.write_text(f'#!{sys.executable}\n'
        'import sys\nfrom cortex_v2.cli import agent_request as bridge\n'
        'from test_member_client_integration import FixtureReader, member_profile\n'
        f'bridge.load_member_profile = lambda *a, **k: member_profile({origin!r}, FixtureReader())\n'
        'raise SystemExit(bridge.main())\n')
    runner.chmod(0o755)
    env = dict(os.environ, PYTHONPATH=str(ROOT / 'src') + os.pathsep + str(ROOT / 'tests'),
               CORTEX_CONNECTION_PROFILE='/fixture/non-secret-member-profile.json',
               http_proxy='http://127.0.0.1:1', https_proxy='http://127.0.0.1:1',
               HTTP_PROXY='http://127.0.0.1:1', HTTPS_PROXY='http://127.0.0.1:1',
               ALL_PROXY='http://127.0.0.1:1', all_proxy='http://127.0.0.1:1', NO_PROXY='', no_proxy='')
    # Verify the helper supplies its own safe error-display label.
    env.pop('CORTEX_API', None)
    return directory, env


def test_captured_caller_and_native_shim_use_real_selected_origin(tmp_path, project_server):
    origin, records, _ = project_server
    directory, env = runners(tmp_path, origin)
    results = [subprocess.run(['bash', str(directory / name)], env=env,
                             capture_output=True, text=True)
               for name in ('legacy-projects', 'cortex-projects')]
    assert all(r.returncode == 0 and not r.stderr for r in results)
    assert results[0].stdout == results[1].stdout and 'Preserved project' in results[0].stdout
    assert len(records) == 2
    assert all(r == {'method': 'GET', 'path': '/projects', 'selected_member': True,
                     'scope': 'fixture-project'} for r in records)


def test_captured_caller_denial_remains_normal_error_without_secret(tmp_path, project_server):
    origin, records, state = project_server
    state['status'] = 403
    directory, env = runners(tmp_path, origin)
    result = subprocess.run(['bash', str(directory / 'legacy-projects')], env=env,
                            capture_output=True, text=True)
    assert result.returncode == 1 and result.stdout == ''
    assert 'API error 403: selected_member_denied' in result.stderr
    assert 'unbound variable' not in result.stderr and 'A' * 43 not in result.stderr
    assert len(records) == 1 and records[0]['selected_member']
