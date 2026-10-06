"""Finite Linux host process port. Private frames never enter argv or logs."""
from __future__ import annotations

from contextlib import contextmanager
import contextvars
import copy
import fcntl
import datetime
import hashlib
import hmac
import http.client
import json
import math
import os
from pathlib import Path
import platform
import pwd
import re
import secrets
import selectors
import signal
import socket
import stat
import subprocess
import threading
import time
from urllib.parse import urlsplit
from urllib.parse import quote

from . import native_prerequisite as custody
from .native_prerequisite import PrerequisiteRefusal, strict_json
from .provisioning import ProvisionRefusal, _response, validate_request

SELINUX_ENFORCE = Path('/sys/fs/selinux/enforce')
LINUXBREW_PREFIX = Path('/home/linuxbrew/.linuxbrew')

REFUSALS = frozenset({
    'cortex_health_unavailable', 'cortex_health_degraded', 'cortex_credential_refused',
    'cortex_credential_unavailable', 'cortex_provisioning_reissue_required',
    'cortex_provisioning_setup_required', 'cortex_provisioning_owner_required',
    'cortex_provisioning_conflict', 'cortex_descriptor_invalid',
    'cortex_descriptor_owner_mismatch', 'cortex_instance_mismatch',
    'cortex_release_unsupported', 'cortex_release_signature_invalid',
    'cortex_podman_denied', 'cortex_podman_unsupported', 'cortex_image_mismatch',
    'cortex_cgroup_delegation_unavailable',
    'cortex_podman_storage_setup_required',
    'cortex_selinux_refused',
})


def _linuxbrew_program(name: str, *, uid: int) -> dict:
    """Measure only the anchored stock link, physical program and parents."""
    descriptor = None
    try:
        prefix = LINUXBREW_PREFIX
        custody.physical_path(prefix)
        link = prefix / 'bin' / name
        before = link.lstat()
        if not stat.S_ISLNK(before.st_mode) or before.st_uid not in (0, uid) or before.st_nlink != 1:
            raise ValueError
        link_identity = custody._file_identity(before), before.st_ctime_ns
        target = link.resolve(strict=True)
        relative = target.relative_to(prefix).parts
        keg = None
        if name == 'podman':
            if (len(relative) != 5 or relative[:2] != ('Cellar', 'podman')
                    or relative[3:] != ('bin', 'podman')
                    or re.fullmatch(r'(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:_[1-9][0-9]*)?', relative[2]) is None):
                raise ValueError
            keg = relative[2]
        elif name != 'brew' or relative != ('Homebrew', 'bin', 'brew'):
            raise ValueError
        text = os.readlink(link)
        if text not in (str(target), os.path.relpath(target, link.parent)):
            raise ValueError
        custody.physical_path(target)
        parents = {}
        for parent in (*link.parents, *target.parents):
            custody.physical_path(parent)
            info = parent.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, uid) or info.st_mode & 0o022:
                raise ValueError
            parents[parent] = custody._directory_identity(info)
        selected = target.lstat()
        if (not stat.S_ISREG(selected.st_mode) or selected.st_uid not in (0, uid)
                or selected.st_nlink != 1 or selected.st_mode & 0o022 or not selected.st_mode & 0o111
                or not 1 <= selected.st_size <= (268435456 if name == 'podman' else 1048576)):
            raise ValueError
        identity = custody._file_identity(selected), selected.st_ctime_ns
        descriptor = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        opened = os.fstat(descriptor)
        if (custody._file_identity(opened), opened.st_ctime_ns) != identity:
            raise ValueError
        header = os.read(descriptor, 64)
        if name == 'podman':
            if (len(header) != 64 or header[:7] != b'\x7fELF\x02\x01\x01'
                    or header[16:18] not in (b'\x02\x00', b'\x03\x00')
                    or header[18:20] != b'\x3e\x00' or header[20:24] != b'\x01\x00\x00\x00'):
                raise ValueError
        elif not header.startswith(b'#!/bin/bash\n'):
            raise ValueError
        fresh = target.lstat(); opened = os.fstat(descriptor); current = link.lstat()
        if ((custody._file_identity(fresh), fresh.st_ctime_ns) != identity
                or (custody._file_identity(opened), opened.st_ctime_ns) != identity
                or (custody._file_identity(current), current.st_ctime_ns) != link_identity
                or os.readlink(link) != text or link.resolve(strict=True) != target):
            raise ValueError
        for parent, expected in parents.items():
            if custody._directory_identity(parent.lstat()) != expected:
                raise ValueError
        return {'executable': str(target), 'linked_keg': keg,
                'identity': (link_identity, identity, tuple((str(p), v) for p, v in parents.items()), text)}
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _local_engine_context() -> dict:
    """Bind the actual kos identity and physical local engine before execution."""
    try:
        uid = os.getuid()
        if platform.system() != 'Linux' or uid == 0 or os.geteuid() != uid:
            raise ValueError
        account = pwd.getpwuid(uid)
        if account.pw_name != 'kos':
            raise ValueError
        home, runtime = Path(account.pw_dir), Path('/run/user/' + str(uid))
        identities = []
        for path, owner, directory in ((home, uid, True), (runtime, uid, True)):
            custody.physical_path(path)
            info = path.lstat()
            if (info.st_uid != owner or info.st_mode & 0o022
                    or (directory and not stat.S_ISDIR(info.st_mode))
                    or (not directory and (not stat.S_ISREG(info.st_mode) or not info.st_mode & 0o111))):
                raise ValueError
            identities.append(custody._file_identity(info))
        environment = {'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'HOME': str(home),
                       'XDG_RUNTIME_DIR': str(runtime), 'LC_ALL': 'C'}
        try:
            LINUXBREW_PREFIX.lstat()
        except FileNotFoundError:
            # Version-only legacy contexts never qualify the stock provider.
            executable = Path('/usr/bin/podman')
            custody.physical_path(executable)
            info = executable.lstat()
            if info.st_uid != 0 or info.st_mode & 0o022 or not stat.S_ISREG(info.st_mode) or not info.st_mode & 0o111:
                raise ValueError
            identities.append(custody._file_identity(info))
            return {'executable': str(executable), 'uid': uid, 'identity': tuple(identities), 'environment': environment}
        program = _linuxbrew_program('podman', uid=uid)
        return {'executable': program['executable'], 'uid': uid, 'provider': 'linuxbrew',
                'linked_keg': program['linked_keg'], 'identity': (*identities, program['identity']),
                'environment': environment}
    except (OSError, KeyError, ValueError, TypeError, PrerequisiteRefusal):
        raise PrerequisiteRefusal('cortex_podman_unsupported') from None


def observe_linuxbrew_provider(*, cortex_policy: dict, kos_policy: dict,
                              deadline: float | None = None) -> dict:
    """Bind current core formula and installed stock keg to the measured ABI."""
    try:
        now = time.monotonic()
        if deadline is None: deadline = now + 30
        if type(deadline) not in (int, float) or not math.isfinite(deadline) or deadline <= now:
            raise ValueError
        deadline = min(deadline, now + 30)
        context = _local_engine_context()
        if context.get('provider') != 'linuxbrew':
            raise ValueError
        brew = _linuxbrew_program('brew', uid=context['uid'])

        def check():
            if (time.monotonic() >= deadline or _local_engine_context() != context
                    or _linuxbrew_program('brew', uid=context['uid']) != brew):
                raise ValueError

        def read(command, environment):
            check()
            value = _engine_json(command, environment=environment, deadline=deadline)
            check()
            return value

        report = read([context['executable'], '--remote=false', 'version', '--format=json'], context['environment'])
        version = custody.parse_podman_report(json.dumps(report, allow_nan=False).encode(),
            host_os='linux', mode='local', connection_name=None, expected_connection_name=None,
            cortex_policy=cortex_policy, kos_policy=kos_policy)['client_version']
        if custody._version(version) < (6, 0, 2):
            raise ValueError
        environment = dict(context['environment'], HOMEBREW_NO_AUTO_UPDATE='1',
                           HOMEBREW_NO_ANALYTICS='1', HOMEBREW_NO_INSTALL_CLEANUP='1')
        metadata = read([brew['executable'], 'info', '--json=v2', '--formula', 'podman'], environment)
        formulae = metadata['formulae']
        if not isinstance(formulae, list) or len(formulae) != 1:
            raise ValueError
        formula = formulae[0]; revision = formula['revision']
        if (formula['name'] != 'podman' or formula['tap'] != 'homebrew/core'
                or formula['versions']['stable'] != version or type(revision) is not int or revision < 0):
            raise ValueError
        keg = version + ('_' + str(revision) if revision else '')
        installed = formula['installed']
        if not isinstance(installed, list):
            raise ValueError
        matches = [item for item in installed if item['version'] == keg]
        if (context['linked_keg'] != keg or formula['linked_keg'] != keg or len(matches) != 1
                or matches[0]['poured_from_bottle'] is not True):
            raise ValueError
        bottle = formula['bottle']['stable']['files']['x86_64_linux']; checksum = bottle['sha256']
        if (not isinstance(checksum, str) or re.fullmatch(r'[0-9a-f]{64}', checksum) is None
                or bottle['url'] != 'https://ghcr.io/v2/homebrew/core/podman/blobs/sha256:' + checksum):
            raise ValueError
        check()
        return {'schema': 'cortex.linuxbrew-provider.v1', 'provider': 'linuxbrew',
                'formula': 'homebrew/core/podman', 'version': version, 'linked_keg': keg,
                'poured_from_bottle': True, 'bottle_sha256': checksum, 'bottle_url': bottle['url'],
                'checksum_verifier': 'Homebrew',
                'scope': 'current formula metadata and installed stock keg; Homebrew verifies downloaded bottle bytes'}
    except PrerequisiteRefusal as error:
        if error.code == 'cortex_podman_denied':
            raise
        raise PrerequisiteRefusal('cortex_podman_unsupported') from None
    except Exception:
        raise PrerequisiteRefusal('cortex_podman_unsupported') from None


def _engine_json(command: list[str], *, environment: dict, deadline: float) -> dict:
    """Read-only process, bounded pipes and cleanup; raw diagnostics stay local."""
    try:
        if (type(deadline) not in (int, float) or not math.isfinite(deadline)
                or deadline <= time.monotonic() or not isinstance(command, list)
                or not 1 <= len(command) <= 32 or not Path(command[0]).is_absolute()
                or any(not isinstance(arg, str) or '\x00' in arg or len(arg) > 4096 for arg in command)
                or not isinstance(environment, dict)
                or any(not isinstance(k, str) or not isinstance(v, str) or '\x00' in k + v for k, v in environment.items())):
            raise ValueError
        absolute_deadline = min(deadline, time.monotonic() + 5)
        cleanup_budget = min(.1, (absolute_deadline - time.monotonic()) / 4)
        work_deadline = absolute_deadline - cleanup_budget
    except (OSError, ValueError, TypeError):
        raise PrerequisiteRefusal('cortex_podman_unsupported') from None
    process = None
    complete = False
    output, errors = bytearray(), bytearray()
    try:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, env=dict(environment), close_fds=True,
                                   start_new_session=True)
        with selectors.DefaultSelector() as selector:
            for stream, name in ((process.stdout, 'output'), (process.stderr, 'error')):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, name)
            while selector.get_map() or process.poll() is None:
                remaining = work_deadline - time.monotonic()
                if remaining <= 0:
                    raise ValueError
                for key, _ in selector.select(min(remaining, .05)):
                    try:
                        chunk = os.read(key.fileobj.fileno(), 8192)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
                        continue
                    buffer, limit = (output, 65536) if key.data == 'output' else (errors, 4096)
                    if len(buffer) + len(chunk) > limit:
                        raise ValueError
                    buffer.extend(chunk)
            complete = True
        if process.returncode != 0 or errors or time.monotonic() >= work_deadline:
            if (process.returncode == 125 and not output and os.getuid() > 0
                    and command[1:] == ['--remote=false', 'info', '--format=json']
                    and time.monotonic() < work_deadline
                    and bytes(errors) == b'Error: creating runtime static files directory "/var/lib/containers/storage/libpod": mkdir /var/lib/containers/storage: permission denied\n'):
                raise PrerequisiteRefusal('cortex_podman_storage_setup_required')
            raise ValueError
        return strict_json(bytes(output))
    except PrerequisiteRefusal as error:
        if error.code == 'cortex_podman_storage_setup_required':
            raise
        raise PrerequisiteRefusal('cortex_podman_unsupported') from None
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        raise PrerequisiteRefusal('cortex_podman_unsupported') from None
    finally:
        cleanup_failed = False
        if process is not None:
            if not complete:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except OSError:
                    cleanup_failed = True
                try:
                    process.wait(timeout=max(0, absolute_deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    pass
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
        if cleanup_failed:
            raise PrerequisiteRefusal('cortex_podman_unsupported') from None


def observe_linux_engine(*, cortex_policy: dict, kos_policy: dict,
                         deadline: float | None = None) -> dict:
    """Finite actual local reads, before any producer write; no host repair."""
    now = time.monotonic()
    if deadline is None:
        deadline = now + 30
    if type(deadline) not in (int, float) or not math.isfinite(deadline) or deadline <= now:
        raise PrerequisiteRefusal('cortex_podman_unsupported')
    deadline = min(deadline, now + 60)
    context = _local_engine_context()

    def read(kind):
        if time.monotonic() >= deadline or _local_engine_context() != context:
            raise PrerequisiteRefusal('cortex_podman_unsupported')
        value = _engine_json([context['executable'], '--remote=false', kind, '--format=json'],
                             environment=context['environment'], deadline=deadline)
        if time.monotonic() >= deadline or _local_engine_context() != context:
            raise PrerequisiteRefusal('cortex_podman_unsupported')
        return json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')

    try:
        info = read('info')
        if 'store' in strict_json(info):
            custody.check_linux_storage_config(info, uid=context['uid'], home=context['environment']['HOME'])
        delegation = custody.check_linux_delegation(info)
        version = custody.parse_podman_report(read('version'), host_os='linux', mode='local',
            connection_name=None, expected_connection_name=None,
            cortex_policy=cortex_policy, kos_policy=kos_policy)
        return {'schema': 'cortex.linux-engine-readiness.v1', **version, 'delegation': delegation}
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise PrerequisiteRefusal('cortex_podman_unsupported') from None


def read_linux_installation(runtime_root: Path, *, kos_policy: dict,
                            deadline: float | None = None) -> dict:
    """Bind the private ready TEST lifecycle to signed bytes before host effects."""
    now = time.monotonic()
    if deadline is None:
        deadline = now + 30
    if type(deadline) not in (int, float) or not math.isfinite(deadline) or deadline <= now:
        raise PrerequisiteRefusal('cortex_health_unavailable')
    deadline = min(deadline, now + 60)

    def remaining():
        if time.monotonic() >= deadline:
            raise PrerequisiteRefusal('cortex_health_unavailable')

    def identity(path):
        info = path.lstat()
        return custody._file_identity(info), info.st_ctime_ns

    try:
        remaining()
        binding = custody.verify_installed_release(runtime_root, deadline=deadline)
        remaining()
        release = binding['release']
        record_path = runtime_root / 'install.json'
        inner_path = Path(binding['package_root']) / 'release.json'
        parents = {**custody._parents(record_path), **custody._parents(inner_path)}
        record_identity, inner_identity = identity(record_path), identity(inner_path)
        record_raw = custody.read_private_bytes(record_path)
        inner_raw = custody.read_private_bytes(inner_path, limit=1048576)
        record, inner = strict_json(record_raw), strict_json(inner_raw, limit=1048576)
        installation = custody.canonical_uuid(record['installation'])
        namespace = 'cortex_v2_package_test_' + installation.replace('-', '')
        expected_images = {image['config_id'] for image in release['images'].values()}
        loaded = record.get('loaded_images')
        if (set(record) != {'schema', 'version', 'source_sha', 'installation', 'connection',
                'port', 'stage', 'loaded_images', 'namespace', 'service_label'}
                or record['schema'] != 'cortex.test-install.v1' or record['stage'] != 'ready'
                or release['deployment_class'] != 'TEST' or record['connection'] is not None
                or record['source_sha'] != release['source_revision']
                or not isinstance(record['version'], str)
                or not re.fullmatch(r'0\.2\.001-test\.[0-9]{8}\.[1-9][0-9]*', record['version'])
                or release['release_id'] != 'v' + record['version'] or inner.get('version') != record['version']
                or hashlib.sha256(inner_raw).hexdigest() != release['payload_manifest_sha256']
                or record['namespace'] != namespace
                or record['service_label'] != 'ai.kaidera.cortex.TEST-v2.' + installation.replace('-', '')
                or type(record['port']) is not int or not 1024 <= record['port'] <= 65535
                or record['port'] in (8501, 5499, 5500)
                or not isinstance(loaded, list) or len(loaded) != 5 or len(expected_images) != 5
                or any(not isinstance(item, str) for item in loaded) or set(loaded) != expected_images):
            raise ProvisionRefusal('cortex_provisioning_setup_required')
        remaining()
        engine = observe_linux_engine(cortex_policy=release['podman'], kos_policy=kos_policy, deadline=deadline)
        remaining()
        if (identity(record_path) != record_identity or identity(inner_path) != inner_identity
                or custody.read_private_bytes(record_path) != record_raw
                or custody.read_private_bytes(inner_path, limit=1048576) != inner_raw):
            raise ProvisionRefusal('cortex_provisioning_setup_required')
        custody._recheck_parents(parents)
        remaining()
        return {**binding, 'installation_id': installation, 'host_os': 'linux',
                'runtime_root': str(runtime_root), 'namespace': namespace,
                'origin': 'http://127.0.0.1:' + str(record['port']), 'engine': engine}
    except (OSError, ValueError, TypeError, KeyError):
        raise ProvisionRefusal('cortex_provisioning_setup_required') from None


CONTAINER_PROJECTION = ('{"name":{{json .Name}},"id":{{json .ID}},"image_id":{{json .Image}},'
    '"owner":{{json (index .Config.Labels "com.kaidera.candidate")}},'
    '"deployment_class":{{json (index .Config.Labels "com.kaidera.deployment-class")}},'
    '"running":{{json .State.Running}},"networks":{{json .NetworkSettings.Networks}},'
    '"ports":{{json .HostConfig.PortBindings}}}')
IMAGE_PROJECTION = ('{"id":{{json .ID}},"digest":{{json .Digest}},"os":{{json .Os}},'
    '"architecture":{{json .Architecture}},'
    '"source":{{json (index .Labels "org.opencontainers.image.revision")}},'
    '"version":{{json (index .Labels "org.opencontainers.image.version")}},'
    '"deployment_class":{{json (index .Labels "com.kaidera.deployment-class")}}}')
NETWORK_PROJECTION = ('{"name":{{json .Name}},"id":{{json .ID}},"internal":{{json .Internal}},'
    '"owner":{{json (index .Labels "com.kaidera.candidate")}}}')


def _read_linux_runtime_uncached(runtime_root: Path, *, kos_policy: dict,
                       deadline: float | None = None) -> dict:
    """Bind exact owned live objects to signed bytes through fixed public projections."""
    now = time.monotonic()
    if deadline is None:
        deadline = now + 30
    if type(deadline) not in (int, float) or not math.isfinite(deadline) or deadline <= now:
        raise PrerequisiteRefusal('cortex_health_unavailable')
    deadline = min(deadline, now + 60)

    def identity(path):
        info = path.lstat()
        return custody._file_identity(info), info.st_ctime_ns

    def remaining():
        if time.monotonic() >= deadline:
            raise PrerequisiteRefusal('cortex_health_unavailable')

    try:
        remaining()
        record_path = runtime_root / 'install.json'
        record_parents = custody._parents(record_path)
        record_identity = identity(record_path)
        record_raw = custody.read_private_bytes(record_path)
        binding = read_linux_installation(runtime_root, kos_policy=kos_policy, deadline=deadline)
        remaining()
        release, package = binding['release'], Path(binding['package_root'])
        inner_path = package / 'release.json'
        inner = strict_json(custody.read_private_bytes(inner_path, limit=1048576), limit=1048576)
        signed = runtime_root / 'signed'
        paths = [record_path, inner_path, signed / 'release.json', signed / 'release.json.minisig',
                 signed / release['archive']['name']]
        paths.extend(package / name for name in inner['files'])
        if (package / 'SHA256SUMS').exists():
            paths.append(package / 'SHA256SUMS')
        files = {path: identity(path) for path in paths}
        parents = dict(record_parents)
        for path in paths:
            parents.update(custody._parents(path))
        context = _local_engine_context()

        def recheck():
            remaining()
            if (_local_engine_context() != context
                    or identity(record_path) != record_identity
                    or custody.read_private_bytes(record_path) != record_raw
                    or any(identity(path) != expected for path, expected in files.items())):
                raise PrerequisiteRefusal('cortex_image_mismatch')
            custody._recheck_parents(parents)
            remaining()

        def read(kind, projection, target):
            recheck()
            try:
                value = _engine_json([context['executable'], '--remote=false', kind, 'inspect',
                                      '--format', projection, target],
                    environment=context['environment'], deadline=min(deadline, time.monotonic() + 5))
            except PrerequisiteRefusal:
                raise
            except Exception:
                raise PrerequisiteRefusal('cortex_podman_unsupported') from None
            recheck()
            if not isinstance(value, dict):
                raise PrerequisiteRefusal('cortex_image_mismatch')
            return value

        containers = {}
        network = binding['namespace'] + '_net'
        ports = {'8601/tcp': [{'HostIp': '127.0.0.1', 'HostPort': binding['origin'].rsplit(':', 1)[1]}]}
        for role in custody.ROLES:
            image = release['images'][role]
            container = read('container', CONTAINER_PROJECTION, binding['namespace'] + '_' + role)
            object_id = container.get('id')
            if (set(container) != {'name', 'id', 'image_id', 'owner', 'deployment_class', 'running', 'networks', 'ports'}
                    or container['name'] != binding['namespace'] + '_' + role
                    or not isinstance(object_id, str) or not custody.HEX.fullmatch(object_id)
                    or object_id in containers.values()
                    or container['image_id'] != image['config_id'].removeprefix('sha256:')
                    or container['owner'] != binding['installation_id']
                    or container['deployment_class'] != 'TEST' or container['running'] is not True
                    or not isinstance(container['networks'], dict) or set(container['networks']) != {network}
                    or (container['ports'] != ports if role == 'api' else container['ports'] not in (None, {}))):
                raise PrerequisiteRefusal('cortex_image_mismatch')
            containers[role] = object_id
            observed_image = read('image', IMAGE_PROJECTION, image['config_id'])
            if observed_image != {'id': image['config_id'].removeprefix('sha256:'),
                    'digest': image['manifest_digest'], 'os': 'linux', 'architecture': 'amd64',
                    'source': release['source_revision'], 'version': release['release_id'].removeprefix('v'),
                    'deployment_class': 'TEST'}:
                raise PrerequisiteRefusal('cortex_image_mismatch')
        observed_network = read('network', NETWORK_PROJECTION, network)
        network_id = observed_network.get('id')
        if (set(observed_network) != {'name', 'id', 'owner', 'internal'}
                or observed_network['name'] != network or not isinstance(network_id, str)
                or not custody.HEX.fullmatch(network_id) or observed_network['internal'] is not True
                or observed_network['owner'] != binding['installation_id']):
            raise PrerequisiteRefusal('cortex_image_mismatch')
        # Snapshot identities guard every read. Remeasure bytes to close the gap
        # between initial signature verification and the subsequent snapshot.
        measured = custody.verify_installed_release(runtime_root, deadline=deadline)
        if any(measured[key] != binding[key] for key in measured):
            raise PrerequisiteRefusal('cortex_image_mismatch')
        recheck()
        return {**binding, 'containers': containers, 'network_id': network_id}
    except (OSError, ValueError, TypeError, KeyError):
        raise PrerequisiteRefusal('cortex_image_mismatch') from None


_RUNTIME_OBSERVATION = contextvars.ContextVar('cortex_linux_runtime_observation', default=None)


class _RuntimeObservation:
    def __init__(self, root, policy, deadline):
        self.root = root; self.policy = strict_json(json.dumps(policy, allow_nan=False).encode())
        self.deadline = deadline; self.markers = []; self.context = None
        self.local = _local_engine_context()
        release = custody.validate_release_manifest(strict_json(custody.read_private_bytes(
            root / 'signed/release.json', limit=1048576), limit=1048576), target='linux-x86_64')
        inner = strict_json(custody.read_private_bytes(root / 'package/release.json', limit=1048576), limit=1048576)
        package = root / 'package'
        self.paths = [root / 'install.json', package / 'release.json', root / 'signed/release.json',
                      root / 'signed/release.json.minisig', root / 'signed' / release['archive']['name']]
        for name in inner['files']:
            part = Path(name)
            if (not name or part.is_absolute() or part.as_posix() != name
                    or any(p in ('.', '..') for p in name.split('/'))): raise ValueError
            self.paths.append(package / part)
        if (package / 'SHA256SUMS').exists(): self.paths.append(package / 'SHA256SUMS')
        self.parents = {}; self.files = {}
        for path in self.paths:
            self.parents.update(custody._parents(path))
            self.files[path] = (custody._file_identity(path.lstat()), path.lstat().st_ctime_ns)
        self.inventory = self.package_inventory()

    def package_inventory(self):
        result = {}; pending = [self.root / 'package']; count = 0
        while pending:
            directory = pending.pop()
            with os.scandir(directory) as entries:
                for entry in entries:
                    count += 1
                    if count > 8192: raise ValueError
                    path = Path(entry.path); info = path.lstat()
                    if stat.S_ISDIR(info.st_mode):
                        pending.append(path); result[path] = ('directory', custody._directory_identity(info))
                    elif stat.S_ISREG(info.st_mode): result[path] = ('file', custody._file_identity(info), info.st_ctime_ns)
                    else: raise ValueError
        return result

    def check(self, root=None, policy=None, deadline=None):
        try:
            if root is not None and root != self.root: raise ValueError
            if policy is not None and policy != self.policy: raise ValueError
            if deadline is not None and (type(deadline) not in (int, float) or not math.isfinite(deadline)):
                raise ValueError
            if time.monotonic() >= min(self.deadline, self.deadline if deadline is None else deadline):
                raise ProvisionRefusal('cortex_health_unavailable')
            custody._recheck_parents(self.parents)
            if (_local_engine_context() != self.local or self.package_inventory() != self.inventory
                    or any((custody._file_identity(path.lstat()), path.lstat().st_ctime_ns) != value
                           for path, value in self.files.items())):
                raise ValueError
        except (OSError, ValueError, KeyError, TypeError):
            raise ProvisionRefusal('cortex_image_mismatch') from None

    def revoke_markers(self):
        for path, marker in reversed(self.markers):
            directory = None
            try:
                custody._recheck_parents(marker['parents'])
                directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                if custody._directory_identity(os.fstat(directory)) != marker['parents'][path.parent]: continue
                info = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
                if (custody._file_identity(info), info.st_ctime_ns) != marker['identity']: continue
                os.unlink(path.name, dir_fd=directory); os.fsync(directory)
            except Exception:
                pass
            finally:
                if directory is not None:
                    try: os.close(directory)
                    except OSError: pass


@contextmanager
def runtime_observation_scope(runtime_root: Path, *, kos_policy: dict, deadline: float | None = None):
    """Two full measurements, with live custody checks through one operation."""
    state = None; token = None
    try:
        now = time.monotonic()
        if deadline is None: deadline = now + 30
        if type(deadline) not in (int, float) or not math.isfinite(deadline) or deadline <= now:
            raise ProvisionRefusal('cortex_health_unavailable')
        deadline = min(deadline, now + 60)
        previous = _RUNTIME_OBSERVATION.get()
        if previous is not None:
            previous.check(runtime_root, kos_policy, deadline)
            yield previous
            previous.check(runtime_root, kos_policy, deadline)
            return
        state = _RuntimeObservation(runtime_root, kos_policy, deadline)
        state.check()
        state.context = _read_linux_runtime_uncached(runtime_root, kos_policy=state.policy, deadline=deadline)
        state.check()
        token = _RUNTIME_OBSERVATION.set(state)
        yield state
        _RUNTIME_OBSERVATION.reset(token); token = None
        state.check()
        final = _read_linux_runtime_uncached(runtime_root, kos_policy=state.policy, deadline=deadline)
        state.check()
        if final != state.context: raise ProvisionRefusal('cortex_instance_mismatch')
    except BaseException as error:
        if state is not None: state.revoke_markers()
        if isinstance(error, PrerequisiteRefusal): raise
        raise ProvisionRefusal('cortex_image_mismatch') from None
    finally:
        if token is not None: _RUNTIME_OBSERVATION.reset(token)
        if state is not None:
            state.markers.clear()
            if isinstance(state.context, dict): state.context.clear()


def read_linux_runtime(runtime_root: Path, *, kos_policy: dict, deadline: float | None = None) -> dict:
    observation = _RUNTIME_OBSERVATION.get()
    if observation is None:
        return _read_linux_runtime_uncached(runtime_root, kos_policy=kos_policy, deadline=deadline)
    observation.check(runtime_root, kos_policy, deadline)
    return copy.deepcopy(observation.context)


def read_linux_host_security(runtime_root: Path, *, kos_policy: dict,
                             deadline: float | None = None) -> dict:
    """Observe physical private stores and enforcing SELinux without host repair."""
    now = time.monotonic()
    if deadline is None: deadline = now + 30
    if type(deadline) not in (int, float) or not math.isfinite(deadline) or deadline <= now:
        raise PrerequisiteRefusal('cortex_health_unavailable')
    deadline = min(deadline, now + 30)
    descriptors = []
    try:
        binding = read_linux_runtime(runtime_root, kos_policy=kos_policy, deadline=deadline)
        installation = custody.canonical_uuid(binding['installation_id'])
        context = _local_engine_context()

        def same():
            if time.monotonic() >= deadline:
                raise PrerequisiteRefusal('cortex_health_unavailable')
            if _local_engine_context() != context:
                raise PrerequisiteRefusal('cortex_instance_mismatch')

        def identity(info):
            return custody._file_identity(info), info.st_ctime_ns

        def observe():
            same()
            value = _engine_json([context['executable'], '--remote=false', 'info', '--format=json'],
                environment=context['environment'], deadline=deadline)
            same()
            raw = json.dumps(value, allow_nan=False).encode()
            selected = custody.check_linux_storage_config(raw, uid=context['uid'], home=context['environment']['HOME'])
            custody.check_linux_delegation(raw)
            if value['host']['security'].get('selinuxEnabled') is not True:
                raise PrerequisiteRefusal('cortex_selinux_refused')
            return selected

        storage = observe()
        directories = []
        try:
            for key in ('graph_root', 'run_root'):
                path = Path(storage[key])
                parents = custody._parents(path / '.cortex-custody-observation')
                before = path.lstat()
                if (not stat.S_ISDIR(before.st_mode) or before.st_uid != context['uid']
                        or stat.S_IMODE(before.st_mode) != 0o700):
                    raise ValueError
                fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK)
                descriptors.append(fd)
                if identity(os.fstat(fd)) != identity(before): raise ValueError
                directories.append((path, fd, identity(before), parents))
        except Exception:
            raise PrerequisiteRefusal('cortex_podman_storage_setup_required') from None

        try:
            path = SELINUX_ENFORCE
            custody.physical_path(path)
            parents = {}
            for parent in path.parents:
                info = parent.lstat()
                if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
                    raise ValueError
                parents[parent] = custody._directory_identity(info)
            before = path.lstat()
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != 0 or before.st_nlink != 1
                    or before.st_mode & 0o022 or not 0 <= before.st_size <= 2):
                raise ValueError
            kernel = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            descriptors.append(kernel)
            selected = identity(before)
            def enforce():
                custody.physical_path(path)
                custody._recheck_parents(parents)
                if identity(path.lstat()) != selected or identity(os.fstat(kernel)) != selected:
                    raise ValueError
                os.lseek(kernel, 0, os.SEEK_SET)
                raw = os.read(kernel, 2)
                if raw not in (b'1', b'1\n') or identity(os.fstat(kernel)) != selected:
                    raise ValueError
                return raw
            enforcement = enforce()
        except Exception:
            raise PrerequisiteRefusal('cortex_selinux_refused') from None

        if observe() != storage:
            raise PrerequisiteRefusal('cortex_podman_storage_setup_required')
        if read_linux_runtime(runtime_root, kos_policy=kos_policy, deadline=deadline) != binding:
            raise PrerequisiteRefusal('cortex_instance_mismatch')
        same()
        try:
            for root, fd, selected_root, root_parents in directories:
                custody._recheck_parents(root_parents)
                if identity(root.lstat()) != selected_root or identity(os.fstat(fd)) != selected_root:
                    raise ValueError
        except Exception:
            raise PrerequisiteRefusal('cortex_podman_storage_setup_required') from None
        try:
            if enforce() != enforcement: raise ValueError
        except Exception:
            raise PrerequisiteRefusal('cortex_selinux_refused') from None
        same()
        return {'schema': 'cortex.linux-host-security.v1', 'installation_id': installation,
                'rootless': True, 'storage_config_matches': True, 'storage_custody_matches': True,
                'selinux': 'Enforcing'}
    except PrerequisiteRefusal:
        raise
    except Exception:
        raise PrerequisiteRefusal('cortex_health_unavailable') from None
    finally:
        refused = False
        for fd in reversed(descriptors):
            try: os.close(fd)
            except OSError: refused = True
        if refused: raise PrerequisiteRefusal('cortex_health_unavailable') from None


def read_recipient_admission(runtime_root: Path, args: dict, response: dict, role: str,
                             *, kos_policy: dict, store, deadline: float | None = None) -> dict:
    """Authenticate one exact stored recipient without publishing private material."""
    body = validate_request(args)
    _response(response, body)
    now = time.monotonic()
    if deadline is None:
        deadline = now + 60
    if (role not in ('lead', 'console') or type(deadline) not in (int, float)
            or not math.isfinite(deadline) or not now < deadline <= now + 60):
        raise ProvisionRefusal('cortex_provisioning_setup_required')
    context = read_linux_runtime(runtime_root, kos_policy=kos_policy, deadline=deadline)
    label = body['lead_name'] if role == 'lead' else 'console'
    receipt = response[role]
    if store.installation != context['installation_id'] or store.backend != 'file':
        raise ProvisionRefusal('cortex_credential_unavailable')
    transport = LoopbackTransport(context['origin'])
    snapshot = None

    def get(path):
        nonlocal snapshot
        try:
            record = store.read(body['project_key'], label)
            if (record is None or not isinstance(record.token, str)
                    or re.fullmatch(r'[A-Za-z0-9_-]{43}', record.token) is None
                    or record.metadata.managed_by != receipt['manager']
                    or record.metadata.expires_at != receipt['expires_at']
                    or record.metadata.due_state(datetime.datetime.now(datetime.timezone.utc)) == 'expired'):
                raise ValueError
            if snapshot is None:
                snapshot = record
            elif (not hmac.compare_digest(record.token, snapshot.token)
                    or record.metadata != snapshot.metadata):
                raise ProvisionRefusal('cortex_credential_refused')
        except PrerequisiteRefusal:
            raise
        except Exception:
            raise ProvisionRefusal('cortex_credential_unavailable') from None
        remaining = min(5, deadline - time.monotonic())
        if remaining <= 0:
            raise ProvisionRefusal('cortex_health_unavailable')
        try:
            status, raw = transport.get(context['origin'] + path,
                headers={'Authorization': 'Bearer ' + record.token, 'X-Cortex-Scope': body['project_key']},
                timeout=remaining, max_bytes=65536)
            if time.monotonic() >= deadline:
                raise ProvisionRefusal('cortex_health_unavailable')
            if read_linux_runtime(runtime_root, kos_policy=kos_policy, deadline=deadline) != context:
                raise ProvisionRefusal('cortex_instance_mismatch')
            if type(status) is not int or status != 200:
                raise ProvisionRefusal('cortex_credential_refused' if status in (401, 403, 404)
                                       else 'cortex_health_unavailable')
            value = strict_json(raw)
            json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')
            return value
        except PrerequisiteRefusal:
            raise
        except Exception:
            raise ProvisionRefusal('cortex_health_unavailable') from None

    try:
        if get('/health/ready').get('status') != 'ready':
            raise ProvisionRefusal('cortex_health_degraded')
        principal = get('/v1/auth/principal')['data']
        if (principal['installation_id'] != context['installation_id']
                or principal['principal_id'] != receipt['principal_id']
                or not isinstance(principal['scopes'], list) or len(principal['scopes']) != 1):
            raise ValueError
        grant = principal['scopes'][0]
        if (grant['scope_id'] != response['project_id'] or grant['primary_alias'] != body['project_key']
                or grant['scope_kind'] != 'project' or grant['can_read'] is not True
                or grant['can_write'] is not True or grant['can_publish'] is not (role == 'lead')):
            raise ValueError
        roster = get('/v1/scopes/' + quote(body['project_key'], safe='') + '/roster')['data']
        if (roster['scope_id'] != response['project_id'] or not isinstance(roster['entries'], list)
                or type(roster['roster_revision']) is not int or roster['roster_revision'] < 0):
            raise ValueError
        entries = [e for e in roster['entries'] if e.get('display_name') == label
                   or e.get('principal_id') == receipt['principal_id']]
        if len(entries) != 1:
            raise ValueError
        member = entries[0]
        if (member['principal_id'] != receipt['principal_id'] or member['display_name'] != label
                or member['actor_kind'] != ('agent' if role == 'lead' else 'service')
                or member['role'] != ('lead' if role == 'lead' else 'member') or member['status'] != 'active'):
            raise ValueError
        custody.canonical_uuid(member['actor_id'])
        return {'roster_revision': roster['roster_revision'], 'principal_id': receipt['principal_id'],
                'actor_id': member['actor_id'], 'scope_id': response['project_id'],
                'project': body['project_key'], 'member_name': label, 'actor_kind': member['actor_kind'],
                'role': member['role'], 'status': 'active', 'can_read': True, 'can_write': True,
                'can_publish': role == 'lead', 'project_root': body['repo_root']}
    except PrerequisiteRefusal:
        raise
    except Exception:
        raise ProvisionRefusal('cortex_credential_refused') from None
    finally:
        snapshot = None


def owned_api_command(runtime_root: Path, frame: dict, *, kos_policy: dict,
                      deadline: float | None = None) -> dict:
    """Measure the signed owned API before delivering one private command.

    The caller journals issuance before create-project. This port never retries
    an uncertain command and never selects a container by a mutable name.
    """
    from ..native_prerequisite import _frame
    try:
        now = time.monotonic()
        if deadline is None:
            deadline = now + 60
        if (type(deadline) not in (int, float) or not math.isfinite(deadline)
                or not now < deadline <= now + 60):
            raise ValueError
        private = strict_json(json.dumps(frame, ensure_ascii=False, allow_nan=False,
                                        separators=(',', ':')).encode())
        mode, _ = _frame(private)
    except (ValueError, TypeError, UnicodeError, RecursionError, PrerequisiteRefusal):
        raise ProvisionRefusal('cortex_provisioning_setup_required') from None
    context = read_linux_runtime(runtime_root, kos_policy=kos_policy, deadline=deadline)
    if mode != 'payload' and private['installation_id'] != context['installation_id']:
        raise ProvisionRefusal('cortex_instance_mismatch')
    engine = _local_engine_context()
    api_id = context['containers']['api']
    if not isinstance(api_id, str) or re.fullmatch(r'[0-9a-f]{64}', api_id) is None:
        raise ProvisionRefusal('cortex_image_mismatch')
    command = [engine['executable'], '--remote=false', 'exec', '--interactive', api_id,
               '/opt/venv/bin/python', '-m', 'cortex_v2.native_prerequisite']

    def execute(value):
        try:
            remaining = min(5, deadline - time.monotonic())
            if remaining <= 0 or _local_engine_context() != engine:
                raise ProvisionRefusal('cortex_health_unavailable')
            result = private_json_command(command, value, timeout=remaining,
                                          environment=engine['environment'])
            fresh = read_linux_runtime(runtime_root, kos_policy=kos_policy, deadline=deadline)
            if (time.monotonic() >= deadline or fresh != context
                    or _local_engine_context() != engine):
                raise ProvisionRefusal('cortex_instance_mismatch')
            return result
        except PrerequisiteRefusal:
            raise
        except Exception:
            raise ProvisionRefusal('cortex_health_unavailable') from None

    payload = execute({'mode': 'payload'})
    try:
        if (not isinstance(payload, dict) or set(payload) != {'files', 'sha256'}
                or not isinstance(payload['files'], dict) or not 1 <= len(payload['files']) <= 4096):
            raise ValueError
        for name, digest in payload['files'].items():
            if (not isinstance(name, str) or not name.startswith(('src/', 'migrations/'))
                    or any(part in ('', '.', '..') for part in name.split('/'))
                    or any(ord(c) < 32 or ord(c) == 127 for c in name) or '\\' in name
                    or not isinstance(digest, str) or re.fullmatch(r'[0-9a-f]{64}', digest) is None):
                raise ValueError
        measured = hashlib.sha256(json.dumps(payload['files'], sort_keys=True,
                                            separators=(',', ':')).encode()).hexdigest()
        migrations = {'migrations/' + item['name']: item['sha256']
                      for item in context['release']['migrations']}
        if (payload['sha256'] != measured
                or measured != context['release']['images']['api']['source_payload_sha256']
                or {n: d for n, d in payload['files'].items() if n.startswith('migrations/')} != migrations):
            raise ValueError
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        raise ProvisionRefusal('cortex_image_mismatch') from None
    if mode == 'payload':
        return payload
    result = execute(private)
    if mode == 'authorize-owner' and (not isinstance(result, dict)
            or set(result) != {'authorized', 'installation_id'} or result['authorized'] is not True
            or result['installation_id'] != context['installation_id']):
        raise ProvisionRefusal('cortex_provisioning_owner_required')
    return result


class LoopbackTransport:
    """One bounded GET to the explicitly bound local API; never proxy or redirect."""

    def __init__(self, origin: str):
        if (not isinstance(origin, str)
                or not re.fullmatch(r'http://127\.0\.0\.1:[0-9]{1,5}', origin)
                or not 1 <= int(origin.rsplit(':', 1)[1]) <= 65535):
            raise ProvisionRefusal('cortex_health_unavailable')
        self.origin = origin
        self.port = int(origin.rsplit(':', 1)[1])

    def get(self, url: str, *, headers: dict, timeout: float, max_bytes: int) -> tuple[int, bytes]:
        try:
            if (not isinstance(url, str) or len(url) > 4096 or '#' in url
                    or any(ord(c) <= 32 or ord(c) >= 127 for c in url)
                    or type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 5
                    or type(max_bytes) is not int or not 1 <= max_bytes <= 65536
                    or not isinstance(headers, dict) or set(headers) != {'Authorization', 'X-Cortex-Scope'}
                    or not isinstance(headers['Authorization'], str)
                    or not re.fullmatch(r'Bearer [A-Za-z0-9_-]{43}', headers['Authorization'])
                    or not isinstance(headers['X-Cortex-Scope'], str)
                    or not custody.IDENTIFIER.fullmatch(headers['X-Cortex-Scope'])):
                raise ValueError
            parsed = urlsplit(url)
            if (parsed.scheme != 'http' or 'http://' + parsed.netloc != self.origin
                    or not parsed.path.startswith('/') or parsed.fragment):
                raise ValueError
            path = parsed.path + ('?' + parsed.query if parsed.query else '')
        except (ValueError, TypeError):
            raise ProvisionRefusal('cortex_health_unavailable') from None

        deadline = time.monotonic() + timeout
        work_deadline = deadline - min(.05, timeout / 4)
        connection = response = owned_socket = timer = None
        try:
            connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=timeout)
            connection.connect()
            owned_socket = connection.sock
            remaining = work_deadline - time.monotonic()
            if remaining <= 0:
                raise ProvisionRefusal('cortex_health_unavailable')
            owned_socket.settimeout(remaining)

            def interrupt():
                # Own the socket object, not a descriptor number which may be reused.
                try:
                    owned_socket.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

            timer = threading.Timer(remaining, interrupt)
            timer.daemon = True
            timer.start()
            connection.request('GET', path, headers=dict(headers))
            response = connection.getresponse()
            raw = response.read(max_bytes + 1)
            if (len(raw) > max_bytes or time.monotonic() >= work_deadline
                    or response.length not in (None, 0)):
                raise ProvisionRefusal('cortex_health_unavailable')
            return response.status, raw
        except (OSError, ValueError, http.client.HTTPException):
            raise ProvisionRefusal('cortex_health_unavailable') from None
        finally:
            if timer is not None:
                timer.cancel()
                timer.join(timeout=max(0, min(.05, deadline - time.monotonic())))
            if response is not None:
                response.close()
            if connection is not None:
                connection.close()
            if owned_socket is not None:
                owned_socket.close()


def _one_frame(raw: bytes, limit: int) -> dict:
    if (not raw.endswith(b'\n') or raw.count(b'\n') != 1
            or not raw.startswith(b'{') or raw[:-1].strip() != raw[:-1]):
        raise ProvisionRefusal('cortex_health_unavailable')
    try:
        value = strict_json(raw, limit=limit)
        # Escaped unpaired surrogates cannot become a valid UTF-8 response.
        json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')
        return value
    except (PrerequisiteRefusal, ValueError, UnicodeError, TypeError, RecursionError):
        raise ProvisionRefusal('cortex_health_unavailable') from None


def private_json_command(command: list[str], frame: dict, *, timeout: float = 5,
                         environment: dict | None = None) -> dict:
    """One private request/response with an absolute work and cleanup deadline.

    The caller binds the executable and owned object before calling this port.
    This function has no ambient credentials, engine defaults or retry.
    """
    try:
        if (not isinstance(command, list) or not 1 <= len(command) <= 64
                or any(not isinstance(arg, str) or '\x00' in arg for arg in command)
                or not Path(command[0]).is_absolute()
                or not isinstance(frame, dict) or type(timeout) not in (int, float)
                or not math.isfinite(timeout) or not 0 < timeout <= 5):
            raise ValueError
        request = json.dumps(frame, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode() + b'\n'
        if len(request) > 65536: raise ValueError
        strict_json(request)
        if environment is not None:
            if not isinstance(environment, dict) or environment != _local_engine_context()['environment']:
                raise ValueError
            environment = dict(environment)
    except (ValueError, TypeError, UnicodeError, PrerequisiteRefusal, RecursionError):
        raise ProvisionRefusal('cortex_provisioning_setup_required') from None
    absolute_deadline = time.monotonic() + timeout
    cleanup_budget = min(.1, timeout / 4)
    work_deadline = absolute_deadline - cleanup_budget
    if environment is None:
        environment = {'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'LC_ALL': 'C',
                       'HOME': pwd.getpwuid(os.getuid()).pw_dir}
    environment.update(PYTHONDONTWRITEBYTECODE='1', PYTHONUTF8='1')
    process = None
    streams_closed = False
    output, errors = bytearray(), bytearray()
    try:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, env=environment, close_fds=True,
                                   start_new_session=True)
        with selectors.DefaultSelector() as selector:
            for stream, name, event in ((process.stdin, 'input', selectors.EVENT_WRITE),
                                        (process.stdout, 'output', selectors.EVENT_READ),
                                        (process.stderr, 'error', selectors.EVENT_READ)):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, event, name)
            position = 0
            while selector.get_map() or process.poll() is None:
                remaining = work_deadline - time.monotonic()
                if remaining <= 0: raise ProvisionRefusal('cortex_health_unavailable')
                for key, _ in selector.select(min(remaining, .05)):
                    stream = key.fileobj
                    if key.data == 'input':
                        try:
                            written = os.write(stream.fileno(), request[position:position + 4096])
                        except BlockingIOError:
                            continue
                        except BrokenPipeError:
                            selector.unregister(stream); stream.close()
                            continue
                        position += written
                        if position == len(request):
                            selector.unregister(stream); stream.close()
                    else:
                        try: chunk = os.read(stream.fileno(), 8192)
                        except BlockingIOError: continue
                        if not chunk:
                            selector.unregister(stream); stream.close()
                            continue
                        buffer, limit = (output, 65536) if key.data == 'output' else (errors, 4096)
                        buffer.extend(chunk)
                        if len(buffer) > limit:
                            raise ProvisionRefusal('cortex_health_unavailable')
            streams_closed = True
        if process.returncode == 0 and not errors:
            return _one_frame(bytes(output), 65536)
        if process.returncode == 2 and not output:
            refusal = _one_frame(bytes(errors), 4096)
            code = refusal.get('code')
            if isinstance(code, str) and code in REFUSALS:
                # Child messages, timestamps and all other fields are discarded.
                raise ProvisionRefusal(code)
        raise ProvisionRefusal('cortex_health_unavailable')
    except ProvisionRefusal:
        raise
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        raise ProvisionRefusal('cortex_health_unavailable') from None
    finally:
        if process is not None:
            cleanup_refused = False
            if not streams_closed:
                try: os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError: pass
                except OSError: cleanup_refused = True
                try: process.wait(timeout=max(0, absolute_deadline - time.monotonic()))
                except subprocess.TimeoutExpired: pass
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None: stream.close()
            if cleanup_refused:
                raise ProvisionRefusal('cortex_health_unavailable') from None


@contextmanager
def operation_lock(path: Path):
    """Nonblocking private inode lock; retain its inode between operations."""
    descriptor = None
    acquired = False
    try:
        try:
            parents = custody._parents(path)
            if path.exists() or path.is_symlink():
                custody.read_private_bytes(path, limit=0)
            descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
            opened = os.fstat(descriptor); custody._private_file(opened)
            if opened.st_size != 0 or custody._file_identity(path.lstat()) != custody._file_identity(opened):
                raise ProvisionRefusal('cortex_provisioning_conflict')
            custody._recheck_parents(parents)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except BlockingIOError:
                pass
        except (OSError, PrerequisiteRefusal):
            raise ProvisionRefusal('cortex_provisioning_conflict') from None
        # Body refusals belong to the operation and keep their original code.
        yield acquired
        if not acquired:
            return
        try:
            custody._recheck_parents(parents)
            if custody._file_identity(path.lstat()) != custody._file_identity(os.fstat(descriptor)):
                raise ProvisionRefusal('cortex_provisioning_conflict')
        except (OSError, PrerequisiteRefusal):
            raise ProvisionRefusal('cortex_provisioning_conflict')
    finally:
        if descriptor is not None:
            try:
                if acquired: fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)


class DeliveryJournal:
    """Private nonsecret receipt state; never recover or replay plaintext.

    The caller holds the operation lock. Recipient verification must authenticate
    each atomic native KeyStore record against its exact principal using the
    finite installed adapter. Metadata alone cannot establish that binding.
    """
    FIELDS = {'schema', 'installation_id', 'request_sha256', 'operation_id',
              'project_id', 'project_key', 'lead', 'console', 'state'}
    RECIPIENT = ('principal_id', 'manager', 'expires_at')

    def __init__(self, path: Path, args: dict, installation_id: str, store):
        self.path = path
        self.body = validate_request(args)
        self.installation = custody.canonical_uuid(installation_id)
        self.store = store
        request = json.dumps(self.body, sort_keys=True, separators=(',', ':'),
                             ensure_ascii=False, allow_nan=False).encode('utf-8')
        self.request_sha256 = hashlib.sha256(request).hexdigest()
        self.started = False

    def _base(self):
        return {'schema': 'cortex.private-delivery.v1', 'installation_id': self.installation,
                'request_sha256': self.request_sha256, 'project_key': self.body['project_key']}

    def _read(self):
        try:
            custody._parents(self.path)
            if not self.path.exists() and not self.path.is_symlink(): return None
            value = custody.read_private_json(self.path)
            if (set(value) != self.FIELDS or any(value.get(k) != v for k, v in self._base().items())
                    or value['state'] not in ('command_started', 'delivery_pending', 'keys_committed')
                    or self.store.installation != self.installation or self.store.backend != 'file'):
                raise ValueError
            if value['state'] == 'command_started':
                if any(value[k] is not None for k in ('operation_id', 'project_id', 'lead', 'console')):
                    raise ValueError
            else:
                custody.canonical_uuid(value['operation_id']); custody.canonical_uuid(value['project_id'])
                for name in ('lead', 'console'):
                    if not isinstance(value[name], dict) or set(value[name]) != set(self.RECIPIENT):
                        raise ValueError
                    custody.canonical_uuid(value[name]['principal_id'])
            return value
        except (OSError, ValueError, TypeError, KeyError, PrerequisiteRefusal):
            raise ProvisionRefusal('cortex_provisioning_reissue_required') from None

    def begin(self) -> str:
        existing = self._read()
        if existing is not None:
            if existing['state'] == 'keys_committed': return 'resume'
            raise ProvisionRefusal('cortex_provisioning_reissue_required')
        if self.store.installation != self.installation or self.store.backend != 'file':
            raise ProvisionRefusal('cortex_provisioning_reissue_required')
        value = dict(self._base(), operation_id=None, project_id=None, lead=None,
                     console=None, state='command_started')
        custody.atomic_private_json(self.path, value)
        self.started = True
        return 'new'

    def _receipt(self, response: dict, state: str):
        return dict(self._base(), operation_id=response['operation_id'], project_id=response['project_id'],
                    lead={k: response['lead'][k] for k in self.RECIPIENT},
                    console={k: response['console'][k] for k in self.RECIPIENT}, state=state)

    def _records(self, response: dict, *, verify, issued: bool) -> bool:
        for name, label in (('lead', self.body['lead_name']), ('console', 'console')):
            record = self.store.read(self.body['project_key'], label)
            if (record is None or not isinstance(record.token, str)
                    or re.fullmatch(r'[A-Za-z0-9_-]{43}', record.token) is None
                    or record.metadata.managed_by != response[name]['manager']
                    or record.metadata.expires_at != response[name]['expires_at']):
                return False
            if issued and not hmac.compare_digest(record.token, response[name + '_token']): return False
            if verify(name, record, response[name]['principal_id']) is not True: return False
        return True

    def persist(self, response: dict, *, verify) -> None:
        try:
            issued = _response(response, self.body)
            current = self._read()
            if not issued or not self.started or current is None or current['state'] != 'command_started':
                raise ValueError
            # No second delivery attempt, even on the same in-memory object.
            self.started = False
            pending = self._receipt(response, 'delivery_pending')
            custody.atomic_private_json(self.path, pending)
            for name, label in (('lead', self.body['lead_name']), ('console', 'console')):
                self.store.put(self.body['project_key'], label, response[name + '_token'],
                               managed_by=response[name]['manager'], expires_at=response[name]['expires_at'])
            if not self._records(response, verify=verify, issued=True): raise ValueError
            custody.atomic_private_json(self.path, self._receipt(response, 'keys_committed'))
        except Exception:
            raise ProvisionRefusal('cortex_provisioning_reissue_required') from None

    def complete(self, response: dict, *, verify) -> bool:
        try:
            issued = _response(response, self.body)
            saved = self._read()
            if saved != self._receipt(response, 'keys_committed'): return False
            return self._records(response, verify=verify, issued=issued)
        except Exception:
            return False


@contextmanager
def admitted_recipients(runtime_root: Path, args: dict, response: dict,
                        *, kos_policy: dict, journal: DeliveryJournal,
                        deadline: float | None = None):
    """Hold both exact private records through delivery, readiness and publication.

    The caller holds the operation lock and has journaled issuance with begin.
    The yielded callback reauthenticates both recipients and returns metadata
    only. Receipt replay reads existing records without delivering any token.
    """
    snapshots = {}; members = {}; active = False; private = None
    try:
        request = strict_json(json.dumps(args, ensure_ascii=False, allow_nan=False).encode())
        private = strict_json(json.dumps(response, ensure_ascii=False, allow_nan=False).encode())
        policy = strict_json(json.dumps(kos_policy, ensure_ascii=False, allow_nan=False).encode())
        body = validate_request(request); issued = _response(private, body)
        now = time.monotonic()
        if deadline is None: deadline = now + 60
        if (type(deadline) not in (int, float) or not math.isfinite(deadline)
                or not now < deadline <= now + 60
                or not isinstance(journal, DeliveryJournal) or journal.body != body):
            raise ProvisionRefusal('cortex_provisioning_setup_required')
        context = read_linux_runtime(runtime_root, kos_policy=policy, deadline=deadline)
        store = journal.store
        if context['installation_id'] != journal.installation:
            raise ProvisionRefusal('cortex_instance_mismatch')
        active = True

        def records():
            if not active or time.monotonic() >= deadline:
                raise ProvisionRefusal('cortex_health_unavailable')
            try:
                if store.installation != journal.installation or store.backend != 'file':
                    raise ValueError
                current = {}
                for role, label in (('lead', body['lead_name']), ('console', 'console')):
                    record = store.read(body['project_key'], label)
                    receipt = private[role]
                    if (record is None or not isinstance(record.token, str)
                            or re.fullmatch(r'[A-Za-z0-9_-]{43}', record.token) is None
                            or record.metadata.managed_by != receipt['manager']
                            or record.metadata.expires_at != receipt['expires_at']
                            or record.metadata.due_state(datetime.datetime.now(datetime.timezone.utc)) == 'expired'):
                        raise ValueError
                    previous = snapshots.get(role)
                    if previous is not None and (record.metadata != previous.metadata
                            or not hmac.compare_digest(record.token, previous.token)):
                        raise ProvisionRefusal('cortex_credential_refused')
                    current[role] = record
                if not snapshots: snapshots.update(current)
                return current
            except PrerequisiteRefusal:
                raise
            except Exception:
                raise ProvisionRefusal('cortex_credential_unavailable') from None

        def runtime_check():
            records()
            if read_linux_runtime(runtime_root, kos_policy=policy, deadline=deadline) != context:
                raise ProvisionRefusal('cortex_instance_mismatch')
            records()

        class PinnedStore:
            @property
            def installation(self): return store.installation
            @property
            def backend(self): return store.backend
            def read(self, project, label):
                runtime_check()
                if project != body['project_key'] or label not in (body['lead_name'], 'console'):
                    raise ProvisionRefusal('cortex_credential_refused')
                return records()['lead' if label == body['lead_name'] else 'console']

        def verify(role, record, principal):
            runtime_check()
            expected = snapshots[role]
            if (principal != private[role]['principal_id'] or record.metadata != expected.metadata
                    or not hmac.compare_digest(record.token, expected.token)):
                raise ProvisionRefusal('cortex_credential_refused')
            member = read_recipient_admission(runtime_root, request, private, role,
                kos_policy=policy, store=PinnedStore(), deadline=deadline)
            runtime_check()
            if (role in members and member != members[role]
                    or any(member['roster_revision'] != prior['roster_revision']
                           for prior in members.values())):
                raise ProvisionRefusal('cortex_provisioning_conflict')
            members[role] = member
            return True

        if issued:
            journal.persist(private, verify=verify)
        elif journal.complete(private, verify=verify) is not True:
            raise ProvisionRefusal('cortex_provisioning_reissue_required')
        if set(members) != {'lead', 'console'}:
            raise ProvisionRefusal('cortex_provisioning_reissue_required')
        runtime_check()

        def recheck():
            runtime_check()
            for role, record in records().items():
                verify(role, record, private[role]['principal_id'])
            runtime_check()
            return {role: dict(member) for role, member in members.items()}

        yield recheck
        runtime_check()
    except PrerequisiteRefusal:
        raise
    except Exception:
        raise ProvisionRefusal('cortex_provisioning_reissue_required') from None
    finally:
        active = False
        snapshots.clear(); members.clear()
        if isinstance(private, dict): private.clear()


def read_linux_catalog(runtime_root: Path, args: dict, response: dict, *, kos_policy: dict,
                       store, deadline: float | None = None) -> dict:
    """Bind one app-role catalog observation to signed inventory and Console.

    The surrounding admitted_recipients context holds both recipients. This
    finite boundary neither publishes a descriptor nor asserts whole readiness.
    """
    from .provisioning import _member
    private = request = frame = snapshot = None
    try:
        now = time.monotonic()
        if deadline is None: deadline = now + 60
        if (type(deadline) not in (int, float) or not math.isfinite(deadline)
                or not now < deadline <= now + 60):
            raise ProvisionRefusal('cortex_provisioning_setup_required')
        request = strict_json(json.dumps(args, ensure_ascii=False, allow_nan=False).encode())
        private = strict_json(json.dumps(response, ensure_ascii=False, allow_nan=False).encode())
        policy = strict_json(json.dumps(kos_policy, ensure_ascii=False, allow_nan=False).encode())
        body = validate_request(request); _response(private, body)
        context = read_linux_runtime(runtime_root, kos_policy=policy, deadline=deadline)
        release = context['release']
        path = Path(context['package_root']) / 'native/rls-inventory.json'
        parents = custody._parents(path)
        def identity():
            info = path.lstat()
            return custody._file_identity(info), info.st_ctime_ns
        selected = identity()
        raw = custody.read_private_bytes(path)
        digest = hashlib.sha256(raw).hexdigest()
        if (release['files'].get('native/rls-inventory.json') != digest
                or release['rls_inventory']['sha256'] != digest):
            raise ProvisionRefusal('cortex_image_mismatch')
        inventory = strict_json(raw)
        migrations = {item['name']: item['sha256'] for item in release['migrations']}
        if (set(inventory) != {'schema', 'source_revision', 'api_source_payload_sha256', 'migrations', 'relations'}
                or inventory['schema'] != 'cortex.rls-inventory.v1'
                or inventory['source_revision'] != release['source_revision']
                or inventory['api_source_payload_sha256'] != release['images']['api']['source_payload_sha256']
                or inventory['migrations'] != migrations
                or not isinstance(inventory['relations'], list) or not 1 <= len(inventory['relations']) <= 1024):
            raise ProvisionRefusal('cortex_image_mismatch')
        names = []
        for row in inventory['relations']:
            if (not isinstance(row, dict) or set(row) != {'name', 'rls', 'forced', 'app_direct_grant'}
                    or not isinstance(row['name'], str)
                    or re.fullmatch(r'cortex_(?:auth|core)\.[a-z_][a-z0-9_]{0,62}', row['name']) is None
                    or any(type(row[key]) is not bool for key in ('rls', 'forced', 'app_direct_grant'))
                    or row['rls'] and not row['forced'] or not row['rls'] and row['app_direct_grant']):
                raise ProvisionRefusal('cortex_image_mismatch')
            names.append(row['name'])
        if names != sorted(set(names)):
            raise ProvisionRefusal('cortex_image_mismatch')

        def record():
            nonlocal snapshot
            if time.monotonic() >= deadline:
                raise ProvisionRefusal('cortex_health_unavailable')
            if store.installation != context['installation_id'] or store.backend != 'file':
                raise ProvisionRefusal('cortex_credential_unavailable')
            current = store.read(body['project_key'], 'console')
            receipt = private['console']
            if (current is None or not isinstance(current.token, str)
                    or re.fullmatch(r'[A-Za-z0-9_-]{43}', current.token) is None
                    or current.metadata.managed_by != receipt['manager']
                    or current.metadata.expires_at != receipt['expires_at']
                    or current.metadata.due_state(datetime.datetime.now(datetime.timezone.utc)) == 'expired'):
                raise ProvisionRefusal('cortex_credential_unavailable')
            if snapshot is None: snapshot = current
            elif (current.metadata != snapshot.metadata
                    or not hmac.compare_digest(current.token, snapshot.token)):
                raise ProvisionRefusal('cortex_credential_refused')
            return current

        def check():
            current = record()
            if read_linux_runtime(runtime_root, kos_policy=policy, deadline=deadline) != context:
                raise ProvisionRefusal('cortex_instance_mismatch')
            if identity() != selected or custody.read_private_bytes(path) != raw:
                raise ProvisionRefusal('cortex_image_mismatch')
            custody._recheck_parents(parents)
            record()
            return current

        class PinnedStore:
            @property
            def installation(self): return store.installation
            @property
            def backend(self): return store.backend
            def read(self, project, name):
                if project != body['project_key'] or name != 'console':
                    raise ProvisionRefusal('cortex_credential_refused')
                return check()

        check()
        member = _member(read_recipient_admission(runtime_root, request, private, 'console',
            kos_policy=policy, store=PinnedStore(), deadline=deadline), body, private)
        current = check()
        frame = {'mode': 'readiness', 'installation_id': context['installation_id'],
                 'credential': current.token, 'project': body['project_key'], 'project_root': body['repo_root'],
                 'migrations': migrations, 'expected_relations': inventory['relations']}
        result = owned_api_command(runtime_root, frame, kos_policy=policy, deadline=deadline)
        check()
        predicates = {'migration_checksums_match', 'required_rls_enabled_and_forced',
                      'app_role_non_superuser_without_bypassrls', 'app_role_not_migrator_or_table_owner',
                      'database_instance_matches'}
        if (not isinstance(result, dict) or set(result) != predicates | {'project_binding'}
                or any(result[key] is not True for key in predicates)
                or result['project_binding'] != {'scope_id': private['project_id'],
                     'primary_alias': body['project_key'], 'primary_root': body['repo_root']}):
            raise ProvisionRefusal('cortex_health_degraded')
        custody.canonical_uuid(result['project_binding']['scope_id'])
        fresh = _member(read_recipient_admission(runtime_root, request, private, 'console',
            kos_policy=policy, store=PinnedStore(), deadline=deadline), body, private)
        check()
        if fresh != member:
            raise ProvisionRefusal('cortex_provisioning_conflict')
        return {'schema': 'cortex.linux-catalog-readiness.v1', 'installation_id': context['installation_id'],
                'source_payload_matches': True, **strict_json(json.dumps(result, allow_nan=False).encode())}
    except PrerequisiteRefusal:
        raise
    except Exception:
        raise ProvisionRefusal('cortex_health_degraded') from None
    finally:
        snapshot = None
        for value in (private, request, frame):
            if isinstance(value, dict): value.clear()


def read_linux_readiness(runtime_root: Path, args: dict, response: dict, *, kos_policy: dict,
                         store, recheck_recipients, deadline: float | None = None) -> dict:
    """Compose fresh read-only observations while both recipients remain pinned.

    The caller holds admitted_recipients and the operation lock. This returns
    the measured projection; descriptor/nonce binding and publication are separate.
    """
    private = request = None
    try:
        now = time.monotonic()
        if deadline is None: deadline = now + 30
        if (type(deadline) not in (int, float) or not math.isfinite(deadline) or deadline <= now
                or not callable(recheck_recipients)):
            raise ProvisionRefusal('cortex_provisioning_setup_required')
        deadline = min(deadline, now + 30)
        request = strict_json(json.dumps(args, allow_nan=False).encode())
        private = strict_json(json.dumps(response, allow_nan=False).encode())
        policy = strict_json(json.dumps(kos_policy, allow_nan=False).encode())
        body = validate_request(request); _response(private, body)
        context = read_linux_runtime(runtime_root, kos_policy=policy, deadline=deadline)
        installation = custody.canonical_uuid(context['installation_id'])
        release = context['release']
        custody.validate_release_manifest(release, target='linux-x86_64')
        if (context['host_os'] != 'linux' or store.installation != installation or store.backend != 'file'):
            raise ProvisionRefusal('cortex_credential_unavailable')
        if (context['helper_sha256'] != release['files']['bin/cortex']
                or not custody.HEX.fullmatch(context['helper_sha256'])
                or not custody.HEX.fullmatch(context['release_manifest_sha256'])):
            raise ProvisionRefusal('cortex_image_mismatch')
        engine = context['engine']
        if (set(engine) != {'schema', 'mode', 'client_version', 'server_version', 'connection_name', 'delegation'}
                or engine['schema'] != 'cortex.linux-engine-readiness.v1' or engine['mode'] != 'local'
                or engine['server_version'] is not None or engine['connection_name'] is not None
                or engine['delegation'] != {'schema': 'cortex.linux-cgroup-readiness.v1', 'rootless': True,
                    'cgroup_version': 'v2', 'cgroup_controllers': ['cpu', 'memory', 'pids']}
                or engine['delegation']['rootless'] is not True):
            raise ProvisionRefusal('cortex_podman_unsupported')
        version = custody.parse_podman_report(json.dumps({'Client': {'Version': engine['client_version']}},
            allow_nan=False).encode(), host_os='linux', mode='local', connection_name=None,
            expected_connection_name=None, cortex_policy=release['podman'], kos_policy=policy)['client_version']
        local = _local_engine_context()
        if local.get('provider') != 'linuxbrew':
            raise ProvisionRefusal('cortex_podman_unsupported')
        project = {'scope_id': private['project_id'], 'primary_alias': body['project_key'], 'primary_root': body['repo_root']}
        fields = {'roster_revision', 'principal_id', 'actor_id', 'scope_id', 'project', 'member_name',
                  'actor_kind', 'role', 'status', 'can_read', 'can_write', 'can_publish', 'project_root'}

        def members():
            if time.monotonic() >= deadline:
                raise ProvisionRefusal('cortex_health_unavailable')
            value = strict_json(json.dumps(recheck_recipients(), allow_nan=False).encode())
            if time.monotonic() >= deadline or set(value) != {'lead', 'console'}:
                raise ProvisionRefusal('cortex_credential_refused')
            for role in ('lead', 'console'):
                row = value[role]
                expected = {'principal_id': private[role]['principal_id'], 'scope_id': private['project_id'],
                    'project': body['project_key'], 'member_name': body['lead_name'] if role == 'lead' else 'console',
                    'actor_kind': 'agent' if role == 'lead' else 'service', 'role': 'lead' if role == 'lead' else 'member',
                    'status': 'active', 'project_root': body['repo_root']}
                if (not isinstance(row, dict) or set(row) != fields or any(row[k] != v for k,v in expected.items())
                        or row['can_read'] is not True or row['can_write'] is not True
                        or row['can_publish'] is not (role == 'lead')
                        or type(row['roster_revision']) is not int or row['roster_revision'] < 0):
                    raise ProvisionRefusal('cortex_credential_refused')
                custody.canonical_uuid(row['actor_id'])
            if (value['lead']['actor_id'] == value['console']['actor_id']
                    or value['lead']['roster_revision'] != value['console']['roster_revision']):
                raise ProvisionRefusal('cortex_provisioning_conflict')
            return value

        selected_members = members()

        def check():
            if time.monotonic() >= deadline:
                raise ProvisionRefusal('cortex_health_unavailable')
            if (read_linux_runtime(runtime_root, kos_policy=policy, deadline=deadline) != context
                    or _local_engine_context() != local):
                raise ProvisionRefusal('cortex_instance_mismatch')
            if members() != selected_members:
                raise ProvisionRefusal('cortex_provisioning_conflict')
            if time.monotonic() >= deadline:
                raise ProvisionRefusal('cortex_health_unavailable')

        predicates = {'source_payload_matches', 'migration_checksums_match', 'required_rls_enabled_and_forced',
                      'app_role_non_superuser_without_bypassrls', 'app_role_not_migrator_or_table_owner', 'database_instance_matches'}
        provider_fields = {'schema', 'provider', 'formula', 'version', 'linked_keg', 'poured_from_bottle',
                           'bottle_sha256', 'bottle_url', 'checksum_verifier', 'scope'}
        security_fields = {'schema', 'installation_id', 'rootless', 'storage_config_matches', 'storage_custody_matches', 'selinux'}
        observed = None
        check()
        for _ in range(2):
            provider = observe_linuxbrew_provider(cortex_policy=release['podman'], kos_policy=policy, deadline=deadline)
            check()
            if (set(provider) != provider_fields or provider['schema'] != 'cortex.linuxbrew-provider.v1'
                    or provider['provider'] != 'linuxbrew' or provider['formula'] != 'homebrew/core/podman'
                    or provider['version'] != version or provider['linked_keg'] != local['linked_keg']
                    or re.fullmatch(re.escape(version) + r'(?:_[1-9][0-9]*)?', provider['linked_keg']) is None
                    or provider['poured_from_bottle'] is not True or not custody.HEX.fullmatch(provider['bottle_sha256'])
                    or provider['bottle_url'] != 'https://ghcr.io/v2/homebrew/core/podman/blobs/sha256:' + provider['bottle_sha256']
                    or provider['checksum_verifier'] != 'Homebrew'
                    or provider['scope'] != 'current formula metadata and installed stock keg; Homebrew verifies downloaded bottle bytes'):
                raise ProvisionRefusal('cortex_podman_unsupported')
            security = read_linux_host_security(runtime_root, kos_policy=policy, deadline=deadline)
            check()
            if (set(security) != security_fields or security['schema'] != 'cortex.linux-host-security.v1'
                    or security['installation_id'] != installation or security['rootless'] is not True
                    or security['storage_config_matches'] is not True or security['storage_custody_matches'] is not True
                    or security['selinux'] != 'Enforcing'):
                raise ProvisionRefusal('cortex_health_degraded')
            catalog = read_linux_catalog(runtime_root, request, private, kos_policy=policy, store=store, deadline=deadline)
            check()
            if (set(catalog) != predicates | {'schema', 'installation_id', 'project_binding'}
                    or catalog['schema'] != 'cortex.linux-catalog-readiness.v1' or catalog['installation_id'] != installation
                    or any(catalog[k] is not True for k in predicates) or catalog['project_binding'] != project):
                raise ProvisionRefusal('cortex_health_degraded')
            if observed is None: observed = (provider, security, catalog)
            elif observed != (provider, security, catalog):
                raise ProvisionRefusal('cortex_health_degraded')
        check()
        origin = urlsplit(context['origin'])
        if (origin.scheme != 'http' or origin.hostname != '127.0.0.1' or origin.username is not None
                or origin.password is not None or origin.path or origin.query or origin.fragment
                or origin.port is None or not 1024 <= origin.port <= 65535 or origin.port in (8501, 5499, 5500)):
            raise ProvisionRefusal('cortex_instance_mismatch')
        return {'schema': 'cortex.linux-readiness.v1', 'status': 'READY', 'installation_id': installation,
                'release_manifest_sha256': context['release_manifest_sha256'], 'target': 'linux-x86_64',
                'engine': {'client_version': version, 'server_version': None, 'rootless': True, 'os': 'linux',
                    'architecture': 'amd64', 'provider': 'cortex-native-lifecycle', 'mode': 'local', 'connection_name': None},
                'api_binding': {'loopback_port': origin.port, 'installation_label_matches': True,
                    'api_image_id': release['images']['api']['config_id'], 'exact_owned_network': True, 'network_internal': True},
                'images': {role: release['images'][role]['config_id'] for role in custody.ROLES},
                'selinux': 'Enforcing', 'signed_helper_verified': True, 'helper_sha256': context['helper_sha256'],
                'project_binding': project, **{key: catalog[key] for key in predicates}}
    except PrerequisiteRefusal as error:
        raise ProvisionRefusal(error.code if error.code in REFUSALS else 'cortex_health_degraded',
                               http_status=error.http_status) from None
    except Exception:
        raise ProvisionRefusal('cortex_health_degraded') from None
    finally:
        for value in (private, request):
            if isinstance(value, dict): value.clear()


def _publish_private_once(path: Path, value: dict, recheck) -> dict:
    """Publish one private inode without replacing any existing name.

    The caller supplies a live observation guard. A matching existing file is
    retained byte for byte. This primitive never creates or repairs a parent.
    """
    directory = descriptor = None
    temporary = None
    written = None
    created = False
    parents = {}

    def identity(info):
        return custody._file_identity(info), info.st_ctime_ns

    def check():
        recheck()
        custody._recheck_parents(parents)
        if custody._directory_identity(os.fstat(directory)) != parents[path.parent]:
            raise ProvisionRefusal('cortex_descriptor_invalid')

    def same_bytes_identity(info):
        current = custody._file_identity(info)
        expected = custody._file_identity(written)
        return current[:4] == expected[:4] and current[5:] == expected[5:]

    try:
        if not callable(recheck): raise ValueError
        raw = json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode() + b'\n'
        wanted = strict_json(raw)
        parents = custody._parents(path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        check()
        try:
            existing = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None:
            custody._private_file(existing)
            descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            opened = os.fstat(descriptor); custody._private_file(opened)
            if identity(opened) != identity(existing): raise ValueError
            actual = os.read(descriptor, 65537)
            if strict_json(actual) != wanted: raise ProvisionRefusal('cortex_provisioning_conflict')
            check()
            final = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
            if identity(final) != identity(opened) or identity(os.fstat(descriptor)) != identity(opened):
                raise ValueError
            return {'created': False, 'identity': identity(final), 'raw': actual, 'parents': parents}

        temporary = '.' + secrets.token_hex(16)
        descriptor = os.open(temporary, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=directory)
        custody._private_file(os.fstat(descriptor))
        offset = 0
        while offset < len(raw):
            check()
            count = os.write(descriptor, raw[offset:])
            if count <= 0: raise ValueError
            offset += count
        os.fsync(descriptor)
        written = os.fstat(descriptor); custody._private_file(written)
        check()
        if identity(os.stat(temporary, dir_fd=directory, follow_symlinks=False)) != identity(written):
            raise ValueError
        os.link(temporary, path.name, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
        created = True
        linked = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
        if linked.st_nlink != 2 or not same_bytes_identity(linked): raise ValueError
        temporary_info = os.stat(temporary, dir_fd=directory, follow_symlinks=False)
        if custody._file_identity(temporary_info) != custody._file_identity(linked): raise ValueError
        os.unlink(temporary, dir_fd=directory); temporary = None
        os.fsync(directory)
        check()
        final = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
        custody._private_file(final)
        if not same_bytes_identity(final) or identity(final) != identity(os.fstat(descriptor)): raise ValueError
        os.lseek(descriptor, 0, os.SEEK_SET)
        if os.read(descriptor, 65537) != raw: raise ValueError
        check()
        if identity(os.stat(path.name, dir_fd=directory, follow_symlinks=False)) != identity(final): raise ValueError
        return {'created': True, 'identity': identity(final), 'raw': raw, 'parents': parents}
    except BaseException as error:
        if created and written is not None and directory is not None:
            try:
                custody._recheck_parents(parents)
                current = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
                if same_bytes_identity(current) and current.st_nlink in (1, 2):
                    os.unlink(path.name, dir_fd=directory)
            except Exception:
                pass
        if isinstance(error, PrerequisiteRefusal):
            raise ProvisionRefusal(error.code if error.code in REFUSALS else 'cortex_descriptor_invalid') from None
        raise ProvisionRefusal('cortex_descriptor_invalid') from None
    finally:
        cleanup_failed = False
        if temporary is not None and directory is not None and descriptor is not None:
            try:
                current = os.stat(temporary, dir_fd=directory, follow_symlinks=False)
                opened = os.fstat(descriptor)
                if custody._file_identity(current) != custody._file_identity(opened): raise ValueError
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass
            except Exception:
                cleanup_failed = True
        for fd in (descriptor, directory):
            if fd is not None:
                try: os.close(fd)
                except OSError: cleanup_failed = True
        if cleanup_failed: raise ProvisionRefusal('cortex_descriptor_invalid') from None


def publish_linux_prerequisite(runtime_root: Path, args: dict, response: dict, proof: dict,
                               *, kos_policy: dict, store, recheck_recipients,
                               connection_file: Path, descriptor_file: Path,
                               deadline: float | None = None) -> dict:
    """Publish state and connection before the independently bound descriptor.

    The operation lock and admitted_recipients context remain held by the caller.
    Partial valid companions are retained for an explicit receipt-only resume.
    """
    private = request = None
    published = {}
    marker = None
    try:
        now = time.monotonic()
        if deadline is None: deadline = now + 30
        if (type(deadline) not in (int, float) or not math.isfinite(deadline) or deadline <= now
                or not callable(recheck_recipients)):
            raise ProvisionRefusal('cortex_provisioning_setup_required')
        deadline = min(deadline, now + 30)
        request = strict_json(json.dumps(args, allow_nan=False).encode())
        private = strict_json(json.dumps(response, allow_nan=False).encode())
        measured = strict_json(json.dumps(proof, allow_nan=False).encode())
        policy = strict_json(json.dumps(kos_policy, allow_nan=False).encode())
        body = validate_request(request); _response(private, body)
        context = read_linux_runtime(runtime_root, kos_policy=policy, deadline=deadline)
        installation = custody.canonical_uuid(context['installation_id'])
        release = custody.validate_release_manifest(context['release'], target='linux-x86_64')
        if (context['host_os'] != 'linux' or context['runtime_root'] != str(runtime_root)
                or context['package_root'] != str(runtime_root / 'package')
                or store.installation != installation or store.backend != 'file'):
            raise ProvisionRefusal('cortex_instance_mismatch')

        fields = {'roster_revision', 'principal_id', 'actor_id', 'scope_id', 'project', 'member_name',
                  'actor_kind', 'role', 'status', 'can_read', 'can_write', 'can_publish', 'project_root'}
        def members():
            value = strict_json(json.dumps(recheck_recipients(), allow_nan=False).encode())
            if set(value) != {'lead', 'console'}: raise ValueError
            for role in ('lead', 'console'):
                row = value[role]
                expected = {'principal_id': private[role]['principal_id'], 'scope_id': private['project_id'],
                    'project': body['project_key'], 'member_name': body['lead_name'] if role == 'lead' else 'console',
                    'actor_kind': 'agent' if role == 'lead' else 'service', 'role': 'lead' if role == 'lead' else 'member',
                    'status': 'active', 'project_root': body['repo_root']}
                if (set(row) != fields or any(row[k] != v for k,v in expected.items())
                        or row['can_read'] is not True or row['can_write'] is not True
                        or row['can_publish'] is not (role == 'lead')
                        or type(row['roster_revision']) is not int or row['roster_revision'] < 0):
                    raise ProvisionRefusal('cortex_credential_refused')
                custody.canonical_uuid(row['actor_id'])
            if (value['lead']['actor_id'] == value['console']['actor_id']
                    or value['lead']['roster_revision'] != value['console']['roster_revision']):
                raise ProvisionRefusal('cortex_provisioning_conflict')
            return value

        selected_members = members()
        def check():
            if time.monotonic() >= deadline: raise ProvisionRefusal('cortex_health_unavailable')
            if read_linux_runtime(runtime_root, kos_policy=policy, deadline=deadline) != context:
                raise ProvisionRefusal('cortex_instance_mismatch')
            if members() != selected_members: raise ProvisionRefusal('cortex_provisioning_conflict')
            if time.monotonic() >= deadline: raise ProvisionRefusal('cortex_health_unavailable')

        def ready():
            check()
            fresh = read_linux_readiness(runtime_root, request, private, kos_policy=policy, store=store,
                                         recheck_recipients=recheck_recipients, deadline=deadline)
            if fresh != measured or fresh.get('schema') != 'cortex.linux-readiness.v1' or fresh.get('status') != 'READY':
                raise ProvisionRefusal('cortex_health_degraded')
            check()

        ready()
        if (measured['installation_id'] != installation or measured['target'] != 'linux-x86_64'
                or measured['release_manifest_sha256'] != context['release_manifest_sha256']
                or measured['helper_sha256'] != context['helper_sha256']
                or measured['helper_sha256'] != release['files']['bin/cortex']):
            raise ProvisionRefusal('cortex_image_mismatch')
        connection = custody.validate_connection({'schema': 'cortex.console-connection.v1',
            'origin': context['origin'], 'installation_id': installation, 'project': body['project_key'],
            'member_name': 'console', 'project_root': body['repo_root'],
            **{k: selected_members['console'][k] for k in ('principal_id', 'actor_id', 'scope_id')}})
        receipt = {k: private[k] for k in ('operation_id', 'project_id', 'project_key')}
        receipt['delivery_state'] = 'reissue_required'
        receipt.update({role: {k: private[role][k] for k in DeliveryJournal.RECIPIENT} for role in ('lead', 'console')})
        state = {'schema': 'cortex.prerequisite-state.v1', 'installation_id': installation,
                 'create_project': body, 'receipt': receipt, 'kos_policy': policy}
        descriptor = {'schema': 'cortex.prerequisite.v2', 'owner_uid': os.getuid(), 'host_os': 'linux',
            'target': 'linux-x86_64', 'api_origin': context['origin'], 'installation_id': installation,
            'runtime_root': str(runtime_root), 'package_root': context['package_root'],
            'release_manifest': str(runtime_root / 'signed/release.json'),
            'release_signature': str(runtime_root / 'signed/release.json.minisig'),
            'release_manifest_sha256': context['release_manifest_sha256'], 'connection_file': str(connection_file),
            'member_reader_archive_sha256': release['member_reader_archive_sha256'],
            'podman': {'minimum_version': release['podman']['minimum_version'],
                'policy_sha256': hashlib.sha256(json.dumps(release['podman'], sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
                'provider': 'cortex-native-lifecycle', 'connection_name': None, 'machine_name': None}}
        if descriptor_file.name.endswith('.state.json'):
            raise ProvisionRefusal('cortex_descriptor_invalid')
        state_file = descriptor_file.with_name(descriptor_file.name + '.state.json')
        values = {state_file: state, connection_file: connection, descriptor_file: descriptor}
        if len(values) != 3: raise ProvisionRefusal('cortex_descriptor_invalid')
        selected = {}
        for path, value in values.items():
            custody.physical_path(path)
            if (any(path != other and (path in other.parents or other in path.parents) for other in values)
                    or path == runtime_root / 'install.json'
                    or path.is_relative_to(runtime_root / 'package') or path.is_relative_to(runtime_root / 'signed')):
                raise ProvisionRefusal('cortex_descriptor_invalid')
            parents = custody._parents(path)
            try: info = path.lstat()
            except FileNotFoundError: info = None
            if info is not None:
                custody._private_file(info)
                if custody.read_private_json(path) != value: raise ProvisionRefusal('cortex_provisioning_conflict')
            selected[path] = (parents, None if info is None else (custody._file_identity(info), info.st_ctime_ns))

        def output_check():
            check()
            for path, (parents, original) in selected.items():
                custody._recheck_parents(parents)
                if path in published: expected = published[path]['identity']
                else: expected = original
                try: info = path.lstat()
                except FileNotFoundError: info = None
                actual = None if info is None else (custody._file_identity(info), info.st_ctime_ns)
                if actual != expected: raise ProvisionRefusal('cortex_descriptor_invalid')

        for path, value in values.items():
            output_check()
            # The per-file guard checks live custody, while the primitive owns
            # its newly linked name until its return registers that identity.
            result = _publish_private_once(path, value, check)
            published[path] = result
            if path == descriptor_file:
                marker = result
                observation = _RUNTIME_OBSERVATION.get()
                if observation is not None and result['created']:
                    observation.markers.append((path, result))
            output_check()
        ready()
        output_check()
        for path, value in values.items():
            if custody.read_private_bytes(path) != published[path]['raw'] or custody.read_private_json(path) != value:
                raise ProvisionRefusal('cortex_descriptor_invalid')
        output_check()
        return {'connection_file': str(connection_file), 'descriptor_file': str(descriptor_file)}
    except BaseException as error:
        if marker is not None and marker['created']:
            directory = None
            try:
                custody._recheck_parents(marker['parents'])
                directory = os.open(descriptor_file.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                if custody._directory_identity(os.fstat(directory)) != marker['parents'][descriptor_file.parent]: raise ValueError
                info = os.stat(descriptor_file.name, dir_fd=directory, follow_symlinks=False)
                if (custody._file_identity(info), info.st_ctime_ns) != marker['identity']: raise ValueError
                os.unlink(descriptor_file.name, dir_fd=directory)
                os.fsync(directory)
            except Exception:
                pass
            finally:
                if directory is not None:
                    try: os.close(directory)
                    except OSError: pass
        if isinstance(error, PrerequisiteRefusal):
            raise ProvisionRefusal(error.code if error.code in REFUSALS else 'cortex_descriptor_invalid') from None
        raise ProvisionRefusal('cortex_descriptor_invalid') from None
    finally:
        for value in (private, request):
            if isinstance(value, dict): value.clear()
