"""Real public files/dirs; native runtime, Linux kernel ownership and engine intercepted."""
import copy
import importlib
import os
from pathlib import Path
import stat
import time

import pytest
from test_cm2_linux_engine_observation import POLICY, INFO

CASES = ['valid', 'engine-disabled', 'kernel-disabled', 'kernel-missing', 'kernel-garbage',
         'kernel-linked', 'kernel-hardlink', 'kernel-owner', 'kernel-mode', 'kernel-oversized',
         'kernel-replaced', 'kernel-changed', 'graph-linked', 'graph-mode', 'graph-owner',
         'run-mode', 'run-owner', 'graph-replaced', 'context-change', 'runtime-change',
         'info-change', 'rootful', 'store-missing']


def required():
    module = importlib.import_module('cortex_v2.clients.linux_provisioning')
    assert callable(getattr(module, 'read_linux_host_security', None)), 'Linux host security observation missing'
    return module


@pytest.mark.parametrize('case', CASES)
def test_actual_store_directory_and_enforcement_bytes_remain_bound_through_runtime_recheck(case, tmp_path, monkeypatch):
    module = required(); uid = os.getuid()
    home = tmp_path / 'home'; graph = home / '.local/share/containers/storage'
    graph.mkdir(parents=True, mode=0o700)
    # Fixture parents are genuinely owned and non-writable by other users.
    for parent in [home, home / '.local', home / '.local/share', home / '.local/share/containers']:
        parent.chmod(0o700)
    run = tmp_path / 'run'; run.mkdir(mode=0o700)
    kernel = tmp_path / 'enforce'; kernel.write_bytes(b'1'); kernel.chmod(0o644)
    monkeypatch.setattr(module, 'SELINUX_ENFORCE', kernel)
    context = {'executable': '/usr/bin/podman', 'uid': uid, 'identity': (1, 2),
               'environment': {'HOME': str(home), 'XDG_RUNTIME_DIR': '/run/user/' + str(uid), 'PATH': '/usr/bin:/bin'}}
    binding = {'installation_id': '10000000-0000-4000-8000-000000000001', 'containers': {'api': 'a' * 64}}
    info = copy.deepcopy(INFO); info['host']['security']['selinuxEnabled'] = case != 'engine-disabled'
    info['store'] = {'graphRoot': str(graph), 'runRoot': '/run/user/' + str(uid) + '/containers', 'graphDriverName': 'overlay'}
    if case == 'rootful': info['store']['graphRoot'] = '/var/lib/containers/storage'
    if case == 'store-missing': info.pop('store')
    if case == 'kernel-disabled': kernel.write_bytes(b'0')
    if case == 'kernel-garbage': kernel.write_bytes(b'x')
    if case == 'kernel-oversized': kernel.write_bytes(b'1\nx')
    if case == 'kernel-missing': kernel.unlink()
    if case == 'kernel-linked':
        kernel.rename(tmp_path / 'actual-enforce'); kernel.symlink_to(tmp_path / 'actual-enforce')
    if case == 'kernel-hardlink': os.link(kernel, tmp_path / 'other-enforce')
    if case == 'kernel-mode': kernel.chmod(0o666)
    if case == 'graph-mode': graph.chmod(0o777)
    if case == 'run-mode': run.chmod(0o777)
    if case == 'graph-linked':
        graph.rename(graph.with_name('actual-storage')); graph.symlink_to(graph.with_name('actual-storage'))
    original_stat = Path.lstat; original_open = os.open; original_fstat = os.fstat; original_close = os.close
    expected_run = Path('/run/user/' + str(uid) + '/containers')
    def mapped(path):
        if path == expected_run: return run
        if path in expected_run.parents and path != Path('/'): return tmp_path
        return path
    def observation(path, *args, **kwargs):
        result = original_stat(mapped(path), *args, **kwargs); fields = list(result)
        if path == kernel or path in kernel.parents:
            fields[4] = uid if case == 'kernel-owner' and path == kernel else 0
        if case == 'graph-owner' and path == graph or case == 'run-owner' and path == expected_run:
            fields[4] = uid + 1
        if path == expected_run.parent:
            result = original_stat(tmp_path); fields = list(result); fields[0] = stat.S_IFDIR | 0o700
        return os.stat_result(fields) if fields != list(result) else result
    # Map the exact native runtime directory into the real owned fixture.
    original_islink = Path.is_symlink
    monkeypatch.setattr(Path, 'is_symlink', lambda path: False if path in [expected_run, *expected_run.parents] else original_islink(path))
    monkeypatch.setattr(Path, 'lstat', observation)
    handles = {}; closed = []
    def opened(path, flags, *args):
        selected = Path(path); fd = original_open(mapped(selected), flags, *args)
        if selected in (graph, expected_run, kernel):
            assert flags & os.O_NOFOLLOW
            handles[fd] = selected
        return fd
    def fstat(fd):
        result = original_fstat(fd)
        if handles.get(fd) == kernel:
            fields = list(result); fields[4] = uid if case == 'kernel-owner' else 0
            return os.stat_result(fields)
        return result
    def close(fd):
        if fd in handles: closed.append(fd)
        return original_close(fd)
    monkeypatch.setattr(module.os, 'open', opened); monkeypatch.setattr(module.os, 'fstat', fstat)
    monkeypatch.setattr(module.os, 'close', close)
    contexts = []; runtimes = []; captures = []
    def local_context():
        contexts.append(True); value = copy.deepcopy(context)
        if case == 'context-change' and len(contexts) > 2: value['identity'] = (3, 4)
        return value
    def runtime(root, *, kos_policy, deadline):
        runtimes.append(True); value = copy.deepcopy(binding)
        if case == 'runtime-change' and len(runtimes) > 1: value['containers']['api'] = 'b' * 64
        return value
    def capture(command, *, environment, deadline):
        captures.append(command); value = copy.deepcopy(info)
        if len(captures) > 1:
            if case == 'info-change': value['store']['graphDriverName'] = 'vfs'
            if case == 'graph-replaced': graph.rename(graph.with_name('old-storage')); graph.mkdir(mode=0o700)
            if case == 'kernel-replaced': kernel.rename(tmp_path / 'old-enforce'); kernel.write_bytes(b'1'); kernel.chmod(0o644)
            if case == 'kernel-changed': kernel.write_bytes(b'0')
        return value
    monkeypatch.setattr(module, '_local_engine_context', local_context)
    monkeypatch.setattr(module, 'read_linux_runtime', runtime); monkeypatch.setattr(module, '_engine_json', capture)
    call = lambda: module.read_linux_host_security(tmp_path / 'runtime', kos_policy=POLICY, deadline=time.monotonic() + 2)
    if case == 'valid':
        assert call() == {'schema': 'cortex.linux-host-security.v1', 'installation_id': binding['installation_id'],
                          'rootless': True, 'storage_config_matches': True, 'storage_custody_matches': True, 'selinux': 'Enforcing'}
        assert len(captures) == len(runtimes) == 2 and len(handles) == 3
    else:
        with pytest.raises(module.PrerequisiteRefusal) as error: call()
        expected = ('cortex_instance_mismatch' if case in ('context-change', 'runtime-change') else
                    'cortex_selinux_refused' if case.startswith('kernel-') or case == 'engine-disabled' else
                    'cortex_podman_storage_setup_required')
        assert error.value.code == expected
    assert set(closed) == set(handles) and len(closed) == len(handles)
    assert all(c == ['/usr/bin/podman', '--remote=false', 'info', '--format=json'] for c in captures)


@pytest.mark.parametrize('deadline', [True, float('nan'), float('inf'), 0, -1])
def test_host_security_invalid_deadline_refuses_before_native_context_or_files(deadline, tmp_path, monkeypatch):
    module = required()
    monkeypatch.setattr(module, 'read_linux_runtime', lambda *a, **kw: pytest.fail('bad deadline read runtime'))
    monkeypatch.setattr(module, '_local_engine_context', lambda: pytest.fail('bad deadline read context'))
    with pytest.raises(module.PrerequisiteRefusal):
        module.read_linux_host_security(tmp_path, kos_policy=POLICY, deadline=deadline)
