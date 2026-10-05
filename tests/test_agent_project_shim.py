"""R92 project shim, real loopback transport, unissued in-memory markers only."""
import importlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from cortex_v2.clients.key_store import KeyStoreError
from test_member_client_integration import FixtureReader, member_profile, receiver  # noqa: F401
from test_client_transport_origin import isolated_transport_environment  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
PROJECT = {
    'project_key': 'fixture-project', 'project_id': 'original-7',
    'display_name': 'Preserved project', 'default_agent': 'worker',
    'status': 'active', 'parent_project_key': 'parent',
    'repo_root': '/fixtures/project', 'created_at': '2026-01-01T00:00:00Z',
    'updated_at': '2026-10-05T00:00:00Z', 'agent_count': 3, 'profile_count': 4,
    'roots': [{'path': '/fixtures/project', 'kind': 'primary', 'repo_type': 'git'},
              {'path': '/fixtures/second', 'kind': 'secondary', 'repo_type': 'git'}],
}


@pytest.fixture
def shim():
    return importlib.import_module('cortex_v2.cli.agent_request')


def invoke(shim, monkeypatch, profile, argv, incoming=''):
    monkeypatch.setattr(shim, 'load_member_profile', lambda *a, **k: profile)
    stdout, stderr = io.StringIO(), io.StringIO()
    result = shim.main(argv, stdin=io.StringIO(incoming), stdout=stdout, stderr=stderr)
    return result, stdout.getvalue(), stderr.getvalue()


def test_reads_once_each_time_and_preserves_raw_body(shim, receiver):
    origin, records = receiver()
    reader = FixtureReader()
    profile = member_profile(origin, reader)
    first = shim.request_projects(profile)
    reader.version = 2
    second = shim.request_projects(profile)
    assert first.body == second.body == b'{"data":{"ok":true}}'
    assert first.headers['cortex-key-expires'] == '2027-10-05T01:00:00+00:00'
    assert reader.reads == 2
    assert [r['version'] for r in records] == [1, 2]
    assert all(r['scope'] == 'fixture-project' and r['path'] == '/projects'
               and r['method'] == 'GET' and r['body'] is None for r in records)


@pytest.mark.parametrize('reason', ['missing', 'locked', 'expired', 'unsafe', 'denied'])
def test_refusal_after_success_does_not_reuse_key(shim, receiver, reason):
    origin, records = receiver()
    reader = FixtureReader()
    profile = member_profile(origin, reader)
    shim.request_projects(profile)
    reader.refusal = reason
    with pytest.raises(shim.ClientConfigError):
        shim.request_projects(profile)
    assert reader.reads == 2 and len(records) == 1


@pytest.mark.parametrize('status', [401, 403, 409, 302])
def test_api_errors_are_one_response_without_retry(shim, receiver, status):
    origin, records = receiver(status=status)
    reader = FixtureReader()
    response = shim.request_projects(member_profile(origin, reader))
    assert response.status == status
    assert reader.reads == 1 and len(records) == 1


@pytest.mark.parametrize('arguments', [
    {'method': 'POST'}, {'path': '//foreign.example/projects'},
    {'path': 'https://foreign.example/projects'}, {'path': '/projects?scope=other'},
    {'path': '/projects#x'}, {'path': '/admin/projects'}, {'payload': {}},
    {'agent_name': 'owner'},
])
def test_unsupported_calls_refuse_before_credential(shim, receiver, arguments):
    origin, records = receiver()
    reader = FixtureReader()
    with pytest.raises(shim.ClientConfigError):
        shim.request_projects(member_profile(origin, reader), **arguments)
    assert reader.reads == 0 and records == []


def legacy_table(tmp_path, data):
    directory = tmp_path / 'frozen'
    directory.mkdir()
    shutil.copyfile(ROOT / 'tests/fixtures/legacy-cortex-projects', directory / 'cortex-projects')
    payload = directory / 'response.json'
    payload.write_text(json.dumps(data))
    (directory / '_cortex_api.sh').write_text(
        'CORTEX_API=http://127.0.0.1:1\ncortex_api_call() { cat "$FIXTURE_RESPONSE"; }\n')
    result = subprocess.run(['bash', str(directory / 'cortex-projects')],
                            env=dict(os.environ, FIXTURE_RESPONSE=str(payload)),
                            capture_output=True, text=True)
    assert result.returncode == 0 and not result.stderr
    return result.stdout


@pytest.mark.parametrize('projects', [[PROJECT], []])
def test_native_projects_matches_exact_frozen_command(shim, tmp_path, projects):
    data = {'projects': projects}
    assert shim.format_projects(data) == legacy_table(tmp_path, data)


@pytest.mark.parametrize('data', [None, {}, {'projects': None}, {'projects': {}},
    {'projects': [{}]}, {'projects': [dict(PROJECT, roots=None)]},
    {'projects': [dict(PROJECT, agent_count=True)]},
    {'projects': [dict(PROJECT, profile_count=-1)]},
    {'projects': [dict(PROJECT, repo_root=None)]},
    {'projects': [dict(PROJECT, roots=[{'kind': 'primary'}])]},
])
def test_malformed_success_is_never_an_empty_success(shim, data):
    with pytest.raises(shim.ClientConfigError):
        shim.format_projects(data)


def test_cli_keeps_raw_api_success_body_and_reads_profile(shim, monkeypatch, receiver):
    origin, records = receiver()
    reader = FixtureReader()
    result, stdout, stderr = invoke(shim, monkeypatch, member_profile(origin, reader),
                                    ['--config', '/fixture/connection.json', 'api', 'GET', '/projects'])
    assert result == 0 and stdout == '{"data":{"ok":true}}' and stderr == ''
    assert reader.reads == 1 and len(records) == 1


@pytest.mark.parametrize('status', [401, 403, 409, 302])
def test_cli_denial_has_no_body_or_credential_on_stdout(shim, monkeypatch, receiver, status):
    origin, records = receiver(status=status)
    reader = FixtureReader()
    result, stdout, stderr = invoke(shim, monkeypatch, member_profile(origin, reader),
                                    ['api', 'GET', '/projects'])
    assert result == 22 and stdout == '' and f'API error {status}' in stderr
    assert 'Bearer' not in stderr and 'A' * 43 not in stderr
    assert reader.reads == 1 and len(records) == 1


def test_cli_local_failure_has_safe_stderr(shim, monkeypatch, receiver):
    origin, records = receiver()
    reader = FixtureReader()
    reader.refusal = 'private-diagnostic-unissued-marker'
    result, stdout, stderr = invoke(shim, monkeypatch, member_profile(origin, reader),
                                    ['api', 'GET', '/projects'])
    assert result == 2 and stdout == ''
    assert 'private-diagnostic' not in stderr and 'Traceback' not in stderr
    assert reader.reads == 1 and records == []


def test_cli_invalid_body_refuses_before_read(shim, monkeypatch, receiver):
    origin, records = receiver()
    reader = FixtureReader()
    result, stdout, stderr = invoke(shim, monkeypatch, member_profile(origin, reader),
                                    ['api', 'GET', '/projects'], incoming='{}')
    assert result == 2 and stdout == '' and reader.reads == 0 and records == []


def test_release_shell_bridge_passes_only_non_secret_arguments(tmp_path):
    directory = tmp_path / 'bin'
    directory.mkdir()
    shutil.copyfile(ROOT / 'scripts/agent-shims/_cortex_api.sh', directory / '_cortex_api.sh')
    runner = directory / 'cortex-agent'
    runner.write_text('#!/usr/bin/env python3\nimport json,sys\nprint(json.dumps(sys.argv[1:]))\n')
    runner.chmod(0o755)
    env = dict(os.environ, CORTEX_CONNECTION_PROFILE='/fixture/profile.json')
    result = subprocess.run(['bash', '-c', 'source "$1"; cortex_api_call GET /projects',
                             'fixture', str(directory / '_cortex_api.sh')],
                            env=env, capture_output=True, text=True)
    assert result.returncode == 0 and result.stderr == ''
    assert json.loads(result.stdout) == ['--config', '/fixture/profile.json', 'api', 'GET', '/projects']
    refused = subprocess.run(['bash', '-c', 'source "$1"; cortex_api_call GET /projects "" "" --location',
                              'fixture', str(directory / '_cortex_api.sh')],
                             env=env, capture_output=True, text=True)
    assert refused.returncode != 0 and refused.stdout == ''


def test_native_build_wiring_inventories_both_programs(tmp_path, monkeypatch):
    path = ROOT / 'scripts/release/build-candidate.py'
    spec = importlib.util.spec_from_file_location('fixture_build_candidate', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    calls = []
    def record(args, *, read=False):
        calls.append(args)
        if 'PyInstaller' in args:
            name = next(a[7:] for a in args if a.startswith('--name='))
            target = tmp_path / 'bin' / name
            target.parent.mkdir(exist_ok=True)
            target.write_bytes(b'unissued-native-build-fixture')
        if args[0] == 'lipo':
            return 'arm64'
        if 'PyInstaller.utils.cliutils.archive_viewer' in args:
            return 'fixture-archive-modules'
        return 'fixture-dependencies' if read else ''
    monkeypatch.setattr(module, 'run', record)
    programs = module.freeze_host_programs(tmp_path)
    assert set(programs) == {'cortex-test', 'cortex-agent'}
    assert (tmp_path / 'agent-archive-inventory.txt').read_text() == 'fixture-archive-modules\n'
    assert all(any(str(tmp_path / 'bin' / name) in command and '--help' in command
                   for command in calls) for name in programs)
    assert (tmp_path / 'bin/cortex-projects').read_bytes() == (ROOT / 'scripts/agent-shims/cortex-projects').read_bytes()
