"""R277: intercepted CI setup, actual bounded public cgroup fixture bytes."""
import importlib.util
import json
import os
from pathlib import Path
import secrets
import sys

import pytest

from test_linux_ci_diagnostics import engine_fixture, product as diagnostics_product

ROOT = Path(__file__).resolve().parents[1]
DROPIN = b'[Service]\nDelegate=cpu cpuset io memory pids\n'
PRIVILEGED = ['sudo', '-n', 'timeout', '--kill-after=2s', '20s']


def product(monkeypatch):
    path = ROOT / 'scripts/release/linux_ci_delegation.py'
    assert path.is_file(), 'accepted effective CPU delegation helper is absent'
    monkeypatch.syspath_prepend(str(path.parent))
    spec = importlib.util.spec_from_file_location('r277_delegation_test_product', path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    monkeypatch.setattr(module, 'native_context', lambda target: None)
    return module


def fixture(tmp_path, monkeypatch, *, raw=b'0::/actions_job/cortex-build.scope\n'):
    module = product(monkeypatch)
    path = tmp_path / 'cgroup'; path.write_bytes(raw)
    calls = []; private = secrets.token_urlsafe(32).encode(); info = engine_fixture()
    def capture(prefix, args, *, input_data=None, timeout=90, public=False):
        assert 0 < timeout <= 25 and public is False
        calls.append((list(prefix), list(args), input_data))
        stdout = json.dumps(info).encode() if args == ['info', '--format=json'] else (
            b'{"Client":{"Version":"6.1.3"}}' if args == ['version', '--format=json'] else b'')
        return {'exit_code': 0, 'stdout': stdout, 'stderr': private,
                'overflow': False, 'timed_out': False}
    return module, path, calls, capture, info, private


def test_outside_job_enables_exact_current_uid_then_checks_actual_engine(tmp_path, monkeypatch):
    module, path, calls, capture, info, private = fixture(tmp_path, monkeypatch)
    value = module.prepare_delegation(cgroup_path=path, capture=capture)
    assert calls == [
        (PRIVILEGED, ['mkdir', '-p', '/etc/systemd/system/user@.service.d'], None),
        (PRIVILEGED, ['tee', '/etc/systemd/system/user@.service.d/90-cortex-ci-delegate.conf'], DROPIN),
        (PRIVILEGED, ['systemctl', 'daemon-reload'], None),
        (PRIVILEGED, ['systemctl', 'restart', 'user@' + str(os.getuid()) + '.service'], None),
        (['podman', '--remote=false'], ['info', '--format=json'], None),
        (['podman', '--remote=false'], ['version', '--format=json'], None),
    ]
    assert value['schema'] == 'cortex.linux-ci-delegation.v1'
    assert value['status'] == 'ready' and value['job_outside_user_manager'] is True
    assert value['uid'] == os.getuid() and value['required_controllers'] == ['cpu', 'memory', 'pids']
    assert value['engine']['cgroup_controllers'] == ['cpu', 'memory', 'pids']
    assert private not in json.dumps(value).encode()


@pytest.mark.parametrize('raw', [
    b'0::/user.slice/user-1001.slice/user@1001.service/app.slice/job.scope\n',
    b'0::/user.slice/user-501.slice/user@501.service/job.scope\n',
    b'0::/user@other.service/job\n', b'', b'garbage\n', b'1:cpu:/job\n',
    b'0::relative\n', b'0::/job/../other\n', b'0::/job//other\n',
    b'0::/job\n0::/other\n', b'0::/job\x00\n', b'0::/\xff\n', b'x' * 65537,
])
def test_inside_unknown_or_unbounded_job_placement_refuses_before_privileged_effects(raw, tmp_path, monkeypatch):
    module, path, calls, capture, info, private = fixture(tmp_path, monkeypatch, raw=raw)
    with pytest.raises(module.DelegationRefused) as error:
        module.prepare_delegation(cgroup_path=path, capture=capture)
    assert not calls
    assert private not in str(error.value).encode()


@pytest.mark.parametrize('condition', ['cpu', 'memory', 'pids', 'v1', 'rootful', 'unknown-field'])
def test_missing_effective_delegation_or_unsafe_engine_refuses_before_build(condition, tmp_path, monkeypatch):
    module, path, calls, capture, info, private = fixture(tmp_path, monkeypatch)
    if condition in ('cpu', 'memory', 'pids'): info['host']['cgroupControllers'].remove(condition)
    elif condition == 'v1': info['host']['cgroupVersion'] = 'v1'
    elif condition == 'rootful': info['host']['security']['rootless'] = False
    else: info['host']['ociRuntime']['path'] = '/private/' + private.decode()
    with pytest.raises(module.DelegationRefused) as error:
        module.prepare_delegation(cgroup_path=path, capture=capture)
    assert len(calls) == 6
    assert private not in str(error.value).encode()


@pytest.mark.parametrize('condition', ['nonzero', 'timeout', 'overflow', 'spawn', 'bad-json'])
def test_command_and_capture_refusals_are_fixed_and_do_not_continue(condition, tmp_path, monkeypatch):
    module, path, calls, original, info, private = fixture(tmp_path, monkeypatch)
    def capture(prefix, args, **kwargs):
        result = original(prefix, args, **kwargs)
        if condition == 'bad-json':
            if args == ['info', '--format=json']: result['stdout'] = private
            return result
        if condition == 'spawn': raise OSError(private.decode())
        if condition == 'nonzero': result['exit_code'] = 1
        else: result['timed_out' if condition == 'timeout' else 'overflow'] = True
        return result
    with pytest.raises(module.DelegationRefused) as error:
        module.prepare_delegation(cgroup_path=path, capture=capture)
    assert len(calls) == (6 if condition == 'bad-json' else 1)
    assert private not in str(error.value).encode()


def test_job_placement_is_rechecked_immediately_before_manager_restart(tmp_path, monkeypatch):
    module, path, calls, original, info, private = fixture(tmp_path, monkeypatch)
    def capture(prefix, args, **kwargs):
        result = original(prefix, args, **kwargs)
        if args == ['systemctl', 'daemon-reload']:
            path.write_bytes(b'0::/user.slice/user-1001.slice/user@1001.service/job.scope\n')
        return result
    with pytest.raises(module.DelegationRefused):
        module.prepare_delegation(cgroup_path=path, capture=capture)
    assert len(calls) == 3 and not any('restart' in args for _, args, _ in calls)


@pytest.mark.parametrize('uid,euid', [(0, 0), (501, 0)])
def test_root_or_mismatched_effective_uid_refuses_before_effects(uid, euid, tmp_path, monkeypatch):
    module, path, calls, capture, info, private = fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(module.os, 'getuid', lambda: uid)
    monkeypatch.setattr(module.os, 'geteuid', lambda: euid)
    with pytest.raises(module.DelegationRefused):
        module.prepare_delegation(cgroup_path=path, capture=capture)
    assert not calls


def test_workflow_delegation_step_precedes_every_image_build_and_keeps_caps():
    raw = (ROOT / '.github/workflows/cortex-linux-candidate.yml').read_text()
    assert 'python3 scripts/release/linux_ci_delegation.py' in raw, 'effective delegation is not wired before build'
    assert raw.index('linux_ci_runtime.py') < raw.index('linux_ci_delegation.py') < raw.index('Observe local engine') < raw.index('Build immutable OCI images')
    assert '--source-sha "$CANDIDATE_SHA"' in raw and '--output "$RUNNER_TEMP/cortex-linux-diagnostics"' in raw
    assert "'--memory', '512m', '--cpus', '1'" in (ROOT / 'scripts/release/linux_ci_diagnostics.py').read_text()


@pytest.mark.parametrize('prefix', [b'crun: controller `cpu` is not available under ',
    b'Error: OCI runtime error: unable to start container "' + b'e' * 64 + b'": crun: controller `cpu` is not available under '])
def test_observed_cpu_controller_signature_exposes_only_fixed_category(prefix):
    module = diagnostics_product(); private = secrets.token_urlsafe(32).encode()
    value = module.classify_stderr(prefix + b'/sys/fs/cgroup/public.scope\n' + private)
    assert value == {'signature': 'cgroup_cpu_unavailable', 'component': 'crun'}
    assert private not in json.dumps(value).encode()


@pytest.mark.parametrize('raw', [b'context crun: controller `cpu` is not available',
    b'crun: controller `pids` is not available', b'crun: controller `cpu` is not availablex',
    b'Error: OCI runtime error: unable to start container "arbitrary": crun: controller `cpu` is not available'])
def test_nearby_unrecognized_controller_text_stays_unknown(raw):
    assert diagnostics_product().classify_stderr(raw) == {'signature': 'unknown', 'component': 'unknown'}
