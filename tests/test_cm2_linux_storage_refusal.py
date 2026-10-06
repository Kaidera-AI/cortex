"""Public storage observations and finite children; no Podman or config effects."""
import copy
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest
from test_cm2_linux_engine_observation import context_fixture, observe, INFO, VERSION

CODE = 'cortex_podman_storage_setup_required'
FIX = 'Complete the per-user ~/.config/containers/storage.conf step in INSTALL-linux.md; Cortex does not write Podman configuration.'
DIAGNOSTIC = b'Error: creating runtime static files directory "/var/lib/containers/storage/libpod": mkdir /var/lib/containers/storage: permission denied\n'
PURE_CASES = ['valid', 'rootful', 'missing-store', 'driver', 'runroot', 'foreign-home', 'rootless',
              'uid-bool', 'uid-root', 'uid-negative', 'home-relative', 'home-dot', 'home-control',
              'graph-missing', 'run-missing', 'nonobject', 'duplicate', 'oversized', 'unknown-json']
OBSERVE_CASES = ['valid', 'rootful', 'malformed-store', 'absent-legacy']
PROCESS_CASES = ['rootful', 'unknown', 'foreign-root', 'non-info', 'root-user', 'non125', 'output', 'overflow']


def required():
    custody = importlib.import_module('cortex_v2.clients.native_prerequisite')
    assert callable(getattr(custody, 'check_linux_storage_config', None)), 'Linux storage refusal predicate missing'
    return custody, importlib.import_module('cortex_v2.clients.linux_provisioning')


def actual_info(uid=1001):
    value = copy.deepcopy(INFO)
    value['store'] = {'graphRoot': '/home/kos/.local/share/containers/storage',
                      'runRoot': '/run/user/' + str(uid) + '/containers', 'graphDriverName': 'overlay'}
    return value


@pytest.mark.parametrize('case', PURE_CASES)
def test_actual_storage_predicate_refuses_rootful_or_unknown_config_with_named_guide_step(case):
    custody, _ = required(); value = actual_info(); uid = 1001; home = '/home/kos'
    if case == 'rootful': value['store']['graphRoot'] = '/var/lib/containers/storage'
    if case == 'missing-store': value.pop('store')
    if case == 'driver': value['store']['graphDriverName'] = 'vfs'
    if case == 'runroot': value['store']['runRoot'] = '/run/containers/storage'
    if case == 'foreign-home': value['store']['graphRoot'] = '/home/other/.local/share/containers/storage'
    if case == 'rootless': value['host']['security']['rootless'] = False
    if case == 'uid-bool': uid = True
    if case == 'uid-root': uid = 0
    if case == 'uid-negative': uid = -1
    if case == 'home-relative': home = 'relative'
    if case == 'home-dot': home = '/home/kos/../other'
    if case == 'home-control': home = '/home/kos\n'
    if case == 'graph-missing': value['store'].pop('graphRoot')
    if case == 'run-missing': value['store'].pop('runRoot')
    raw = json.dumps(value).encode()
    if case == 'nonobject': raw = b'[]'
    if case == 'duplicate': raw = b'{"store":{},"store":{}}'
    if case == 'oversized': raw = b' ' * 65537
    if case == 'unknown-json': raw = b'{invalid'
    call = lambda: custody.check_linux_storage_config(raw, uid=uid, home=home)
    if case == 'valid':
        assert call() == {'schema': 'cortex.linux-storage-config.v1', 'rootless': True,
                          'graph_root': '/home/kos/.local/share/containers/storage',
                          'run_root': '/run/user/1001/containers', 'driver': 'overlay'}
    else:
        with pytest.raises(custody.PrerequisiteRefusal) as error: call()
        assert error.value.code == CODE and str(error.value) == CODE + ': ' + FIX
        public = error.value.public()
        assert public['safe_message'] == FIX and public['install_guide'] == 'INSTALL-linux.md'
        assert set(public) == {'code', 'safe_message', 'install_guide', 'utc'}


@pytest.mark.parametrize('case', OBSERVE_CASES)
def test_finite_engine_observation_detects_actual_store_before_version_without_writing(case, monkeypatch):
    custody, module = required(); context_fixture(module, monkeypatch)
    value = actual_info(os.getuid()); calls = []
    if case == 'rootful': value['store']['graphRoot'] = '/var/lib/containers/storage'
    if case == 'malformed-store': value['store'] = None
    if case == 'absent-legacy': value.pop('store')
    def capture(command, *, environment, deadline):
        calls.append(command); return copy.deepcopy(value if 'info' in command else VERSION)
    monkeypatch.setattr(module, '_engine_json', capture)
    if case in ('valid', 'absent-legacy'):
        result = observe(module)
        assert result['schema'] == 'cortex.linux-engine-readiness.v1'
        assert result['client_version'] == '6.1.3' and 'storage' not in result
        assert len(calls) == 2
    else:
        with pytest.raises(custody.PrerequisiteRefusal) as error: observe(module)
        assert error.value.code == CODE and error.value.public()['safe_message'] == FIX
        assert len(calls) == 1
    assert all(command[:2] == ['/usr/bin/podman', '--remote=false'] for command in calls)


@pytest.mark.parametrize('case', PROCESS_CASES)
def test_exact_rootful_initialization_diagnostic_maps_to_safe_refusal_and_discards_raw_bytes(case, monkeypatch, tmp_path):
    custody, module = required(); native_popen = subprocess.Popen; launched = []
    diagnostic = DIAGNOSTIC; output = b''; code = 125
    if case == 'unknown': diagnostic = b'PUBLIC_UNTRUSTED_DIAGNOSTIC\n'
    if case == 'foreign-root': diagnostic = DIAGNOSTIC.replace(b'/var/lib/containers/storage', b'/home/other/store')
    if case == 'non125': code = 1
    if case == 'output': output = b'{}\n'
    if case == 'overflow': diagnostic += b'x' * 4097
    command = ['/usr/bin/podman', '--remote=false', 'version' if case == 'non-info' else 'info', '--format=json']
    if case == 'root-user': monkeypatch.setattr(module.os, 'getuid', lambda: 0)
    def spawn(actual, **kwargs):
        assert actual == command and kwargs['stdin'] == subprocess.DEVNULL
        assert kwargs['start_new_session'] is True
        script = 'import os;os.write(1,' + repr(output) + ');os.write(2,' + repr(diagnostic) + ');raise SystemExit(' + str(code) + ')'
        process = native_popen([sys.executable, '-c', script], **kwargs); launched.append(process); return process
    monkeypatch.setattr(module.subprocess, 'Popen', spawn)
    start = time.monotonic()
    with pytest.raises(custody.PrerequisiteRefusal) as error:
        module._engine_json(command, environment={'PATH': '/usr/bin:/bin', 'HOME': str(tmp_path), 'LC_ALL': 'C'}, deadline=start + 2)
    expected = CODE if case == 'rootful' else 'cortex_podman_unsupported'
    assert error.value.code == expected
    assert '/var/lib/containers/storage' not in str(error.value)
    assert 'PUBLIC_UNTRUSTED_DIAGNOSTIC' not in str(error.value)
    if case == 'rootful': assert error.value.public()['safe_message'] == FIX
    assert time.monotonic() - start < 2 and len(launched) == 1
    assert launched[0].poll() is not None and launched[0].stdout.closed and launched[0].stderr.closed
