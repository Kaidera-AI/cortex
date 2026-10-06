"""R263 diagnostic-only effects: public canary, private real-start stderr."""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import secrets
import sys
import time
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).parents[1]
SOURCE = '1ce41a2f644a13330be56ca4bd18a7bf46bb2425'
NONCE = '90' * 16
OWNER = '10000000-0000-4000-8000-000000000001'


def product():
    path = ROOT / 'scripts/release/linux_ci_diagnostics.py'
    assert path.is_file(), 'accepted Linux CI diagnostics helper is absent'
    spec = importlib.util.spec_from_file_location('r263_diagnostics_test_product', path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def engine_fixture():
    return {'host': {'cgroupVersion': 'v2', 'cgroupControllers': ['cpu', 'memory', 'pids'],
        'networkBackend': 'netavark', 'security': {'rootless': True},
        'conmon': {'path': '/home/linuxbrew/.linuxbrew/bin/conmon', 'version': 'conmon version 2.1.13, commit: example'},
        'ociRuntime': {'name': 'crun', 'path': '/home/linuxbrew/.linuxbrew/bin/crun',
                       'version': 'crun version 1.30.1\ncommit: public-fixture'},
        'private_unused': 'discard this field'}, 'store': {'graphDriverName': 'overlay'},
        'registries': {'unused': 'never publish this'}}


@pytest.mark.parametrize('raw,signature,component', [
    (b'Error: pasta failed with exit code 1\n', 'pasta_failed', 'pasta'),
    (b'Error: could not find pasta or slirp4netns\n', 'rootless_helper_missing', 'podman'),
    (b'Error: setting up network namespace: exec: "pasta": executable file not found in $PATH\n', 'pasta_missing', 'pasta'),
    (b'Error: OCI runtime error: crun: operation not permitted\n', 'crun_failed', 'crun'),
    (b'crun: create cgroup: Permission denied\n', 'cgroup_permission', 'crun'),
    (b'Error: unable to start container process: conmon failed\n', 'conmon_failed', 'conmon'),
    (b'Error: netavark: iptables: executable file not found\n', 'network_backend_failed', 'podman'),
    (b'Error: creating cgroup: operation not permitted\n', 'cgroup_permission', 'podman'),
])
def test_known_allowlisted_signature_reports_only_fixed_public_categories(raw, signature, component):
    module = product(); private = secrets.token_urlsafe(32).encode()
    value = module.classify_stderr(raw + private + b'\n')
    assert value == {'signature': signature, 'component': component}
    assert bool(private not in json.dumps(value).encode())


@pytest.mark.parametrize('raw', [b'', b'permission denied\n', b'pasta\n', b'conmon\n',
    b'crun\n', b'not a known error', b'\xff\x00invalid', b'x' * 65537])
def test_unrecognized_stderr_never_becomes_public_text_or_a_hash(raw):
    module = product(); private = secrets.token_urlsafe(32).encode()
    value = module.classify_stderr(raw + private)
    assert value == {'signature': 'unknown', 'component': 'unknown'}
    assert bool(private not in json.dumps(value).encode())


def test_engine_projection_has_exact_public_fields_and_parsed_versions():
    module = product(); info = engine_fixture(); private = secrets.token_urlsafe(32)
    info['host']['private_unused'] = private; info['registries']['unused'] = private
    value = module.engine_report(info, {'Client': {'Version': '6.0.2'}},
                                 presence={'pasta': True, 'slirp4netns': False})
    assert value == {'schema': 'cortex.linux-ci-engine.v1', 'cgroup_version': 'v2',
        'cgroup_controllers': ['cpu', 'memory', 'pids'], 'network_backend': 'netavark',
        'pasta_present': True, 'slirp4netns_present': False, 'rootless': True,
        'storage_driver': 'overlay', 'podman_version': '6.0.2',
        'conmon': {'path': '/home/linuxbrew/.linuxbrew/bin/conmon', 'version': '2.1.13'},
        'oci_runtime': {'name': 'crun', 'path': '/home/linuxbrew/.linuxbrew/bin/crun', 'version': '1.30.1'}}
    assert bool(private not in json.dumps(value))


@pytest.mark.parametrize('change', ['controllers', 'cgroup', 'backend', 'rootless',
    'driver', 'conmon-path', 'runtime-path', 'version', 'missing'])
def test_engine_unknown_or_unsafe_fields_refuse_without_echoing_input(change):
    module = product(); info = engine_fixture(); private = secrets.token_urlsafe(32)
    version = {'Client': {'Version': '6.0.2'}}
    if change == 'controllers': info['host']['cgroupControllers'] = ['cpu', private]
    elif change == 'cgroup': info['host']['cgroupVersion'] = private
    elif change == 'backend': info['host']['networkBackend'] = private
    elif change == 'rootless': info['host']['security']['rootless'] = private
    elif change == 'driver': info['store']['graphDriverName'] = private
    elif change == 'conmon-path': info['host']['conmon']['path'] = '/private/' + private
    elif change == 'runtime-path': info['host']['ociRuntime']['path'] = '../' + private
    elif change == 'version': version['Client']['Version'] = private
    elif change == 'missing': info.pop('host')
    with pytest.raises(module.DiagnosticsRefused) as error:
        module.engine_report(info, version, presence={'pasta': True, 'slirp4netns': False})
    assert bool(private not in str(error.value))


def database_args():
    prefix = 'cortex_v2_package_test_' + OWNER.replace('-', '')
    args = ['create', '--pull=never', '--name', prefix + '_db', '--label',
        'com.kaidera.candidate=' + OWNER, '--label', 'com.kaidera.deployment-class=TEST',
        '--network', prefix + '_net', '--memory', '512m', '--cpus', '1', '--restart=no',
        '--network-alias=db', '--env', 'POSTGRES_USER=cortex_v2_owner', '--env',
        'POSTGRES_DB=cortex_v2', '--env', 'POSTGRES_PASSWORD_FILE=/run/secrets/db-owner-password',
        '--volume', prefix + '_pgdata:/var/lib/postgresql']
    for name in ('db-owner-password', 'db-app-password', 'db-migrator-password'):
        args += ['--secret', prefix + '-' + name + ',target=' + name + ',uid=999,gid=999,mode=0400']
    return args + ['a' * 64]


def option(args, name):
    return args[args.index(name) + 1]


def test_canary_clones_actual_limits_network_user_namespace_and_mount_kinds():
    module = product(); source = database_args(); original = copy.deepcopy(source)
    plan = module.canary_plan(source, NONCE)
    command = plan['create_command']
    assert source == original and command[-1] == 'a' * 64
    for key in ('--network', '--memory', '--cpus'):
        assert option(command, key) == option(source, key)
    assert '--restart=no' in command and '--pull=never' in command
    assert '--userns' not in command and '--user' not in command
    assert '--entrypoint=/bin/true' in command
    assert option(command, '--name') != option(source, '--name')
    assert option(command, '--volume').split(':', 1)[1] == '/var/lib/postgresql'
    assert option(command, '--volume').split(':', 1)[0] != option(source, '--volume').split(':', 1)[0]
    original_secrets = [source[i+1] for i,a in enumerate(source) if a == '--secret']
    canary_secrets = [command[i+1] for i,a in enumerate(command) if a == '--secret']
    assert len(canary_secrets) == len(original_secrets) == 3
    assert [v.split(',',1)[1] for v in canary_secrets] == [v.split(',',1)[1] for v in original_secrets]
    assert not set(canary_secrets) & set(original_secrets)
    assert plan['public_mount_bytes'] == b'R263 PUBLIC NONCREDENTIAL MOUNT FIXTURE\n'
    assert '--network-alias=db' not in command


@pytest.mark.parametrize('change', ['image', 'name', 'network', 'memory', 'cpu',
    'owner', 'secret', 'env', 'mount', 'extra', 'nonce'])
def test_unrecognized_database_recipe_or_identity_refuses_canary_before_effects(change):
    module = product(); args = database_args(); nonce = NONCE
    if change == 'image': args[-1] = 'latest'
    elif change == 'name': args[args.index('--name') + 1] = 'foreign_db'
    elif change == 'network': args[args.index('--network') + 1] = 'host'
    elif change == 'memory': args[args.index('--memory') + 1] = 'unbounded'
    elif change == 'cpu': args[args.index('--cpus') + 1] = '0'
    elif change == 'owner': args[args.index('--label') + 1] = 'com.kaidera.candidate=foreign'
    elif change == 'secret': args[args.index('--secret') + 1] = 'foreign-secret'
    elif change == 'env': args[args.index('--env') + 1] = 'PRIVATE_VALUE=not-authorized'
    elif change == 'mount': args[args.index('--volume') + 1] = '/private:/var/lib/postgresql'
    elif change == 'extra': args.insert(-1, '--privileged')
    elif change == 'nonce': nonce = '../unsafe'
    with pytest.raises(module.DiagnosticsRefused): module.canary_plan(args, nonce)


class NativeRefusal(RuntimeError):
    pass


class InterceptedBuilder:
    """Explicit process/engine boundary; no container is created on this host."""
    def __init__(self, target='linux-x86_64'):
        self.prefix = ['/usr/bin/podman', '--remote=false']
        self.target = target; self.commands = []; self.objects = {}; self.created = []

    def run(self, args, *, read=False, input_data=None, timeout=90, allowed=(0,)):
        self.commands.append((list(args), input_data))
        if args[:2] == ['info', '--format=json']: return json.dumps(engine_fixture())
        if args[:2] == ['version', '--format=json']: return json.dumps({'Client': {'Version': '6.0.2'}})
        if len(args) >= 2 and args[1] == 'exists': return '0' if args[-1] in self.objects else '1'
        if len(args) >= 2 and args[1] == 'inspect': return self.objects[args[-1]]
        if len(args) >= 2 and args[1] == 'rm': self.objects.pop(args[-1], None); return '0'
        return '0'


@pytest.mark.parametrize('canary_status,actual_status', [(0,125), (125,125), (0,0)])
def test_instrumented_real_start_runs_once_and_private_stderr_never_reaches_public_report(tmp_path, monkeypatch, canary_status, actual_status):
    module = product(); private = secrets.token_urlsafe(32).encode(); captured = []
    monkeypatch.setattr(module, 'native_context', lambda target: None)
    monkeypatch.setattr(module.shutil, 'which', lambda name: '/usr/bin/' + name)
    diagnostic = module.instrument_builder(InterceptedBuilder, tmp_path / 'diagnostics', SOURCE)
    engine = diagnostic('linux-x86_64')
    def capture(prefix, args, *, input_data=None, timeout=90, public=False):
        captured.append((list(args), input_data, public))
        if public:
            if args[:2] in (['volume', 'create'], ['secret', 'create']):
                engine.objects[args[-1] if args[-1] != '-' else args[-2]] = NONCE
            elif args[0] == 'create': engine.objects[option(args, '--name')] = NONCE
            return {'exit_code': canary_status if args[0]=='start' else 0,
                    'stdout': b'', 'stderr': b'Error: pasta failed with exit code 1\n' if args[0]=='start' and canary_status else b'',
                    'timed_out': False, 'overflow': False}
        return {'exit_code': actual_status, 'stdout': b'',
                'stderr': b'Error: pasta failed with exit code 1\n' + private,
                'timed_out': False, 'overflow': False}
    monkeypatch.setattr(module, 'capture_command', capture)
    monkeypatch.setattr(module, 'new_nonce', lambda: NONCE)
    engine.run(database_args())
    actual = ['start', option(database_args(), '--name')]
    if actual_status:
        with pytest.raises(Exception): engine.run(actual)
    else: assert engine.run(actual) == '0'
    starts = [c for c in captured if c[0][0] == 'start']
    assert len(starts) == 2 and starts[-1][0] == actual and starts[0][2] is True and starts[1][2] is False
    public = b'\n'.join(p.read_bytes() for p in (tmp_path / 'diagnostics').glob('*.json'))
    assert bool(private not in public)
    assert any(p.name.startswith('engine') for p in (tmp_path / 'diagnostics').glob('*.json'))
    assert all(c[1] in (None, b'R263 PUBLIC NONCREDENTIAL MOUNT FIXTURE\n') for c in captured if c[2])
    assert not engine.objects


@pytest.mark.parametrize('system,machine,github,target', [
    ('Darwin','arm64','true','linux-x86_64'), ('Linux','aarch64','true','linux-x86_64'),
    ('Linux','x86_64','false','linux-x86_64'), ('Windows','AMD64','true','linux-x86_64'),
    ('Linux','x86_64','true','macos-arm64')])
def test_unsupported_native_context_refuses_before_any_diagnostic_effect(monkeypatch, system, machine, github, target):
    module = product(); monkeypatch.setattr(module.platform,'system',lambda:system)
    monkeypatch.setattr(module.platform,'machine',lambda:machine); monkeypatch.setenv('GITHUB_ACTIONS',github)
    with pytest.raises(module.DiagnosticsRefused): module.native_context(target)


@pytest.mark.parametrize('fail', [False, True])
def test_linux_driver_delegates_once_and_restores_rehearsal_builder(tmp_path, monkeypatch, fail):
    module = product(); monkeypatch.setattr(module,'native_context',lambda target:None)
    rehearsal = SimpleNamespace(NativeBuilder=InterceptedBuilder); calls=[]
    args=SimpleNamespace(target='linux-x86_64',source_sha=SOURCE,version='0.2.001-test.20261006.1',
                         output=tmp_path/'images',diagnostics_output=tmp_path/'diagnostics')
    def delegate(argv):
        calls.append(argv)
        assert rehearsal.NativeBuilder is not InterceptedBuilder
        if fail: raise NativeRefusal('intercepted original build failure')
    if fail:
        with pytest.raises(NativeRefusal): module.run_images(args, delegate=delegate, rehearsal=rehearsal)
    else: module.run_images(args, delegate=delegate, rehearsal=rehearsal)
    assert rehearsal.NativeBuilder is InterceptedBuilder and len(calls)==1
    assert calls[0]==['images','--target','linux-x86_64','--source-sha',SOURCE,
                      '--version',args.version,'--output',str(args.output)]


def test_actual_capture_reports_exit_and_retains_private_bytes_only_in_memory():
    module = product(); private = secrets.token_urlsafe(32).encode()
    script = 'import os; v=os.read(0,4096); os.write(2,v); raise SystemExit(125)'
    value = module.capture_command([sys.executable], ['-c', script], input_data=private, timeout=2)
    assert value['exit_code'] == 125 and value['stdout'] == b''
    assert bool(value['stderr'] == private) and value['timed_out'] is False and value['overflow'] is False


def test_actual_capture_has_one_process_deadline():
    module = product(); start = time.monotonic()
    value = module.capture_command([sys.executable], ['-c', 'import time;time.sleep(3)'], timeout=.15)
    assert time.monotonic() - start < 1.5
    assert value['timed_out'] is True


def test_actual_capture_stops_excess_output_at_bounded_buffers():
    module = product()
    value = module.capture_command([sys.executable], ['-c', 'import os;os.write(2,b"x"*5000000)'], timeout=2)
    assert value['overflow'] is True and len(value['stderr']) <= 65536


def test_linux_workflow_routes_to_diagnostics_driver_and_retains_failure_evidence():
    value=yaml.load((ROOT/'.github/workflows/cortex-linux-candidate.yml').read_text(),Loader=yaml.BaseLoader)
    steps=value['jobs']['images']['steps']
    build=next(s for s in steps if s.get('name')=='Build immutable OCI images and package inventory')
    assert 'python3 scripts/release/linux_ci_diagnostics.py' in build['run']
    assert '--diagnostics-output "$RUNNER_TEMP/cortex-linux-diagnostics"' in build['run']
    uploads=[s for s in steps if s.get('with',{}).get('name')=='cortex-linux-diagnostics-${{ github.sha }}']
    assert len(uploads)==1 and uploads[0]['if']=='always()'
    assert uploads[0]['with']['retention-days']=='3'
    assert uploads[0]['with']['path']=='${{ runner.temp }}/cortex-linux-diagnostics'
    host_bytes=json.dumps(value['jobs']['host'],sort_keys=True,separators=(',',':')).encode()
    assert hashlib.sha256(host_bytes).hexdigest()=='0a10f59d11f76f4ddbdfc2b590d8894da2fd7b526640a8b88866e0aa2c8dcd5d'
