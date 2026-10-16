"""Linux source-contract delta and unbound private B1 observation collector.

The collector algorithm runs only when its fixed protected bindings exist. They
remain absent in this checkout, so the production supplier refuses before any
syscall. Its private syscall table is replaceable only by module-local tests;
source fixtures cannot turn caller data into native evidence. A future protected
binding must provide the complete original graph, a connected Unix socket,
SO_PEERCRED plus a held SO_PEERPIDFD, namespace identities/maps, held objects and
the whole executed closure independently of C.

The conservative profile uses pathname Unix stream sockets, ID-preserving maps
in V's admitted namespace, and no default ACLs. Source positives qualify DATA
only. Actual roots, accounts, namespaces, capabilities, OS and image/R01 proofs
still need separate native qualification. No syscall, repair, launch or effect.
"""
import errno
from dataclasses import replace
import hashlib
import json
import os
import select
import socket
import re
import stat
import struct
from pathlib import PurePosixPath

from .. import owner
from ...release.codec import canonical_bytes

__all__ = []
_MAX = 2**63 - 1
_UID_MAX = 2**32 - 2
_NS = frozenset(('user', 'pid', 'mnt', 'net', 'ipc', 'uts', 'cgroup', 'time'))
_CAPS = frozenset(('inheritable', 'permitted', 'effective', 'ambient', 'bounding'))
_PROBES = frozenset(('ptrace', 'proc_mem', 'process_vm_writev', 'setns', 'setuid',
                     'file_capability', 'launch_write', 'backend_bypass'))
_LIFE = frozenset(('pid', 'boot_id', 'start_ticks', 'pidfd_id'))
_PROCESS = _LIFE | {'uids', 'gids', 'groups', 'code_sha256'}
_POLICY = frozenset(('version', 'peer_uid', 'peer_gid', 'peer_groups', 'owner_uid', 'owner_gid',
    'owner_groups', 'peer_process', 'owner_process', 'namespaces', 'maps', 'capabilities',
    'endpoint', 'objects', 'peer_code_sha256'))
_RECEIPT = frozenset(('version', 'host_os', 'owner_epoch', 'session_id', 'peer_lifetime',
    'host_identity_ref', 'transport', 'peer_process', 'owner_process', 'namespaces', 'maps',
    'privileges', 'startup', 'objects'))
_ROLES = frozenset(('endpoint', 'interpreter', 'source', 'import', 'native', 'schema',
                    'policy', 'state', 'journal', 'lock', 'originals', 'launch'))
_CODE = frozenset(('interpreter', 'source', 'import', 'native'))
_LINUX_BINDING = None  # The protected owner originals and connected peer FD are not supplied.
_SO_PEERPIDFD = getattr(socket, 'SO_PEERPIDFD', 77)
_ACL_TAGS = {0x01: ('owner', False), 0x02: ('user', True), 0x04: ('group', False),
             0x08: ('named_group', True), 0x10: ('mask', False), 0x20: ('other', False)}


def _installed_linux_inputs():
    """Fixed zero-argument supplier; unbound in production until trusted inputs exist."""
    binding = _LINUX_BINDING
    if (type(binding) is not dict or set(binding) != {'inputs', 'peer_fd'}
            or type(binding['inputs']) is not owner._Inputs
            or type(binding['peer_fd']) is not int or binding['peer_fd'] < 0):
        raise owner.OwnerRefusal('required_binding_gap')  # BL-G00
    return _collect_linux_inputs(binding['inputs'], binding['peer_fd'])


def _read_fd(fd, limit):
    data = bytearray()
    while len(data) <= limit:
        chunk = os.read(fd, min(65536, limit + 1 - len(data)))
        if not chunk:
            break
        data.extend(chunk)
    if len(data) > limit:
        raise owner.OwnerRefusal('owner_unavailable')
    return bytes(data)


def _read_proc_at(proc_fd, name, limit=262144):
    if name not in {'status', 'stat', 'uid_map', 'gid_map'}:
        raise owner.OwnerRefusal('required_binding_gap')
    fd = os.open(name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=proc_fd)
    try:
        return _read_fd(fd, limit)
    finally:
        os.close(fd)


def _open_proc_dir(pid):
    return os.open(f'/proc/{pid}', os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW)


def _read_boot_id():
    fd = os.open('/proc/sys/kernel/random/boot_id', os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        value = _read_fd(fd, 128).decode('ascii').strip()
    finally:
        os.close(fd)
    if not _text(value):
        raise owner.OwnerRefusal('owner_unavailable')
    return value


def _read_executable_digest(proc_fd):
    # exe is a procfs magic link; dir_fd pins lookup to the held process entry.
    fd = os.open('exe', os.O_RDONLY | os.O_CLOEXEC, dir_fd=proc_fd)
    digest = hashlib.sha256()
    total = 0
    try:
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            total += len(chunk)
            if total > 268435456:
                raise owner.OwnerRefusal('owner_unavailable')
            digest.update(chunk)
    finally:
        os.close(fd)
    return digest.hexdigest()


def _parse_status(proc_fd):
    try:
        rows = {}
        for line in _read_proc_at(proc_fd, 'status').decode('ascii').splitlines():
            key, separator, value = line.partition(':')
            if separator:
                rows[key] = value.strip()
        uids = [int(value) for value in rows['Uid'].split()]
        gids = [int(value) for value in rows['Gid'].split()]
        groups = [int(value) for value in rows.get('Groups', '').split()]
        capabilities = {name: int(rows[key], 16) for name, key in (
            ('inheritable', 'CapInh'), ('permitted', 'CapPrm'), ('effective', 'CapEff'),
            ('bounding', 'CapBnd'), ('ambient', 'CapAmb'))}
        no_new_privs = int(rows['NoNewPrivs'])
    except (KeyError, UnicodeError, ValueError):
        raise owner.OwnerRefusal('owner_unavailable') from None
    if (len(uids) != 4 or len(gids) != 4 or not _ids(uids, 4) or not _ids(gids, 4)
            or not _ids(groups) or not _cap_shape(capabilities) or no_new_privs not in (0, 1)):
        raise owner.OwnerRefusal('owner_unavailable')
    return uids, gids, groups, capabilities, bool(no_new_privs)


def _start_ticks(proc_fd):
    try:
        raw = _read_proc_at(proc_fd, 'stat').decode('ascii')
        close = raw.rfind(')')
        if close < 0:
            raise ValueError
        fields = raw[close + 1:].split()
        value = int(fields[19])
    except (IndexError, UnicodeError, ValueError):
        raise owner.OwnerRefusal('owner_unavailable') from None
    if value < 1:
        raise owner.OwnerRefusal('owner_unavailable')
    return value


def _peer_credentials(fd):
    sock = socket.fromfd(fd, socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        if sock.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) != socket.SOCK_STREAM:
            raise owner.OwnerRefusal('peer_denied')
        raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('=3i'))
    finally:
        sock.close()
    try:
        pid, uid, gid = struct.unpack('=iII', raw)
    except struct.error:
        raise owner.OwnerRefusal('owner_unavailable') from None
    if pid < 1 or uid < 0 or gid < 0:
        raise owner.OwnerRefusal('peer_denied')
    return pid, uid, gid


def _peer_transport(fd):
    sock = socket.fromfd(fd, socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        domain = sock.getsockopt(socket.SOL_SOCKET, socket.SO_DOMAIN)
        socket_type = sock.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE)
        endpoint = sock.getsockname()
        sock.getpeername()  # Refuse an unconnected listener or an unbound socketpair.
    finally:
        sock.close()
    if (domain != socket.AF_UNIX or socket_type != socket.SOCK_STREAM
            or type(endpoint) is not str or not endpoint.startswith('/')
            or '\0' in endpoint or str(PurePosixPath(endpoint)) != endpoint):
        raise owner.OwnerRefusal('peer_denied')
    return dict(family='AF_UNIX', type='SOCK_STREAM', kind='pathname',
                source='SO_PEERCRED', endpoint=endpoint)


def _peer_pidfd(fd):
    sock = socket.fromfd(fd, socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        raw = sock.getsockopt(socket.SOL_SOCKET, _SO_PEERPIDFD, struct.calcsize('=i'))
    finally:
        sock.close()
    try:
        pidfd, = struct.unpack('=i', raw)
    except struct.error:
        raise owner.OwnerRefusal('required_binding_gap') from None
    if pidfd < 0:
        raise owner.OwnerRefusal('required_binding_gap')
    return pidfd


def _pidfd_open(pid):
    return os.pidfd_open(pid, 0)


def _pidfd_pid(pidfd):
    fd = os.open(f'/proc/self/fdinfo/{pidfd}', os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        raw = _read_fd(fd, 4096).decode('ascii')
    finally:
        os.close(fd)
    for line in raw.splitlines():
        key, separator, value = line.partition(':')
        if separator and key == 'Pid':
            try:
                pid = int(value.strip())
            except ValueError:
                break
            if pid > 0:
                return pid
    raise owner.OwnerRefusal('peer_denied')


def _pidfd_alive(pidfd):
    poller = select.poll()
    poller.register(pidfd, select.POLLIN | select.POLLHUP | select.POLLERR)
    return not bool(poller.poll(0))


def _pidfd_close(pidfd):
    os.close(pidfd)


def _process_snapshot(pid, pidfd):
    proc_fd = _open_proc_dir(pid)
    try:
        uids, gids, groups, capabilities, no_new_privs = _parse_status(proc_fd)
        boot_id = _read_boot_id()
        start_ticks = _start_ticks(proc_fd)
        life = dict(pid=pid, boot_id=boot_id, start_ticks=start_ticks,
                    pidfd_id='pidfd:' + hashlib.sha256(
                        f'{pid}\0{boot_id}\0{start_ticks}'.encode('ascii')).hexdigest()[:32])
        process = dict(life, uids=uids, gids=gids, groups=groups,
                       code_sha256=_read_executable_digest(proc_fd))
        return {'process': process, 'capabilities': capabilities, 'no_new_privs': no_new_privs}
    finally:
        os.close(proc_fd)


def _namespace_snapshot(pid, pidfd):
    observed = {}
    proc_fd = _open_proc_dir(pid)
    try:
        ns_fd = os.open('ns', os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=proc_fd)
        try:
            for name in sorted(_NS):
                value = os.stat(name, dir_fd=ns_fd)
                observed[name] = [value.st_dev, value.st_ino]
        finally:
            os.close(ns_fd)
    except OSError:
        raise owner.OwnerRefusal('required_binding_gap') from None
    finally:
        os.close(proc_fd)
    return observed


def _parse_id_map(proc_fd, name):
    try:
        rows = []
        for line in _read_proc_at(proc_fd, name).decode('ascii').splitlines():
            values = [int(value) for value in line.split()]
            if len(values) != 3:
                raise ValueError
            rows.append(values)
    except (UnicodeError, ValueError):
        raise owner.OwnerRefusal('owner_unavailable') from None
    if type(rows) is not list or not 0 < len(rows) <= 340 or not all(
            all(_number(value, 2**32-1) for value in row) and row[2] > 0 for row in rows):
        raise owner.OwnerRefusal('owner_unavailable')
    return rows


def _maps_snapshot(pid, pidfd):
    proc_fd = _open_proc_dir(pid)
    try:
        return {'uid': _parse_id_map(proc_fd, 'uid_map'), 'gid': _parse_id_map(proc_fd, 'gid_map')}
    finally:
        os.close(proc_fd)


def _fd_mount_id(fd):
    info_fd = os.open(f'/proc/self/fdinfo/{fd}', os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        raw = _read_fd(info_fd, 4096).decode('ascii')
    finally:
        os.close(info_fd)
    for line in raw.splitlines():
        key, separator, value = line.partition(':')
        if separator and key == 'mnt_id':
            try:
                mount_id = int(value.strip())
            except ValueError:
                break
            if mount_id > 0:
                return mount_id
    raise owner.OwnerRefusal('owner_unavailable')


def _acl_entries(data):
    if len(data) < 4 or (len(data) - 4) % 8:
        raise owner.OwnerRefusal('custody_changed')
    version, = struct.unpack_from('<I', data, 0)
    if version != 2:
        raise owner.OwnerRefusal('custody_changed')
    result = []
    for offset in range(4, len(data), 8):
        tag, perm, ident = struct.unpack_from('<HHI', data, offset)
        if tag not in _ACL_TAGS:
            raise owner.OwnerRefusal('custody_changed')
        name, named = _ACL_TAGS[tag]
        result.append({'tag': name, 'id': ident if named else None, 'perm': perm})
    return result


def _xattr_acl(fd, name, *, fallback=False):
    absent = (errno.ENODATA, getattr(errno, 'ENOATTR', errno.ENODATA))
    try:
        raw = os.getxattr(fd, name)
    except OSError as error:
        if error.errno in absent:
            return []
        if not fallback or error.errno != errno.EBADF:
            raise
        try:
            raw = os.getxattr(f'/proc/self/fd/{fd}', name)
        except OSError as retry_error:
            if retry_error.errno in absent:
                return []
            raise
    return _acl_entries(raw)


def _object_snapshot(path):
    if not _safe_path(path):
        raise owner.OwnerRefusal('custody_changed')
    parts = PurePosixPath(path).parts[1:]
    parent_fd = os.open('/', os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY)
    fd = None
    try:
        if parts:
            for component in parts[:-1]:
                child_fd = os.open(component, os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY
                    | os.O_NOFOLLOW, dir_fd=parent_fd)
                os.close(parent_fd)
                parent_fd = child_fd
            name = parts[-1]
            before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if stat.S_ISLNK(before.st_mode):
                raise owner.OwnerRefusal('custody_changed')
            flags = os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
            if stat.S_ISSOCK(before.st_mode):
                flags |= getattr(os, 'O_PATH', 0)
            else:
                flags |= os.O_RDONLY
                if stat.S_ISDIR(before.st_mode):
                    flags |= os.O_DIRECTORY
            fd = os.open(name, flags, dir_fd=parent_fd)
        else:
            before = os.fstat(parent_fd)
            fd = parent_fd
        held = os.fstat(fd)
        after = os.stat(name, dir_fd=parent_fd, follow_symlinks=False) if parts else os.fstat(parent_fd)
        identity_pair = (before.st_dev, before.st_ino)
        if identity_pair != (held.st_dev, held.st_ino) or identity_pair != (after.st_dev, after.st_ino):
            raise owner.OwnerRefusal('custody_changed')
        if stat.S_ISDIR(held.st_mode):
            kind = 'directory'
        elif stat.S_ISREG(held.st_mode):
            kind = 'file'
        elif stat.S_ISSOCK(held.st_mode):
            kind = 'socket'
        else:
            raise owner.OwnerRefusal('custody_changed')
        digest = None
        if kind == 'file':
            hasher = hashlib.sha256()
            total = 0
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > 268435456:
                    raise owner.OwnerRefusal('owner_unavailable')
                hasher.update(chunk)
            digest = hasher.hexdigest()
        acl = _xattr_acl(fd, 'system.posix_acl_access', fallback=(kind == 'socket'))
        default_acl = (_xattr_acl(fd, 'system.posix_acl_default')
                       if kind == 'directory' else [])
        identity = [held.st_dev, held.st_ino, _fd_mount_id(fd)]
        return dict(path=path, kind=kind, identity=identity, held_identity=list(identity),
                    sha256=digest, uid=held.st_uid, gid=held.st_gid,
                    mode=stat.S_IMODE(held.st_mode), acl=acl, default_acl=default_acl,
                    nlink=held.st_nlink)
    except owner.OwnerRefusal:
        raise
    except OSError:
        raise owner.OwnerRefusal('custody_changed') from None
    finally:
        if fd is not None and fd != parent_fd:
            os.close(fd)
        os.close(parent_fd)


def _startup_snapshot(pid, pidfd):
    # The accepted source pin has no complete owner startup/import originals.
    raise owner.OwnerRefusal('required_binding_gap')


def _backend_denials(pid, pidfd):
    # Do not probe by attempting ptrace, namespace entry, privilege change or backend access.
    # No accepted non-invasive Linux denial source is bound in this checkout.
    raise owner.OwnerRefusal('required_binding_gap')


_LINUX_SYSCALLS = {
    'peer_transport': _peer_transport,
    'peer_credentials': _peer_credentials,
    'peer_pidfd': _peer_pidfd,
    'pidfd_open': _pidfd_open,
    'pidfd_pid': _pidfd_pid,
    'pidfd_alive': _pidfd_alive,
    'pidfd_close': _pidfd_close,
    'self_pid': os.getpid,
    'process_snapshot': _process_snapshot,
    'namespace_snapshot': _namespace_snapshot,
    'maps_snapshot': _maps_snapshot,
    'object_snapshot': _object_snapshot,
    'startup_snapshot': _startup_snapshot,
    'backend_denials': _backend_denials,
}


def _encode_original(value):
    return canonical_bytes(value)


def _original_ref(path, raw):
    return {'path': path, 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}


def _collect_linux_inputs(inputs, peer_fd):
    try:
        context = owner._decode(inputs.context_raw, 'owner_unavailable')
        peer = owner._original(inputs, context['peer_ref'])
        policy = owner._original(inputs, context['policy_ref']).get('linux')
        receipt = owner._original(inputs, peer.get('linux_ref'))
        collected = _collect_linux_receipt(policy, receipt, peer_fd)
        originals = dict(inputs.originals)
        receipt_raw = _encode_original(collected)
        receipt_ref = _original_ref(peer['linux_ref']['path'], receipt_raw)
        originals[receipt_ref['path']] = receipt_raw
        peer['linux_ref'] = receipt_ref
        peer_raw = _encode_original(peer)
        peer_ref = _original_ref(context['peer_ref']['path'], peer_raw)
        originals[peer_ref['path']] = peer_raw
        context['peer_ref'] = peer_ref
        return replace(inputs, context_raw=_encode_original(context), originals=originals)
    except owner.OwnerRefusal:
        raise
    except (KeyError, TypeError, ValueError, UnicodeError):
        raise owner.OwnerRefusal('owner_unavailable') from None


def _collect_linux_receipt(policy, template, peer_fd):
    """Collect one read-only Linux receipt through the fixed private syscall seam."""
    calls = _LINUX_SYSCALLS
    peer_pidfd = owner_pidfd = None
    failure = None
    try:
        owner._require(type(policy) is dict and type(template) is dict
            and _shape(policy, template), 'owner_unavailable')
        owner._require(_safe_path(policy['endpoint']) and all(
            _safe_path(obj['path']) for obj in policy['objects']), 'custody_changed')
        transport = calls['peer_transport'](peer_fd)
        expected_transport = {key: template['transport'][key] for key in
            ('family', 'type', 'kind', 'source', 'endpoint')}
        owner._require(type(transport) is dict and transport == expected_transport, 'peer_denied')
        credentials = calls['peer_credentials'](peer_fd)
        owner._require(type(credentials) in (tuple, list) and len(credentials) == 3
            and all(type(value) is int for value in credentials), 'peer_denied')
        peer_pid, peer_uid, peer_gid = credentials
        peer_expected = policy['peer_process']
        owner_expected = policy['owner_process']
        owner._require((peer_pid, peer_uid, peer_gid) ==
            (peer_expected['pid'], policy['peer_uid'], policy['peer_gid']), 'peer_denied')
        owner_pid = calls['self_pid']()
        owner._require(owner_pid == owner_expected['pid'], 'peer_denied')
        # SO_PEERPIDFD binds the held descriptor to the connection-time peer; do not
        # fall back to pidfd_open(peer_pid), which could follow a reused PID.
        peer_pidfd = calls['peer_pidfd'](peer_fd)
        owner_pidfd = calls['pidfd_open'](owner_pid)
        owner._require(type(peer_pidfd) is int and peer_pidfd >= 0
            and type(owner_pidfd) is int and owner_pidfd >= 0 and peer_pidfd != owner_pidfd,
            'required_binding_gap')
        owner._require(calls['pidfd_alive'](peer_pidfd)
            and calls['pidfd_alive'](owner_pidfd), 'peer_denied')
        owner._require(calls['pidfd_pid'](peer_pidfd) == peer_pid
            and calls['pidfd_pid'](owner_pidfd) == owner_pid, 'peer_denied')
        peer_first = calls['process_snapshot'](peer_pid, peer_pidfd)
        owner_first = calls['process_snapshot'](owner_pid, owner_pidfd)
        peer_process = _process_observation(peer_first)
        owner_process = _process_observation(owner_first)
        owner._require(_matches_privilege_policy(peer_first, policy['capabilities']), 'peer_denied')
        owner._require(_matches_privilege_policy(owner_first, policy['capabilities']), 'peer_denied')
        owner._require(all({key: process[key] for key in _LIFE} == expected for process, expected in (
            (peer_process, peer_expected), (owner_process, owner_expected))), 'peer_denied')
        owner._require(peer_first['process']['uids'][0] == peer_uid
            and peer_first['process']['gids'][0] == peer_gid, 'peer_denied')

        namespaces = {
            'peer': calls['namespace_snapshot'](peer_pid, peer_pidfd),
            'owner': calls['namespace_snapshot'](owner_pid, owner_pidfd),
        }
        maps = {
            'peer': calls['maps_snapshot'](peer_pid, peer_pidfd),
            'owner': calls['maps_snapshot'](owner_pid, owner_pidfd),
        }
        owner._require(_namespaces(namespaces) and namespaces == policy['namespaces']
            and _map_shape(maps) and maps == policy['maps'], 'peer_denied')

        objects = []
        for expected in policy['objects']:
            observed = calls['object_snapshot'](expected['path'])
            owner._require(_object_shape(observed, observed=True)
                and observed['path'] == expected['path']
                and observed['kind'] == ('socket' if expected['role'] == 'endpoint'
                    else 'directory' if expected['role'] in ('root', 'ancestor', 'originals') else 'file')
                and observed['identity'] == expected['identity']
                and observed['held_identity'] == observed['identity'], 'custody_changed')
            if expected['sha256'] is not None:
                owner._require(observed['sha256'] == expected['sha256'], 'custody_changed')
            objects.append(observed)

        startup = calls['startup_snapshot'](owner_pid, owner_pidfd)
        owner._require(type(startup) is dict and startup == template['startup'], 'custody_changed')
        paths = calls['backend_denials'](owner_pid, owner_pidfd)
        owner._require(type(paths) is dict and set(paths) == _PROBES
            and all(value == 'denied' for value in paths.values()), 'peer_denied')
        capabilities = owner_first['capabilities']

        owner._require(calls['pidfd_alive'](peer_pidfd)
            and calls['pidfd_alive'](owner_pidfd), 'peer_denied')
        peer_last = calls['process_snapshot'](peer_pid, peer_pidfd)
        owner_last = calls['process_snapshot'](owner_pid, owner_pidfd)
        owner._require(peer_last == peer_first and owner_last == owner_first, 'peer_denied')

        result = json.loads(json.dumps(template))
        result['transport']['credentials'] = {'pid': peer_pid, 'uid': peer_uid, 'gid': peer_gid}
        result['peer_process'] = peer_first['process']
        result['owner_process'] = owner_first['process']
        result['namespaces'] = namespaces
        result['maps'] = maps
        result['privileges'] = {
            'capabilities': capabilities,
            'no_new_privs': owner_first['no_new_privs'],
            'peer_lifetime': {key: peer_first['process'][key] for key in _LIFE},
            'owner_lifetime': {key: owner_first['process'][key] for key in _LIFE},
            'paths': paths,
        }
        result['startup'] = startup
        result['objects'] = objects
        owner._require(_shape(policy, result), 'owner_unavailable')
        return result
    except owner.OwnerRefusal as error:
        failure = error
        raise
    except (OSError, KeyError, TypeError, ValueError, UnicodeError, struct.error):
        failure = owner.OwnerRefusal('required_binding_gap')
        raise failure from None
    finally:
        closed = set()
        close_error = None
        for fd in (owner_pidfd, peer_pidfd):
            if type(fd) is int and fd >= 0 and fd not in closed:
                closed.add(fd)
                try:
                    calls['pidfd_close'](fd)
                except OSError as error:
                    if close_error is None:
                        close_error = error
        if failure is None and close_error is not None:
            raise owner.OwnerRefusal('required_binding_gap') from close_error


def _process_observation(value):
    if (type(value) is not dict or set(value) != {'process', 'capabilities', 'no_new_privs'}
            or type(value['process']) is not dict or not _process(value['process'])
            or not _cap_shape(value['capabilities']) or type(value['no_new_privs']) is not bool):
        raise owner.OwnerRefusal('peer_denied')
    return value['process']


def _matches_privilege_policy(value, expected):
    return (type(value) is dict and _cap_shape(value.get('capabilities'))
            and value['capabilities'] == expected and value.get('no_new_privs') is True)


def _closed(value, fields):
    return type(value) is dict and set(value) == fields


def _number(value, maximum=_MAX, minimum=0):
    return type(value) is int and minimum <= value <= maximum


def _text(value, limit=128):
    return type(value) is str and 0 < len(value.encode('utf-8')) <= limit


def _safe_path(value):
    return (type(value) is str and value.startswith('/') and '\\' not in value and '\0' not in value
            and str(PurePosixPath(value)) == value and '//' not in value
            and '..' not in PurePosixPath(value).parts)


def _digest(value):
    return type(value) is str and re.fullmatch('[0-9a-f]{64}', value) is not None


def _ids(value, size=None):
    return (type(value) is list and 0 < len(value) <= 256
            and (size is None or len(value) == size)
            and all(_number(x, _UID_MAX) for x in value))


def _lifetime(value):
    return (_closed(value, _LIFE) and _number(value['pid'], 2**31-1, 1)
            and _number(value['start_ticks'], minimum=1)
            and _text(value['boot_id']) and _text(value['pidfd_id']))


def _process(value):
    return (_closed(value, _PROCESS) and _lifetime({k:value[k] for k in _LIFE})
            and _ids(value['uids'], 4) and _ids(value['gids'], 4)
            and _ids(value['groups']) and len(set(value['groups'])) == len(value['groups'])
            and _digest(value['code_sha256']))


def _namespaces(value):
    return (_closed(value, {'peer', 'owner'}) and all(_closed(v, _NS)
            and all(type(identity) is list and len(identity) == 2
                    and all(_number(n, minimum=1) for n in identity) for identity in v.values())
            for v in value.values()))


def _map_shape(value):
    return (_closed(value, {'peer', 'owner'}) and all(_closed(v, {'uid', 'gid'})
            and all(type(rows) is list and 0 < len(rows) <= 340
                    and all(type(row) is list and len(row) == 3
                            and all(_number(n, 2**32-1) for n in row) and row[2] > 0
                            for row in rows) for rows in v.values()) for v in value.values()))


def _cap_shape(value):
    return _closed(value, _CAPS) and all(_number(x) for x in value.values())


def _object_shape(value, *, observed):
    fields = {'path', 'kind', 'identity', 'held_identity', 'sha256', 'uid', 'gid', 'mode',
              'acl', 'default_acl', 'nlink'} if observed else {'path', 'kind', 'role', 'identity', 'sha256'}
    if not _closed(value, fields):
        return False
    if not (_text(value['path'], 4096) and _text(value['kind'])
            and type(value['identity']) is list and len(value['identity']) == 3
            and all(_number(n, minimum=1) for n in value['identity'])
            and (value['sha256'] is None or _digest(value['sha256']))):
        return False
    if not observed:
        return _text(value['role'])
    return (type(value['held_identity']) is list and len(value['held_identity']) == 3
            and all(_number(n, minimum=1) for n in value['held_identity'])
            and _number(value['uid'], _UID_MAX) and _number(value['gid'], _UID_MAX)
            and _number(value['mode'], 0o7777) and _number(value['nlink'], minimum=1)
            and type(value['acl']) is list and len(value['acl']) <= 259
            and type(value['default_acl']) is list and len(value['default_acl']) <= 259)


def _shape(policy, receipt):
    """Closed bounded source records; all failures use the existing C0 detail."""
    try:
        if not (_closed(policy, _POLICY) and _closed(receipt, _RECEIPT)):
            return False
        if not (_number(policy['version'], 1, 1) and _number(receipt['version'], 1, 1)
                and all(_number(policy[k], _UID_MAX) for k in ('peer_uid','peer_gid','owner_uid','owner_gid'))
                and all(_ids(policy[k]) and len(set(policy[k])) == len(policy[k])
                        for k in ('peer_groups','owner_groups'))
                and _lifetime(policy['peer_process']) and _lifetime(policy['owner_process'])
                and _process(receipt['peer_process']) and _process(receipt['owner_process'])
                and all(_namespaces(v['namespaces']) and _map_shape(v['maps']) for v in (policy,receipt))
                and _cap_shape(policy['capabilities']) and _digest(policy['peer_code_sha256'])
                and _text(policy['endpoint'], 107)
                and all(_text(receipt[k]) for k in ('host_os','owner_epoch','session_id','peer_lifetime'))
                and type(receipt['host_identity_ref']) is dict):
            return False
        transport = receipt['transport']; privileges = receipt['privileges']; startup = receipt['startup']
        return (_closed(transport, {'family','type','kind','source','endpoint','credentials'})
                and all(_text(transport[k]) for k in ('family','type','kind','source'))
                and _text(transport['endpoint'], 107)
                and _closed(transport['credentials'], {'pid','uid','gid'})
                and _number(transport['credentials']['pid'], 2**31-1, 1)
                and all(_number(transport['credentials'][k], _UID_MAX) for k in ('uid','gid'))
                and _closed(privileges, {'capabilities','no_new_privs','peer_lifetime','owner_lifetime','paths'})
                and _cap_shape(privileges['capabilities']) and type(privileges['no_new_privs']) is bool
                and all(_lifetime(privileges[k]) for k in ('peer_lifetime','owner_lifetime'))
                and _closed(privileges['paths'], _PROBES) and all(_text(v) for v in privileges['paths'].values())
                and _closed(startup, {'executable_path','python','crypto','dependencies','isolated','no_site','environment','pth',
                                     'candidate_imports','loaded_paths','native_paths'})
                and _text(startup['executable_path'],4096)
                and all(_text(startup[k]) for k in ('python','crypto'))
                and all(type(startup[k]) is bool for k in ('isolated','no_site'))
                and all(type(startup[k]) is list
                        and all(_text(v,4096) for v in startup[k]) for k in
                        ('dependencies','environment','pth','candidate_imports','loaded_paths','native_paths'))
                and all(type(v['objects']) is list and 0 < len(v['objects']) for v in (policy,receipt))
                and all(len(v['objects']) <= 280 for v in (policy,receipt))
                and all(_object_shape(v,observed=False) for v in policy['objects'])
                and all(_object_shape(v,observed=True) for v in receipt['objects']))
    except (KeyError, TypeError, ValueError, UnicodeError):
        return False


def _mapped(rows, inside):
    # Both sides must be unambiguous; overlapping or overflowing ranges refuse.
    for i, (start, outside, size) in enumerate(rows):
        if start + size > 2**32 or outside + size > 2**32:
            return None
        for other, outer, length in rows[:i]:
            if max(start,other) < min(start+size,other+length) or max(outside,outer) < min(outside+size,outer+length):
                return None
    found = [outside + inside - start for start,outside,size in rows if start <= inside < start+size]
    return found[0] if len(found) == 1 else None


def _acl_valid(obj):
    entries = obj['acl']
    if not entries:
        return True  # Collector normalizes an absent extended ACL to [].
    seen = set(); base = {}; named = False
    for entry in entries:
        if not _closed(entry, {'tag','id','perm'}) or not _number(entry['perm'],7):
            return False
        tag = entry['tag']; ident = entry['id']
        if type(tag) is not str or tag not in ('owner','group','other','mask','user','named_group'):
            return False
        if tag in ('user','named_group'):
            if not _number(ident,_UID_MAX):
                return False
            named = True
        elif ident is not None:
            return False
        key = (tag,ident)
        if key in seen:
            return False
        seen.add(key)
        if tag not in ('user','named_group'):
            base[tag] = entry['perm']
    return ({'owner','group','other'} <= set(base) and (not named or 'mask' in base)
            and base['owner'] == (obj['mode'] >> 6) & 7
            and base.get('mask',base['group']) == (obj['mode'] >> 3) & 7
            and base['other'] == obj['mode'] & 7)


def _access(obj, uid, groups):
    """POSIX access ACL single-bit write/search queries; matched entries do not fall through."""
    if not obj['acl']:
        shift = 6 if obj['uid'] == uid else 3 if obj['gid'] in groups else 0
        return (obj['mode'] >> shift) & 7
    base = {e['tag']:e['perm'] for e in obj['acl'] if e['tag'] in ('owner','group','other','mask')}
    if obj['uid'] == uid:
        return base['owner']
    mask = base.get('mask',7)
    for entry in obj['acl']:
        if entry['tag'] == 'user' and entry['id'] == uid:
            return entry['perm'] & mask
    matched = obj['gid'] in groups
    perms = base['group'] if matched else 0
    for entry in obj['acl']:
        if entry['tag'] == 'named_group' and entry['id'] in groups:
            matched = True
            perms |= entry['perm']
    return perms & mask if matched else base['other']


def _installed_owner_inputs():
    """The common B1 observer shape: zero arguments -> exact existing _Inputs DATA."""
    inputs = _installed_linux_inputs()
    owner._require(type(inputs) is owner._Inputs and type(inputs.originals) is dict
        and len(inputs.originals) <= 256 and all(type(v) is bytes for v in inputs.originals.values())
        and sum(len(v) for v in inputs.originals.values()) <= 8*1024*1024, 'owner_unavailable')  # BL-G01
    context = owner._decode(inputs.context_raw, 'owner_unavailable')
    owner._require(set(context) == owner._CONTEXT, 'owner_unavailable')  # BL-G02
    peer = owner._original(inputs, context['peer_ref'])
    policy_original = owner._original(inputs, context['policy_ref'])
    receipt = owner._original(inputs, peer.get('linux_ref'))
    policy = policy_original.get('linux')
    owner._require(_shape(policy, receipt), 'owner_unavailable')  # BL-G03
    c = receipt['peer_process']; v = receipt['owner_process']; t = receipt['transport']
    owner._require(receipt['host_os'] == 'linux' and type(context['target']) is dict
        and context['target'].get('host_os') == 'linux' and receipt['owner_epoch'] == context['owner_epoch'] == peer.get('owner_epoch')
        and receipt['session_id'] == context['session_id'] == peer.get('session_id')
        and receipt['peer_lifetime'] == peer.get('lifetime')
        and receipt['host_identity_ref'] == peer.get('host_identity_ref'), 'peer_denied')  # BL-G04
    owner._require(t['family'] == 'AF_UNIX' and t['type'] == 'SOCK_STREAM' and t['kind'] == 'pathname'
        and t['source'] == 'SO_PEERCRED' and t['endpoint'] == policy['endpoint'], 'peer_denied')  # BL-G05
    owner._require(t['credentials'] == {'pid':c['pid'],'uid':c['uids'][0],'gid':c['gids'][0]}, 'peer_denied')  # BL-G06
    owner._require({k:c[k] for k in _LIFE} == policy['peer_process'], 'peer_denied')  # BL-G07
    owner._require(c['uids'] == [policy['peer_uid']]*4 and c['gids'] == [policy['peer_gid']]*4
        and sorted(c['groups']) == sorted(policy['peer_groups']) and policy['peer_gid'] in c['groups']
        and type(peer.get('owner_uid')) is int and peer['owner_uid'] == policy['peer_uid'], 'peer_denied')  # BL-G08
    owner._require(policy['owner_uid'] != 0 and policy['peer_uid'] != 0
        and policy['owner_uid'] != policy['peer_uid'] and policy['owner_gid'] not in c['groups']
        and v['uids'] == [policy['owner_uid']]*4 and v['gids'] == [policy['owner_gid']]*4
        and sorted(v['groups']) == sorted(policy['owner_groups']) and policy['owner_gid'] in v['groups'], 'peer_denied')  # BL-G09
    owner._require({k:v[k] for k in _LIFE} == policy['owner_process']
        and policy['owner_process'] != policy['peer_process'] and v['boot_id'] == c['boot_id'], 'peer_denied')  # BL-G10
    owner._require(receipt['namespaces'] == policy['namespaces'], 'peer_denied')  # BL-G11
    owner._require(receipt['maps'] == policy['maps'], 'peer_denied')  # BL-G12
    owner._require(all(_mapped(receipt['maps'][who]['uid'], policy[who+'_uid']) == policy[who+'_uid']
        and all(_mapped(receipt['maps'][who]['gid'], gid) == gid for gid in policy[who+'_groups'])
        for who in ('peer','owner')), 'peer_denied')  # BL-G13
    privileges = receipt['privileges']
    owner._require(privileges['no_new_privs'] and privileges['capabilities'] == policy['capabilities']
        and all(privileges['capabilities'][k] == 0 for k in ('inheritable','permitted','effective','ambient')), 'peer_denied')  # BL-G14
    owner._require(privileges['peer_lifetime'] == policy['peer_process']
        and privileges['owner_lifetime'] == policy['owner_process']
        and all(v == 'denied' for v in privileges['paths'].values()), 'peer_denied')  # BL-G15
    owner._require(c['code_sha256'] == policy['peer_code_sha256'], 'peer_denied')  # BL-G29
    # P1 already bounds each original to 262144 bytes; do not truncate the closure.
    expected = policy['objects']; observed = receipt['objects']; startup = receipt['startup']
    owner._require(startup['executable_path'] in [x['path'] for x in expected if x['role'] == 'interpreter']
        and [x['sha256'] for x in expected if x['role'] == 'interpreter'] == [v['code_sha256']]
        and startup == dict(executable_path=startup['executable_path'], python='3.12.15', crypto='cryptography-50.0.0',
        dependencies=['cffi-2.1.1','pycparser-3.0'], isolated=True, no_site=True,
        environment=[], pth=[], candidate_imports=[],
        loaded_paths=[x['path'] for x in expected if x['role'] in _CODE],
        native_paths=[x['path'] for x in expected if x['role'] == 'native']), 'custody_changed')  # BL-G16
    with owner._current_observation(inputs):
        owner._original(inputs, context['current_state_ref'])  # bind the observed state file to B1's original
        owner._require(_ROLES <= {x['role'] for x in expected} <= _ROLES | {'root','ancestor'}
            and len({x['path'] for x in expected}) == len(expected)
            and len({tuple(x['identity']) for x in expected}) == len(expected)
            and [x['path'] for x in observed] == [x['path'] for x in expected]
            and [x['path'] for x in expected if x['role'] == 'endpoint'] == [policy['endpoint']], 'custody_changed')  # BL-G17
        owner._require(all(x['path'].startswith('/') and '\\' not in x['path'] and '\x00' not in x['path']
            and str(PurePosixPath(x['path'])) == x['path'] and '//' not in x['path']
            and '..' not in PurePosixPath(x['path']).parts for x in expected), 'custody_changed')  # BL-G18
        by_path = {x['path']:x for x in observed}
        owner._require(all(all(str(parent) in by_path and by_path[str(parent)]['kind'] == 'directory'
            for parent in PurePosixPath(x['path']).parents) for x in expected), 'custody_changed')  # BL-G19
        owner._require(all(a['kind'] == b['kind'] == ('socket' if a['role'] == 'endpoint'
            else 'directory' if a['role'] in ('root','ancestor','originals') else 'file')
            and (b['nlink'] == 1 or a['kind'] == 'directory') for a,b in zip(expected,observed)), 'custody_changed')  # BL-G20
        owner._require(all(a['identity'] == b['identity'] == b['held_identity'] for a,b in zip(expected,observed)), 'custody_changed')  # BL-G21
        owner._require(all((a['sha256'] is None and b['sha256'] == context[
            'policy_ref' if a['role'] == 'policy' else 'current_state_ref']['sha256'])
            if a['role'] in ('policy','state') else
            (a['sha256'] == b['sha256'] and (a['sha256'] is not None if a['kind'] == 'file'
            else a['sha256'] is None)) for a,b in zip(expected,observed)), 'custody_changed')  # BL-G22
        owner._require(all(x['uid'] in (0,policy['owner_uid']) for x in observed), 'custody_changed')  # BL-G23
        owner._require(all(x['mode'] & 0o7000 == 0 for x in observed), 'custody_changed')  # BL-G24
        owner._require(all(_acl_valid(x) for x in observed), 'custody_changed')  # BL-G25
        owner._require(all(not (_access(x,policy['peer_uid'],c['groups']) & 2)
            for x in observed if x['path'] != policy['endpoint']), 'custody_changed')  # BL-G26
        owner._require(all(not x['default_acl'] for x in observed), 'custody_changed')  # BL-G27
        endpoint = by_path[policy['endpoint']]
        owner._require(endpoint['kind'] == 'socket' and _access(endpoint,policy['peer_uid'],c['groups']) & 2
            and all(_access(by_path[str(parent)],policy['peer_uid'],c['groups']) & 1
                    for parent in PurePosixPath(policy['endpoint']).parents), 'custody_changed')  # BL-G28
    return inputs
