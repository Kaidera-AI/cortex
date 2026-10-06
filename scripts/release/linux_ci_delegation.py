"""Linux CI only: safely activate and verify rootless CPU delegation."""
from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import time

from linux_ci_diagnostics import (ROOT, DiagnosticsRefused, _write, capture_command,
                                  engine_report, native_context)

DROPIN = b'[Service]\nDelegate=cpu cpuset io memory pids\n'
PRIVILEGED = ['sudo', '-n', 'timeout', '--kill-after=2s', '20s']
REQUIRED = ['cpu', 'memory', 'pids']


class DelegationRefused(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__('CI CPU delegation refused: ' + code)


def _placement(path: Path) -> bytes:
    """Keep raw job placement local; unknown placement never authorizes restart."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError
            raw = stream.read(65537)
        if not raw or len(raw) > 65536 or not raw.endswith(b'\n'):
            raise ValueError
        line = raw[:-1].decode('ascii', errors='strict')
        if (not line.startswith('0::/') or any(ord(c) < 32 or ord(c) > 126 for c in line)):
            raise ValueError
        cgroup = line[3:]
        parts = cgroup[1:].split('/') if cgroup != '/' else []
        if any(not part or part in ('.', '..') or any(ord(c) < 33 or ord(c) > 126 for c in part)
               for part in parts):
            raise ValueError
        # Refuse every user-manager segment, including an unrecognized UID.
        if any(part.startswith('user@') for part in parts):
            raise DelegationRefused('job_inside_user_manager')
        return raw
    except DelegationRefused:
        raise
    except (OSError, ValueError, UnicodeError, TypeError):
        raise DelegationRefused('job_placement_unavailable') from None


def prepare_delegation(*, cgroup_path: Path = Path('/proc/self/cgroup'),
                       capture=capture_command) -> dict:
    native_context('linux-x86_64')
    uid = os.getuid()
    if uid == 0 or os.geteuid() != uid:
        raise DelegationRefused('nonroot_runner_uid_required')
    placement = _placement(cgroup_path)
    deadline = time.monotonic() + 120

    def command(prefix, args, *, data=None, limit=25):
        remaining = min(limit, deadline - time.monotonic())
        if remaining <= 0:
            raise DelegationRefused('command_deadline')
        try:
            result = capture(prefix, args, input_data=data, timeout=remaining)
            if (type(result['exit_code']) is not int or result['exit_code'] != 0
                    or result['timed_out'] is not False or result['overflow'] is not False
                    or not isinstance(result['stdout'], bytes) or len(result['stdout']) > 1048576
                    or not isinstance(result['stderr'], bytes) or len(result['stderr']) > 65536
                    or time.monotonic() >= deadline):
                raise ValueError
            return result['stdout']
        except (OSError, ValueError, TypeError, KeyError, DiagnosticsRefused):
            raise DelegationRefused('command_refused') from None

    command(PRIVILEGED, ['mkdir', '-p', '/etc/systemd/system/user@.service.d'])
    command(PRIVILEGED, ['tee', '/etc/systemd/system/user@.service.d/90-cortex-ci-delegate.conf'], data=DROPIN)
    command(PRIVILEGED, ['systemctl', 'daemon-reload'])
    # A manager restart cannot be authorized by an earlier placement snapshot.
    if _placement(cgroup_path) != placement:
        raise DelegationRefused('job_placement_changed')
    command(PRIVILEGED, ['systemctl', 'restart', 'user@' + str(uid) + '.service'])
    if _placement(cgroup_path) != placement:
        raise DelegationRefused('job_placement_changed')
    info = command(['podman', '--remote=false'], ['info', '--format=json'], limit=5)
    version = command(['podman', '--remote=false'], ['version', '--format=json'], limit=5)
    try:
        report = engine_report(json.loads(info.decode('utf-8', errors='strict')),
                               json.loads(version.decode('utf-8', errors='strict')),
                               presence={name: shutil.which(name) is not None
                                         for name in ('pasta', 'slirp4netns')})
        if (report['cgroup_version'] != 'v2' or report['rootless'] is not True
                or not set(REQUIRED) <= set(report['cgroup_controllers'])):
            raise ValueError
    except (ValueError, TypeError, KeyError, UnicodeError, DiagnosticsRefused):
        raise DelegationRefused('effective_cpu_memory_pids_required') from None
    return {'schema': 'cortex.linux-ci-delegation.v1', 'status': 'ready', 'uid': uid,
            'job_outside_user_manager': True, 'required_controllers': list(REQUIRED),
            'engine': report}


def _public_stream(raw, name: str) -> dict:
    """R283 allows only the fixed commands' secret-free diagnostic streams."""
    available = isinstance(raw, bytes)
    bounded = raw[:4096] if available else b''
    return {name + '_b64': base64.b64encode(bounded).decode('ascii'),
            name + '_utf8': bounded.decode('utf-8', errors='replace'),
            name + '_available': available,
            name + '_truncated': available and len(raw) > 4096}


def observe_delegation(*, cgroup_path: Path = Path('/proc/self/cgroup'),
                       capture=capture_command) -> dict:
    """Public facts and fixed stages; retain partial proof on every refusal."""
    native_context('linux-x86_64')
    uid = os.getuid()
    value = {'schema': 'cortex.linux-ci-delegation.v1', 'status': 'refused',
             'uid': uid, 'required_controllers': list(REQUIRED), 'facts': {}, 'steps': []}
    deadline = time.monotonic() + 120
    readonly = [
        ('loginctl_user', ['loginctl'], ['show-user', str(uid), '-p', 'Linger', '-p', 'State']),
        ('systemctl_user', ['systemctl'], ['show', 'user@' + str(uid) + '.service',
            '-p', 'ActiveState', '-p', 'SubState', '-p', 'Result', '-p', 'ControlGroup']),
    ]
    stages = {(tuple(prefix), tuple(args)): stage for stage, prefix, args in readonly}
    for stage, prefix, args in [
        ('mkdir_dropin', PRIVILEGED, ['mkdir', '-p', '/etc/systemd/system/user@.service.d']),
        ('write_dropin', PRIVILEGED, ['tee', '/etc/systemd/system/user@.service.d/90-cortex-ci-delegate.conf']),
        ('reload_systemd', PRIVILEGED, ['systemctl', 'daemon-reload']),
        ('restart_user_manager', PRIVILEGED, ['systemctl', 'restart', 'user@' + str(uid) + '.service']),
        ('engine_info', ['podman', '--remote=false'], ['info', '--format=json']),
        ('engine_version', ['podman', '--remote=false'], ['version', '--format=json']),
    ]:
        stages[(tuple(prefix), tuple(args))] = stage

    def observed(prefix, args, *, input_data=None, timeout=90, public=False):
        stage = stages.get((tuple(prefix), tuple(args)))
        if stage is None or input_data != (DROPIN if stage == 'write_dropin' else None):
            raise DiagnosticsRefused('capture scope refused')
        step = {'stage': stage, 'exit_code': None, 'timed_out': None,
                'overflow': None, 'launch_status': 'unknown', **_public_stream(None, 'stderr')}
        value['steps'].append(step)
        try:
            remaining = min(timeout, deadline - time.monotonic())
            if remaining <= 0:
                step['timed_out'] = True
                raise DiagnosticsRefused('capture deadline')
            result = capture(prefix, args, input_data=input_data, timeout=remaining, public=False)
            if not isinstance(result, dict):
                raise DiagnosticsRefused('capture result unavailable')
            step.update(_public_stream(result.get('stderr'), 'stderr'))
            valid = (type(result.get('exit_code')) is int and -128 <= result['exit_code'] <= 255
                     and type(result.get('timed_out')) is bool and type(result.get('overflow')) is bool
                     and isinstance(result.get('stdout'), bytes) and len(result['stdout']) <= 1048576
                     and isinstance(result.get('stderr'), bytes) and len(result['stderr']) <= 65536)
            if not valid:
                raise DiagnosticsRefused('capture result unavailable')
            step.update(exit_code=result['exit_code'], timed_out=result['timed_out'],
                        overflow=result['overflow'] or step['stderr_truncated'], launch_status='started')
            return {**result, 'overflow': step['overflow']}
        except Exception:
            raise DiagnosticsRefused('capture unavailable') from None

    try:
        if uid == 0 or os.geteuid() != uid:
            raise DelegationRefused('nonroot_runner_uid_required')
        try:
            fd = os.open(cgroup_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, 'rb') as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise ValueError
                raw = stream.read(4097)
            value['facts']['cgroup'] = _public_stream(raw, 'stdout')
            if value['facts']['cgroup']['stdout_truncated']:
                raise ValueError
        except (OSError, ValueError, TypeError):
            raise DelegationRefused('job_placement_unavailable') from None
        for stage, prefix, args in readonly:
            try:
                result = observed(prefix, args, timeout=5)
                value['facts'][stage] = _public_stream(result['stdout'], 'stdout')
                if value['facts'][stage]['stdout_truncated']:
                    value['steps'][-1]['overflow'] = True
                if (result['exit_code'] != 0 or result['timed_out'] or result['overflow']
                        or value['facts'][stage]['stdout_truncated']):
                    raise DiagnosticsRefused('public facts unavailable')
            except DiagnosticsRefused:
                raise DelegationRefused('public_facts_unavailable') from None
        value.update(prepare_delegation(cgroup_path=cgroup_path, capture=observed))
    except DelegationRefused as error:
        value.update(status='refused', reason=error.code)
    return value


def observe_readonly_delegation(*, capture=capture_command) -> dict:
    """R288 CI path: observe current delegation; never change or restart systemd."""
    native_context('linux-x86_64')
    uid = os.getuid()
    value = {'schema': 'cortex.linux-ci-delegation.v1', 'status': 'refused',
             'uid': uid, 'required_controllers': list(REQUIRED), 'facts': {}, 'steps': []}
    deadline = time.monotonic() + 15

    def read(stage, prefix, args, *, fact=False):
        step = {'stage': stage, 'exit_code': None, 'timed_out': None,
                'overflow': None, 'launch_status': 'unknown', **_public_stream(None, 'stderr')}
        value['steps'].append(step)
        try:
            remaining = min(5, deadline - time.monotonic())
            if remaining <= 0 or os.getuid() != uid or os.geteuid() != uid:
                raise ValueError
            result = capture(prefix, args, input_data=None, timeout=remaining, public=False)
            if not isinstance(result, dict):
                raise ValueError
            step.update(_public_stream(result.get('stderr'), 'stderr'))
            if fact:
                value['facts'][stage] = _public_stream(result.get('stdout'), 'stdout')
            if (type(result.get('exit_code')) is not int or not -128 <= result['exit_code'] <= 255
                    or type(result.get('timed_out')) is not bool or type(result.get('overflow')) is not bool
                    or not isinstance(result.get('stdout'), bytes) or len(result['stdout']) > 1048576
                    or not isinstance(result.get('stderr'), bytes) or len(result['stderr']) > 65536):
                raise ValueError
            overflow = (result['overflow'] or step['stderr_truncated']
                        or fact and value['facts'][stage]['stdout_truncated'])
            step.update(exit_code=result['exit_code'], timed_out=result['timed_out'],
                        overflow=bool(overflow), launch_status='started')
            if (result['exit_code'] != 0 or result['timed_out'] or overflow
                    or time.monotonic() >= deadline or os.getuid() != uid or os.geteuid() != uid):
                raise ValueError
            return result['stdout']
        except Exception:
            raise DelegationRefused('command_refused') from None

    def engine_json(raw):
        def unique(pairs):
            result = {}
            for key, item in pairs:
                if key in result:
                    raise ValueError
                result[key] = item
            return result
        def nonfinite(value):
            raise ValueError
        return json.loads(raw.decode('utf-8', errors='strict'),
                          object_pairs_hook=unique, parse_constant=nonfinite)

    try:
        if uid == 0 or os.geteuid() != uid:
            raise DelegationRefused('nonroot_runner_uid_required')
        properties = read('systemctl_delegate', ['systemctl'],
            ['show', 'user@' + str(uid) + '.service', '-p', 'Delegate', '-p', 'DelegateControllers'], fact=True)
        try:
            if (not properties.endswith(b'\n') or b'\r' in properties
                    or any(c < 32 and c != 10 or c > 126 for c in properties)):
                raise ValueError
            rows = [line.split('=', 1) for line in properties[:-1].decode('ascii').split('\n')]
            if len(rows) != 2 or any(len(row) != 2 for row in rows) or len({row[0] for row in rows}) != 2:
                raise ValueError
            parsed = dict(rows)
            controllers = parsed['DelegateControllers'].split(' ')
            known = {'cpu', 'cpuset', 'io', 'memory', 'pids', 'hugetlb', 'rdma', 'misc'}
            if (set(parsed) != {'Delegate', 'DelegateControllers'} or parsed['Delegate'] != 'yes'
                    or any(c not in known for c in controllers) or len(set(controllers)) != len(controllers)
                    or not set(REQUIRED) <= set(controllers)):
                raise ValueError
            value['systemd'] = {'delegate': True, 'controllers': controllers}
        except (ValueError, KeyError, TypeError, UnicodeError):
            raise DelegationRefused('systemd_delegation_unavailable') from None
        info_raw = read('engine_info', ['podman', '--remote=false'], ['info', '--format=json'])
        version_raw = read('engine_version', ['podman', '--remote=false'], ['version', '--format=json'])
        try:
            report = engine_report(engine_json(info_raw), engine_json(version_raw),
                presence={name: shutil.which(name) is not None for name in ('pasta', 'slirp4netns')})
            value['engine'] = report
            if (report['cgroup_version'] != 'v2' or report['rootless'] is not True
                    or not set(REQUIRED) <= set(report['cgroup_controllers'])
                    or time.monotonic() >= deadline):
                raise ValueError
        except (ValueError, KeyError, TypeError, UnicodeError, RecursionError, DiagnosticsRefused):
            raise DelegationRefused('effective_cpu_memory_pids_required') from None
        value['status'] = 'ready'
    except DelegationRefused as error:
        value.update(status='refused', reason=error.code)
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--read-only', action='store_true', help='observe existing delegation without privileged changes')
    parser.add_argument('--source-sha', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    native_context('linux-x86_64')
    if not re.fullmatch(r'[0-9a-f]{40}', args.source_sha):
        parser.error('exact source identity required')
    output = args.output
    if (not output.is_absolute() or '..' in output.parts or output == ROOT or ROOT in output.parents
            or any(path.is_symlink() for path in (output, *output.parents))):
        parser.error('physical CI diagnostics output outside source required')
    observer = observe_readonly_delegation if args.read_only else observe_delegation
    value = {**observer(capture=capture_command), 'source_sha': args.source_sha}
    _write(output, 'delegation.json', value)
    if value['status'] != 'ready':
        print(json.dumps(value, sort_keys=True), file=sys.stderr)
        raise SystemExit(1) from None
    print(json.dumps(value, sort_keys=True))


if __name__ == '__main__':
    main()
