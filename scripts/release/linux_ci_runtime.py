"""Job-owned Linuxbrew runtime selection and one pinned-base native preflight."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import stat
import subprocess
import sys
from linux_ci_storage import physical_path

BREW = Path('/home/linuxbrew/.linuxbrew')
MESSAGE = 'native CI runtime qualification refused'
HELPERS = {'crun': ['bin/crun'], 'conmon': ['bin/conmon'],
           'podman': ['libexec/podman/rootlessport', 'libexec/podman/netavark', 'libexec/podman/aardvark-dns'],
           'passt': ['bin/pasta'], 'fuse-overlayfs': ['bin/fuse-overlayfs']}


def _helpers(prefix):
    prefix = Path(prefix); physical_path(prefix)
    for formula, names in HELPERS.items():
        for name in names:
            path = prefix / 'opt' / formula / name
            actual = path.resolve(strict=True)
            if (not actual.is_relative_to(prefix / 'Cellar' / formula)
                    or not actual.is_file() or not os.access(path, os.X_OK)):
                raise ValueError
    return prefix


def _private(path):
    physical_path(path); info = path.lstat()
    if (os.getuid() == 0 or os.geteuid() != os.getuid() or info.st_uid != os.getuid()
            or not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o7077):
        raise ValueError


def _write(path, value):
    physical_path(path.parent)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        stream.write(value); stream.flush(); os.fsync(stream.fileno())


def configure(runner_temp, *, brew_prefix=BREW):
    try:
        runner = Path(runner_temp); physical_path(runner)
        parent = runner / 'cortex-podman'; _private(parent)
        prefix = _helpers(brew_prefix)
        config = parent / 'containers.conf'
        conmon = str(prefix / 'opt/conmon/bin/conmon')
        crun = str(prefix / 'opt/crun/bin/crun')
        directories = [str(prefix / p) for p in ('opt/podman/libexec/podman', 'opt/passt/bin', 'opt/fuse-overlayfs/bin')]
        value = '[engine]\nconmon_path = ' + json.dumps([conmon]) + '\nruntime = "crun"\nhelper_binaries_dir = ' + json.dumps(directories) + '\n[engine.runtimes]\ncrun = ' + json.dumps([crun]) + '\n'
        _write(config, value)
        return config
    except Exception:
        raise RuntimeError(MESSAGE) from None


def _json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result: raise ValueError
            result[key] = value
        return result
    if not isinstance(raw, str) or len(raw.encode()) > 1048576: raise ValueError
    value = json.loads(raw, object_pairs_hook=unique, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    if not isinstance(value, dict): raise ValueError
    return value


def qualify(run, *, source_root, config, destination, brew_prefix=BREW):
    try:
        prefix = _helpers(brew_prefix); config = Path(config)
        _private(config.parent); physical_path(config)
        info = config.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
            raise ValueError
        import tomllib
        raw_config = config.read_bytes(); parsed = tomllib.loads(raw_config.decode())['engine']
        expected = {'conmon_path': [str(prefix / 'opt/conmon/bin/conmon')], 'runtime': 'crun',
                    'helper_binaries_dir': [str(prefix / p) for p in ('opt/podman/libexec/podman', 'opt/passt/bin', 'opt/fuse-overlayfs/bin')],
                    'runtimes': {'crun': [str(prefix / 'opt/crun/bin/crun')]}}
        if parsed != expected: raise ValueError
        observed = _json(run(['podman', '--remote=false', 'info', '--format', 'json'], read=True))
        runtime, conmon = observed['host']['ociRuntime'], observed['host']['conmon']
        for value, formula, binary in [(runtime, 'crun', 'crun'), (conmon, 'conmon', 'conmon')]:
            if (not isinstance(value.get('version'), str) or not value['version'].strip()
                    or len(value['version'].encode()) > 8192
                    or Path(value['path']).resolve(strict=True) != (prefix / 'opt' / formula / 'bin' / binary).resolve(strict=True)):
                raise ValueError
        if runtime.get('name') != 'crun': raise ValueError
        version = run(['podman', '--remote=false', 'version', '--format', '{{.Client.Version}}'], read=True)
        from podman_policy import validate_local_version
        validate_local_version(version)
        recipe = Path(source_root) / 'deploy/release/Dockerfile.linux-amd64'; physical_path(recipe)
        first = recipe.read_text().splitlines()[0]
        match = re.fullmatch(r'FROM (docker.io/library/python:([0-9]+\.[0-9]+\.[0-9]+)-[a-z0-9-]+@sha256:[a-f0-9]{64}) AS builder', first)
        if match is None: raise ValueError
        base, python_version = match.groups()
        command = ['podman', '--remote=false', 'run', '--rm', '--name=kaidera-test-cortex-runtime',
                   '--read-only', '--network=none', '--cap-drop=ALL', '--security-opt=no-new-privileges',
                   '--user=65534:65534', '--entrypoint=python', base, '-B', '-c',
                   'import json,platform;print(json.dumps(dict(machine=platform.machine(),python=platform.python_version())))']
        execution = _json(run(command, read=True))
        if execution != {'machine': 'x86_64', 'python': python_version} or config.read_bytes() != raw_config:
            raise ValueError
        result = {'schema': 'cortex.native-runtime-preflight.v1', 'status': 'PASS',
                  'runtime': {k: runtime[k] for k in ('name', 'path', 'version')},
                  'conmon': {k: conmon[k] for k in ('path', 'version')}, 'podman_version': version,
                  'base': base, 'execution': execution, 'config_sha256': hashlib.sha256(raw_config).hexdigest()}
        _write(Path(destination), json.dumps(result, indent=2) + '\n')
        return result
    except Exception:
        raise RuntimeError(MESSAGE) from None


def _publish(path, config):
    physical_path(path)
    fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'w') as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
                or (info.st_size and os.pread(stream.fileno(), 1, info.st_size - 1) != b'\n')):
            raise ValueError
        stream.write('CONTAINERS_CONF=' + str(config) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage', choices=('configure', 'qualify'))
    parser.add_argument('--runner-temp', type=Path, required=True)
    parser.add_argument('--github-env', type=Path)
    parser.add_argument('--receipt', type=Path)
    args = parser.parse_args()
    try:
        if (os.environ.get('GITHUB_ACTIONS') != 'true' or platform.system() != 'Linux'
                or platform.machine() != 'x86_64' or str(args.runner_temp) != os.environ.get('RUNNER_TEMP')
                or os.environ.get('CONTAINERS_CONF_OVERRIDE')):
            raise ValueError
        if args.stage == 'configure':
            if not args.github_env or str(args.github_env) != os.environ.get('GITHUB_ENV'): raise ValueError
            config = configure(args.runner_temp); _publish(args.github_env, config)
            print(json.dumps({'scope': 'job-owned runtime configuration', 'CONTAINERS_CONF': str(config)}))
        else:
            config = args.runner_temp / 'cortex-podman/containers.conf'
            if not args.receipt or str(config) != os.environ.get('CONTAINERS_CONF'): raise ValueError
            def run(command, *, read=False):
                result = subprocess.run(command, capture_output=True, text=True, timeout=180)
                if result.returncode:
                    print(result.stderr[-8192:], file=sys.stderr)
                    raise RuntimeError(MESSAGE)
                return result.stdout.strip()
            print(json.dumps(qualify(run, source_root=Path(__file__).resolve().parents[2],
                                    config=config, destination=args.receipt)))
        return 0
    except Exception:
        print(MESSAGE, file=sys.stderr); return 2

if __name__ == '__main__':
    raise SystemExit(main())
