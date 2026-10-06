"""Linux CI only: one public startup canary and private real-start classification.

The shared builder and installer retain their bytes and runtime policy. Nothing
from the real container's stderr, including a digest, leaves process memory.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
import platform
import re
import selectors
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LABEL = 'com.kaidera.candidate'
PUBLIC_MOUNT = b'R263 PUBLIC NONCREDENTIAL MOUNT FIXTURE\n'
STDERR_LIMIT = 65536
STDOUT_LIMIT = 1024 * 1024
SIGNATURES = (
    (b'Error: pasta failed with exit code ', 'pasta_failed', 'pasta'),
    (b'Error: could not find pasta or slirp4netns', 'rootless_helper_missing', 'podman'),
    (b'Error: setting up network namespace: exec: "pasta": executable file not found in $PATH', 'pasta_missing', 'pasta'),
    (b'Error: OCI runtime error: crun: operation not permitted', 'crun_failed', 'crun'),
    (b'crun: create cgroup: Permission denied', 'cgroup_permission', 'crun'),
    (b'Error: unable to start container process: conmon failed', 'conmon_failed', 'conmon'),
    (b'Error: netavark: iptables: executable file not found', 'network_backend_failed', 'podman'),
    (b'Error: creating cgroup: operation not permitted', 'cgroup_permission', 'podman'),
)


class DiagnosticsRefused(RuntimeError):
    """Only fixed, nonsecret messages may be used here."""


def classify_stderr(raw: bytes) -> dict:
    unknown = {'signature': 'unknown', 'component': 'unknown'}
    if not isinstance(raw, bytes) or len(raw) > STDERR_LIMIT:
        return unknown
    try:
        raw.decode('utf-8', errors='strict')
    except UnicodeDecodeError:
        return unknown
    for line in raw.splitlines():
        for prefix, signature, component in SIGNATURES:
            if line.startswith(prefix):
                return {'signature': signature, 'component': component}
    return unknown


def engine_report(info: dict, version: dict, *, presence: dict) -> dict:
    """Project known fields; never forward arbitrary values from engine output."""
    def choice(value, options):
        if not isinstance(value, str) or value not in options:
            raise DiagnosticsRefused('engine field unavailable')
        return value

    def boolean(value):
        if type(value) is not bool:
            raise DiagnosticsRefused('engine boolean unavailable')
        return value

    def tool(value, name):
        path = value['path']
        # Paths can carry arbitrary user data: accept only standard locations
        # and the exact observed tool basename, never a generic absolute path.
        if not isinstance(path, str) or not re.fullmatch(
                r'(?:/usr/(?:local/)?(?:s?bin|libexec/podman)|/s?bin|/home/linuxbrew/\.linuxbrew/bin)/' + name, path):
            raise DiagnosticsRefused('engine tool path unavailable')
        raw = value['version']
        if not isinstance(raw, str) or len(raw) > 4096:
            raise DiagnosticsRefused('engine tool version unavailable')
        match = re.match(re.escape(name) + r' version ([0-9]{1,3}\.[0-9]{1,3}(?:\.[0-9]{1,3})?)(?=[,\s]|$)', raw)
        if not match:
            raise DiagnosticsRefused('engine tool version unavailable')
        return {'path': path, 'version': match[1]}

    try:
        host = info['host']
        controllers = host['cgroupControllers']
        known = {'cpu', 'cpuset', 'io', 'memory', 'pids', 'hugetlb', 'rdma', 'misc', 'devices', 'freezer', 'blkio', 'net_cls', 'net_prio', 'perf_event'}
        if (not isinstance(controllers, list) or len(controllers) > len(known)
                or any(not isinstance(c, str) or c not in known for c in controllers)
                or len(set(controllers)) != len(controllers)):
            raise DiagnosticsRefused('engine controllers unavailable')
        podman = version['Client']['Version']
        if not isinstance(podman, str) or not re.fullmatch(r'[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}', podman):
            raise DiagnosticsRefused('engine version unavailable')
        runtime_name = choice(host['ociRuntime']['name'], {'crun', 'runc'})
        runtime = tool(host['ociRuntime'], runtime_name)
        return {'schema': 'cortex.linux-ci-engine.v1',
                'cgroup_version': choice(host['cgroupVersion'], {'v1', 'v2'}),
                'cgroup_controllers': list(controllers),
                'network_backend': choice(host['networkBackend'], {'netavark', 'cni'}),
                'pasta_present': boolean(presence['pasta']),
                'slirp4netns_present': boolean(presence['slirp4netns']),
                'rootless': boolean(host['security']['rootless']),
                'storage_driver': choice(info['store']['graphDriverName'], {'overlay', 'vfs', 'btrfs', 'zfs', 'devicemapper'}),
                'podman_version': podman, 'conmon': tool(host['conmon'], 'conmon'),
                'oci_runtime': {'name': runtime_name, **runtime}}
    except (KeyError, TypeError, ValueError, AttributeError):
        raise DiagnosticsRefused('engine fields unavailable') from None


def new_nonce() -> str:
    return uuid.uuid4().hex


def canary_plan(actual: list[str], nonce: str) -> dict:
    """Recognize the immutable shared DB recipe before deriving public inputs."""
    if not isinstance(nonce, str) or not re.fullmatch(r'[0-9a-f]{32}', nonce):
        raise DiagnosticsRefused('canary identity refused')
    try:
        label = actual[actual.index('--label') + 1]
        owner = label.removeprefix(LABEL + '=')
        identity = uuid.UUID(owner)
        if str(identity) != owner or label != LABEL + '=' + owner:
            raise ValueError
        prefix = 'cortex_v2_package_test_' + identity.hex
        image = actual[-1]
        if not isinstance(image, str) or not re.fullmatch(r'(?:sha256:)?[0-9a-f]{64}', image):
            raise ValueError
        expected = ['create', '--pull=never', '--name', prefix + '_db', '--label', label,
                    '--label', 'com.kaidera.deployment-class=TEST', '--network', prefix + '_net',
                    '--memory', '512m', '--cpus', '1', '--restart=no', '--network-alias=db',
                    '--env', 'POSTGRES_USER=cortex_v2_owner', '--env', 'POSTGRES_DB=cortex_v2',
                    '--env', 'POSTGRES_PASSWORD_FILE=/run/secrets/db-owner-password',
                    '--volume', prefix + '_pgdata:/var/lib/postgresql']
        suffixes = ('db-owner-password', 'db-app-password', 'db-migrator-password')
        for suffix in suffixes:
            expected += ['--secret', prefix + '-' + suffix + ',target=' + suffix + ',uid=999,gid=999,mode=0400']
        if actual != expected + [image]:
            raise ValueError
    except (ValueError, TypeError, IndexError, AttributeError):
        raise DiagnosticsRefused('unrecognized database recipe') from None
    name = 'cortex_linux_diagnostic_' + nonce
    command = list(actual)
    command[command.index('--name') + 1] = name
    command[command.index('--label') + 1] = LABEL + '=' + nonce
    command[command.index('--volume') + 1] = name + '_pgdata:/var/lib/postgresql'
    command[command.index('--network-alias=db')] = '--network-alias=' + name
    for index, arg in enumerate(command):
        if arg == '--secret':
            suffix = command[index + 1].split(',target=', 1)[1].split(',', 1)[0]
            command[index + 1] = name + '-' + suffix + ',target=' + suffix + ',uid=999,gid=999,mode=0400'
    command.insert(-1, '--entrypoint=/bin/true')
    return {'create_command': command, 'public_mount_bytes': PUBLIC_MOUNT,
            'objects': [('volume', name + '_pgdata'),
                        *[('secret', name + '-' + s) for s in suffixes], ('container', name)],
            'nonce': nonce}


def capture_command(prefix: list[str], args: list[str], *, input_data=None, timeout=90, public=False) -> dict:
    """Bound both pipes and stdin under one deadline, with no disk or logging."""
    if (not prefix or not args or any(not isinstance(a, str) or '\x00' in a for a in prefix + args)
            or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0
            or (input_data is not None and (not isinstance(input_data, bytes) or len(input_data) > STDERR_LIMIT))
            or (public and input_data not in (None, PUBLIC_MOUNT))):
        raise DiagnosticsRefused('capture arguments refused')
    deadline = time.monotonic() + timeout
    buffers = {'stdout': bytearray(), 'stderr': bytearray()}
    timed_out = overflow = False
    process = None
    selector = selectors.DefaultSelector()
    try:
        process = subprocess.Popen(prefix + args, stdin=subprocess.PIPE if input_data is not None else subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        for name in buffers:
            stream = getattr(process, name)
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, name)
        offset = 0
        if process.stdin is not None:
            if input_data:
                os.set_blocking(process.stdin.fileno(), False)
                selector.register(process.stdin, selectors.EVENT_WRITE, 'stdin')
            else:
                process.stdin.close()
        while selector.get_map() or process.poll() is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            for key, _ in selector.select(min(remaining, .05)):
                if key.data == 'stdin':
                    try:
                        offset += os.write(key.fd, input_data[offset:offset + 8192])
                    except BrokenPipeError:
                        offset = len(input_data)
                    except BlockingIOError:
                        continue
                    if offset == len(input_data):
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
                else:
                    try:
                        chunk = os.read(key.fd, 8192)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
                        continue
                    limit = STDERR_LIMIT if key.data == 'stderr' else STDOUT_LIMIT
                    available = limit - len(buffers[key.data])
                    buffers[key.data].extend(chunk[:available])
                    if len(chunk) > available:
                        overflow = True
                        break
            if overflow:
                break
        if timed_out or overflow:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.wait(timeout=max(.25, deadline - time.monotonic()))
        return {'exit_code': process.returncode, **{k: bytes(v) for k, v in buffers.items()},
                'timed_out': timed_out, 'overflow': overflow}
    except (OSError, subprocess.TimeoutExpired):
        raise DiagnosticsRefused('diagnostic process unavailable') from None
    finally:
        if process is not None:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=.5)
            for name in ('stdin', 'stdout', 'stderr'):
                stream = getattr(process, name)
                if stream is not None:
                    stream.close()
        selector.close()


def native_context(target):
    if (target != 'linux-x86_64' or platform.system() != 'Linux'
            or platform.machine() != 'x86_64' or os.environ.get('GITHUB_ACTIONS') != 'true'):
        raise DiagnosticsRefused('diagnostics require native Linux x86_64 CI')


def _write(output: Path, name: str, value: dict):
    output = Path(output)
    if not output.is_absolute() or '..' in output.parts or output == ROOT or ROOT in output.parents:
        raise DiagnosticsRefused('diagnostic output refused')
    if any(p.is_symlink() for p in (output, *output.parents)):
        raise DiagnosticsRefused('linked diagnostic output refused')
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    data = json.dumps(value, sort_keys=True, indent=2).encode() + b'\n'
    if len(data) > 1024 * 1024:
        raise DiagnosticsRefused('diagnostic report size refused')
    fd = os.open(output / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def instrument_builder(original, output: Path, source_sha: str):
    if not isinstance(source_sha, str) or not re.fullmatch(r'[0-9a-f]{40}', source_sha):
        raise DiagnosticsRefused('diagnostic source identity refused')
    refusal = getattr(sys.modules.get(original.__module__), 'Refusal', DiagnosticsRefused)

    class DiagnosticBuilder(original):
        def __init__(self, target='linux-x86_64'):
            native_context(target)
            self._database_recipe = None
            self._diagnosed = False
            self._engine_available = False
            super().__init__(target)
            try:
                info_raw = super().run(['info', '--format=json'], read=True)
                version_raw = super().run(['version', '--format=json'], read=True)
                if len(info_raw) > STDOUT_LIMIT or len(version_raw) > STDERR_LIMIT:
                    raise DiagnosticsRefused('engine output size refused')
                report = engine_report(json.loads(info_raw), json.loads(version_raw),
                                       presence={n: shutil.which(n) is not None for n in ('pasta', 'slirp4netns')})
                self._engine_available = True
            except Exception:
                report = {'schema': 'cortex.linux-ci-engine.v1', 'status': 'unavailable', 'reason': 'engine fields unavailable'}
            _write(output, 'engine.json', {**report, 'source_sha': source_sha})

        def _canary(self, recipe):
            plan = canary_plan(recipe, new_nonce())
            events = []
            created = []
            status = 'unavailable'
            cleanup_ok = True

            def public(args, input_data=None):
                result = capture_command(self.prefix, args, input_data=input_data, public=True)
                events.append({'operation': args[0], 'exit_code': result['exit_code'],
                               'timed_out': result['timed_out'], 'overflow': result['overflow'],
                               'stderr': result['stderr'].decode('utf-8', errors='replace')})
                if result['exit_code'] != 0 or result['timed_out'] or result['overflow']:
                    raise DiagnosticsRefused('public canary command refused')

            def exists(kind, name):
                return super(DiagnosticBuilder, self).run([kind, 'exists', name], allowed=(0, 1)) == '0'

            def owned(kind, name):
                field = '.Config.Labels' if kind == 'container' else '.Spec.Labels' if kind == 'secret' else '.Labels'
                template = '{{index ' + field + ' "' + LABEL + '"}}'
                return super(DiagnosticBuilder, self).run([kind, 'inspect', '--format', template, name], read=True) == plan['nonce']

            try:
                if any(exists(kind, name) for kind, name in plan['objects']):
                    status = 'collision'
                    raise DiagnosticsRefused('public canary object collision')
                for kind, name in plan['objects']:
                    # Retain intent before a command which could time out after
                    # creation; never remove an object without its exact label.
                    created.append((kind, name))
                    if kind == 'container':
                        public(plan['create_command'])
                    else:
                        command = [kind, 'create', '--label', LABEL + '=' + plan['nonce'], name]
                        public(command + ['-'] if kind == 'secret' else command,
                               plan['public_mount_bytes'] if kind == 'secret' else None)
                public(['start', plan['objects'][-1][1]])
                status = 'started'
            except Exception:
                if status != 'collision':
                    status = 'refused'
            finally:
                for kind, name in reversed(created):
                    try:
                        if not exists(kind, name):
                            continue
                        if not owned(kind, name):
                            cleanup_ok = False
                            continue
                        if kind == 'container':
                            super(DiagnosticBuilder, self).run(['container', 'stop', '--time=5', name])
                            if not owned(kind, name):
                                cleanup_ok = False
                                continue
                        super(DiagnosticBuilder, self).run([kind, 'rm', name])
                        if exists(kind, name):
                            cleanup_ok = False
                    except Exception:
                        cleanup_ok = False
                _write(output, 'canary.json', {'schema': 'cortex.linux-ci-canary.v1', 'source_sha': source_sha,
                        'status': status, 'cleanup_complete': cleanup_ok, 'events': events,
                        'input': 'fixed public noncredential marker; separate empty volume',
                        'recipe': 'exact built db image; original network/limits/default user namespace/mount kinds; /bin/true'})
            # A nonzero canary start is useful evidence when the actual start
            # fails too; an incomplete canary setup cannot qualify a success.
            return status == 'started' and cleanup_ok

        def run(self, args, *, read=False, input_data=None, timeout=90, allowed=(0,)):
            recipe = self._database_recipe
            if recipe is not None and args == ['start', recipe[recipe.index('--name') + 1]] and not self._diagnosed:
                self._diagnosed = True
                canary_ok = self._canary(recipe)
                # Exactly one actual start. Neither a canary failure nor a
                # diagnostics refusal authorizes a fallback or another start.
                try:
                    result = capture_command(self.prefix, args, input_data=input_data, timeout=timeout, public=False)
                except DiagnosticsRefused:
                    _write(output, 'real-start.json', {'schema': 'cortex.linux-ci-real-start.v1', 'source_sha': source_sha,
                            'role': 'db', 'exit_code': None, 'timed_out': False, 'overflow': False,
                            'signature': 'process_unavailable', 'component': 'podman'})
                    raise refusal('Podman start unavailable; private diagnostic classified') from None
                classification = classify_stderr(result['stderr'])
                if result['timed_out'] or result['overflow']:
                    classification = {'signature': 'timeout' if result['timed_out'] else 'output_limit', 'component': 'podman'}
                _write(output, 'real-start.json', {'schema': 'cortex.linux-ci-real-start.v1', 'source_sha': source_sha,
                        'role': 'db', 'exit_code': result['exit_code'], 'timed_out': result['timed_out'],
                        'overflow': result['overflow'], **classification})
                if result['exit_code'] not in allowed or result['timed_out'] or result['overflow']:
                    raise refusal('Podman start refused; private diagnostic classified') from None
                if not canary_ok or not self._engine_available:
                    raise DiagnosticsRefused('diagnostic evidence incomplete')
                return result['stdout'].decode().strip() if read else str(result['exit_code'])
            value = super().run(args, read=read, input_data=input_data, timeout=timeout, allowed=allowed)
            if args and args[0] == 'create' and '--name' in args and args[args.index('--name') + 1].endswith('_db'):
                canary_plan(args, '0' * 32)  # Recognition only; no engine effect.
                self._database_recipe = list(args)
            return value

    return DiagnosticBuilder


def run_images(args, *, delegate=None, rehearsal=None):
    native_context(args.target)
    previous_path = list(sys.path)
    original = None
    try:
        if rehearsal is None:
            # The original images entrypoint adds scripts/ before importing
            # prepare_sandbox. Resolve that same shared path before wrapping.
            sys.path.insert(0, str(ROOT / 'scripts'))
            import package_rehearsal as rehearsal
        if delegate is None:
            spec = importlib.util.spec_from_file_location('cortex_linux_ci_original_build', ROOT / 'scripts/release/build-candidate.py')
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            def delegate(argv):
                previous = sys.argv
                try:
                    sys.argv = [str(ROOT / 'scripts/release/build-candidate.py'), *argv]
                    module.main()
                finally:
                    sys.argv = previous

        original = rehearsal.NativeBuilder
        rehearsal.NativeBuilder = instrument_builder(original, args.diagnostics_output, args.source_sha)
        return delegate(['images', '--target', args.target, '--source-sha', args.source_sha,
                         '--version', args.version, '--output', str(args.output)])
    finally:
        if original is not None:
            rehearsal.NativeBuilder = original
        sys.path[:] = previous_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', choices=('linux-x86_64',), required=True)
    parser.add_argument('--source-sha', required=True)
    parser.add_argument('--version', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--diagnostics-output', type=Path, required=True)
    run_images(parser.parse_args())


if __name__ == '__main__':
    main()
