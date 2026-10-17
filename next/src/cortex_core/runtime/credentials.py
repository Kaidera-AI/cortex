"""One purpose-bound private-file reader; no credential store or driver effects."""
import os
from pathlib import Path
import stat

from .signed_member import Refusal


def read_private_file(path, *, limit):
    """Walk from / with no-follow directory FDs, then bound the opened inode.

    No path-based stat/open race, symlink parent, hardlink or device is admitted.
    Error details never include the path, candidate bytes or OS exception text.
    """
    descriptors = []
    try:
        path = Path(path)
        if not path.is_absolute() or any(p in ('.', '..') for p in path.parts):
            raise Refusal('private_file_required')
        # Path normalizes dots; require its spelling to be canonical at the API
        # boundary too, before traversing every component without following it.
        flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
        parent = os.open('/', flags | os.O_DIRECTORY)
        descriptors.append(parent)
        for part in path.parts[1:-1]:
            parent = os.open(part, flags | os.O_DIRECTORY, dir_fd=parent)
            descriptors.append(parent)
        fd = os.open(path.name, flags | os.O_NONBLOCK, dir_fd=parent)
        descriptors.append(fd)
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid()
                or before.st_mode & 0o077 or before.st_nlink != 1
                or before.st_size <= 0 or before.st_size > limit):
            raise Refusal('private_file_required')
        raw = bytearray()
        while len(raw) <= limit:
            chunk = os.read(fd, min(8192, limit + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        after = os.fstat(fd)
        if (len(raw) > limit or len(raw) != before.st_size
                or (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
            raise Refusal('private_file_changed')
        return bytes(raw)
    except (OSError, TypeError, ValueError):
        raise Refusal('private_file_required') from None
    finally:
        for fd in reversed(descriptors):
            os.close(fd)


def resolve_credential(config, purpose):
    from .bootstrap_config import BootstrapConfig, MODE_PURPOSES
    if type(config) is not BootstrapConfig or type(purpose) is not str:
        raise Refusal('purpose_not_admitted')
    if purpose == 'client' and config.mode == 'client':
        reference = config.client_ref
    elif (type(config.mode) is str and purpose in MODE_PURPOSES.get(config.mode, ())
          and purpose in config.roles):
        reference = config.roles[purpose].secret_ref
    else:
        raise Refusal('purpose_not_admitted')
    return read_private_file(Path(config.vault_root) / reference, limit=4096)
