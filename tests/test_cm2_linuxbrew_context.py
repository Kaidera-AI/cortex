"""Actual public Cellar/link/header bytes; Linux identity and commands intercepted."""
import copy
import importlib
import os
from pathlib import Path
import stat
import struct
import time
from types import SimpleNamespace

import pytest
from test_cm2_linux_engine_observation import POLICY, VERSION
from test_cm2_linux_owned_api import fixture as api_fixture

CONTEXT_CASES = ['valid-755', 'valid-555', 'revision', 'bad-magic', 'bad-class', 'bad-endian',
                 'bad-ident-version', 'bad-machine', 'bad-type', 'bad-elf-version', 'short',
                 'foreign-link', 'outside-cellar', 'unstable-keg', 'zero-revision',
                 'parent-linked', 'prefix-mode', 'bin-mode', 'parent-owner', 'link-owner',
                 'exe-owner', 'exe-mode', 'exe-noexec', 'hardlink', 'exe-linked',
                 'exe-replaced', 'link-changed', 'parent-changed', 'broken-link']
PROVIDER_CASES = ['valid', 'revision', 'future', 'below-minimum', 'cortex-denial', 'kos-denial',
                  'tap', 'stable', 'linked-keg', 'selected-keg', 'not-bottle', 'two-installed',
                  'bottle-missing', 'checksum', 'url', 'metadata-error', 'remote-version',
                  'context-change', 'brew-missing', 'brew-script', 'brew-mode', 'brew-linked-parent',
                  'brew-changed', 'distro']


def required():
    module = importlib.import_module('cortex_v2.clients.linux_provisioning')
    assert callable(getattr(module, 'observe_linuxbrew_provider', None)), 'stock Linuxbrew context/provider missing'
    return module


def files(module, tmp_path, monkeypatch, keg='6.1.3'):
    uid = os.getuid(); home = tmp_path / 'home'; home.mkdir(mode=0o700)
    runtime = tmp_path / 'runtime'; runtime.mkdir(mode=0o700)
    prefix = tmp_path / 'linuxbrew'; target = prefix / 'Cellar/podman' / keg / 'bin/podman'
    target.parent.mkdir(parents=True, mode=0o755)
    header = bytearray(64); header[:7] = b'\x7fELF\x02\x01\x01'
    struct.pack_into('<HHI', header, 16, 2, 62, 1)
    target.write_bytes(header); target.chmod(0o755)
    (prefix / 'bin').mkdir(mode=0o755)
    link = prefix / 'bin/podman'; link.symlink_to('../Cellar/podman/' + keg + '/bin/podman')
    brew = prefix / 'Homebrew/bin/brew'; brew.parent.mkdir(parents=True, mode=0o755)
    brew.write_bytes(b'#!/bin/bash\n# Public finite fixture: never executed.\n'); brew.chmod(0o755)
    (prefix / 'bin/brew').symlink_to('../Homebrew/bin/brew')
    monkeypatch.setattr(module, 'LINUXBREW_PREFIX', prefix, raising=False)
    monkeypatch.setattr(module.platform, 'system', lambda: 'Linux')
    monkeypatch.setattr(module.pwd, 'getpwuid', lambda _: SimpleNamespace(pw_name='kos', pw_dir=str(home)))
    original = Path.lstat
    def observed(path, *args, **kwargs):
        return original(runtime if path == Path('/run/user/' + str(uid)) else path, *args, **kwargs)
    monkeypatch.setattr(Path, 'lstat', observed)
    return prefix, target, link, brew, home, runtime


@pytest.mark.parametrize('case', CONTEXT_CASES)
def test_actual_fixed_cellar_link_physical_custody_and_elf_header_bind_execution(case, tmp_path, monkeypatch):
    module = required(); keg = '6.1.3_1' if case == 'revision' else '6.1.3'
    prefix, target, link, brew, home, runtime = files(module, tmp_path, monkeypatch, keg)
    raw = bytearray(target.read_bytes())
    indexes = {'bad-magic': 0, 'bad-class': 4, 'bad-endian': 5, 'bad-ident-version': 6,
               'bad-type': 16, 'bad-machine': 18, 'bad-elf-version': 20}
    if case in indexes: raw[indexes[case]] = 0; target.write_bytes(raw)
    if case == 'short': target.write_bytes(b'\x7fELF')
    if case == 'valid-555': target.chmod(0o555)
    if case == 'exe-mode': target.chmod(0o777)
    if case == 'exe-noexec': target.chmod(0o644)
    if case == 'hardlink': os.link(target, tmp_path / 'second-podman')
    if case == 'prefix-mode': prefix.chmod(0o777)
    if case == 'bin-mode': link.parent.chmod(0o777)
    if case in ('unstable-keg', 'zero-revision'):
        new = target.parent.parent.with_name('6.1.3-rc1' if case == 'unstable-keg' else '6.1.3_0')
        target.parent.parent.rename(new); link.unlink(); link.symlink_to(new / 'bin/podman')
    if case in ('foreign-link', 'outside-cellar'):
        other = (tmp_path / 'foreign') if case == 'foreign-link' else (prefix / 'foreign')
        other.write_bytes(raw); other.chmod(0o755); link.unlink(); link.symlink_to(other)
    if case == 'parent-linked':
        parent = target.parent.parent; parent.rename(parent.with_name('actual-keg')); parent.symlink_to(parent.with_name('actual-keg'))
    if case == 'exe-linked':
        target.rename(target.with_name('real-podman')); target.symlink_to(target.with_name('real-podman'))
    if case == 'broken-link': target.unlink()
    original_stat = Path.lstat
    def metadata(path, *args, **kwargs):
        value = original_stat(path, *args, **kwargs)
        if ((case == 'exe-owner' and path == target) or (case == 'link-owner' and path == link)
                or (case == 'parent-owner' and path == target.parent)):
            fields = list(value); fields[4] = os.getuid() + 1; return os.stat_result(fields)
        return value
    monkeypatch.setattr(Path, 'lstat', metadata)
    original_read = os.read; original_open = os.open; original_close = os.close
    handles = {}; closed = []; changed = []
    def opened(path, flags, *args):
        fd = original_open(path, flags, *args)
        if Path(path) == target:
            assert flags & os.O_NOFOLLOW and flags & os.O_NONBLOCK; handles[fd] = True
        return fd
    def read(fd, count):
        value = original_read(fd, count)
        if fd in handles and not changed:
            changed.append(True)
            if case == 'exe-replaced': target.rename(target.with_name('old')); target.write_bytes(raw); target.chmod(0o755)
            if case == 'link-changed': link.unlink(); link.symlink_to('../Cellar/podman/' + keg + '/bin/podman')
            if case == 'parent-changed': target.parent.chmod(0o777)
        return value
    def close(fd):
        if fd in handles: closed.append(fd)
        return original_close(fd)
    monkeypatch.setattr(module.os, 'open', opened); monkeypatch.setattr(module.os, 'read', read)
    monkeypatch.setattr(module.os, 'close', close)
    monkeypatch.setenv('CONTAINER_HOST', 'ignored-public-fixture')
    monkeypatch.setenv('PATH', '/foreign')
    if case in ('valid-755', 'valid-555', 'revision'):
        value = module._local_engine_context()
        assert value['executable'] == str(target) and value['uid'] == os.getuid()
        assert value['provider'] == 'linuxbrew' and value['linked_keg'] == keg
        assert value['environment'] == {'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'HOME': str(home),
                                        'XDG_RUNTIME_DIR': '/run/user/' + str(os.getuid()), 'LC_ALL': 'C'}
        assert handles
    else:
        with pytest.raises(module.PrerequisiteRefusal) as error: module._local_engine_context()
        assert error.value.code == 'cortex_podman_unsupported'
    assert set(closed) == set(handles) and len(closed) == len(handles)


def metadata(version='6.1.3', revision=0):
    keg = version + ('_' + str(revision) if revision else '')
    digest = 'a' * 64
    return {'formulae': [{'name': 'podman', 'tap': 'homebrew/core', 'revision': revision,
        'versions': {'stable': version}, 'linked_keg': keg,
        'installed': [{'version': keg, 'poured_from_bottle': True}],
        'bottle': {'stable': {'files': {'x86_64_linux': {'sha256': digest,
            'url': 'https://ghcr.io/v2/homebrew/core/podman/blobs/sha256:' + digest}}}}}]}


@pytest.mark.parametrize('case', PROVIDER_CASES)
def test_actual_stock_keg_and_brew_script_bind_finite_version_and_formula_reads(case, tmp_path, monkeypatch):
    module = required(); version = '7.0.0' if case == 'future' else '6.0.1' if case == 'below-minimum' else '6.1.3'
    revision = 1 if case == 'revision' else 0; keg = version + ('_1' if revision else '')
    prefix, target, link, brew, home, runtime = files(module, tmp_path, monkeypatch, keg)
    observed = metadata(version, revision); formula = observed['formulae'][0]
    if case == 'tap': formula['tap'] = 'foreign/tap'
    if case == 'stable': formula['versions']['stable'] = '6.1.4'
    if case == 'linked-keg': formula['linked_keg'] = '6.1.4'
    if case == 'selected-keg':
        formula['revision'] = 1; formula['linked_keg'] = '6.1.3_1'; formula['installed'][0]['version'] = '6.1.3_1'
    if case == 'not-bottle': formula['installed'][0]['poured_from_bottle'] = False
    if case == 'two-installed': formula['installed'].append(copy.deepcopy(formula['installed'][0]))
    if case == 'bottle-missing': formula['bottle']['stable']['files'].clear()
    if case == 'checksum': formula['bottle']['stable']['files']['x86_64_linux']['sha256'] = 'invalid'
    if case == 'url': formula['bottle']['stable']['files']['x86_64_linux']['url'] = 'https://foreign.invalid/podman'
    if case == 'brew-missing': brew.unlink()
    if case == 'brew-script': brew.write_bytes(b'not the stock script\n')
    if case == 'brew-mode': brew.chmod(0o777)
    if case == 'brew-linked-parent':
        brew.parent.rename(brew.parent.with_name('actual-bin')); brew.parent.symlink_to(brew.parent.with_name('actual-bin'))
    cortex = copy.deepcopy(POLICY); kos = copy.deepcopy(POLICY)
    if case.endswith('-denial'):
        (cortex if case == 'cortex-denial' else kos)['denylist']['entries'] = [{'version': version, 'date': '2026-10-06', 'reason': 'public fixture'}]
    original_context = module._local_engine_context; contexts = []
    def context():
        contexts.append(True)
        value = original_context()
        if case == 'context-change' and len(contexts) > 2: value['identity'] = ('changed',)
        if case == 'distro': value['provider'] = 'distro'
        return value
    monkeypatch.setattr(module, '_local_engine_context', context)
    calls = []
    def capture(argv, *, environment, deadline):
        calls.append((argv, environment, deadline))
        assert deadline > time.monotonic()
        assert 'CONTAINER_HOST' not in environment and 'CONTAINERS_STORAGE_CONF' not in environment
        if argv[0] == str(target):
            assert argv == [str(target), '--remote=false', 'version', '--format=json']
            assert environment == {'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'HOME': str(home),
                                   'XDG_RUNTIME_DIR': '/run/user/' + str(os.getuid()), 'LC_ALL': 'C'}
            value = {'Client': {'Version': version}}
            if case == 'remote-version': value['Server'] = {'Version': version}
            if case == 'brew-changed': brew.write_bytes(b'#!/bin/bash\n# Changed public fixture\n')
            return value
        assert argv == [str(brew), 'info', '--json=v2', '--formula', 'podman']
        assert environment == {'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'HOME': str(home),
            'XDG_RUNTIME_DIR': '/run/user/' + str(os.getuid()), 'LC_ALL': 'C',
            'HOMEBREW_NO_AUTO_UPDATE': '1', 'HOMEBREW_NO_ANALYTICS': '1', 'HOMEBREW_NO_INSTALL_CLEANUP': '1'}
        if case == 'metadata-error': raise module.PrerequisiteRefusal('cortex_podman_unsupported')
        return copy.deepcopy(observed)
    monkeypatch.setattr(module, '_engine_json', capture)
    monkeypatch.setenv('CONTAINER_HOST', 'ignored-public-fixture')
    call = lambda: module.observe_linuxbrew_provider(cortex_policy=cortex, kos_policy=kos, deadline=time.monotonic() + 2)
    if case in ('valid', 'revision', 'future'):
        value = call()
        assert value == {'schema': 'cortex.linuxbrew-provider.v1', 'provider': 'linuxbrew',
            'formula': 'homebrew/core/podman', 'version': version, 'linked_keg': keg,
            'poured_from_bottle': True, 'bottle_sha256': 'a' * 64,
            'bottle_url': 'https://ghcr.io/v2/homebrew/core/podman/blobs/sha256:' + 'a' * 64,
            'checksum_verifier': 'Homebrew',
            'scope': 'current formula metadata and installed stock keg; Homebrew verifies downloaded bottle bytes'}
        assert len(calls) == 2 and calls[0][2] == calls[1][2]
    else:
        with pytest.raises(module.PrerequisiteRefusal) as error: call()
        assert error.value.code == ('cortex_podman_denied' if case.endswith('-denial') else 'cortex_podman_unsupported')
        assert 'foreign.invalid' not in str(error.value)
        if case in ('brew-missing', 'brew-script', 'brew-mode', 'brew-linked-parent', 'distro'): assert calls == []


@pytest.mark.parametrize('mode', ['payload', 'create-project'])
def test_owned_api_both_measurement_and_private_request_use_bound_cellar_executable(mode, tmp_path, monkeypatch):
    required()
    module, root, release, binding, current, engine, payload, frame, response, actions, runtimes, old = api_fixture(tmp_path, monkeypatch)
    engine['executable'] = '/home/linuxbrew/.linuxbrew/Cellar/podman/6.1.3/bin/podman'
    frames = []
    def command(argv, private, **kwargs):
        assert argv[0] == engine['executable'] and argv[1:] == ['--remote=false', 'exec', '--interactive', binding['containers']['api'], '/opt/venv/bin/python', '-m', 'cortex_v2.native_prerequisite']
        assert kwargs['environment'] == engine['environment']; frames.append(copy.deepcopy(private))
        return copy.deepcopy(payload if private['mode'] == 'payload' else response)
    monkeypatch.setattr(module, 'private_json_command', command)
    if mode == 'payload': frame = {'mode': 'payload'}
    value = module.owned_api_command(root, frame, kos_policy={'minimum_version': '6.0.2'}, deadline=time.monotonic() + 2)
    assert value == (payload if mode == 'payload' else response)
    assert [f['mode'] for f in frames] == (['payload'] if mode == 'payload' else ['payload', 'create-project'])


@pytest.mark.parametrize('deadline', [True, float('nan'), float('inf'), 0, -1])
def test_provider_bad_deadline_refuses_before_context_or_process(deadline, monkeypatch):
    module = required()
    monkeypatch.setattr(module, '_local_engine_context', lambda: pytest.fail('bad deadline reached context'))
    monkeypatch.setattr(module, '_engine_json', lambda *a, **kw: pytest.fail('bad deadline reached process'))
    with pytest.raises(module.PrerequisiteRefusal):
        module.observe_linuxbrew_provider(cortex_policy=POLICY, kos_policy=POLICY, deadline=deadline)
