"""J1 project unlock key (day 3, row feb9dc8b): the installer-side private opener. Its only effect is to open, read once (to derive the key id), and return a fresh read-only descriptor.

WHAT IT IS. The installer-side private opener for ONE option-C project root: 32 raw bytes in a host-private key file, outside the vault, one file per project. It admits the file and
returns a FRESH read-only descriptor that Core's existing `runtime.custody_fs.read_unlock(fd)` already accepts (regular file, effective-uid owner, no group/other bits, one link, 32 bytes).
It never returns, stores, logs or puts key bytes in an exception. It selects no daemon, env var, argv, keychain, cloud store or second custody mechanism.

THE CLOSED REFERENCE (nonsecret; Core's decoder, Finn's file, must carry exactly these keys under `custody_unlock` and compare project_id with the config's own project_id):
  {"kind": "project-key-file", "path": <canonical absolute>, "key_id": <[A-Za-z0-9_-]{1,64}>, "identity": <execution-identity name>, "project_id": <uuid of the config's project>}
  path      canonical, absolute, no `..`, final component == key_id + ".key" (the file name carries the id).
  key_id    DERIVED: "pk1-" + sha256(b"cortex-project-key-id-v1\\0" + key).hexdigest()[:32]. A swapped, wrong or installation key is refused by the opener before Core is touched. Core keeps it opaque.
  identity  the OS execution identity (account name) that must equal the CURRENT process's effective account. Root (euid 0) is refused.

SEAM (what Cox, Finn and Bob call):
  derive_key_id(key: bytes) -> str
  validate_reference(ref, *, project_id) -> dict            closed shape, no IO
  open_unlock(ref, *, project_id, installation_key_id) -> int   admit + return ONE fresh non-inheritable read-only fd (the caller closes it)
  UnlockSession(ref, *, project_id, installation_key_id, deadline)   one lead session: .fresh_fd() re-admits every call and returns a NEW fd; .close(); refuses after close, after the
                                                                    original deadline, in another process, when copied or pickled.

ADMISSION (every check before any descriptor leaves this module; no repair, no chmod, no create):
  shape -> identity (account == reference, not root) -> path chain opened from "/" with O_NOFOLLOW|O_DIRECTORY, parent directory owned by the effective uid, mode 0700, ancestors owned by root or
  the effective uid and not group/other-writable -> leaf opened O_NOFOLLOW: regular file, effective-uid owner, mode exactly 0400, one link, size 32, no ACL, filesystem that honours ownership ->
  read once to derive the id: must equal reference.key_id and must NOT equal installation_key_id -> the name still resolves to the same device/inode -> lseek(0) and return.
REFUSALS (`KeyRefusal.reason`, never a path or a byte): unlock_reference_invalid, unlock_reference_foreign, unlock_identity_mismatch, unlock_identity_privileged, unlock_path_unsafe, unlock_key_absent,
unlock_key_unsafe, unlock_acl_present, unlock_mount_unsafe, unlock_key_mismatch, unlock_key_installation, unlock_io_failed, unlock_session_closed, unlock_session_expired, unlock_session_foreign_process,
unlock_session_copy, unlock_deadline_invalid; nothing else is ever raised from an admission.
NOT CLAIMED: native isolation, a second OS user, reboot, Linux, any admitted host identity. Fixtures are not native credit."""
import ctypes
import errno
import hashlib
import hmac
import os
import pwd
import re
import stat
import sys
import time

KIND = 'project-key-file'
REFERENCE_KEYS = frozenset({'kind', 'path', 'key_id', 'identity', 'project_id'})
KEY_ID = re.compile(r'[A-Za-z0-9_-]{1,64}\Z')
KEY_ID_DOMAIN = b'cortex-project-key-id-v1\x00'
KEY_ID_PREFIX = 'pk1-'
KEY_BYTES = 32
IDENTITY = re.compile(r'[A-Za-z_][A-Za-z0-9._-]{0,62}\Z')
UUID_TEXT = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z')
MAX_PATH = 4096
NIL_UUID = '00000000-0000-0000-0000-000000000000'
MNT_IGNORE_OWNERSHIP = 0x00200000   # <sys/mount.h>, Darwin: the volume does not honour file ownership (the noowners mount)
ACL_TYPE_EXTENDED = 0x00000100      # <sys/acl.h>, Darwin
OWNERSHIP_HONOURING = frozenset({'ext2', 'ext3', 'ext4', 'xfs', 'btrfs', 'tmpfs', 'f2fs', 'zfs'})
_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_LEAF = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


class KeyRefusal(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def derive_key_id(key):
    if type(key) is not bytes or len(key) != KEY_BYTES:
        raise KeyRefusal('unlock_key_unsafe')
    return KEY_ID_PREFIX + hashlib.sha256(KEY_ID_DOMAIN + key).hexdigest()[:32]


def _text(value, pattern):
    return type(value) is str and pattern.fullmatch(value) is not None


def validate_reference(ref, *, project_id):
    if type(ref) is not dict or set(ref) != REFERENCE_KEYS or not _text(project_id, UUID_TEXT):
        raise KeyRefusal('unlock_reference_invalid')
    path = ref['path']
    ok = (ref['kind'] == KIND and type(ref['kind']) is str and _text(ref['key_id'], KEY_ID) and _text(ref['identity'], IDENTITY)
          and _text(ref['project_id'], UUID_TEXT) and ref['project_id'] != NIL_UUID
          and type(path) is str and 2 <= len(path.encode('utf-8', 'surrogatepass')) <= MAX_PATH and path.startswith('/') and not path.startswith('//')
          and '\x00' not in path and not path.endswith('/') and '/./' not in path and '/../' not in path and not path.endswith(('/.', '/..'))
          and path.rsplit('/', 1)[1] == ref['key_id'] + '.key' and '//' not in path)
    if not ok:
        raise KeyRefusal('unlock_reference_invalid')
    if ref['project_id'] != project_id:
        raise KeyRefusal('unlock_reference_foreign')
    return dict(ref)


# Injection points (trusted-code composition; tests replace these, no caller passes them).
def _euid():
    return os.geteuid()


def _account(uid):
    return pwd.getpwuid(uid).pw_name


def _pid():
    return os.getpid()


def _now():
    return time.monotonic()


def _after_read():
    """Hook point between the single key read and the final name re-check; a no-op in production."""
    return None


def _dir_safe(st, euid, *, private):
    """A directory on the key's path. The key's own parent must be private (euid owner, exactly 0700); every ancestor must be owned by root or euid and not group/other-writable, except a
    sticky directory (a /tmp-like parent where another user cannot rename or delete our entry)."""
    if not stat.S_ISDIR(st.st_mode):
        return False
    mode = stat.S_IMODE(st.st_mode)
    if private:
        return st.st_uid == euid and mode == 0o700
    return st.st_uid in (0, euid) and (mode & 0o022 == 0 or bool(mode & stat.S_ISVTX))


def _leaf_safe(st, euid):
    return (stat.S_ISREG(st.st_mode) and st.st_uid == euid and stat.S_IMODE(st.st_mode) == 0o400
            and st.st_nlink == 1 and st.st_size == KEY_BYTES)


def _darwin_flags_ignore_ownership(f_flags):
    if type(f_flags) is not int:
        raise KeyRefusal('unlock_mount_unsafe')
    return bool(f_flags & MNT_IGNORE_OWNERSHIP)


def _mount_honours_ownership_linux(mountinfo, st_dev):
    """True when every mount of this device is a filesystem that honours ownership; a device not found or a malformed table refuses (fail closed)."""
    wanted = '%d:%d' % (os.major(st_dev), os.minor(st_dev))
    kinds = []
    for line in mountinfo.splitlines():
        left, separator, right = line.partition(' - ')
        fields = left.split(' ')
        if not separator or len(fields) < 6 or not right.split(' ')[0]:
            raise KeyRefusal('unlock_mount_unsafe')
        if fields[2] == wanted:
            kinds.append(right.split(' ')[0])
    if not kinds:
        raise KeyRefusal('unlock_mount_unsafe')
    return all(kind in OWNERSHIP_HONOURING for kind in kinds)


class _Statfs(ctypes.Structure):
    _fields_ = [('f_bsize', ctypes.c_uint32), ('f_iosize', ctypes.c_int32), ('f_blocks', ctypes.c_uint64), ('f_bfree', ctypes.c_uint64),
                ('f_bavail', ctypes.c_uint64), ('f_files', ctypes.c_uint64), ('f_ffree', ctypes.c_uint64), ('f_fsid', ctypes.c_int32 * 2),
                ('f_owner', ctypes.c_uint32), ('f_type', ctypes.c_uint32), ('f_flags', ctypes.c_uint32), ('f_fssubtype', ctypes.c_uint32),
                ('f_fstypename', ctypes.c_char * 16), ('f_mntonname', ctypes.c_char * 1024), ('f_mntfromname', ctypes.c_char * 1024),
                ('f_flags_ext', ctypes.c_uint32), ('f_reserved', ctypes.c_uint32 * 7)]


def _libc():
    return ctypes.CDLL(None, use_errno=True)


def _mount_ignores_ownership(fd):
    """True when the filesystem holding the descriptor does not honour ownership. Fail closed: an unsupported OS or an unreadable table refuses."""
    try:
        if sys.platform == 'darwin':
            info = _Statfs()
            libc = _libc()
            libc.fstatfs.argtypes = [ctypes.c_int, ctypes.POINTER(_Statfs)]
            if libc.fstatfs(fd, ctypes.byref(info)) != 0:
                raise KeyRefusal('unlock_mount_unsafe')
            return _darwin_flags_ignore_ownership(int(info.f_flags))
        if sys.platform.startswith('linux'):
            table = os.open('/proc/self/mountinfo', os.O_RDONLY | os.O_CLOEXEC)
            try:
                chunks = []
                while True:
                    chunk = os.read(table, 65536)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    if sum(map(len, chunks)) > 4 * 1024 * 1024:
                        raise KeyRefusal('unlock_mount_unsafe')
            finally:
                os.close(table)
            return not _mount_honours_ownership_linux(b''.join(chunks).decode('utf-8'), os.fstat(fd).st_dev)
    except (OSError, UnicodeDecodeError, AttributeError, ValueError):
        raise KeyRefusal('unlock_mount_unsafe') from None
    raise KeyRefusal('unlock_mount_unsafe')


def _acl_present(fd):
    """True when the file carries an ACL entry. Fail closed on anything but a clean 'no ACL' answer."""
    try:
        if sys.platform == 'darwin':
            libc = _libc()
            libc.acl_get_fd_np.argtypes = [ctypes.c_int, ctypes.c_uint]
            libc.acl_get_fd_np.restype = ctypes.c_void_p
            libc.acl_free.argtypes = [ctypes.c_void_p]
            ctypes.set_errno(0)
            found = libc.acl_get_fd_np(fd, ACL_TYPE_EXTENDED)
            if found:
                libc.acl_free(found)
                return True
            if ctypes.get_errno() in (0, errno.ENOENT):
                return False
            raise KeyRefusal('unlock_acl_present')
        if sys.platform.startswith('linux'):
            try:
                names = os.listxattr(fd)
            except OSError as error:
                if error.errno in (errno.ENOTSUP, errno.ENODATA):
                    return False
                raise
            return any(name.startswith('system.posix_acl_') or name.startswith('security.nfs4') for name in names)
    except (OSError, AttributeError, ValueError):
        raise KeyRefusal('unlock_acl_present') from None
    raise KeyRefusal('unlock_acl_present')


def _walk(path, euid, closer):
    """Open the directory chain from '/' with no symlink followed. Returns (parent_fd, leaf_name, edges); every descriptor is registered with `closer`."""
    parts = path.split('/')[1:]
    edges = []
    current = os.open('/', _DIR)
    closer.append(current)
    for index, name in enumerate(parts):
        last = index == len(parts) - 1
        if last:
            return current, name, edges
        try:
            opened = os.open(name, _DIR, dir_fd=current)
        except FileNotFoundError:
            raise KeyRefusal('unlock_key_absent') from None
        except OSError:
            raise KeyRefusal('unlock_path_unsafe') from None
        closer.append(opened)
        st = os.fstat(opened)
        if not _dir_safe(st, euid, private=index == len(parts) - 2):
            raise KeyRefusal('unlock_path_unsafe')
        edges.append((current, name, opened, (st.st_dev, st.st_ino)))
        current = opened
    raise KeyRefusal('unlock_reference_invalid')


def open_unlock(ref, *, project_id, installation_key_id):
    ref = validate_reference(ref, project_id=project_id)
    if not _text(installation_key_id, KEY_ID):
        raise KeyRefusal('unlock_reference_invalid')
    euid = _euid()
    if euid == 0:
        raise KeyRefusal('unlock_identity_privileged')
    try:
        account = _account(euid)
    except (KeyError, OSError):
        raise KeyRefusal('unlock_identity_mismatch') from None
    if account != ref['identity']:
        raise KeyRefusal('unlock_identity_mismatch')
    closer, leaf, buffer = [], None, bytearray(KEY_BYTES + 1)
    try:
        try:
            parent, name, edges = _walk(ref['path'], euid, closer)
            try:
                leaf = os.open(name, _LEAF, dir_fd=parent)
            except FileNotFoundError:
                raise KeyRefusal('unlock_key_absent') from None
            except OSError:
                raise KeyRefusal('unlock_key_unsafe') from None
            st = os.fstat(leaf)
            if not _leaf_safe(st, euid):
                raise KeyRefusal('unlock_key_unsafe')
            if _acl_present(leaf):
                raise KeyRefusal('unlock_acl_present')
            if _mount_ignores_ownership(leaf):
                raise KeyRefusal('unlock_mount_unsafe')
            count = os.readv(leaf, [buffer])
            if count != KEY_BYTES:
                raise KeyRefusal('unlock_key_unsafe')
            digest = hashlib.sha256()
            digest.update(KEY_ID_DOMAIN)
            digest.update(memoryview(buffer)[:KEY_BYTES])
            derived = KEY_ID_PREFIX + digest.hexdigest()[:32]
            if not hmac.compare_digest(derived.encode(), ref['key_id'].encode()):
                raise KeyRefusal('unlock_key_mismatch')
            if hmac.compare_digest(derived.encode(), installation_key_id.encode()):
                raise KeyRefusal('unlock_key_installation')
            _after_read()
            now = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if (now.st_dev, now.st_ino, now.st_nlink) != (st.st_dev, st.st_ino, 1):
                raise KeyRefusal('unlock_key_unsafe')
            for above, label, held, identity in edges:
                seen = os.stat(label, dir_fd=above, follow_symlinks=False)
                if (seen.st_dev, seen.st_ino) != identity or (os.fstat(held).st_dev, os.fstat(held).st_ino) != identity:
                    raise KeyRefusal('unlock_path_unsafe')
            os.lseek(leaf, 0, os.SEEK_SET)
        except OSError:
            raise KeyRefusal('unlock_io_failed') from None
        result, leaf = leaf, None
        return result
    finally:
        buffer[:] = bytes(len(buffer))
        if leaf is not None:
            os.close(leaf)
        for fd in reversed(closer):
            os.close(fd)


class UnlockSession:
    """One lead session over ONE admitted reference. It holds no key and no descriptor: every `fresh_fd` re-admits and returns a NEW descriptor for one finite operation."""
    __slots__ = ('_ref', '_project', '_installation', '_deadline', '_owner', '_closed')

    def __init__(self, ref, *, project_id, installation_key_id, deadline):
        if (type(deadline) not in (int, float) or deadline != deadline or deadline in (float('inf'), float('-inf'))):
            raise KeyRefusal('unlock_deadline_invalid')
        if _now() >= deadline:
            raise KeyRefusal('unlock_session_expired')
        admitted = validate_reference(ref, project_id=project_id)
        os.close(open_unlock(admitted, project_id=project_id, installation_key_id=installation_key_id))
        object.__setattr__(self, '_ref', admitted)
        object.__setattr__(self, '_project', project_id)
        object.__setattr__(self, '_installation', installation_key_id)
        object.__setattr__(self, '_deadline', deadline)
        object.__setattr__(self, '_owner', _pid())
        object.__setattr__(self, '_closed', False)

    def __setattr__(self, name, value):
        raise KeyRefusal('unlock_session_copy')

    def __repr__(self):
        return '<UnlockSession private>'

    def __copy__(self):
        raise KeyRefusal('unlock_session_copy')

    def __deepcopy__(self, memo):
        raise KeyRefusal('unlock_session_copy')

    def __reduce_ex__(self, protocol):
        raise KeyRefusal('unlock_session_copy')

    def fresh_fd(self):
        if self._closed:
            raise KeyRefusal('unlock_session_closed')
        if self._owner != _pid():
            raise KeyRefusal('unlock_session_foreign_process')
        if _now() >= self._deadline:
            raise KeyRefusal('unlock_session_expired')
        return open_unlock(self._ref, project_id=self._project, installation_key_id=self._installation)

    def close(self):
        object.__setattr__(self, '_closed', True)
