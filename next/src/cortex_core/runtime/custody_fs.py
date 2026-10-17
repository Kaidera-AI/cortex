"""Descriptor-only private custody IO. Cipher and seed policy live elsewhere."""
from contextlib import contextmanager
import errno
import fcntl
import hashlib
import os
from pathlib import PurePosixPath
import re
import select
import stat
import sys
import time

from .signed_member import Refusal

MAX_RECORD = 65536
_FLAGS = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK


def anonymous_pipe(fd, *, writing=False):
    """Recognize an opened anonymous pipe on the two supported OS families."""
    try:
        s = os.fstat(fd)
        access = fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE
        if (not stat.S_ISFIFO(s.st_mode) or s.st_uid != os.geteuid()
                or access != (os.O_WRONLY if writing else os.O_RDONLY)):
            return False
        if sys.platform == 'darwin':
            return s.st_nlink == 0
        if sys.platform.startswith('linux'):
            return re.fullmatch(r'pipe:\[[0-9]+\]', os.readlink('/proc/self/fd/' + str(fd))) is not None
        return False
    except (OSError, ValueError, TypeError):
        return False


def read_unlock(fd):
    duplicate = None
    try:
        if type(fd) is not int or fd < 0 or fd in (1, 2):
            raise Refusal('custody_locked')
        duplicate = os.dup(fd)
        os.set_inheritable(duplicate, False)
        s = os.fstat(duplicate)
        regular = (stat.S_ISREG(s.st_mode) and s.st_uid == os.geteuid()
                   and not s.st_mode & 0o077 and s.st_nlink == 1 and s.st_size == 32
                   and fcntl.fcntl(duplicate, fcntl.F_GETFL) & os.O_ACCMODE == os.O_RDONLY)
        if not regular and not anonymous_pipe(duplicate):
            raise Refusal('custody_locked')
        if fd == 0 and regular:
            raise Refusal('custody_locked')
        deadline = time.monotonic() + 1
        raw = bytearray()
        while len(raw) <= 32:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([duplicate], [], [], remaining)[0]:
                raise Refusal('custody_locked')
            chunk = os.read(duplicate, 33 - len(raw))
            if not chunk:
                break
            raw.extend(chunk)
        if len(raw) != 32:
            raise Refusal('custody_locked')
        return bytes(raw)
    except (OSError, ValueError, TypeError) as error:
        if type(error) is Refusal:
            raise
        raise Refusal('custody_locked') from None
    finally:
        if duplicate is not None:
            os.close(duplicate)


def _private_directory(fd):
    s = os.fstat(fd)
    if (not stat.S_ISDIR(s.st_mode) or s.st_uid != os.geteuid()
            or stat.S_IMODE(s.st_mode) != 0o700 or s.st_nlink < 1):
        raise Refusal('custody_private_required')


class PrivateDirectory:
    """Hold the admitted directory chain; never resolve a candidate symlink."""
    def __init__(self, root):
        self._fds = []
        self._edges = []
        self.fd = None
        try:
            path = PurePosixPath(root)
            if type(root) is not str or not path.is_absolute() or str(path) != root or '..' in path.parts:
                raise Refusal('custody_private_required')
            current = os.open('/', _FLAGS | os.O_DIRECTORY)
            self._fds.append(current)
            parts = path.parts[1:]
            for i, name in enumerate((*parts, 'secrets', 'cortex')):
                opened = os.open(name, _FLAGS | os.O_DIRECTORY, dir_fd=current)
                self._fds.append(opened)
                self._edges.append((current, name, opened, i >= len(parts)-1))
                if i >= len(parts)-1:
                    _private_directory(opened)
                current = opened
            self.fd = current
        except (OSError, TypeError, ValueError) as error:
            self.close()
            if type(error) is Refusal:
                raise
            raise Refusal('custody_private_required') from None

    def check(self):
        if self.fd is None:
            raise Refusal('custody_closed')
        try:
            for parent, name, fd, private in self._edges:
                named = os.stat(name, dir_fd=parent, follow_symlinks=False)
                opened = os.fstat(fd)
                if (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino):
                    raise Refusal('custody_changed')
                if private:
                    _private_directory(fd)
        except OSError:
            raise Refusal('custody_changed') from None

    def close(self):
        self.fd = None
        for fd in reversed(self._fds):
            os.close(fd)
        self._fds.clear()


@contextmanager
def record_lock(directory, name):
    directory.check()
    parent = fd = None
    try:
        try:
            os.mkdir('.locks', mode=0o700, dir_fd=directory.fd)
        except FileExistsError:
            pass
        parent = os.open('.locks', _FLAGS | os.O_DIRECTORY, dir_fd=directory.fd)
        _private_directory(parent)
        fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
                     0o600, dir_fd=parent)
        s = os.fstat(fd)
        if (not stat.S_ISREG(s.st_mode) or s.st_uid != os.geteuid()
                or stat.S_IMODE(s.st_mode) != 0o600 or s.st_nlink != 1):
            raise Refusal('custody_private_required')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno in (errno.EAGAIN, errno.EACCES):
                raise Refusal('custody_busy') from None
            raise
        current = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (s.st_dev, s.st_ino):
            raise Refusal('custody_changed')
        directory.check()
        yield
    except OSError:
        raise Refusal('custody_private_required') from None
    finally:
        if fd is not None:
            os.close(fd)  # releases ONLY this opened lock; never unlink its inode
        if parent is not None:
            os.close(parent)


def _stamp(s):
    return (s.st_dev, s.st_ino, s.st_mode, s.st_nlink, s.st_uid,
            s.st_size, s.st_mtime_ns, s.st_ctime_ns)


def read_record(directory, name):
    """None means ENOENT only. Unsafe/corrupt/changed leaves are never absence."""
    directory.check()
    fd = None
    try:
        try:
            fd = os.open(name, _FLAGS, dir_fd=directory.fd)
        except FileNotFoundError:
            return None
        s = os.fstat(fd)
        if (not stat.S_ISREG(s.st_mode) or s.st_uid != os.geteuid()
                or stat.S_IMODE(s.st_mode) != 0o600 or s.st_nlink != 1
                or not 1 <= s.st_size <= MAX_RECORD):
            raise Refusal('custody_private_required')
        raw = bytearray()
        while len(raw) <= MAX_RECORD:
            chunk = os.read(fd, min(8192, MAX_RECORD+1-len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        after = os.fstat(fd)
        named = os.stat(name, dir_fd=directory.fd, follow_symlinks=False)
        if (_stamp(s) != _stamp(after) or len(raw) != s.st_size
                or (named.st_dev, named.st_ino) != (s.st_dev, s.st_ino)):
            raise Refusal('custody_changed')
        directory.check()
        return bytes(raw), (_stamp(s), hashlib.sha256(raw).digest())
    except OSError:
        raise Refusal('custody_private_required') from None
    finally:
        if fd is not None:
            os.close(fd)


def write_record(directory, name, raw, *, expected):
    """Absent-only publication or snapshot-checked replacement, then dir fsync."""
    directory.check()
    temporary = '.tmp-' + os.urandom(16).hex()
    fd = None
    published = False
    try:
        current = read_record(directory, name)
        if (None if current is None else current[1]) != expected:
            raise Refusal('custody_changed')
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                     0o600, dir_fd=directory.fd)
        view = memoryview(raw)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError('incomplete write')
            view = view[written:]
        os.fsync(fd)
        os.close(fd)
        fd = None
        directory.check()
        current = read_record(directory, name)
        if (None if current is None else current[1]) != expected:
            raise Refusal('custody_changed')
        if expected is None:
            os.link(temporary, name, src_dir_fd=directory.fd, dst_dir_fd=directory.fd,
                    follow_symlinks=False)
            published = True
            os.unlink(temporary, dir_fd=directory.fd)
        else:
            os.replace(temporary, name, src_dir_fd=directory.fd, dst_dir_fd=directory.fd)
            published = True
        os.fsync(directory.fd)
        directory.check()
        return read_record(directory, name)[1]
    except OSError:
        raise Refusal('custody_commit_unknown' if published else 'custody_write_failed') from None
    finally:
        if fd is not None:
            os.close(fd)
        try:
            os.unlink(temporary, dir_fd=directory.fd)
        except FileNotFoundError:
            pass
        except OSError:
            # No key/plaintext is ever in this temp. Do not touch another name.
            pass
