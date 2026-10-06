"""Linux CI only: conditionally allow user namespaces for one exact Podman path."""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import time

from linux_ci_delegation import _public_stream
from linux_ci_diagnostics import ROOT, _write, capture_command, native_context

SELECTED = '/home/linuxbrew/.linuxbrew/bin/podman'
REAL_PATH = re.compile(r'/home/linuxbrew/\.linuxbrew/Cellar/podman/[0-9]+(?:\.[0-9]+){2}(?:_[0-9]+)?/bin/podman')
POLICY = Path('/sys/kernel/security/apparmor/policy/profiles')
PRIVILEGED = ['sudo', '-n', 'timeout', '--kill-after=2s', '20s']


class AppArmorRefused(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__('CI AppArmor preflight refused: ' + code)


def _selected_podman() -> dict:
    try:
        if shutil.which('podman') != SELECTED:
            raise ValueError
        path = Path(SELECTED).resolve(strict=True)
        info = path.lstat()
        if (not REAL_PATH.fullmatch(str(path)) or not stat.S_ISREG(info.st_mode)
                or info.st_mode & 0o022 or not info.st_mode & 0o111):
            raise ValueError
        return {'selected_path': SELECTED, 'resolved_path': str(path),
                'mode': format(stat.S_IMODE(info.st_mode), '04o'),
                'identity': (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns)}
    except Exception:
        raise AppArmorRefused('podman_path_unavailable') from None


def _attachment_snapshot(root: Path) -> dict:
    """Read kernel attachment metadata, never source policy or process records."""
    records = {}; pending = [root]; total = 0
    while pending:
        directory = pending.pop()
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError
        for profile in sorted(directory.iterdir()):
            if len(records) >= 1024 or profile.is_symlink() or not profile.is_dir():
                raise ValueError
            path = profile / 'attach'
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, 'rb') as stream:
                before = os.fstat(stream.fileno())
                if not stat.S_ISREG(before.st_mode):
                    raise ValueError
                raw = stream.read(4097)
                after = os.fstat(stream.fileno())
            current = path.lstat()
            identity = lambda s: (s.st_dev, s.st_ino, s.st_mtime_ns, s.st_ctime_ns)
            total += len(raw)
            if (identity(before) != identity(after) or identity(after) != identity(current)
                    or not raw or len(raw) > 4096 or total > 262144
                    or not raw.endswith(b'\n') or b'\n' in raw[:-1]
                    or any(c < 32 or c > 126 for c in raw[:-1])):
                raise ValueError
            records[str(path)] = (identity(current), raw[:-1].decode('ascii'))
            children = profile / 'profiles'
            if children.exists() or children.is_symlink():
                pending.append(children)
    return records


def _patterns(attachment: str) -> list[str]:
    # fnmatch's * also spans slashes. That deliberately overestimates coverage:
    # it can prevent a repair, but cannot authorize replacement of a profile.
    if attachment == '<unknown>' or any(c in attachment for c in ('@', '\\', '[', ']')):
        raise ValueError
    patterns = [attachment]
    while any('{' in p or '}' in p for p in patterns):
        expanded = []
        for pattern in patterns:
            if '{' not in pattern and '}' not in pattern:
                expanded.append(pattern); continue
            match = re.search(r'\{([^{}]*)\}', pattern)
            if match is None:
                raise ValueError
            choices = match.group(1).split(',')
            if len(choices) < 2:
                raise ValueError
            expanded.extend(pattern[:match.start()] + c + pattern[match.end():] for c in choices)
        if len(expanded) > 64:
            raise ValueError
        patterns = expanded
    return patterns


def profile_coverage(path: str, *, policy_root: Path = POLICY) -> dict:
    try:
        if not REAL_PATH.fullmatch(path):
            raise ValueError
        first = _attachment_snapshot(policy_root)
        if _attachment_snapshot(policy_root) != first:
            raise ValueError
        covered = False; uncertain = False
        for _, attachment in first.values():
            if attachment != '<unknown>' and not attachment.startswith('/'):
                continue  # A named profile without an attachment cannot auto-attach.
            try:
                covered |= any(fnmatch.fnmatchcase(path, p) for p in _patterns(attachment))
            except ValueError:
                uncertain = True
        if uncertain and not covered:
            raise ValueError
        return {'covered': covered, 'profile_count': len(first)}
    except Exception:
        raise AppArmorRefused('profile_coverage_unavailable') from None


def prepare_userns(*, capture=capture_command) -> dict:
    native_context('linux-x86_64')
    uid = os.getuid(); deadline = time.monotonic() + 120
    value = {'schema': 'cortex.linux-ci-apparmor.v1', 'status': 'refused', 'uid': uid,
             'facts': {}, 'steps': [], 'profile_loaded': False}
    selected = None

    def recheck():
        if time.monotonic() >= deadline or os.getuid() != uid or os.geteuid() != uid:
            raise AppArmorRefused('command_refused')
        if _selected_podman() != selected:
            raise AppArmorRefused('podman_path_changed')

    def command(stage, prefix, args, *, data=None, fact=False):
        step = {'stage': stage, 'exit_code': None, 'timed_out': None,
                'overflow': None, 'launch_status': 'unknown', **_public_stream(None, 'stderr')}
        value['steps'].append(step)
        try:
            recheck()
            result = capture(prefix, args, input_data=data,
                             timeout=min(25, deadline - time.monotonic()), public=False)
            if not isinstance(result, dict):
                raise ValueError
            step.update(_public_stream(result.get('stderr'), 'stderr'))
            if (type(result.get('exit_code')) is not int or type(result.get('timed_out')) is not bool
                    or type(result.get('overflow')) is not bool):
                raise ValueError
            step.update(exit_code=result['exit_code'], timed_out=result['timed_out'],
                        overflow=result['overflow'] or step['stderr_truncated'], launch_status='started')
            stdout = result.get('stdout')
            if fact:
                value['facts'][stage] = _public_stream(stdout, 'stdout')
            if (result['exit_code'] != 0 or step['timed_out'] or step['overflow']
                    or not isinstance(stdout, bytes) or len(stdout) > (4096 if fact else 1048576)
                    or not isinstance(result.get('stderr'), bytes)):
                raise ValueError
            recheck()
            return stdout
        except Exception:
            raise AppArmorRefused('command_refused') from None

    def coverage(stage):
        raw = command(stage, PRIVILEGED + ['/usr/bin/python3'],
            [str(Path(__file__).resolve()), '--profiles-for-path', selected['resolved_path']], fact=True)
        def unique(pairs):
            result = {}
            for key, item in pairs:
                if key in result:
                    raise ValueError
                result[key] = item
            return result
        parsed = json.loads(raw, object_pairs_hook=unique)
        if (not isinstance(parsed, dict) or set(parsed) != {'covered', 'profile_count'}
                or type(parsed['covered']) is not bool or type(parsed['profile_count']) is not int
                or not 0 <= parsed['profile_count'] <= 1024):
            raise AppArmorRefused('profile_coverage_unavailable')
        value['facts'][stage]['coverage'] = parsed
        return parsed['covered']

    try:
        if uid == 0 or os.geteuid() != uid:
            raise AppArmorRefused('nonroot_runner_uid_required')
        selected = _selected_podman()
        if (selected['selected_path'] != SELECTED or not REAL_PATH.fullmatch(selected['resolved_path'])
                or selected['mode'] not in ('0755', '0555')):
            raise AppArmorRefused('podman_path_unavailable')
        value['facts']['podman'] = {k: selected[k] for k in ('selected_path', 'resolved_path', 'mode')}
        raw = command('restriction', ['sysctl'], ['kernel.apparmor_restrict_unprivileged_userns'], fact=True)
        if raw not in (b'kernel.apparmor_restrict_unprivileged_userns = 0\n',
                       b'kernel.apparmor_restrict_unprivileged_userns = 1\n'):
            raise AppArmorRefused('restriction_unavailable')
        restriction = raw.endswith(b'1\n')
        enabled_raw = command('enabled', ['cat'], ['/sys/module/apparmor/parameters/enabled'], fact=True)
        if enabled_raw not in (b'Y\n', b'N\n'):
            raise AppArmorRefused('apparmor_state_unavailable')
        covered = coverage('coverage_before')
        if restriction and enabled_raw == b'Y\n' and not covered:
            profile = ('abi <abi/4.0>,\ninclude <tunables/global>\n\n"' + selected['resolved_path']
                       + '" flags=(unconfined) {\n  userns,\n}\n').encode('ascii')
            command('profile_load', PRIVILEGED + ['apparmor_parser'], ['-r'], data=profile)
            value['profile_loaded'] = True
            if not coverage('coverage_after'):
                raise AppArmorRefused('profile_not_effective')
        command('engine_info', [selected['resolved_path'], '--remote=false'], ['info', '--format=json'])
        value['status'] = 'ready'
    except AppArmorRefused as error:
        value.update(status='refused', reason=error.code)
    except Exception:
        value.update(status='refused', reason='public_facts_unavailable')
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profiles-for-path')
    parser.add_argument('--source-sha')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    native_context('linux-x86_64')
    if args.profiles_for_path is not None:
        if args.source_sha is not None or args.output is not None:
            parser.error('one read-only coverage query required')
        try:
            print(json.dumps(profile_coverage(args.profiles_for_path), sort_keys=True))
        except AppArmorRefused:
            print('{"covered":null,"profile_count":0}')
            raise SystemExit(1) from None
        return
    if args.source_sha is None or not re.fullmatch(r'[0-9a-f]{40}', args.source_sha):
        parser.error('exact source identity required')
    output = args.output
    if (output is None or not output.is_absolute() or '..' in output.parts
            or output == ROOT or ROOT in output.parents
            or any(path.is_symlink() for path in (output, *output.parents))):
        parser.error('physical CI diagnostics output outside source required')
    value = {**prepare_userns(capture=capture_command), 'source_sha': args.source_sha}
    _write(output, 'apparmor.json', value)
    if value['status'] != 'ready':
        print(json.dumps(value, sort_keys=True), file=sys.stderr)
        raise SystemExit(1) from None
    print(json.dumps(value, sort_keys=True))


if __name__ == '__main__':
    main()
