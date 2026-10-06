"""R283 public fixed-command observations; all native commands intercepted."""
import base64
import json
import os
import sys

import pytest

from test_linux_ci_delegation import product, fixture as legacy_fixture
from test_linux_ci_diagnostics import engine_fixture

STAGES = ['loginctl_user', 'systemctl_user', 'mkdir_dropin', 'write_dropin',
          'reload_systemd', 'restart_user_manager', 'engine_info', 'engine_version']


def fixture(tmp_path, monkeypatch):
    module = product(monkeypatch)
    path = tmp_path / 'cgroup'; path.write_bytes(b'0::/actions_job/public.scope\n')
    calls = []
    def capture(prefix, args, *, input_data=None, timeout=90, public=False):
        calls.append((list(prefix), list(args), input_data))
        if args == ['info', '--format=json']: raw = json.dumps(engine_fixture()).encode()
        elif args == ['version', '--format=json']: raw = b'{"Client":{"Version":"6.1.3"}}'
        elif prefix == ['loginctl']: raw = b'Linger=no\nState=active\n'
        elif prefix == ['systemctl']: raw = b'ActiveState=active\nSubState=running\nResult=success\nControlGroup=/user.slice/public.service\n'
        else: raw = b''
        return {'stdout': raw, 'stderr': b'public fixed-command diagnostic\n',
                'exit_code': 0, 'timed_out': False, 'overflow': False}
    return module, path, calls, capture


def observe(module, path, capture):
    assert hasattr(module, 'observe_delegation'), 'stage-bound public delegation observation is absent'
    return module.observe_delegation(cgroup_path=path, capture=capture)


def test_three_exact_public_facts_precede_every_mutation_and_all_stages_are_fixed(tmp_path, monkeypatch):
    module, path, calls, capture = fixture(tmp_path, monkeypatch)
    value = observe(module, path, capture)
    assert value['status'] == 'ready' and value['job_outside_user_manager'] is True
    assert [s['stage'] for s in value['steps']] == STAGES
    assert calls[:2] == [
        (['loginctl'], ['show-user', str(os.getuid()), '-p', 'Linger', '-p', 'State'], None),
        (['systemctl'], ['show', 'user@' + str(os.getuid()) + '.service', '-p', 'ActiveState', '-p', 'SubState', '-p', 'Result', '-p', 'ControlGroup'], None)]
    assert 'mkdir' in calls[2][1] and 'restart' in calls[5][1]
    assert base64.b64decode(value['facts']['cgroup']['stdout_b64']) == path.read_bytes()
    assert base64.b64decode(value['facts']['loginctl_user']['stdout_b64']) == b'Linger=no\nState=active\n'
    assert b'ControlGroup=' in base64.b64decode(value['facts']['systemctl_user']['stdout_b64'])
    for step in value['steps']:
        assert step['exit_code'] == 0 and step['timed_out'] is False and step['overflow'] is False
        assert step['launch_status'] == 'started'
        assert base64.b64decode(step['stderr_b64']) == b'public fixed-command diagnostic\n'
        assert step['stderr_truncated'] is False
    assert 'process' not in json.dumps(value['facts']).lower()


@pytest.mark.parametrize('stage', STAGES[2:])
@pytest.mark.parametrize('condition', ['exit', 'timeout', 'overflow', 'spawn', 'malformed'])
def test_each_failure_has_a_public_stage_status_and_stderr_without_later_commands(stage, condition, tmp_path, monkeypatch):
    module, path, calls, original = fixture(tmp_path, monkeypatch)
    index = STAGES.index(stage)
    def capture(prefix, args, **kwargs):
        result = original(prefix, args, **kwargs)
        if len(calls) - 1 == index:
            if condition == 'spawn': raise OSError('untrusted exception text must not become output')
            if condition == 'exit': result['exit_code'] = 125
            if condition == 'timeout': result['timed_out'] = True
            if condition == 'overflow': result['overflow'] = True
            if condition == 'malformed': result['exit_code'] = True
        return result
    value = observe(module, path, capture)
    assert value['status'] == 'refused' and value['reason'] == 'command_refused'
    assert len(calls) == index + 1
    assert [s['stage'] for s in value['steps']] == STAGES[:index + 1]
    step = value['steps'][-1]
    assert step['exit_code'] == (125 if condition == 'exit' else None if condition in ('spawn', 'malformed') else 0)
    assert step['launch_status'] == ('unknown' if condition in ('spawn', 'malformed') else 'started')
    if condition == 'timeout': assert step['timed_out'] is True
    if condition == 'overflow': assert step['overflow'] is True
    if condition != 'spawn': assert base64.b64decode(step['stderr_b64']) == b'public fixed-command diagnostic\n'
    assert 'untrusted exception text' not in json.dumps(value)


@pytest.mark.parametrize('fact', ['loginctl_user', 'systemctl_user'])
@pytest.mark.parametrize('condition', ['exit', 'stdout-overflow'])
def test_unavailable_public_facts_refuse_before_privileged_commands(fact, condition, tmp_path, monkeypatch):
    module, path, calls, original = fixture(tmp_path, monkeypatch)
    index = STAGES.index(fact)
    def capture(prefix, args, **kwargs):
        result = original(prefix, args, **kwargs)
        if len(calls) - 1 == index:
            if condition == 'exit': result['exit_code'] = 1
            else: result['stdout'] = b'x' * 4097
        return result
    value = observe(module, path, capture)
    assert value['status'] == 'refused' and value['reason'] == 'public_facts_unavailable'
    assert len(calls) == index + 1 and not any('sudo' in prefix for prefix, _, _ in calls)
    if condition == 'stdout-overflow':
        assert len(base64.b64decode(value['facts'][fact]['stdout_b64'])) == 4096
        assert value['facts'][fact]['stdout_truncated'] is True


@pytest.mark.parametrize('size', [4096, 4097])
def test_public_stderr_is_lossless_with_explicit_four_kib_limit(size, tmp_path, monkeypatch):
    module, path, calls, original = fixture(tmp_path, monkeypatch)
    marker = (b'\xff\x1b' + b'x' * size)[:size]
    def capture(prefix, args, **kwargs):
        result = original(prefix, args, **kwargs)
        if len(calls) == 3: result['stderr'] = marker
        return result
    value = observe(module, path, capture)
    step = value['steps'][2]
    assert base64.b64decode(step['stderr_b64']) == marker[:4096]
    assert step['stderr_truncated'] is (size > 4096)
    assert value['status'] == ('refused' if size > 4096 else 'ready')
    if size > 4096: assert step['overflow'] is True and len(calls) == 3


@pytest.mark.parametrize('raw', [b'0::/user.slice/user@1001.service/job\n', b'0::/public.scope\r\n'])
def test_observation_cannot_bypass_inherited_placement_fences(raw, tmp_path, monkeypatch):
    module, path, calls, capture = fixture(tmp_path, monkeypatch); path.write_bytes(raw)
    value = observe(module, path, capture)
    assert value['status'] == 'refused'
    assert not any('sudo' in prefix for prefix, _, _ in calls)


def test_actual_cli_artifact_keeps_failed_command_evidence_and_exit_one(tmp_path, monkeypatch, capsys):
    module, path, calls, original = fixture(tmp_path, monkeypatch)
    def capture(prefix, args, **kwargs):
        result = original(prefix, args, **kwargs)
        if len(calls) == 4: result['exit_code'] = 1
        return result
    observer = module.observe_delegation if hasattr(module, 'observe_delegation') else None
    assert observer is not None, 'CLI stage-bound observer is absent'
    monkeypatch.setattr(module, 'observe_delegation', lambda **kwargs: observer(cgroup_path=path, capture=capture))
    output = tmp_path / 'public'; source = 'b' * 40
    monkeypatch.setattr(sys, 'argv', ['linux_ci_delegation.py', '--source-sha', source, '--output', str(output)])
    with pytest.raises(SystemExit) as error: module.main()
    assert error.value.code == 1
    value = json.loads((output / 'delegation.json').read_bytes())
    assert value['source_sha'] == source and value['steps'][-1]['stage'] == 'write_dropin'
    assert value['steps'][-1]['exit_code'] == 1 and value['status'] == 'refused'
    assert (output / 'delegation.json').stat().st_mode & 0o777 == 0o600
    assert 'public fixed-command diagnostic' in capsys.readouterr().err


def test_legacy_private_prepare_still_discards_arbitrary_stderr(tmp_path, monkeypatch):
    module, path, calls, capture, info, private = legacy_fixture(tmp_path, monkeypatch)
    value = module.prepare_delegation(cgroup_path=path, capture=capture)
    assert len(calls) == 6 and private not in json.dumps(value).encode()
