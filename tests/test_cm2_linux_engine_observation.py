"""Actual finite read processes; Linux identity and Podman remain intercepted."""
import copy
import importlib
import json
import os
from pathlib import Path
import stat
import sys
import time
from types import SimpleNamespace

import pytest

POLICY = json.loads((Path(__file__).parent / 'fixtures/cm2-revision4/contract.json').read_text())['podman']
INFO = {'host': {'cgroupVersion': 'v2', 'cgroupControllers': ['cpu', 'memory', 'pids'], 'security': {'rootless': True}}}
VERSION = {'Client': {'Version': '6.1.3'}}


def product():
    module = importlib.import_module('cortex_v2.clients.linux_provisioning')
    assert hasattr(module, 'observe_linux_engine'), 'finite kos-user engine observation is absent'
    return module


def context_fixture(module, monkeypatch):
    context = {'executable': '/usr/bin/podman', 'uid': os.getuid(), 'environment': {'HOME': '/home/kos', 'XDG_RUNTIME_DIR': '/run/user/' + str(os.getuid())}, 'identity': (1, 2)}
    monkeypatch.setattr(module, '_local_engine_context', lambda: copy.deepcopy(context))
    calls = []
    def capture(command, *, environment, deadline):
        calls.append((command, environment, deadline))
        return copy.deepcopy(INFO if 'info' in command else VERSION)
    monkeypatch.setattr(module, '_engine_json', capture)
    return calls


def observe(module, *, deadline=None, cortex_policy=None, kos_policy=None):
    return module.observe_linux_engine(cortex_policy=POLICY if cortex_policy is None else cortex_policy,
        kos_policy=POLICY if kos_policy is None else kos_policy,
        deadline=time.monotonic() + 2 if deadline is None else deadline)


def test_local_exact_commands_apply_both_policies_and_return_only_fixed_projection(monkeypatch):
    p = product(); calls = context_fixture(p, monkeypatch)
    result = observe(p)
    assert result == {'schema': 'cortex.linux-engine-readiness.v1', 'mode': 'local', 'client_version': '6.1.3', 'server_version': None, 'connection_name': None,
        'delegation': {'schema': 'cortex.linux-cgroup-readiness.v1', 'rootless': True, 'cgroup_version': 'v2', 'cgroup_controllers': ['cpu', 'memory', 'pids']}}
    assert [c[0] for c in calls] == [['/usr/bin/podman', '--remote=false', 'info', '--format=json'], ['/usr/bin/podman', '--remote=false', 'version', '--format=json']]
    assert calls[0][2] == calls[1][2]


@pytest.mark.parametrize('case', ['cpu', 'memory', 'pids', 'rootful', 'v1', 'malformed', 'context-change'])
def test_missing_controller_or_changed_context_refuses_before_further_effects(case, monkeypatch):
    p = product(); calls = context_fixture(p, monkeypatch)
    info = copy.deepcopy(INFO)
    if case in ('cpu', 'memory', 'pids'): info['host']['cgroupControllers'].remove(case)
    if case == 'rootful': info['host']['security']['rootless'] = False
    if case == 'v1': info['host']['cgroupVersion'] = 'v1'
    if case == 'malformed': info = {}
    if case == 'context-change':
        original = p._local_engine_context; count = []
        def changed():
            value = original(); count.append(True)
            if len(count) > 1: value['identity'] = (3, 4)
            return value
        monkeypatch.setattr(p, '_local_engine_context', changed)
    def capture(command, *, environment, deadline):
        calls.append(command); return info
    monkeypatch.setattr(p, '_engine_json', capture)
    with pytest.raises(p.PrerequisiteRefusal) as error: observe(p)
    assert error.value.code == ('cortex_podman_unsupported' if case == 'context-change' else 'cortex_cgroup_delegation_unavailable')
    assert len(calls) <= 1


@pytest.mark.parametrize('denier', ['cortex', 'kos'])
def test_either_signed_version_policy_can_refuse(denier, monkeypatch):
    p = product(); context_fixture(p, monkeypatch)
    policy = copy.deepcopy(POLICY); policy['denylist']['entries'] = [{'version': '6.1.3', 'date': '2026-10-06', 'reason': 'public fixture'}]
    with pytest.raises(p.PrerequisiteRefusal) as error:
        observe(p, cortex_policy=policy if denier == 'cortex' else POLICY, kos_policy=policy if denier == 'kos' else POLICY)
    assert error.value.code == 'cortex_podman_denied'


@pytest.mark.parametrize('deadline', [True, float('nan'), float('inf'), -1, 0])
def test_bad_deadline_refuses_before_context_or_process(deadline, monkeypatch):
    p = product()
    def forbidden(): pytest.fail('invalid deadline reached host context')
    monkeypatch.setattr(p, '_local_engine_context', forbidden)
    with pytest.raises(p.PrerequisiteRefusal): observe(p, deadline=deadline)


@pytest.mark.parametrize('case', ['valid', 'macos', 'root', 'effective', 'account', 'home-mode', 'runtime-mode', 'exe-owner', 'exe-mode'])
def test_actual_identity_and_physical_engine_guards(case, tmp_path, monkeypatch):
    p = product(); uid = os.getuid()
    home = tmp_path / 'home'; home.mkdir(mode=0o700)
    runtime = tmp_path / 'runtime'; runtime.mkdir(mode=0o700)
    monkeypatch.setattr(p.platform, 'system', lambda: 'Darwin' if case == 'macos' else 'Linux')
    monkeypatch.setattr(p.os, 'getuid', lambda: 0 if case == 'root' else uid)
    monkeypatch.setattr(p.os, 'geteuid', lambda: uid + 1 if case == 'effective' else (0 if case == 'root' else uid))
    monkeypatch.setattr(p.pwd, 'getpwuid', lambda _: SimpleNamespace(pw_name='other' if case == 'account' else 'kos', pw_dir=str(home)))
    real = Path.lstat
    def observation(path, *args, **kwargs):
        if str(path) == '/usr/bin/podman':
            base = real(Path(sys.executable)); values = list(base)
            values[0] = stat.S_IFREG | (0o777 if case == 'exe-mode' else 0o755)
            values[4] = uid if case == 'exe-owner' else 0
            return os.stat_result(values)
        if str(path) == '/run/user/' + str(uid):
            base = real(runtime); values = list(base)
            values[0] = stat.S_IFDIR | (0o777 if case == 'runtime-mode' else 0o700)
            return os.stat_result(values)
        return real(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'lstat', observation)
    if case == 'home-mode': home.chmod(0o777)
    if case == 'valid':
        value = p._local_engine_context()
        assert value['uid'] == uid and value['executable'] == '/usr/bin/podman'
        assert value['environment']['HOME'] == str(home)
        assert value['environment']['XDG_RUNTIME_DIR'] == '/run/user/' + str(uid)
        assert set(value['environment']) == {'PATH', 'HOME', 'XDG_RUNTIME_DIR', 'LC_ALL'}
    else:
        with pytest.raises(p.PrerequisiteRefusal): p._local_engine_context()


@pytest.mark.parametrize('case', ['pretty', 'duplicate', 'oversized', 'stderr', 'exit', 'deadline', 'child-pipe'])
def test_actual_read_process_is_bounded_and_discards_untrusted_errors(case, monkeypatch, tmp_path):
    p = product(); deadline = time.monotonic() + (.2 if case in ('deadline', 'child-pipe') else 2)
    scripts = {
        'pretty': 'import json,os,sys; assert sys.stdin.read()==""; assert "CONTAINER_HOST" not in os.environ; print(json.dumps({"ok":True},indent=2))',
        'duplicate': 'print("{\\"x\\":1,\\"x\\":2}")',
        'oversized': 'import os; os.write(1,b"x"*65537)',
        'stderr': 'import os; os.write(2,b"untrusted engine diagnostic"); print("{}")',
        'exit': 'print("{}"); raise SystemExit(125)',
        'deadline': 'import time; time.sleep(3)',
        'child-pipe': 'import subprocess,sys; subprocess.Popen([sys.executable,"-c","import time; time.sleep(3)"]); print("{}")',
    }
    monkeypatch.setenv('CONTAINER_HOST', 'ambient-unused-engine-value')
    environment = {'PATH': '/usr/bin:/bin', 'HOME': str(tmp_path), 'LC_ALL': 'C'}
    start = time.monotonic()
    if case == 'pretty':
        assert p._engine_json([sys.executable, '-c', scripts[case]], environment=environment, deadline=deadline) == {'ok': True}
    else:
        with pytest.raises(p.PrerequisiteRefusal) as error:
            p._engine_json([sys.executable, '-c', scripts[case]], environment=environment, deadline=deadline)
        assert error.value.code == 'cortex_podman_unsupported'
        assert 'untrusted engine diagnostic' not in str(error.value)
    assert time.monotonic() - start < (1 if case in ('deadline', 'child-pipe') else 3)
