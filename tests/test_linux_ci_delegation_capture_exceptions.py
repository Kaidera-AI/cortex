"""R283-FA1: unexpected capture errors retain the fixed public refusal artifact."""
import json
import subprocess
import sys

import pytest

from test_linux_ci_delegation_observability import STAGES, fixture, observe

PUBLIC_EXCEPTION_MARKER = 'PUBLIC_CAPTURE_EXCEPTION_TEST_MARKER'


def unexpected_capture(original, calls, stage, kind):
    index = STAGES.index(stage)
    def capture(prefix, args, **kwargs):
        result = original(prefix, args, **kwargs)
        if len(calls) - 1 == index:
            if kind == 'runtime':
                raise RuntimeError(PUBLIC_EXCEPTION_MARKER)
            raise subprocess.TimeoutExpired(PUBLIC_EXCEPTION_MARKER, 1)
        return result
    return capture


@pytest.mark.parametrize('stage', ['loginctl_user', 'mkdir_dropin', 'engine_info'])
@pytest.mark.parametrize('kind', ['runtime', 'timeout-expired'])
def test_unexpected_capture_error_retains_partial_stages_and_stops_later_commands(stage, kind, tmp_path, monkeypatch):
    module, path, calls, original = fixture(tmp_path, monkeypatch)
    capture = unexpected_capture(original, calls, stage, kind)
    value = observe(module, path, capture)
    index = STAGES.index(stage)
    assert value['status'] == 'refused'
    assert value['reason'] == ('public_facts_unavailable' if index < 2 else 'command_refused')
    assert len(calls) == index + 1
    assert [s['stage'] for s in value['steps']] == STAGES[:index + 1]
    step = value['steps'][-1]
    assert step['launch_status'] == 'unknown'
    assert step['exit_code'] is None and step['timed_out'] is None and step['overflow'] is None
    assert step['stderr_available'] is False and step['stderr_b64'] == ''
    assert 'cgroup' in value['facts']
    assert PUBLIC_EXCEPTION_MARKER not in json.dumps(value)
    if index < 2:
        assert not any('sudo' in prefix for prefix, _, _ in calls)


def test_actual_cli_writes_partial_artifact_on_unexpected_capture_error(tmp_path, monkeypatch, capsys):
    module, path, calls, original = fixture(tmp_path, monkeypatch)
    capture = unexpected_capture(original, calls, 'mkdir_dropin', 'runtime')
    observer = module.observe_delegation
    monkeypatch.setattr(module, 'observe_delegation', lambda **kwargs: observer(cgroup_path=path, capture=capture))
    output = tmp_path / 'public'; source = 'c' * 40
    monkeypatch.setattr(sys, 'argv', ['linux_ci_delegation.py', '--source-sha', source, '--output', str(output)])
    with pytest.raises(SystemExit) as error:
        module.main()
    assert error.value.code == 1 and len(calls) == 3
    value = json.loads((output / 'delegation.json').read_bytes())
    assert value['source_sha'] == source and value['status'] == 'refused'
    assert value['reason'] == 'command_refused'
    assert [s['stage'] for s in value['steps']] == STAGES[:3]
    assert value['steps'][-1]['launch_status'] == 'unknown'
    assert (output / 'delegation.json').stat().st_mode & 0o777 == 0o600
    streams = capsys.readouterr()
    assert streams.out == '' and PUBLIC_EXCEPTION_MARKER not in streams.err
    assert 'Traceback' not in streams.err
    assert json.loads(streams.err) == value
