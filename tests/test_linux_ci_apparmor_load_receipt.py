"""R301-AA-001: retain successful load evidence while every later guard refuses."""
import json

import pytest

from test_linux_ci_apparmor_userns import fixture, TEXT, PRIVILEGED

DRIFTS = ['path', 'uid', 'euid', 'deadline']
CASES = [stage + '-' + drift for stage in ('before', 'load', 'after') for drift in DRIFTS]
CASES += ['parser-' + error for error in ('exit', 'timeout', 'overflow', 'exception', 'bool-exit', 'stdout-shape')]
CASES += ['repair', 'working-info']


@pytest.mark.parametrize('case', CASES)
def test_successful_parser_acknowledgement_survives_postcheck_refusal(case, monkeypatch):
    module, calls, selected, original = fixture(monkeypatch)
    started = module.time.monotonic()

    def capture(prefix, args, **kw):
        result = original(prefix, args, **kw)
        stage = calls[-1][0]
        if stage == 'engine_info_before' and case == 'working-info':
            result.update(exit_code=0, stderr=b'')
        changed_stage = {'before': 'engine_info_before', 'load': 'profile_load', 'after': 'engine_info_after'}
        drift_stage, _, drift = case.partition('-')
        if stage == changed_stage.get(drift_stage):
            if drift == 'path': selected['identity'] = (1, 2, 3, 5)
            if drift == 'uid': monkeypatch.setattr(module.os, 'getuid', lambda: 1002)
            if drift == 'euid': monkeypatch.setattr(module.os, 'geteuid', lambda: 0)
            if drift == 'deadline': monkeypatch.setattr(module.time, 'monotonic', lambda: started + 200)
        if stage == 'profile_load' and case.startswith('parser-'):
            if case == 'parser-exit': result.update(exit_code=1, stderr=b'PUBLIC parser refusal\n')
            if case == 'parser-timeout': result['timed_out'] = True
            if case == 'parser-overflow': result['overflow'] = True
            if case == 'parser-exception': raise RuntimeError('PUBLIC intercepted parser refusal')
            if case == 'parser-bool-exit': result['exit_code'] = False
            if case == 'parser-stdout-shape': result['stdout'] = 'invalid capture stdout'
        return result

    value = module.prepare_userns(capture=capture)
    loaded = case == 'repair' or case.startswith(('load-', 'after-'))
    assert value['profile_loaded'] is loaded, 'successful parser acknowledgement was erased by the later guard'
    assert value['status'] == ('ready' if case in ('repair', 'working-info') else 'refused')
    policy = [call for call in calls if call[0] == 'profile_load']
    attempted = case != 'working-info' and not case.startswith('before-')
    assert len(policy) == int(attempted)
    if policy:
        assert policy[0][1:4] == (PRIVILEGED + ['apparmor_parser'], ['-r'], TEXT)
    postflight = case == 'repair' or case.startswith('after-')
    assert sum(call[0] == 'engine_info_after' for call in calls) == int(postflight)
    if case.startswith('load-'):
        assert calls[-1][0] == 'profile_load'
        assert value['steps'][-1]['exit_code'] == 0
        assert value['steps'][-1]['timed_out'] is False
        assert value['steps'][-1]['overflow'] is False
    if case.startswith('before-'):
        assert calls[-1][0] == 'engine_info_before'
    assert 'discard this field' not in json.dumps(value)
    assert not any('-w' in call[2] or 'restart' in call[2] for call in calls)
