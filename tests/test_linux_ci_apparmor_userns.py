"""R295 exact-path policy effects are intercepted; loaded attachments use real fixtures."""
import base64
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / 'scripts/release/linux_ci_apparmor.py'
REAL = '/home/linuxbrew/.linuxbrew/Cellar/podman/6.1.3/bin/podman'
TEXT = ('abi <abi/4.0>,\ninclude <tunables/global>\n\n"' + REAL +
        '" flags=(unconfined) {\n  userns,\n}\n').encode()
PRIVILEGED = ['sudo', '-n', 'timeout', '--kill-after=2s', '20s']
STAGES = ['restriction', 'enabled', 'engine_info_before', 'profile_load', 'engine_info_after']
MARKER = 'PUBLIC_R295_CAPTURE_EXCEPTION_TEST'


def product(monkeypatch):
    assert PATH.is_file(), 'conditional exact-path AppArmor helper is absent'
    monkeypatch.syspath_prepend(str(PATH.parent))
    spec = importlib.util.spec_from_file_location('r295_apparmor_product', PATH)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    monkeypatch.setattr(module, 'native_context', lambda target: None)
    monkeypatch.setattr(module.os, 'getuid', lambda: 1001)
    monkeypatch.setattr(module.os, 'geteuid', lambda: 1001)
    return module


def fixture(monkeypatch):
    module = product(monkeypatch); calls = []
    selected = {'selected_path': '/home/linuxbrew/.linuxbrew/bin/podman',
                'resolved_path': REAL, 'mode': '0755', 'identity': (1, 2, 3, 4)}
    monkeypatch.setattr(module, '_selected_podman', lambda: dict(selected))
    def capture(prefix, args, *, input_data=None, timeout=90, public=False):
        assert public is False and 0 < timeout <= 25
        prefix, args = list(prefix), list(args)
        if prefix == ['sysctl']:
            assert args == ['kernel.apparmor_restrict_unprivileged_userns']; stage = 'restriction'; out = b'kernel.apparmor_restrict_unprivileged_userns = 1\n'
        elif prefix == ['cat']:
            assert args == ['/sys/module/apparmor/parameters/enabled']; stage = 'enabled'; out = b'Y\n'
        elif prefix == PRIVILEGED + ['apparmor_parser']:
            assert args == ['-r'] and input_data == TEXT; stage = 'profile_load'; out = b''
        else:
            assert prefix == [REAL, '--remote=false'] and args == ['info', '--format=json']
            stage = 'engine_info_after' if any(c[0] == 'engine_info_before' for c in calls) else 'engine_info_before'
            out = b'{"Host":{"not_a_public_projection":"discard this field"}}'
        if stage != 'profile_load': assert input_data is None
        calls.append((stage, prefix, args, input_data))
        return {'exit_code': 1 if stage == 'engine_info_before' else 0, 'stdout': out,
                'stderr': b'failed to reexec: Permission denied\n' if stage == 'engine_info_before' else b'',
                'overflow': False, 'timed_out': False}
    return module, calls, selected, capture


CASES = ['valid', 'restriction-off', 'disabled', 'covered', 'bad-sysctl', 'bad-enabled',
         'unrelated-info-error', 'partial-info-error', 'bad-path', 'unsafe-mode', 'root', 'euid',
         'path-replacement', 'uid-drift', 'deadline', 'info-refused']


@pytest.mark.parametrize('case', CASES)
def test_conditional_profile_requires_all_public_preconditions_and_exact_effect(case, monkeypatch):
    module, calls, selected, original = fixture(monkeypatch)
    if case == 'bad-path': selected['resolved_path'] += '" { / ** rw, }'
    if case == 'unsafe-mode': selected['mode'] = '0777'
    if case == 'root': monkeypatch.setattr(module.os, 'getuid', lambda: 0)
    if case == 'euid': monkeypatch.setattr(module.os, 'geteuid', lambda: 0)
    clock = module.time.monotonic; started = clock()
    if case == 'deadline': monkeypatch.setattr(module.time, 'monotonic', lambda: started + (200 if calls else 0))
    def capture(prefix, args, **kw):
        result = original(prefix, args, **kw); stage = calls[-1][0]
        if stage == 'restriction':
            if case == 'restriction-off': result['stdout'] = b'kernel.apparmor_restrict_unprivileged_userns = 0\n'
            if case == 'bad-sysctl': result['stdout'] = b'kernel.apparmor_restrict_unprivileged_userns = 1\nextra\n'
            if case == 'path-replacement': selected['identity'] = (1, 2, 3, 5)
            if case == 'uid-drift': monkeypatch.setattr(module.os, 'getuid', lambda: 1002)
        if stage == 'enabled':
            if case == 'disabled': result['stdout'] = b'N\n'
            if case == 'bad-enabled': result['stdout'] = b'Y\r\n'
        if stage == 'engine_info_before':
            if case == 'covered': result.update(exit_code=0, stderr=b'')
            if case == 'unrelated-info-error': result['stderr'] = b'Error: disk unavailable\n'
            if case == 'partial-info-error': result['stderr'] = b'failed to reexec: Permission denied suffix\n'
        if case == 'info-refused' and stage == 'engine_info_after':
            result['exit_code'] = 1; result['stderr'] = b'failed to reexec: Permission denied\n'
        return result
    value = module.prepare_userns(capture=capture)
    success = case in ('valid', 'covered')
    assert value['status'] == ('ready' if success else 'refused')
    loaded = [x for x in calls if x[0] == 'profile_load']
    assert len(loaded) == (1 if case in ('valid', 'info-refused') else 0)
    if success:
        assert calls[-1][0] == ('engine_info_after' if case == 'valid' else 'engine_info_before')
        assert value['facts']['podman'] == {'selected_path': selected['selected_path'], 'resolved_path': REAL, 'mode': '0755'}
        assert value['profile_loaded'] is (case == 'valid')
    if case in ('root', 'euid', 'bad-path', 'unsafe-mode'): assert not calls
    assert 'discard this field' not in json.dumps(value)
    assert not any('-w' in x[2] or 'restart' in x[2] or 'sysctl' in x[1] and len(x[2]) != 1 for x in calls)


@pytest.mark.parametrize('stage', STAGES + ['engine_info_before_success'])
@pytest.mark.parametrize('kind', ['exit', 'timeout', 'overflow', 'stderr-overflow', 'non-dict', 'exception'])
def test_fixed_command_refusal_retains_partial_bounded_evidence_without_retry(stage, kind, monkeypatch):
    module, calls, _, original = fixture(monkeypatch)
    def capture(prefix, args, **kw):
        result = original(prefix, args, **kw)
        actual_stage = 'engine_info_before' if stage == 'engine_info_before_success' else stage
        if stage == 'engine_info_before_success' and calls[-1][0] == 'engine_info_before':
            result.update(exit_code=0, stderr=b'')
        if calls[-1][0] == actual_stage:
            if kind == 'exit': result.update(exit_code=1, stderr=b'PUBLIC_R301_UNRELATED_EXIT\n')
            if kind == 'timeout': result['timed_out'] = True
            if kind == 'overflow': result['overflow'] = True
            if kind == 'stderr-overflow': result['stderr'] = b'x' * 4097
            if kind == 'non-dict': return None
            if kind == 'exception': raise RuntimeError(MARKER)
        return result
    value = module.prepare_userns(capture=capture)
    assert value['status'] == 'refused' and value['reason'] == 'command_refused'
    actual_stage = 'engine_info_before' if stage == 'engine_info_before_success' else stage
    assert [x[0] for x in calls] == STAGES[:STAGES.index(actual_stage) + 1]
    step = value['steps'][-1]; assert step['stage'] == actual_stage
    if kind == 'stderr-overflow':
        assert step['stderr_truncated'] and len(base64.b64decode(step['stderr_b64'])) == 4096
    assert MARKER not in json.dumps(value)


@pytest.mark.parametrize('size', [4096, 4097])
def test_public_fixed_command_stderr_is_lossless_and_bounded(size, monkeypatch):
    module, calls, _, original = fixture(monkeypatch); raw = (b'\xff\x1b' + b'x' * size)[:size]
    def capture(prefix, args, **kw):
        value = original(prefix, args, **kw)
        if calls[-1][0] == 'restriction': value['stderr'] = raw
        return value
    value = module.prepare_userns(capture=capture)
    assert base64.b64decode(value['steps'][0]['stderr_b64']) == raw[:4096]
    assert value['status'] == ('ready' if size == 4096 else 'refused')


ATTACHMENTS = [REAL, '/usr/bin/podman', '/home/*/podman/**', '/{home,opt}/linuxbrew/.linuxbrew/Cellar/podman/**',
               '/usr/{bin,sbin}/podman', 'podman', 'unconfined', '<unknown>', '/@{HOME}/podman',
               '/home/**/podman', '/home/linuxbrew/.linuxbrew/Cellar/podman/6.1.3/bin/podma?', '/home/\\*']


@pytest.mark.parametrize('attachment', ATTACHMENTS)
def test_actual_loaded_attachment_metadata_is_authoritative_and_conservative(attachment, tmp_path, monkeypatch):
    module = product(monkeypatch)
    policy = tmp_path / 'profiles'; policy.mkdir(); profile = policy / 'p0'; profile.mkdir()
    (profile / 'attach').write_text(attachment + '\n')
    expected = attachment in (REAL, ATTACHMENTS[2], ATTACHMENTS[3], ATTACHMENTS[9], ATTACHMENTS[10])
    if attachment in ('<unknown>', '/@{HOME}/podman', '/home/\\*'):
        with pytest.raises(module.AppArmorRefused): module.profile_coverage(REAL, policy_root=policy)
    else:
        assert module.profile_coverage(REAL, policy_root=policy) == {'covered': expected, 'profile_count': 1}


@pytest.mark.parametrize('case', ['missing', 'symlink', 'oversized', 'changed', 'postflight-info-refusal'])
def test_unobserved_or_replaced_coverage_never_authorizes_a_policy_change(case, tmp_path, monkeypatch):
    module, calls, _, original = fixture(monkeypatch)
    if case == 'postflight-info-refusal':
        def capture(prefix, args, **kw):
            result = original(prefix, args, **kw)
            if calls[-1][0] == 'engine_info_after': result.update(exit_code=1, stderr=b'failed to reexec: Permission denied\n')
            return result
        value = module.prepare_userns(capture=capture)
        assert value['status'] == 'refused' and calls[-1][0] == 'engine_info_after'
        assert len([x for x in calls if x[0] == 'profile_load']) == 1
        return
    policy = tmp_path / 'profiles'; policy.mkdir(); profile = policy / 'p0'; profile.mkdir()
    path = profile / 'attach'
    if case == 'symlink':
        target = tmp_path / 'attachment'; target.write_text('/usr/bin/podman\n'); path.symlink_to(target)
    elif case == 'oversized': path.write_text('/' + 'x' * 4096 + '\n')
    elif case == 'changed':
        path.write_text('/usr/bin/podman\n'); original_snapshot = module._attachment_snapshot; count = []
        def snapshot(root):
            count.append(True)
            if len(count) == 2: path.write_text(REAL + '\n')
            return original_snapshot(root)
        monkeypatch.setattr(module, '_attachment_snapshot', snapshot)
    with pytest.raises(module.AppArmorRefused): module.profile_coverage(REAL, policy_root=policy)


@pytest.mark.parametrize('case', ['ready', 'exit', 'exception'])
def test_actual_cli_emits_source_bound_artifact_and_partial_refusal(case, tmp_path, monkeypatch, capsys):
    module, calls, _, original = fixture(monkeypatch)
    def capture(prefix, args, **kw):
        value = original(prefix, args, **kw)
        if calls[-1][0] == 'profile_load':
            if case == 'exit': value['exit_code'] = 1
            if case == 'exception': raise RuntimeError(MARKER)
        return value
    monkeypatch.setattr(module, 'capture_command', capture)
    monkeypatch.setattr(sys, 'argv', [str(PATH), '--source-sha', 'e' * 40, '--output', str(tmp_path / 'public')])
    if case == 'ready': module.main()
    else:
        with pytest.raises(SystemExit) as error: module.main()
        assert error.value.code == 1
    value = json.loads((tmp_path / 'public/apparmor.json').read_bytes())
    assert value['source_sha'] == 'e' * 40 and value['status'] == ('ready' if case == 'ready' else 'refused')
    out = capsys.readouterr(); assert json.loads(out.out if case == 'ready' else out.err) == value
    assert MARKER not in out.out + out.err and 'Traceback' not in out.out + out.err


@pytest.mark.parametrize('target', ['linux-x86_64', 'macos-arm64'])
def test_workflow_has_conditional_profile_before_unchanged_postflight_and_guard_routes(target):
    raw = (ROOT / '.github/workflows/cortex-linux-candidate.yml').read_text()
    assert 'linux_ci_apparmor.py' in raw, 'Linux-CI exact-path conditional profile wiring is absent'
    jobs = yaml.load(raw, Loader=yaml.BaseLoader)['jobs']
    assert {n: x['runs-on'] for n, x in jobs.items()} == {'identity': 'ubuntu-22.04', 'images': 'ubuntu-24.04', 'host': 'ubuntu-22.04', 'package': 'ubuntu-22.04'}
    assert raw.index('linux_ci_runtime.py') < raw.index('linux_ci_apparmor.py') < raw.index('linux_ci_delegation.py') < raw.index('Build immutable OCI images')
    assert '--read-only' in next(s['run'] for s in jobs['images']['steps'] if 'linux_ci_delegation.py' in s.get('run', ''))
    spec = importlib.util.spec_from_file_location('r295_guard', ROOT / 'scripts/release/dispatch-once.py')
    guard = importlib.util.module_from_spec(spec); spec.loader.exec_module(guard)
    guard.check_routes({'cortex-linux-candidate.yml': raw, 'cortex-candidate.yml': (ROOT / '.github/workflows/cortex-candidate.yml').read_text()}, target, 'd' * 40)
