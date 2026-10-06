"""Linux CI only: safely activate and verify rootless CPU delegation."""
from __future__ import annotations

import argparse
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
        lines = raw.decode('ascii', errors='strict').splitlines()
        if len(lines) != 1 or not lines[0].startswith('0::/'):
            raise ValueError
        cgroup = lines[0][3:]
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
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
    try:
        value = prepare_delegation()
    except DelegationRefused as error:
        value = {'schema': 'cortex.linux-ci-delegation.v1', 'status': 'refused', 'reason': error.code,
                 'uid': os.getuid(), 'required_controllers': list(REQUIRED)}
        _write(output, 'delegation.json', {**value, 'source_sha': args.source_sha})
        print(json.dumps(value, sort_keys=True), file=sys.stderr)
        raise SystemExit(1) from None
    _write(output, 'delegation.json', {**value, 'source_sha': args.source_sha})
    print(json.dumps(value, sort_keys=True))


if __name__ == '__main__':
    main()
