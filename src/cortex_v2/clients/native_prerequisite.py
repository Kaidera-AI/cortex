"""CM-2 trust, private file custody and bounded member admission.

This module has no provisioning, engine mutation, ambient credentials or DB access.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import subprocess
import time
import uuid
from urllib.parse import quote

RELEASE_PUBLIC_KEY = 'RWQuhegMfku7e4RltjV64sZmxXHEETzAntDePCQsJPYvXXujVMqKIvHL'
RELEASE_KEY_ID = '7BBB4B7E0CE8852E'
READER_SHA256 = '496cad574b823cd13ba01ccc2d5112667f27c11cb7f7677fe1130a6e860aff3a'
HEX = re.compile(r'[0-9a-f]{64}')
IDENTIFIER = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}')
ROLES = ('db', 'api', 'doc', 'embed', 'graph')
CGROUP_FIX = 'Enable systemd delegation of cpu, memory and pids for the kos user; see INSTALL-linux.md.'
STORAGE_FIX = 'Complete the per-user ~/.config/containers/storage.conf step in INSTALL-linux.md; Cortex does not write Podman configuration.'
RELEASE_FIELDS = {'schema', 'release_id', 'release_lineage', 'release_sequence', 'api_contract',
    'source_revision', 'deployment_class', 'target', 'archive', 'payload_manifest_sha256',
    'files', 'images', 'migrations', 'rls_inventory', 'podman', 'member_reader_archive_sha256'}
CONNECTION_FIELDS = {'schema', 'origin', 'installation_id', 'project', 'member_name',
    'project_root', 'principal_id', 'actor_id', 'scope_id'}


class PrerequisiteRefusal(RuntimeError):
    def __init__(self, code: str, *, http_status: int | None = None):
        if not re.fullmatch(r'cortex_[a-z_]+', code):
            code = 'cortex_descriptor_invalid'
        self.code, self.http_status = code, http_status
        message = { 'cortex_cgroup_delegation_unavailable': CGROUP_FIX,
                    'cortex_podman_storage_setup_required': STORAGE_FIX }.get(code, 'prerequisite refused; see INSTALL-linux.md')
        super().__init__(code + ': ' + message)

    def public(self) -> dict:
        value = {'code': self.code, 'safe_message': 'Cortex prerequisite refused.',
                 'install_guide': 'INSTALL-linux.md',
                 'utc': datetime.datetime.now(datetime.timezone.utc).isoformat()}
        if self.code == 'cortex_cgroup_delegation_unavailable':
            value['safe_message'] = CGROUP_FIX
        if self.code == 'cortex_podman_storage_setup_required':
            value['safe_message'] = STORAGE_FIX
        if type(self.http_status) is int:
            value['http_status_if_observed'] = self.http_status
        return value


def check_linux_delegation(raw: bytes) -> dict:
    """Check a bounded actual engine observation; never perform a host repair."""
    try:
        value = strict_json(raw)
        host = value['host']
        controllers = host['cgroupControllers']
        known = {'cpu', 'cpuset', 'io', 'memory', 'pids', 'hugetlb', 'rdma', 'misc',
                 'devices', 'freezer', 'blkio', 'net_cls', 'net_prio', 'perf_event'}
        if (host['cgroupVersion'] != 'v2' or host['security']['rootless'] is not True
                or not isinstance(controllers, list) or not 3 <= len(controllers) <= len(known)
                or any(not isinstance(item, str) or item not in known for item in controllers)
                or len(set(controllers)) != len(controllers)
                or not {'cpu', 'memory', 'pids'} <= set(controllers)):
            raise ValueError
        return {'schema': 'cortex.linux-cgroup-readiness.v1', 'rootless': True,
                'cgroup_version': 'v2', 'cgroup_controllers': ['cpu', 'memory', 'pids']}
    except (PrerequisiteRefusal, KeyError, ValueError, TypeError, AttributeError):
        raise PrerequisiteRefusal('cortex_cgroup_delegation_unavailable') from None


def check_linux_storage_config(raw: bytes, *, uid: int, home: str) -> dict:
    """Check actual engine paths only; filesystem custody is a separate predicate."""
    try:
        value = strict_json(raw)
        if (type(uid) is not int or not 0 < uid <= 4294967295 or not isinstance(home, str)
                or not Path(home).is_absolute() or str(Path(home)) != home or '..' in Path(home).parts
                or any(ord(c) < 32 for c in home) or value['host']['security']['rootless'] is not True):
            raise ValueError
        graph_root = str(Path(home) / '.local/share/containers/storage')
        run_root = '/run/user/' + str(uid) + '/containers'
        store = value['store']
        if (store['graphRoot'] != graph_root or store['runRoot'] != run_root
                or store['graphDriverName'] != 'overlay'):
            raise ValueError
        return {'schema': 'cortex.linux-storage-config.v1', 'rootless': True,
                'graph_root': graph_root, 'run_root': run_root, 'driver': 'overlay'}
    except Exception:
        raise PrerequisiteRefusal('cortex_podman_storage_setup_required') from None


def strict_json(raw: bytes, *, limit: int = 65536) -> dict:
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError('duplicate key')
            value[key] = item
        return value
    def constant(_):
        raise ValueError('non-finite value')
    try:
        if not isinstance(raw, bytes) or len(raw) > limit:
            raise ValueError('bounded bytes required')
        value = json.loads(raw.decode('utf-8', 'strict'), object_pairs_hook=pairs,
                           parse_constant=constant)
        if not isinstance(value, dict):
            raise ValueError('object required')
        return value
    except (ValueError, UnicodeError, TypeError, RecursionError):
        raise PrerequisiteRefusal('cortex_descriptor_invalid') from None


def canonical_uuid(value) -> str:
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise PrerequisiteRefusal('cortex_instance_mismatch') from None
    return value


def physical_path(path: Path) -> None:
    if (not isinstance(path, Path) or not path.is_absolute() or '..' in path.parts
            or any(ord(c) < 32 for c in str(path))
            or any(p.is_symlink() for p in (path, *path.parents))):
        raise PrerequisiteRefusal('cortex_descriptor_invalid')


def _directory_identity(info):
    return info.st_dev, info.st_ino, info.st_uid, stat.S_IMODE(info.st_mode)


def _parents(path: Path) -> dict[Path, tuple]:
    physical_path(path)
    uid = os.getuid()
    if uid == 0 or os.geteuid() != uid:
        raise PrerequisiteRefusal('cortex_descriptor_owner_mismatch')
    result = {}
    for parent in path.parents:
        info = parent.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, uid)
                or stat.S_IMODE(info.st_mode) & 0o022):
            raise PrerequisiteRefusal('cortex_descriptor_invalid')
        result[parent] = _directory_identity(info)
    selected = path.parent.lstat()
    if selected.st_uid != uid or stat.S_IMODE(selected.st_mode) != 0o700:
        raise PrerequisiteRefusal('cortex_descriptor_invalid')
    return result


def _recheck_parents(snapshot) -> None:
    for parent, expected in snapshot.items():
        if _directory_identity(parent.lstat()) != expected:
            raise PrerequisiteRefusal('cortex_descriptor_invalid')


def _file_identity(info):
    return info.st_dev, info.st_ino, info.st_uid, info.st_mode, info.st_nlink, info.st_size, info.st_mtime_ns


def _private_file(info) -> None:
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
        raise PrerequisiteRefusal('cortex_descriptor_invalid')


def read_private_bytes(path: Path, *, limit: int = 65536) -> bytes:
    try:
        parents = _parents(path)
        before = path.lstat(); _private_file(before)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            opened = os.fstat(stream.fileno()); _private_file(opened)
            if _file_identity(opened) != _file_identity(before):
                raise PrerequisiteRefusal('cortex_descriptor_invalid')
            raw = stream.read(limit + 1)
            if len(raw) > limit or _file_identity(os.fstat(stream.fileno())) != _file_identity(opened):
                raise PrerequisiteRefusal('cortex_descriptor_invalid')
        if _file_identity(path.lstat()) != _file_identity(opened):
            raise PrerequisiteRefusal('cortex_descriptor_invalid')
        _recheck_parents(parents)
        return raw
    except OSError:
        raise PrerequisiteRefusal('cortex_descriptor_invalid') from None


def read_private_json(path: Path, *, limit: int = 65536) -> dict:
    return strict_json(read_private_bytes(path, limit=limit), limit=limit)


def atomic_private_json(path: Path, value: dict, *, limit: int = 65536) -> None:
    try:
        raw = json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode() + b'\n'
        strict_json(raw, limit=limit)
        parents = _parents(path)
        if path.exists() or path.is_symlink():
            _private_file(path.lstat())
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        temporary = '.' + secrets.token_hex(16)
        try:
            _recheck_parents(parents)
            if _directory_identity(os.fstat(directory)) != parents[path.parent]:
                raise PrerequisiteRefusal('cortex_descriptor_invalid')
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=directory)
            with os.fdopen(fd, 'wb') as stream:
                _recheck_parents(parents)
                stream.write(raw); stream.flush(); os.fsync(stream.fileno())
            _recheck_parents(parents)
            try:
                existing = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                existing = None
            if existing is not None:
                _private_file(existing)
            os.replace(temporary, path.name, src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
            _recheck_parents(parents)
        finally:
            try: os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError: pass
            os.close(directory)
    except (OSError, ValueError, TypeError):
        raise PrerequisiteRefusal('cortex_descriptor_invalid') from None


def verify_signature(manifest: Path, signature: Path, *, trusted_public_key: str = RELEASE_PUBLIC_KEY,
                     timeout: float = 5) -> None:
    try:
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 5:
            raise ValueError
        before = (read_private_bytes(manifest, limit=1048576), read_private_bytes(signature, limit=4096))
        if not re.fullmatch(r'[A-Za-z0-9+/]{56}', trusted_public_key):
            raise ValueError
        result = subprocess.run([shutil.which('minisign') or 'minisign', '-V', '-P', trusted_public_key,
                                 '-m', str(manifest), '-x', str(signature)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout)
        after = (read_private_bytes(manifest, limit=1048576), read_private_bytes(signature, limit=4096))
        if result.returncode != 0 or before != after:
            raise ValueError
    except (OSError, ValueError, subprocess.TimeoutExpired, PrerequisiteRefusal):
        raise PrerequisiteRefusal('cortex_release_signature_invalid') from None


def verify_installed_release(runtime_root: Path, *, deadline: float | None = None) -> dict:
    """Read and measure signed installed bytes before any native helper execution.

    This proves custody of the signed package. It does not establish a live engine,
    native ABI, image payload or readiness claim.
    """
    now = time.monotonic()
    if deadline is None:
        deadline = now + 30
    if type(deadline) not in (int, float) or not math.isfinite(deadline):
        raise PrerequisiteRefusal('cortex_health_unavailable')
    deadline = min(deadline, now + 60)

    def remaining():
        budget = deadline - time.monotonic()
        if budget <= 0:
            raise PrerequisiteRefusal('cortex_health_unavailable')
        return budget

    parents, files_seen = {}, {}

    def record_parents(path):
        physical_path(path)
        result = {}
        for parent in path.parents:
            info = parent.lstat()
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, os.getuid())
                    or stat.S_IMODE(info.st_mode) & 0o022):
                raise PrerequisiteRefusal('cortex_descriptor_invalid')
            identity = _directory_identity(info)
            if parent in parents and parents[parent] != identity:
                raise PrerequisiteRefusal('cortex_descriptor_invalid')
            parents[parent] = identity
            result[parent] = identity
        return result

    def identity(info):
        return _file_identity(info), info.st_ctime_ns

    def measure(path, *, executable=False, size=None):
        remaining()
        selected_parents = record_parents(path)
        before = path.lstat()

        def check(info):
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
                    or stat.S_IMODE(info.st_mode) & 0o022
                    or executable and not info.st_mode & stat.S_IXUSR
                    or size is not None and info.st_size != size):
                raise PrerequisiteRefusal('cortex_descriptor_invalid')

        check(before)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        digest = hashlib.sha256()
        with os.fdopen(fd, 'rb') as stream:
            opened = os.fstat(stream.fileno()); check(opened)
            if identity(opened) != identity(before):
                raise PrerequisiteRefusal('cortex_descriptor_invalid')
            while True:
                remaining()
                chunk = stream.read(65536)
                if not chunk:
                    break
                digest.update(chunk)
            if identity(os.fstat(stream.fileno())) != identity(opened):
                raise PrerequisiteRefusal('cortex_descriptor_invalid')
        remaining()
        if identity(path.lstat()) != identity(opened):
            raise PrerequisiteRefusal('cortex_descriptor_invalid')
        _recheck_parents(selected_parents)
        files_seen[path] = identity(opened)
        return digest.hexdigest()

    try:
        remaining(); physical_path(runtime_root)
        uid = os.getuid()
        if uid == 0 or os.geteuid() != uid:
            raise PrerequisiteRefusal('cortex_descriptor_owner_mismatch')
        package, signed = runtime_root / 'package', runtime_root / 'signed'
        for directory in (runtime_root, package, signed):
            physical_path(directory)
            info = directory.lstat()
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != uid
                    or stat.S_IMODE(info.st_mode) != 0o700):
                raise PrerequisiteRefusal('cortex_descriptor_invalid')
            parents[directory] = _directory_identity(info)
        manifest, signature = signed / 'release.json', signed / 'release.json.minisig'
        outer_raw = read_private_bytes(manifest, limit=1048576)
        signature_raw = read_private_bytes(signature, limit=4096)
        verify_signature(manifest, signature, timeout=min(5, remaining()))
        remaining()
        if read_private_bytes(manifest, limit=1048576) != outer_raw:
            raise PrerequisiteRefusal('cortex_release_signature_invalid')
        release = validate_release_manifest(strict_json(outer_raw, limit=1048576), target='linux-x86_64')
        archive = release['archive']
        retained = signed / archive['name']
        _private_file(retained.lstat())
        if measure(retained, size=archive['size_bytes']) != archive['sha256']:
            raise PrerequisiteRefusal('cortex_release_signature_invalid')
        inner_path = package / 'release.json'
        inner_raw = read_private_bytes(inner_path, limit=1048576)
        inner_digest = hashlib.sha256(inner_raw).hexdigest()
        if inner_digest != release['payload_manifest_sha256']:
            raise PrerequisiteRefusal('cortex_release_signature_invalid')
        inner = strict_json(inner_raw, limit=1048576)
        if (inner.get('schema') != 'cortex.test-package.v1' or inner.get('target') != release['target']
                or inner.get('deployment_class') != release['deployment_class']
                or inner.get('source_sha') != release['source_revision']
                or not isinstance(inner.get('files'), dict) or not 1 <= len(inner['files']) <= 4096
                or not isinstance(inner.get('images'), dict) or set(inner['images']) != set(ROLES)):
            raise PrerequisiteRefusal('cortex_release_unsupported')
        payload = inner['files']
        for name, digest in payload.items():
            if (not isinstance(name, str) or not name or Path(name).is_absolute()
                    or Path(name).as_posix() != name or any(part in ('.', '..') for part in name.split('/'))
                    or any(ord(c) < 32 for c in name) or name in ('release.json', 'SHA256SUMS')
                    or not isinstance(digest, str) or not HEX.fullmatch(digest)):
                raise PrerequisiteRefusal('cortex_release_unsupported')
        if any(payload.get(name) != digest for name, digest in release['files'].items()):
            raise PrerequisiteRefusal('cortex_release_signature_invalid')
        for role in ROLES:
            image = inner['images'][role]
            if (any(image.get(key) != value for key, value in release['images'][role].items())
                    or payload.get(image['archive']) != image['archive_sha256']):
                raise PrerequisiteRefusal('cortex_release_signature_invalid')

        # Enumerate without following links and refuse unlisted or special objects.
        actual, directories, pending = set(), set(), [package]
        count = 0
        while pending:
            directory = pending.pop(); remaining()
            with os.scandir(directory) as entries:
                for entry in entries:
                    remaining(); count += 1
                    if count > 8192:
                        raise PrerequisiteRefusal('cortex_descriptor_invalid')
                    path = Path(entry.path); info = path.lstat()
                    if info.st_uid != uid or stat.S_IMODE(info.st_mode) & 0o022:
                        raise PrerequisiteRefusal('cortex_descriptor_invalid')
                    name = path.relative_to(package).as_posix()
                    if stat.S_ISDIR(info.st_mode):
                        parents[path] = _directory_identity(info)
                        directories.add(name); pending.append(path)
                    elif stat.S_ISREG(info.st_mode):
                        actual.add(name)
                    else:
                        raise PrerequisiteRefusal('cortex_descriptor_invalid')
        optional_sums = {'SHA256SUMS'} if 'SHA256SUMS' in actual else set()
        expected = set(payload) | {'release.json'} | optional_sums
        expected_dirs = {str(parent) for name in expected for parent in Path(name).parents if str(parent) != '.'}
        if actual != expected or directories != expected_dirs:
            raise PrerequisiteRefusal('cortex_descriptor_invalid')
        for name, digest in payload.items():
            if measure(package / name, executable=name in ('bin/cortex', 'bin/cortex-agent')) != digest:
                raise PrerequisiteRefusal('cortex_release_signature_invalid')
        if optional_sums:
            sums = dict(payload, **{'release.json': inner_digest})
            expected_raw = ''.join(f'{value}  {name}\n' for name, value in sorted(sums.items())).encode()
            if measure(package / 'SHA256SUMS', size=len(expected_raw)) != hashlib.sha256(expected_raw).hexdigest():
                raise PrerequisiteRefusal('cortex_release_signature_invalid')
        if (read_private_bytes(manifest, limit=1048576) != outer_raw
                or read_private_bytes(signature, limit=4096) != signature_raw
                or read_private_bytes(inner_path, limit=1048576) != inner_raw):
            raise PrerequisiteRefusal('cortex_release_signature_invalid')
        for path, expected_identity in files_seen.items():
            remaining()
            if identity(path.lstat()) != expected_identity:
                raise PrerequisiteRefusal('cortex_descriptor_invalid')
        _recheck_parents(parents); remaining()
        return {'release': release, 'package_root': str(package),
                'helper_sha256': payload['bin/cortex'],
                'release_manifest_sha256': hashlib.sha256(outer_raw).hexdigest()}
    except PrerequisiteRefusal:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise PrerequisiteRefusal('cortex_descriptor_invalid') from None


def _version(value) -> tuple[int, int, int]:
    if not isinstance(value, str) or not re.fullmatch(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)', value):
        raise ValueError
    return tuple(map(int, value.split('.')))


def parse_podman_report(raw: bytes, *, host_os: str, mode: str, connection_name,
                        expected_connection_name, cortex_policy: dict, kos_policy: dict) -> dict:
    try:
        report = strict_json(raw)
        client = report['Client']['Version']; server = None
        if host_os == 'linux':
            if mode != 'local' or connection_name is not None or expected_connection_name is not None or report.get('Server') is not None:
                raise ValueError
        elif host_os == 'macos':
            if (mode != 'remote' or not isinstance(connection_name, str) or not connection_name
                    or connection_name != expected_connection_name):
                raise ValueError
            server = report['Server']['Version']
        else:
            raise ValueError
        actual = [client] if server is None else [client, server]
        for version in actual:
            numeric = _version(version)
            for policy in (cortex_policy, kos_policy):
                if numeric < _version(policy['minimum_version']):
                    raise ValueError
                for denial in policy['denylist']['entries']:
                    if _version(denial['version']) == numeric:
                        date = datetime.date.fromisoformat(denial['date']).isoformat()
                        error = PrerequisiteRefusal('cortex_podman_denied')
                        error.args = ('cortex_podman_denied: dated policy ' + date + '; see INSTALL-linux.md',)
                        raise error
        return {'mode': mode, 'client_version': client, 'server_version': server,
                'connection_name': connection_name}
    except PrerequisiteRefusal as error:
        if error.code == 'cortex_podman_denied': raise
        raise PrerequisiteRefusal('cortex_podman_unsupported') from None
    except (KeyError, ValueError, TypeError, AttributeError):
        raise PrerequisiteRefusal('cortex_podman_unsupported') from None


def validate_release_manifest(value: dict, *, target: str) -> dict:
    try:
        if (set(value) != RELEASE_FIELDS or value['schema'] != 'cortex.release.v2'
                or value['release_lineage'] != 'cortex-v2-native'
                or value['api_contract'] != 'cortex-kos-v02009.v2'
                or type(value['release_sequence']) is not int or value['release_sequence'] < 1
                or not isinstance(value['release_id'], str) or not 1 <= len(value['release_id']) <= 128
                or value['target'] != target or target != 'linux-x86_64'
                or value['deployment_class'] not in ('TEST', 'PRODUCTION')
                or not re.fullmatch(r'[0-9a-f]{40}', value['source_revision'])
                or not HEX.fullmatch(value['payload_manifest_sha256'])
                or value['member_reader_archive_sha256'] != READER_SHA256):
            raise ValueError
        archive = value['archive']
        if (set(archive) != {'name', 'sha256', 'size_bytes'}
                or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', archive['name'])
                or not HEX.fullmatch(archive['sha256'])
                or type(archive['size_bytes']) is not int or archive['size_bytes'] <= 0):
            raise ValueError
        if not {'bin/cortex', 'bin/cortex-agent'} <= set(value['files']):
            raise ValueError
        for name, digest in value['files'].items():
            if Path(name).is_absolute() or '..' in Path(name).parts or not HEX.fullmatch(digest):
                raise ValueError
        if set(value['images']) != set(ROLES):
            raise ValueError
        for role, image in value['images'].items():
            if (image['os'] != 'linux' or image['architecture'] != 'amd64'
                    or image['archive'] != 'images/' + role + '.oci.tar'
                    or not re.fullmatch(r'sha256:[0-9a-f]{64}', image['config_id'])
                    or not re.fullmatch(r'sha256:[0-9a-f]{64}', image['manifest_digest'])
                    or not HEX.fullmatch(image['archive_sha256'])
                    or not HEX.fullmatch(image['source_payload_sha256'])):
                raise ValueError
        migrations = value['migrations']
        if not isinstance(migrations, list) or not migrations:
            raise ValueError
        names = []
        for item in migrations:
            if (set(item) != {'name', 'sha256'} or not re.fullmatch(r'[0-9]{4}_[a-z0-9_]+\.sql', item['name'])
                    or not HEX.fullmatch(item['sha256'])):
                raise ValueError
            names.append(item['name'])
        if len(names) != len(set(names)) or names != sorted(names) or not HEX.fullmatch(value['rls_inventory']['sha256']):
            raise ValueError
        policy = value['podman']
        if _version(policy['minimum_version']) < (6, 0, 2) or policy['provider'] != 'cortex-native-lifecycle':
            raise ValueError
        datetime.date.fromisoformat(policy['denylist']['as_of'])
        for item in policy['denylist']['entries']:
            _version(item['version']); datetime.date.fromisoformat(item['date'])
        return value
    except (ValueError, TypeError, KeyError, AttributeError):
        raise PrerequisiteRefusal('cortex_release_unsupported') from None


def validate_connection(value: dict) -> dict:
    try:
        if (set(value) != CONNECTION_FIELDS or value['schema'] != 'cortex.console-connection.v1'
                or value['member_name'] != 'console' or not IDENTIFIER.fullmatch(value['project'])
                or not re.fullmatch(r'http://127\.0\.0\.1:[0-9]{1,5}', value['origin'])
                or not 1 <= int(value['origin'].rsplit(':', 1)[1]) <= 65535):
            raise ValueError
        for field in ('installation_id', 'principal_id', 'actor_id', 'scope_id'):
            canonical_uuid(value[field])
        root = Path(value['project_root']); physical_path(root)
        if not root.is_dir(): raise ValueError
        return value
    except (ValueError, TypeError, KeyError, AttributeError, OSError):
        raise PrerequisiteRefusal('cortex_descriptor_invalid') from None


def read_member_admission(connection: dict, *, reader, transport, deadline: float | None = None) -> dict:
    validate_connection(connection)
    deadline = time.monotonic() + 60 if deadline is None else deadline
    snapshot = None
    def get(path):
        nonlocal snapshot
        try:
            headers = reader.headers()
        except Exception:
            raise PrerequisiteRefusal('cortex_credential_unavailable') from None
        if (not isinstance(headers, dict) or set(headers) != {'Authorization', 'X-Cortex-Scope'}
                or headers.get('X-Cortex-Scope') != connection['project']
                or not re.fullmatch(r'Bearer [A-Za-z0-9_-]{43}', headers.get('Authorization', ''))):
            raise PrerequisiteRefusal('cortex_credential_unavailable')
        if snapshot is None:
            snapshot = dict(headers)
        elif snapshot != headers:
            raise PrerequisiteRefusal('cortex_credential_refused')
        remaining = min(5, deadline - time.monotonic())
        if remaining <= 0: raise PrerequisiteRefusal('cortex_health_unavailable')
        try:
            status, raw = transport.get(connection['origin'] + path, headers=headers,
                                         timeout=remaining, max_bytes=65536)
        except Exception:
            raise PrerequisiteRefusal('cortex_health_unavailable') from None
        if time.monotonic() >= deadline: raise PrerequisiteRefusal('cortex_health_unavailable')
        if status != 200:
            raise PrerequisiteRefusal('cortex_credential_refused' if status in (401, 403, 404)
                                     else 'cortex_health_unavailable', http_status=status)
        return strict_json(raw)
    try:
        health = get('/health/ready')
        if health.get('status') != 'ready': raise ValueError
        principal = get('/v1/auth/principal')['data']
        if principal['installation_id'] != connection['installation_id'] or principal['principal_id'] != connection['principal_id']:
            raise ValueError
        grants = principal['scopes']
        if not isinstance(grants, list) or len(grants) != 1: raise ValueError
        grant = grants[0]
        if (grant['scope_id'] != connection['scope_id'] or grant['primary_alias'] != connection['project']
                or grant['scope_kind'] != 'project' or grant['can_read'] is not True
                or grant['can_write'] is not True or grant['can_publish'] is not False):
            raise ValueError
        roster = get('/v1/scopes/' + quote(connection['project'], safe='') + '/roster')['data']
        entries = [e for e in roster['entries'] if e.get('display_name') == 'console']
        if (roster['scope_id'] != connection['scope_id'] or len(entries) != 1
                or type(roster['roster_revision']) is not int or roster['roster_revision'] < 0):
            raise ValueError
        member = entries[0]
        if (member['principal_id'] != connection['principal_id'] or member['actor_id'] != connection['actor_id']
                or member['actor_kind'] != 'service' or member['role'] != 'member' or member['status'] != 'active'):
            raise ValueError
        return {'roster_revision': roster['roster_revision'], 'principal_id': connection['principal_id'],
                'actor_id': connection['actor_id'], 'scope_id': connection['scope_id'],
                'project': connection['project'], 'member_name': 'console', 'actor_kind': 'service',
                'role': 'member', 'status': 'active', 'can_read': True, 'can_write': True,
                'can_publish': False, 'project_root': connection['project_root']}
    except (ValueError, KeyError, TypeError, AttributeError):
        raise PrerequisiteRefusal('cortex_credential_refused') from None
    finally:
        snapshot = None
