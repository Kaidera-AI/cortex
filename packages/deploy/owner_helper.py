"""Private host invocation of the finite owner client; no ambient engine or secrets.

Only called explicitly by a checksum-validated runtime. Pairing is read-only on
the API; Cortex orchestration never retries owner operations. Podman's own
transport replay still requires qualification. No volume creation, startup or SQL.
"""
from __future__ import annotations

import contextlib
import ctypes
import json
import os
from pathlib import Path
import pwd
import re
import selectors
import shutil
import signal
import socket
import stat
import struct
import subprocess
import sys
import time
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import image_manifest as images

PROTOCOL = 'cortex.owner.v1'
MAX_INPUT, MAX_OUTPUT = 16384, 32768
CID = re.compile(r'[a-f0-9]{64}')


class HelperError(Exception):
    """Non-secret status only. A dispatched operation is never automatically retried."""


def clean_environment():
    # Neither caller-supplied engine overrides nor proxy/provider/DB credentials
    # enter the CLI process. The selected account's normal Podman config remains.
    result = {'HOME': pwd.getpwuid(os.getuid()).pw_dir, 'PATH': os.defpath, 'LANG': 'C.UTF-8'}
    if sys.platform.startswith('linux'):
        result['XDG_RUNTIME_DIR'] = '/run/user/' + str(os.getuid())
    return result


def run_bounded(argv, data=b'', *, limit=1048576, timeout=10):
    """Hard bounds while streaming, not communicate() followed by a size check.

    Stderr is discarded, never accumulated or forwarded. On cancellation only
    this subprocess's new process group is killed; remote CID cleanup is owned
    separately by the exact-create workflow below.
    """
    if len(data) > MAX_INPUT or not 0 < timeout <= 60 or not 0 < limit <= 1048576:
        raise HelperError('invalid owner pipe bounds')
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, env=clean_environment(), start_new_session=True)
    result = bytearray()
    deadline = time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as poll:
            os.set_blocking(proc.stdout.fileno(), False)
            poll.register(proc.stdout, selectors.EVENT_READ)
            if data:
                os.set_blocking(proc.stdin.fileno(), False)
                poll.register(proc.stdin, selectors.EVENT_WRITE)
            else: proc.stdin.close()
            sent = 0
            while poll.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0: raise HelperError('owner helper timed out')
                for key, _ in poll.select(min(remaining, 0.1)):
                    if key.fileobj is proc.stdout:
                        block = os.read(proc.stdout.fileno(), min(4096, limit + 1 - len(result)))
                        if not block:
                            poll.unregister(proc.stdout)
                            continue
                        result.extend(block)
                        if len(result) > limit: raise HelperError('owner helper output exceeded limit')
                    else:
                        sent += os.write(proc.stdin.fileno(), data[sent:sent + 4096])
                        if sent == len(data):
                            poll.unregister(proc.stdin)
                            proc.stdin.close()
            remaining = deadline - time.monotonic()
            if remaining <= 0 or proc.wait(timeout=remaining) != 0:
                raise HelperError('owner helper failed')
        return bytes(result)
    except BaseException:
        # A descendant can retain the pipe after the direct child exits. The
        # new process group remains ours, irrespective of the child's status.
        with contextlib.suppress(ProcessLookupError): os.killpg(proc.pid, signal.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired): proc.wait(timeout=2)
        raise HelperError('owner helper unavailable; outcome unknown, do not retry automatically') from None
    finally:
        proc.stdout.close()
        proc.stdin.close()


def _json(data):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value: raise HelperError('duplicate owner metadata')
            value[key] = item
        return value
    try:
        return json.loads(data, object_pairs_hook=pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(HelperError('invalid owner metadata')))
    except (ValueError, UnicodeError, RecursionError):
        raise HelperError('invalid owner metadata') from None


def _uuid(value):
    if not isinstance(value, str): raise HelperError('invalid owner instance')
    try:
        if str(UUID(value)) != value: raise ValueError
    except ValueError: raise HelperError('invalid owner instance') from None
    return value


def _path(value):
    path = Path(value)
    if (not path.is_absolute() or '..' in path.parts
            or any(p.is_symlink() for p in [*path.parents, path])):
        raise HelperError('owner paths must be absolute and must not follow links')
    return path


def _directory(value):
    path = _path(value)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise HelperError('owner directory must already be private')
    return path


def _private_read(value, limit=MAX_INPUT):
    path = _path(value)
    _directory(path.parent)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > limit):
            raise HelperError('owner input must be a bounded private regular file')
        with os.fdopen(fd, 'rb', closefd=False) as stream: data = stream.read(limit + 1)
        if len(data) > limit: raise HelperError('owner input too large')
        return data
    finally: os.close(fd)


@contextlib.contextmanager
def _exclusive_output(value):
    path = _path(value)
    _directory(path.parent)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    info = os.fstat(fd)
    try:
        yield fd
        current = path.lstat()
        if ((current.st_dev, current.st_ino) != (info.st_dev, info.st_ino)
                or current.st_nlink != 1 or not stat.S_ISREG(current.st_mode)
                or current.st_uid != os.getuid() or stat.S_IMODE(current.st_mode) != 0o600):
            raise HelperError('private owner output was replaced; outcome unknown')
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            current = path.lstat()
            if (current.st_dev, current.st_ino) == (info.st_dev, info.st_ino): path.unlink()
        raise
    finally: os.close(fd)


def _save(fd, data):
    with os.fdopen(fd, 'wb', closefd=False) as stream:
        stream.write(data)
        stream.flush()
        os.fsync(fd)


def _one(raw):
    records = _json(raw)
    if not isinstance(records, list) or len(records) != 1 or not isinstance(records[0], dict):
        raise HelperError('owner inspection requires exactly one result')
    return records[0]


def _keep_id_map(rows):
    # One owner row plus one subordinate range; no generic namespace mapper.
    # Linux's all-ones UID/GID sentinel must not occur in either mapped range.
    limit = 2**32 - 1
    if (not isinstance(rows, list) or len(rows) != 2
            or any(not isinstance(row, dict) or set(row) != {'container_id', 'host_id', 'size'}
                   or any(type(n) is not int or n < 0 for n in row.values())
                   or row['size'] == 0
                   or row['container_id'] + row['size'] > limit
                   or row['host_id'] + row['size'] > limit for row in rows)):
        raise HelperError('owner engine identity mappings unavailable')
    first, subordinate = rows
    if (first['container_id'] != 0 or first['size'] != 1 or first['host_id'] == 0
            or subordinate['container_id'] != 1 or subordinate['host_id'] == 0
            or subordinate['size'] <= 10001
            or subordinate['host_id'] <= first['host_id'] < subordinate['host_id'] + subordinate['size']):
        raise HelperError('owner engine identity mappings unsupported')
    return ['0:1:10001', '10001:0:1', f"10002:10002:{subordinate['size'] - 10001}"]


def _peer_uid(conn):
    if sys.platform.startswith('linux'):
        return struct.unpack('3i', conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1]
    if sys.platform == 'darwin':
        uid, gid = ctypes.c_uint(), ctypes.c_uint()
        getpeereid = ctypes.CDLL(None, use_errno=True).getpeereid
        getpeereid.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_uint)]
        getpeereid.restype = ctypes.c_int
        if getpeereid(conn.fileno(), ctypes.byref(uid), ctypes.byref(gid)) != 0:
            raise HelperError('engine peer identity unavailable')
        return uid.value
    raise HelperError('engine peer identity unsupported')


def _engine_socket_directories(parent):
    """Recognize a private ancestor without relaxing secret/control directories.

    Every intervening directory belongs to this account and is not writable by
    another account. Snapshot security identity, not volatile directory contents.
    """
    chain = []
    for directory in (parent, *parent.parents):
        info = directory.lstat()
        mode = stat.S_IMODE(info.st_mode)
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or mode & 0o7022 or not mode & 0o100):
            raise HelperError('engine path requires an owned private boundary')
        chain.append((str(directory), info.st_dev, info.st_ino,
                      info.st_uid, info.st_gid, info.st_mode))
        if mode == 0o700:
            return tuple(chain)
    raise HelperError('engine path requires an owned private boundary')


def _endpoint(path):
    path = _path(path)
    directories = _engine_socket_directories(path.parent)
    before = path.lstat()
    if (not stat.S_ISSOCK(before.st_mode) or before.st_nlink != 1
            or before.st_uid != os.getuid() or stat.S_IMODE(before.st_mode) != 0o600):
        raise HelperError('engine endpoint must be an owned private socket')
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(2)
        conn.connect(str(path))
        if _peer_uid(conn) != os.getuid(): raise HelperError('engine peer account differs')
    _path(path)  # Recheck all ancestors, including those above the private boundary.
    if _engine_socket_directories(path.parent) != directories:
        raise HelperError('engine path changed during verification')
    after = path.lstat()
    if (before.st_dev, before.st_ino, before.st_mode, before.st_uid) != (
            after.st_dev, after.st_ino, after.st_mode, after.st_uid):
        raise HelperError('engine endpoint changed during verification')
    return {'path': str(path), 'device': before.st_dev, 'inode': before.st_ino, 'uid': before.st_uid}


class Engine:
    def __init__(self, selector, runner):
        self.runner = runner
        binary = shutil.which('podman')
        if not binary or not Path(binary).is_absolute(): raise HelperError('Podman is unavailable')
        self.prefix = [binary, '--log-level=error']
        self.identity = {'selector': selector, 'binary': binary}
        self.endpoint = None
        if selector == 'local':
            if not sys.platform.startswith('linux'): raise HelperError('native engine selection requires Linux')
            self.prefix += ['--remote=false']
        elif isinstance(selector, str) and re.fullmatch(r'connection:[A-Za-z0-9_.-]{1,80}', selector):
            connections = _json(self.call('system', 'connection', 'list', '--format', 'json'))
            matches = [row for row in connections if isinstance(row, dict) and row.get('Name') == selector[11:]]
            if len(matches) != 1: raise HelperError('selected Podman connection is unavailable')
            record = matches[0]
            uri, identity = record.get('URI'), record.get('Identity', '')
            if not isinstance(uri, str) or any(ord(c) <= 32 for c in uri): raise HelperError('invalid engine endpoint')
            parsed = urlsplit(uri)
            # Podman's SSH implementation can reinterpret ssh_config and accept
            # unknown server keys. No SSH authority is inferred from --url.
            if (parsed.scheme != 'unix' or parsed.netloc or parsed.query or parsed.fragment
                    or not parsed.path.startswith('/') or '%' in uri or identity):
                raise HelperError('owner engine requires an explicit protected endpoint')
            self.endpoint = _endpoint(parsed.path)
            self.prefix += ['--url=' + uri]
            self.identity.update(uri=uri, endpoint=self.endpoint)
        else: raise HelperError('choose an explicit native engine or named connection')

    def call(self, *args, data=b'', limit=1048576, timeout=10):
        if self.endpoint is not None and _endpoint(self.endpoint['path']) != self.endpoint:
            raise HelperError('selected engine endpoint changed')
        return self.runner([*self.prefix, *args], data, limit=limit, timeout=timeout)


class OwnerHelper:
    def __init__(self, runtime, *, selector=None, runner=run_bounded):
        self.runtime, self.selector, self.runner = runtime, selector, runner
        self.state = _directory(runtime.args.state_dir)
        if self.state != runtime.state: raise HelperError('owner state identity differs')
        if _json(_private_read(self.state / 'runtime-owner.json')) != {
                'schema': 'cortex.runtime-owner.v1', 'project': runtime.args.project}:
            raise HelperError('owner state belongs to another runtime')
        if os.getuid() == 0: raise HelperError('owner helper requires a normal rootless account')
        self.binding_path = self.state / 'owner-binding.json'

    def _target(self, engine, volume):
        project = self.runtime.args.project
        if volume != project + '_cortex-owner-control': raise HelperError('control volume name is not owned')
        info = _json(engine.call('info', '--format', 'json'))
        host, store = info['host'], info['store']
        if host['security']['rootless'] is not True or host['os'] != 'linux':
            raise HelperError('owner engine is not rootless Linux')
        platform = host['os'] + '/' + host['arch']
        if platform != self.runtime.installation['platform']: raise HelperError('owner backend platform differs')
        maps = host['idMappings']
        for kind in ('uidmap', 'gidmap'):
            _keep_id_map(maps[kind])
        fingerprint = {'os': host['os'], 'arch': host['arch'], 'hostname': host['hostname'],
            'idMappings': maps, 'store': {key: store[key] for key in
                ('graphRoot', 'runRoot', 'graphDriverName', 'volumePath')}}
        if (not isinstance(fingerprint['hostname'], str) or not fingerprint['hostname']
                or any(not isinstance(v, str) or not v for v in fingerprint['store'].values())):
            raise HelperError('owner engine store identity unavailable')
        record = _one(engine.call('volume', 'inspect', volume))
        labels = record.get('Labels') or {}
        if (record.get('Name') != volume or record.get('Driver') != 'local' or record.get('Scope') != 'local'
                or labels.get('com.docker.compose.project') != project
                or labels.get('com.docker.compose.volume') != 'cortex-owner-control'
                or labels.get('org.opencontainers.image.source') != images.IMAGE_SOURCE
                or not isinstance(record.get('CreatedAt'), str) or not record['CreatedAt']):
            raise HelperError('control volume identity is unavailable')
        mountpoint = record.get('Mountpoint')
        if (not isinstance(mountpoint, str) or not mountpoint.startswith('/')
                or any(c in mountpoint for c in ',:\n\r\x00') or '..' in Path(mountpoint).parts):
            raise HelperError('invalid server-side control mountpoint')
        volume_identity = {key: record[key] for key in ('Name', 'Driver', 'Scope', 'Mountpoint', 'CreatedAt', 'Labels')}
        lock = self.runtime.image_inventory['platforms'][platform]['api']
        image = _one(engine.call('image', 'inspect', images.image_ref(lock)))
        images.verify_image(image, lock, platform, self.runtime.identity)
        ids = engine.call('ps', '-a', '--no-trunc', '--filter', 'label=com.docker.compose.project=' + project,
            '--filter', 'label=com.docker.compose.service=cortex-api', '--format', '{{.ID}}').decode().split()
        if len(ids) != 1 or not CID.fullmatch(ids[0]): raise HelperError('one exact API container is required')
        api = _one(engine.call('inspect', ids[0]))
        labels = api.get('Config', {}).get('Labels', {})
        if (api.get('Id') != ids[0] or api.get('State', {}).get('Running') is not True
                or labels.get('com.docker.compose.project') != project
                or labels.get('com.docker.compose.service') != 'cortex-api'
                or any(labels.get('org.opencontainers.image.' + key) != value for key, value in
                    [('source', images.IMAGE_SOURCE), ('version', self.runtime.identity['version']),
                     ('revision', self.runtime.identity['source_revision'])])):
            raise HelperError('owner API source identity differs')
        running_image = _one(engine.call('image', 'inspect', api['Image']))
        images.verify_image(running_image, lock, platform, self.runtime.identity)
        mounts = [m for m in api.get('Mounts', []) if m.get('Destination') == '/run/cortex-owner']
        if (len(mounts) != 1 or mounts[0].get('Type') != 'volume' or mounts[0].get('Name') != volume
                or mounts[0].get('Source') != mountpoint or mounts[0].get('RW') is not True):
            raise HelperError('API does not own the exact control volume')
        return {'engine': engine.identity, 'fingerprint': fingerprint, 'volume': volume_identity,
                'image': image, 'reference': images.image_ref(lock)}

    def _owned_call(self, engine, cid, nonce):
        record = _one(engine.call('inspect', cid, timeout=5))
        if (record.get('Id') != cid or record.get('Name', '').lstrip('/') != 'cortex-owner-' + nonce
                or record.get('Config', {}).get('Labels', {}).get('io.kaidera.cortex.owner-call') != nonce
                or record.get('Config', {}).get('Labels', {}).get('org.opencontainers.image.source') != images.IMAGE_SOURCE):
            raise HelperError('temporary helper ownership differs; no cleanup guessed')
        return record

    def _invoke(self, engine, target, request):
        nonce, cid = uuid4().hex, None
        try:
            command = ['create', '--name=cortex-owner-' + nonce,
                '--label=io.kaidera.cortex.owner-call=' + nonce,
                '--label=org.opencontainers.image.source=' + images.IMAGE_SOURCE,
                '--pull=never', '--network=none', '--log-driver=none', '--user=10001:10001',
                '--userns=keep-id:uid=10001,gid=10001', '--read-only', '--read-only-tmpfs=false',
                '--cap-drop=ALL', '--security-opt=no-new-privileges', '--unsetenv-all',
                '--http-proxy=false', '--image-volume=ignore', '--no-healthcheck', '--systemd=false',
                '--restart=no', '--timeout=30', '--stop-timeout=2', '--interactive',
                '--entrypoint=/usr/local/bin/python3',
                '--mount=type=bind,src=' + target['volume']['Mountpoint'] + ',dst=/run/cortex-owner,ro',
                target['reference'], '-B', '-u', '/app/service_auth_owner_client.py']
            # Only a complete validated returned CID is cleanup authority. If
            # create's response is lost, leave the nonce in a non-secret recovery
            # marker; never rm a guessed name or broad selector.
            marker = self.state / ('owner-call-' + nonce + '.json')
            with _exclusive_output(marker) as fd:
                marker_info = os.fstat(fd)
                _save(fd, images.encode({'schema': 'cortex.owner-call.v1', 'nonce': nonce,
                                         'engine': engine.identity, 'phase': 'creating'}))
            raw = engine.call(*command, limit=128).decode().strip()
            if not CID.fullmatch(raw): raise HelperError('helper create identity unavailable')
            cid = raw
            record = self._owned_call(engine, cid, nonce)
            config, host = record.get('Config', {}), record.get('HostConfig', {})
            mounts = record.get('Mounts', [])
            if (record.get('Image') != target['image'].get('Id') or record.get('State', {}).get('Status') != 'created'
                    or config.get('User') != '10001:10001' or config.get('Tty') is not False
                    or config.get('Env') not in ([], ['container=podman'])
                    or config.get('Entrypoint') != ['/usr/local/bin/python3']
                    or config.get('Cmd') != ['-B', '-u', '/app/service_auth_owner_client.py']
                    or host.get('NetworkMode') != 'none' or host.get('ReadonlyRootfs') is not True
                    or host.get('Privileged') is not False or host.get('LogConfig', {}).get('Type') != 'none'
                    # CapDrop describes configured defaults minus bounding caps.
                    # Only explicit actual sets can establish zero capabilities.
                    or any(key not in record or not (record[key] is None or record[key] == [])
                           for key in ('EffectiveCaps', 'BoundingCaps'))
                    or host.get('CapAdd') != []
                    or host.get('SecurityOpt') != ['no-new-privileges']
                    or host.get('UsernsMode') != 'private'
                    or host.get('Annotations', {}).get('io.podman.annotations.userns') != 'keep-id:uid=10001,gid=10001'
                    or any(host.get('IDMappings', {}).get(observed) != _keep_id_map(target['fingerprint']['idMappings'][kind])
                           for observed, kind in (('UidMap', 'uidmap'), ('GidMap', 'gidmap')))
                    or len(mounts) != 1 or mounts[0].get('Type') != 'bind'
                    or mounts[0].get('Source') != target['volume']['Mountpoint']
                    or mounts[0].get('Destination') != '/run/cortex-owner' or mounts[0].get('RW') is not False):
                raise HelperError('effective helper isolation differs before input')
            # Pair/request prechecks are not a lease on engine metadata. Repeat
            # the exact target check after create/inspect and before each start.
            if self._target(engine, target['volume']['Name']) != target:
                raise HelperError('owner target changed before input')
            raw = engine.call('start', '--attach', '--interactive', cid,
                              data=images.encode(request), limit=MAX_OUTPUT, timeout=35)
            result = _json(raw)
            if (not isinstance(result, dict) or set(result) != {'protocol', 'instance_id', 'result'}
                    or result['protocol'] != PROTOCOL or not isinstance(result['result'], dict)):
                raise HelperError('invalid owner response')
            instance = _uuid(result['instance_id'])
            if (request['instance_id'] is not None and instance != request['instance_id']
                    or request['operation'] == 'identity' and result['result'] != {'instance_id': instance}):
                raise HelperError('owner response instance differs')
            return result
        except BaseException:
            raise HelperError('owner helper unavailable; outcome unknown, do not retry automatically') from None
        finally:
            if cid is not None:
                # Inspection/cleanup has no secret input or output. Failure keeps
                # the call marker for deliberate recovery and never retries auth.
                try:
                    self._owned_call(engine, cid, nonce)
                    engine.call('rm', '--force', cid, timeout=5)
                    current = marker.lstat()
                    if (current.st_dev, current.st_ino) != (marker_info.st_dev, marker_info.st_ino):
                        raise HelperError('owner recovery marker was replaced')
                    marker.unlink()
                except BaseException:
                    raise HelperError('owner cleanup unverified; outcome unknown, do not retry automatically') from None

    def _base_binding(self, target):
        return {'schema': 'cortex.owner-binding.v1', 'account': {'uid': os.getuid(), 'gid': os.getgid()},
                'engine': target['engine'], 'fingerprint': target['fingerprint'], 'volume': target['volume']}

    def pair(self, volume):
        try:
            with _exclusive_output(self.binding_path) as fd:
                engine = Engine(self.selector, self.runner)
                target = self._target(engine, volume)
                result = self._invoke(engine, target, {'protocol': PROTOCOL, 'operation': 'identity', 'instance_id': None})
                binding = {**self._base_binding(target), 'instance_id': _uuid(result['instance_id'])}
                _save(fd, images.encode(binding))
                return {'paired': True, 'instance_id': binding['instance_id']}
        except BaseException:
            raise HelperError('owner pairing unavailable; existing binding is never replaced automatically') from None

    def request(self, request_file, response_file):
        try:
            value = _json(_private_read(request_file))
            binding = _json(_private_read(self.binding_path))
            instance = _uuid(binding['instance_id'])
            if (not isinstance(value, dict) or value.get('protocol') != PROTOCOL
                    or value.get('operation') not in {'identity', 'setup-grant', 'consume-setup'}
                    or value.get('instance_id') != instance
                    or set(binding) != {'schema', 'account', 'engine', 'fingerprint', 'volume', 'instance_id'}):
                raise HelperError('owner request is not bound to the paired instance')
            with _exclusive_output(response_file) as fd:
                selector = binding['engine']['selector']
                if self.selector is not None and self.selector != selector:
                    raise HelperError('owner engine selection differs')
                engine = Engine(selector, self.runner)
                target = self._target(engine, binding['volume']['Name'])
                if {key: binding[key] for key in binding if key != 'instance_id'} != self._base_binding(target):
                    raise HelperError('owner engine, account or volume changed; deliberate pairing required')
                self._invoke(engine, target, {'protocol': PROTOCOL, 'operation': 'identity', 'instance_id': instance})
                result = self._invoke(engine, target, value)
                _save(fd, images.encode(result))
                return {'owner_request': 'complete', 'response_file': str(response_file)}
        except BaseException:
            raise HelperError('owner request unavailable; outcome unknown, do not retry automatically') from None
