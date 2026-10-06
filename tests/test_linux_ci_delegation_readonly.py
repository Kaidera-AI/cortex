"""R288 read-only CI delegation and exact Ubuntu runner split; native calls intercepted."""
import base64
import importlib.util
import json
import os
import subprocess
import sys
import time

import pytest
import yaml

from test_linux_ci_delegation import product, ROOT
from test_linux_ci_diagnostics import engine_fixture

STAGES = ['systemctl_delegate', 'engine_info', 'engine_version']
PUBLIC_EXCEPTION_MARKER = 'PUBLIC_R288_CAPTURE_EXCEPTION_TEST'


def fixture(monkeypatch):
    module = product(monkeypatch)
    calls = []
    properties = b'Delegate=yes\nDelegateControllers=cpu memory pids\n'
    def capture(prefix, args, *, input_data=None, timeout=90, public=False):
        assert input_data is None and public is False and 0 < timeout <= 5
        allowed = [(['systemctl'], ['show', 'user@' + str(os.getuid()) + '.service', '-p', 'Delegate', '-p', 'DelegateControllers']),
                   (['podman', '--remote=false'], ['info', '--format=json']),
                   (['podman', '--remote=false'], ['version', '--format=json'])]
        assert (list(prefix), list(args)) == allowed[len(calls)]
        calls.append((list(prefix), list(args), input_data))
        stdout = properties if len(calls) == 1 else (json.dumps(engine_fixture()).encode() if len(calls) == 2 else b'{"Client":{"Version":"6.1.3"}}')
        return {'exit_code': 0, 'stdout': stdout, 'stderr': b'public readonly diagnostic\n', 'overflow': False, 'timed_out': False}
    def mutating(*args, **kwargs):
        raise AssertionError('legacy privileged delegation path cannot run')
    monkeypatch.setattr(module, 'prepare_delegation', mutating)
    monkeypatch.setattr(module, 'observe_delegation', mutating)
    return module, calls, capture


def observe(module, capture):
    assert hasattr(module, 'observe_readonly_delegation'), 'R288 fixed read-only delegation check is absent'
    return module.observe_readonly_delegation(capture=capture)


@pytest.mark.parametrize('target', ['linux-x86_64', 'macos-arm64'])
def test_unchanged_guard_accepts_hypothetical_images_only_runner_split(target):
    path = ROOT / 'scripts/release/dispatch-once.py'
    spec = importlib.util.spec_from_file_location('r288_guard_product', path)
    guard = importlib.util.module_from_spec(spec); spec.loader.exec_module(guard)
    raw = (ROOT / '.github/workflows/cortex-linux-candidate.yml').read_text()
    proposed = raw.replace('  images:\n    needs: identity\n    runs-on: ubuntu-22.04', '  images:\n    needs: identity\n    runs-on: ubuntu-24.04', 1)
    jobs = yaml.load(proposed, Loader=yaml.BaseLoader)['jobs']
    assert jobs['images']['runs-on'] == 'ubuntu-24.04' and jobs['host']['runs-on'] == 'ubuntu-22.04'
    texts = {'cortex-linux-candidate.yml': proposed, 'cortex-candidate.yml': (ROOT / '.github/workflows/cortex-candidate.yml').read_text()}
    guard.check_routes(texts, target, 'd' * 40)
    assert guard.push_can_trigger(proposed, 'ren-cx/linux-package-build-admitted-' + 'd' * 40)


def test_actual_workflow_has_exact_split_and_explicit_readonly_before_build():
    raw = (ROOT / '.github/workflows/cortex-linux-candidate.yml').read_text()
    jobs = yaml.load(raw, Loader=yaml.BaseLoader)['jobs']
    assert {n: j['runs-on'] for n, j in jobs.items()} == {'identity': 'ubuntu-22.04', 'images': 'ubuntu-24.04', 'host': 'ubuntu-22.04', 'package': 'ubuntu-22.04'}
    steps = jobs['images']['steps']
    check = next(s for s in steps if 'linux_ci_delegation.py' in s.get('run', ''))
    assert '--read-only' in check['run']
    assert raw.index('linux_ci_runtime.py') < raw.index('linux_ci_delegation.py') < raw.index('Build immutable OCI images')
    assert not any(s in check['run'] for s in ('sudo', 'restart', 'daemon-reload', 'tee', 'mkdir'))


CASES = ['valid', 'cpu', 'memory', 'pids', 'v1', 'rootful', 'engine-field', 'delegate-no',
         'delegate-cpu', 'duplicate-property', 'extra-property', 'property-crlf', 'property-utf8',
         'property-overflow', 'duplicate-info', 'nonfinite-info', 'malformed-info', 'duplicate-version', 'deadline']


@pytest.mark.parametrize('case', CASES)
def test_readonly_properties_and_actual_engine_controller_gate(case, monkeypatch):
    module, calls, original = fixture(monkeypatch)
    clock = module.time.monotonic
    started = clock()
    if case == 'deadline':
        monkeypatch.setattr(module.time, 'monotonic', lambda: started + (20 if len(calls) else 0))
    def capture(prefix, args, **kwargs):
        result = original(prefix, args, **kwargs)
        if len(calls) == 1:
            if case == 'delegate-no': result['stdout'] = b'Delegate=no\nDelegateControllers=cpu memory pids\n'
            if case == 'delegate-cpu': result['stdout'] = b'Delegate=yes\nDelegateControllers=memory pids\n'
            if case == 'duplicate-property': result['stdout'] += b'Delegate=yes\n'
            if case == 'extra-property': result['stdout'] += b'Extra=value\n'
            if case == 'property-crlf': result['stdout'] = result['stdout'].replace(b'\n', b'\r\n')
            if case == 'property-utf8': result['stdout'] += b'\xff\n'
            if case == 'property-overflow': result['stdout'] = b'x' * 4097
        elif len(calls) == 2:
            info = engine_fixture()
            if case in ('cpu', 'memory', 'pids'): info['host']['cgroupControllers'].remove(case)
            if case == 'v1': info['host']['cgroupVersion'] = 'v1'
            if case == 'rootful': info['host']['security']['rootless'] = False
            if case == 'engine-field': info['host']['ociRuntime']['path'] = '/untrusted/public-test'
            result['stdout'] = json.dumps(info).encode()
            if case == 'duplicate-info': result['stdout'] = b'{"host":{},' + result['stdout'][1:]
            if case == 'nonfinite-info': result['stdout'] = b'{"ignored":NaN,' + result['stdout'][1:]
            if case == 'malformed-info': result['stdout'] = b'not-json'
        elif case == 'duplicate-version': result['stdout'] = b'{"Client":{},"Client":{"Version":"6.1.3"}}'
        return result
    value = observe(module, capture)
    assert value['uid'] == os.getuid() and value['required_controllers'] == ['cpu', 'memory', 'pids']
    assert all(prefix in (['systemctl'], ['podman', '--remote=false']) and data is None for prefix, args, data in calls)
    assert not any('sudo' in prefix or 'restart' in args for prefix, args, data in calls)
    if case == 'valid':
        assert value['status'] == 'ready' and len(calls) == 3
        assert [s['stage'] for s in value['steps']] == STAGES
        assert value['engine']['cgroup_controllers'] == ['cpu', 'memory', 'pids']
        assert value['systemd'] == {'delegate': True, 'controllers': ['cpu', 'memory', 'pids']}
        assert base64.b64decode(value['facts']['systemctl_delegate']['stdout_b64']) == b'Delegate=yes\nDelegateControllers=cpu memory pids\n'
    else:
        assert value['status'] == 'refused' and len(calls) <= 3
        if case in ('cpu', 'memory', 'pids', 'v1', 'rootful'):
            assert value['engine'] and len(calls) == 3
        if case.startswith('delegate-') or case.startswith('property-') or case in ('duplicate-property', 'extra-property', 'deadline'):
            assert len(calls) == 1
    assert 'discard this field' not in json.dumps(value) and 'never publish this' not in json.dumps(value)


@pytest.mark.parametrize('stage', STAGES)
@pytest.mark.parametrize('kind', ['exit', 'timeout', 'overflow', 'stderr-overflow', 'bad-result', 'runtime', 'timeout-exception', 'non-dict'])
def test_each_readonly_capture_failure_retains_fixed_partial_proof(stage, kind, monkeypatch):
    module, calls, original = fixture(monkeypatch); index = STAGES.index(stage)
    def capture(prefix, args, **kwargs):
        result = original(prefix, args, **kwargs)
        if len(calls) == index + 1:
            if kind == 'exit': result['exit_code'] = 125
            if kind == 'timeout': result['timed_out'] = True
            if kind == 'overflow': result['overflow'] = True
            if kind == 'stderr-overflow': result['stderr'] = b'x' * 4097
            if kind == 'bad-result': result['exit_code'] = True
            if kind == 'runtime': raise RuntimeError(PUBLIC_EXCEPTION_MARKER)
            if kind == 'timeout-exception': raise subprocess.TimeoutExpired(PUBLIC_EXCEPTION_MARKER, 1)
            if kind == 'non-dict': return None
        return result
    value = observe(module, capture)
    assert value['status'] == 'refused' and value['reason'] == 'command_refused'
    assert len(calls) == index + 1 and [s['stage'] for s in value['steps']] == STAGES[:index + 1]
    step = value['steps'][-1]
    assert step['exit_code'] == (125 if kind == 'exit' else None if kind in ('bad-result', 'runtime', 'timeout-exception', 'non-dict') else 0)
    assert step['launch_status'] == ('unknown' if kind in ('bad-result', 'runtime', 'timeout-exception', 'non-dict') else 'started')
    if kind == 'stderr-overflow':
        assert step['stderr_truncated'] is True and step['overflow'] is True and len(base64.b64decode(step['stderr_b64'])) == 4096
    assert PUBLIC_EXCEPTION_MARKER not in json.dumps(value)


@pytest.mark.parametrize('uid,euid', [(0, 0), (1001, 0)])
def test_readonly_root_or_mismatched_identity_refuses_before_commands(uid, euid, monkeypatch):
    module, calls, capture = fixture(monkeypatch)
    monkeypatch.setattr(module.os, 'getuid', lambda: uid); monkeypatch.setattr(module.os, 'geteuid', lambda: euid)
    value = observe(module, capture)
    assert value['status'] == 'refused' and not calls


@pytest.mark.parametrize('case', ['ready', 'exit', 'runtime'])
def test_actual_readonly_cli_writes_source_bound_partial_or_ready_artifact(case, tmp_path, monkeypatch, capsys):
    module, calls, original = fixture(monkeypatch)
    assert hasattr(module, 'observe_readonly_delegation'), 'R288 readonly CLI observer is absent'
    def capture(prefix, args, **kwargs):
        result = original(prefix, args, **kwargs)
        if len(calls) == 2:
            if case == 'exit': result['exit_code'] = 1
            if case == 'runtime': raise RuntimeError(PUBLIC_EXCEPTION_MARKER)
        return result
    monkeypatch.setattr(module, 'capture_command', capture)
    output = tmp_path / 'public'; source = 'e' * 40
    monkeypatch.setattr(sys, 'argv', ['linux_ci_delegation.py', '--read-only', '--source-sha', source, '--output', str(output)])
    if case == 'ready': module.main()
    else:
        with pytest.raises(SystemExit) as error: module.main()
        assert error.value.code == 1
    value = json.loads((output / 'delegation.json').read_bytes())
    assert value['source_sha'] == source and value['status'] == ('ready' if case == 'ready' else 'refused')
    assert len(calls) == (3 if case == 'ready' else 2)
    assert (output / 'delegation.json').stat().st_mode & 0o777 == 0o600
    streams = capsys.readouterr(); text = streams.out if case == 'ready' else streams.err
    assert json.loads(text) == value and PUBLIC_EXCEPTION_MARKER not in text and 'Traceback' not in text


@pytest.mark.parametrize('size', [4096, 4097])
def test_readonly_stderr_preserves_bounded_lossless_bytes(size, monkeypatch):
    module, calls, original = fixture(monkeypatch)
    marker = (b'\xff\x1b' + b'x' * size)[:size]
    def capture(prefix, args, **kwargs):
        result = original(prefix, args, **kwargs)
        if len(calls) == 1: result['stderr'] = marker
        return result
    value = observe(module, capture); step = value['steps'][0]
    assert base64.b64decode(step['stderr_b64']) == marker[:4096]
    assert step['stderr_truncated'] is (size > 4096)
    assert value['status'] == ('refused' if size > 4096 else 'ready')
