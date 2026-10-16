"""Descriptor-safe removal of owned roots, bound to their recorded identities.

Stdlib only: OwnedRoots.cleanup calls remove() in-process, and the same file runs
unchanged inside the engine's user namespace (`<engine> unshare <python> -I -S -B
_owned_remove.py PATH DEV INO ...`) for rootless store files owned by mapped ids.
The deleting process itself opens each root by descriptor and compares its
(device, inode) before touching anything, so a replaced pathname is never followed.
"""
import os
import shutil
import stat
import sys


def remove(path, device, inode):
    if not shutil.rmtree.avoids_symlink_attacks:
        raise ValueError('descriptor-safe cleanup unavailable')
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        held = os.fstat(descriptor)
        if (held.st_dev, held.st_ino) != (device, inode):
            raise ValueError('owned root identity changed')
        for entry in os.scandir(descriptor):
            observed = os.stat(entry.name, dir_fd=descriptor, follow_symlinks=False)
            if stat.S_ISDIR(observed.st_mode):
                shutil.rmtree(entry.name, dir_fd=descriptor)
            else:
                os.unlink(entry.name, dir_fd=descriptor)
    finally:
        os.close(descriptor)
    current = os.stat(path, follow_symlinks=False)
    if (current.st_dev, current.st_ino) != (device, inode):
        raise ValueError('owned root replaced during cleanup')
    os.rmdir(path)


def main(argv):
    if len(argv) % 3 or not argv:
        raise SystemExit(2)
    for index in range(0, len(argv), 3):
        remove(argv[index], int(argv[index + 1]), int(argv[index + 2]))


if __name__ == '__main__':
    main(sys.argv[1:])
