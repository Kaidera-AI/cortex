"""R301: functional exact-path precondition; every privileged effect intercepted."""
import inspect
import json

import pytest

from test_linux_ci_apparmor_userns import fixture, STAGES, REAL, PRIVILEGED, MARKER

CASES = ['works', 'reexec', 'prefixed', 'no-newline', 'extra-lines', 'unrelated', 'partial',
         'binary', 'exit-zero-with-marker', 'negative', 'bool-exit', 'stderr-overflow',
         'captured-overflow', 'timeout', 'unknown-result', 'exception', 'restriction-off',
         'disabled', 'parser-exit', 'parser-timeout', 'parser-overflow', 'postflight-exit',
         'postflight-timeout', 'postflight-overflow', 'path-after-before', 'uid-after-before',
         'euid-after-before', 'deadline-after-before', 'facts-path-replacement',
         'parser-exception', 'postflight-exception', 'stderr-shape']


@pytest.mark.parametrize('case', CASES)
def test_functional_info_alone_selects_one_exact_policy_effect_and_no_root_query(case, monkeypatch):
    module, calls, selected, original = fixture(monkeypatch)
    assert '--profiles-for-path' not in inspect.getsource(module.main), 'root profile CLI query is still exposed'
    original_clock = module.time.monotonic; started = original_clock()
    def capture(prefix, args, **kw):
        result = original(prefix, args, **kw); stage = calls[-1][0]
        if stage == 'restriction':
            if case == 'restriction-off': result['stdout'] = b'kernel.apparmor_restrict_unprivileged_userns = 0\n'
            if case == 'facts-path-replacement': selected['identity'] = (1, 2, 3, 5)
        if stage == 'enabled' and case == 'disabled': result['stdout'] = b'N\n'
        if stage == 'engine_info_before':
            if case == 'works': result.update(exit_code=0, stderr=b'')
            if case == 'prefixed': result['stderr'] = b'Error: failed to reexec: Permission denied\n'
            if case == 'no-newline': result['stderr'] = b'failed to reexec: Permission denied'
            if case == 'extra-lines': result['stderr'] = b'Error: cannot clone: Permission denied\nfailed to reexec: Permission denied\n'
            if case == 'unrelated': result['stderr'] = b'Error: storage unavailable\n'
            if case == 'partial': result['stderr'] = b'prefix failed to reexec: Permission denied suffix\n'
            if case == 'binary': result['stderr'] = b'failed to reexec: Permission denied\n\xff'
            if case == 'exit-zero-with-marker': result['exit_code'] = 0
            if case == 'negative': result['exit_code'] = -9
            if case == 'bool-exit': result['exit_code'] = True
            if case == 'stderr-overflow': result['stderr'] = b'failed to reexec: Permission denied\n' + b'x' * 4097
            if case == 'captured-overflow': result['overflow'] = True
            if case == 'timeout': result['timed_out'] = True
            if case == 'unknown-result': return None
            if case == 'exception': raise RuntimeError(MARKER)
            if case == 'path-after-before': selected['identity'] = (1, 2, 3, 5)
            if case == 'uid-after-before': monkeypatch.setattr(module.os, 'getuid', lambda: 1002)
            if case == 'euid-after-before': monkeypatch.setattr(module.os, 'geteuid', lambda: 0)
            if case == 'deadline-after-before': monkeypatch.setattr(module.time, 'monotonic', lambda: started + 200)
            if case == 'stderr-shape': result['stderr'] = 'invalid public stream type'
        if stage == 'profile_load' and case.startswith('parser-'):
            if case.endswith('exit'): result.update(exit_code=1, stderr=b'PUBLIC parser refusal\n')
            if case.endswith('timeout'): result['timed_out'] = True
            if case.endswith('overflow'): result['overflow'] = True
            if case.endswith('exception'): raise RuntimeError(MARKER)
        if stage == 'engine_info_after' and case.startswith('postflight-'):
            if case.endswith('exit'): result.update(exit_code=1, stderr=b'failed to reexec: Permission denied\n')
            if case.endswith('timeout'): result['timed_out'] = True
            if case.endswith('overflow'): result['overflow'] = True
            if case.endswith('exception'): raise RuntimeError(MARKER)
        return result
    value = module.prepare_userns(capture=capture)
    ready = case in ('works', 'reexec', 'prefixed', 'no-newline', 'extra-lines', 'exit-zero-with-marker')
    assert value['status'] == ('ready' if ready else 'refused')
    effects = [x for x in calls if x[0] == 'profile_load']
    expected_load = case in ('reexec', 'prefixed', 'no-newline', 'extra-lines') or case.startswith(('parser-', 'postflight-'))
    assert len(effects) == int(expected_load)
    assert value['profile_loaded'] is (expected_load and not case.startswith('parser-'))
    if effects: assert effects[0][1] == PRIVILEGED + ['apparmor_parser']
    assert all(x[1] != PRIVILEGED + ['/usr/bin/python3'] for x in calls)
    assert sum(x[0] == 'engine_info_after' for x in calls) == int(expected_load and not case.startswith('parser-'))
    assert 'coverage_before' not in value['facts'] and 'coverage_after' not in value['facts']
    assert MARKER not in json.dumps(value) and 'discard this field' not in json.dumps(value)
    assert not any('-w' in x[2] or 'restart' in x[2] for x in calls)
    if case == 'stderr-overflow': assert value['steps'][-1]['stderr_truncated']
