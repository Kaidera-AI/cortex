"""R295-AA-001: malformed real attachment fixtures cannot authorize a parser effect."""
import json

import pytest

from test_linux_ci_apparmor_userns import fixture, REAL, STAGES

CASES = [('blank', b'\n'), ('whitespace', b' \n'), ('relative', b'home/**/podman\n'),
         ('variable', b'@{HOME}/podman\n'), ('unknown-marker', b'<unknown>suffix\n'),
         ('named-control', b'podman\n')]


@pytest.mark.parametrize('case,raw', CASES)
def test_actual_metadata_refuses_before_any_policy_or_engine_effect(case, raw, tmp_path, monkeypatch):
    module, calls, _, original = fixture(monkeypatch)
    policy = tmp_path / 'profiles'; policy.mkdir(); entry = policy / 'p0'; entry.mkdir()
    attachment = entry / 'attach'; attachment.write_bytes(raw)
    try:
        direct = module.profile_coverage(REAL, policy_root=policy)
    except module.AppArmorRefused as error:
        direct = {'refusal': error.code}

    def capture(prefix, args, **kw):
        result = original(prefix, args, **kw); stage = calls[-1][0]
        if stage.startswith('coverage_'):
            try:
                result['stdout'] = json.dumps(module.profile_coverage(REAL, policy_root=policy)).encode()
            except module.AppArmorRefused:
                result['exit_code'] = 1
                result['stdout'] = b'{"covered":null,"profile_count":0}'
        if stage == 'profile_load':
            # Public fixture effect mirrors loading the exact generated attachment.
            attachment.write_text(REAL + '\n')
        return result

    value = module.prepare_userns(capture=capture)
    if case == 'named-control':
        assert direct == {'covered': False, 'profile_count': 1}
        assert value['status'] == 'ready' and value['profile_loaded'] is True
        assert [c[0] for c in calls] == STAGES
        assert attachment.read_text() == REAL + '\n'
    else:
        assert direct == {'refusal': 'profile_coverage_unavailable'}, 'malformed metadata became confirmed uncovered'
        assert value['status'] == 'refused' and value['profile_loaded'] is False
        assert [c[0] for c in calls] == ['restriction', 'enabled', 'coverage_before']
        assert attachment.read_bytes() == raw
    assert len([c for c in calls if c[0] == 'profile_load']) == (1 if case == 'named-control' else 0)
    assert len([c for c in calls if c[0] == 'engine_info']) == (1 if case == 'named-control' else 0)
