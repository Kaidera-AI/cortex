"""Physical read roots stay outside the key. No extraction or link traversal."""
import os
from pathlib import Path
import stat
from .codec import relative_path
from .inputs import FileObservation


class RootReader:
    def __init__(self, root, limit):
        self.root = Path(root).resolve(strict=True)
        self.limit = limit
        self.fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def _parent(self, path):
        parts = relative_path(path).split('/')
        fd = os.dup(self.fd)
        try:
            for part in parts[:-1]:
                next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = next_fd
            return fd, parts[-1]
        except BaseException:
            os.close(fd)
            raise

    def __call__(self, path):
        fd, name = self._parent(path)
        try:
            before = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISLNK(before.st_mode):
                target = os.readlink(name, dir_fd=fd)
                relative_path(target)
                return FileObservation(target.encode('utf-8'), 'symlink', stat.S_IMODE(before.st_mode), target)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > self.limit:
                raise ValueError('nonregular_alias_or_size')
            file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            with os.fdopen(file_fd, 'rb') as stream:
                actual = os.fstat(stream.fileno())
                if (actual.st_dev, actual.st_ino) != (before.st_dev, before.st_ino):
                    raise ValueError('file_replaced')
                raw = stream.read(self.limit + 1)
                after = os.fstat(stream.fileno())
                if (actual.st_size, actual.st_mtime_ns, actual.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                    raise ValueError('file_changed')
            if len(raw) > self.limit:
                raise ValueError('file_size_bound')
            return FileObservation(raw, 'regular', stat.S_IMODE(actual.st_mode))
        finally:
            os.close(fd)

    def bytes(self, path):
        observed = self(path)
        if observed.kind != 'regular':
            raise ValueError('artifact_symlink')
        return observed.data

    def inventory(self, context_root):
        relative_path(context_root)
        fd, name = self._parent(context_root + '/placeholder')
        try:
            result = []
            def visit(directory_fd, prefix):
                for entry in sorted(os.scandir(directory_fd), key=lambda e:e.name):
                    path = prefix + '/' + entry.name
                    relative_path(path)
                    info = entry.stat(follow_symlinks=False)
                    if stat.S_ISDIR(info.st_mode):
                        child = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd)
                        try:
                            visit(child, path)
                        finally:
                            os.close(child)
                    elif stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                        result.append(path)
                        if len(result) > 50000:
                            raise ValueError('inventory_count')
                    else:
                        raise ValueError('inventory_nonregular')
            visit(fd, context_root)
            return result
        finally:
            os.close(fd)
