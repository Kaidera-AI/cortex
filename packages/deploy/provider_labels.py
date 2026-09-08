"""Finite, owner-approved Linux labels for the existing provider authority.

No settings writer, recursive relabel, engine operation or automatic repair.
The caller owns approval, host selection and any later recovery of a journal.
"""
from contextlib import ExitStack, contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys


TARGET = b"system_u:object_r:container_file_t:s0"
SCHEMA = "cortex.provider-labels.v1"
JOURNAL = "provider-labels.journal.jsonl"
CACHE = "kos-cortex-provider"
MAX_KERNEL = 1024 * 1024
BOOTSTRAP = "cortex-provider-bootstrap.json"
BOOTSTRAP_ORIGIN = {
    "schema": "cortex.provider-bootstrap.v1",
    "author": "cortex",
    "source_repo": "Kaidera-AI/openkai",
    "source_revision": "f3660f3c19939d2a6ff3b95be9aab3f85fb8312a",
    "source_path": ".env.example",
    "sha256": "fd25fc78a15f85a47fa8bae91179dcea3892cc37f81cd42186face03e962fc64",
}


class LabelError(ValueError):
    """Finite diagnostic only; never contains provider bytes or raw errors."""


def _require(condition, code):
    if not condition:
        raise LabelError(code)


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("ascii")


def _digest(value):
    return hashlib.sha256(_json(value)).hexdigest()


def _applicable():
    if sys.platform == "darwin":
        return False
    _require(sys.platform == "linux", "unsupported_platform")
    _require(os.getuid() == os.geteuid() > 0 and os.getgid() == os.getegid(),
             "requires_ordinary_owner")
    return True


def _kernel_bytes(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        chunks, size = [], 0
        while True:
            part = os.read(fd, min(65536, MAX_KERNEL + 1 - size))
            if not part:
                return b"".join(chunks)
            chunks.append(part)
            size += len(part)
            _require(size <= MAX_KERNEL, "kernel_metadata_too_large")
    finally:
        os.close(fd)


def _read_enforce():
    return _kernel_bytes("/sys/fs/selinux/enforce")


def _read_mountinfo():
    return _kernel_bytes("/proc/self/mountinfo")


def _unescape(value):
    _require(not re.search(r"\\(?!040|011|012|134)", value), "invalid_mountinfo")
    return re.sub(r"\\(040|011|012|134)", lambda match: chr(int(match[1], 8)), value)


def _host(authority):
    _require(_read_enforce() in (b"1", b"1\n"), "selinux_not_enforcing")
    raw = _read_mountinfo()
    _require(type(raw) is bytes and 0 < len(raw) <= MAX_KERNEL, "invalid_mountinfo")
    rows = raw.decode("ascii").splitlines()
    _require(bool(rows), "invalid_mountinfo")
    for row in rows:
        fields = row.split()
        _require(len(fields) >= 10 and fields[0].isdigit() and int(fields[0]) > 0
                 and fields[1].isdigit() and re.fullmatch(r"[0-9]+:[0-9]+", fields[2]),
                 "invalid_mountinfo")
        split = fields.index("-")
        _require(split >= 6 and len(fields) == split + 4, "invalid_mountinfo")
        root, mount = Path(_unescape(fields[3])), Path(_unescape(fields[4]))
        _require(root.is_absolute() and mount.is_absolute()
                 and ".." not in root.parts and ".." not in mount.parts,
                 "invalid_mountinfo")
        _require(mount != authority and authority not in mount.parents, "nested_mount")
    return {"enforcing": True, "mountinfo_sha256": hashlib.sha256(raw).hexdigest()}


def _id(info):
    return info.st_dev, info.st_ino


def _private(info, directory=False):
    _require((stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
             and info.st_uid == os.geteuid()
             and stat.S_IMODE(info.st_mode) == (0o700 if directory else 0o600)
             and (directory or info.st_nlink == 1), "unsafe_private_object")


def _path(value):
    path = Path(value)
    _require(path.is_absolute() and ".." not in path.parts, "unsafe_path")
    return path


def _retain(stack, fd):
    stack.callback(os.close, fd)
    return fd


class _Chain:
    """Held directory chain; stat fields that an ordinary child write changes are not pinned."""
    def __init__(self, path, stack):
        self.path, self.entries = path, []
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        fd = _retain(stack, os.open(path.anchor, flags))
        self.entries.append((path.anchor, fd, os.fstat(fd)))
        for part in path.parts[1:]:
            fd = _retain(stack, os.open(part, flags, dir_fd=fd))
            self.entries.append((part, fd, os.fstat(fd)))
        self.fd = fd
        self.check()

    def check(self):
        parent = None
        for index, (name, fd, original) in enumerate(self.entries):
            info = os.fstat(fd)
            attached = os.stat(name, dir_fd=parent, follow_symlinks=False)
            _require(_id(info) == _id(original) == _id(attached)
                     and stat.S_ISDIR(info.st_mode)
                     and (info.st_uid, info.st_gid, info.st_mode)
                     == (original.st_uid, original.st_gid, original.st_mode), "directory_custody_changed")
            _require(info.st_uid in (0, os.geteuid())
                     and not stat.S_IMODE(info.st_mode) & 0o022, "unsafe_ancestor")
            if index == len(self.entries) - 1:
                _private(info, directory=True)
            parent = fd


def _metadata(fd):
    info = os.fstat(fd)
    directory = stat.S_ISDIR(info.st_mode)
    _private(info, directory)
    return {"device": info.st_dev, "inode": info.st_ino,
            "type": "directory" if directory else "file", "uid": info.st_uid,
            "gid": info.st_gid, "mode": stat.S_IMODE(info.st_mode), "links": info.st_nlink,
            "size": info.st_size, "mtime_ns": info.st_mtime_ns, "ctime_ns": info.st_ctime_ns}


def _context(fd):
    raw = os.getxattr(fd, "security.selinux")
    _require(type(raw) is bytes and 0 < len(raw) <= 256, "invalid_context")
    raw = raw[:-1] if raw.endswith(b"\0") else raw
    _require(raw and b"\0" not in raw, "invalid_context")
    value = raw.decode("ascii")
    parts = value.split(":", 3)
    _require(len(parts) == 4 and parts[0] in ("system_u", "unconfined_u")
             and parts[1] == "object_r" and parts[2] in ("user_home_t", "container_file_t"),
             "invalid_context")
    level = parts[3].split(":")
    _require(level[0] == "s0" and len(level) <= 2, "invalid_context")
    if len(level) == 2:
        _require(bool(re.fullmatch(r"c[0-9]{1,4}(?:\.c[0-9]{1,4})?(?:,c[0-9]{1,4}(?:\.c[0-9]{1,4})?)*", level[1])),
                 "invalid_context")
        previous = -1
        for group in level[1].split(","):
            bounds = [int(item[1:]) for item in group.split(".")]
            _require(previous < bounds[0] <= bounds[-1] <= 1023, "invalid_context")
            previous = bounds[-1]
    return value


def _shared(value):
    return value in (TARGET.decode(), "unconfined_u:object_r:container_file_t:s0")


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        _require(key not in value, "duplicate_receipt_key")
        value[key] = item
    return value


def _observer(fd):
    """Bounded historical provenance, not provider content or relabel authority."""
    before = {**_metadata(fd), "context": _context(fd)}
    _require(before["type"] == "file" and before["size"] <= 4096, "invalid_receipt_size")
    data = os.pread(fd, 4097, 0)
    _require(len(data) == before["size"] and len(data) <= 4096, "receipt_read_changed")
    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_object)
    except (ValueError, UnicodeError, RecursionError):
        raise LabelError("invalid_bootstrap_receipt") from None
    _require(type(value) is dict and value == BOOTSTRAP_ORIGIN
             and all(type(item) is str for item in value.values()), "unknown_bootstrap_origin")
    _require({**_metadata(fd), "context": _context(fd)} == before, "observer_metadata_changed")
    return {**before, "content_sha256": hashlib.sha256(data).hexdigest()}


class _Tree:
    def __init__(self, env_file, stack):
        self.env = _path(env_file)
        self.authority = self.env.parent
        _require(self.env.name == ".env" and self.authority != Path.home()
                 and len(self.authority.parts) > 2, "unsafe_authority")
        self.chain = _Chain(self.authority, stack)
        self.host = _host(self.authority)
        self.objects, self.fds, self.parents = {}, {}, {}
        self.observers, self.observer_fds = {}, {}
        afd = self.chain.fd
        self.fds["."] = afd
        self.parents["."] = (self.chain.entries[-2][1], self.authority.name)
        pfd = _retain(stack, os.open(CACHE, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=afd))
        self.fds[CACHE], self.parents[CACHE] = pfd, (afd, CACHE)
        names = set(os.listdir(afd))
        self.auth_present = "auth.json" in names
        self.names = {".env", ".env.lock", CACHE} | ({"auth.json"} if self.auth_present else set())
        if BOOTSTRAP in names:
            self.names.add(BOOTSTRAP)
        _require(names == self.names and set(os.listdir(pfd)) == {"provider.env", "manifest.json"},
                 "unknown_or_missing_object")
        leaves = [(".env", afd), (".env.lock", afd),
                  (CACHE + "/provider.env", pfd), (CACHE + "/manifest.json", pfd)]
        if self.auth_present:
            leaves.append(("auth.json", afd))
        for name, parent in leaves:
            leaf = name.rsplit("/", 1)[-1]
            _private(os.stat(leaf, dir_fd=parent, follow_symlinks=False))
            fd = _retain(stack, os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent))
            self.fds[name], self.parents[name] = fd, (parent, leaf)
        for name, fd in self.fds.items():
            self.objects[name] = {**_metadata(fd), "context": _context(fd)}
        fcntl.flock(self.fds[".env.lock"], fcntl.LOCK_EX | fcntl.LOCK_NB)
        stack.callback(fcntl.flock, self.fds[".env.lock"], fcntl.LOCK_UN)
        if BOOTSTRAP in names:
            _private(os.stat(BOOTSTRAP, dir_fd=afd, follow_symlinks=False))
            fd = _retain(stack, os.open(BOOTSTRAP, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                       dir_fd=afd))
            self.observer_fds[BOOTSTRAP] = fd
            self.observers[BOOTSTRAP] = _observer(fd)
        self.validate()

    def validate(self):
        self.chain.check()
        _require(_host(self.authority) == self.host, "host_metadata_changed")
        _require(set(os.listdir(self.chain.fd)) == self.names
                 and set(os.listdir(self.fds[CACHE])) == {"provider.env", "manifest.json"},
                 "object_set_changed")
        device = self.objects["."]["device"]
        for name, fd in self.fds.items():
            parent, leaf = self.parents[name]
            attached = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
            expected = self.objects[name]
            _require(_id(attached) == (expected["device"], expected["inode"])
                     and expected["device"] == device, "object_custody_changed")
            _require({**_metadata(fd), "context": _context(fd)} == expected, "object_metadata_changed")
        for name, fd in self.observer_fds.items():
            attached = os.stat(name, dir_fd=self.chain.fd, follow_symlinks=False)
            expected = self.observers[name]
            _require(_id(attached) == (expected["device"], expected["inode"])
                     and expected["device"] == device, "observer_custody_changed")
            _require(_observer(fd) == expected, "observer_metadata_changed")
            _require(_id(os.stat(name, dir_fd=self.chain.fd, follow_symlinks=False))
                     == (expected["device"], expected["inode"]), "observer_custody_changed")

    def preview(self):
        value = {"schema": SCHEMA, "status": "ready", "authority": str(self.authority),
                 "auth_present": self.auth_present, "host": self.host, "target": TARGET.decode(),
                 "objects": [{"id": key, **self.objects[key]} for key in sorted(self.objects)],
                 "observers": [{"id": key, **self.observers[key]} for key in sorted(self.observers)]}
        return {**value, "inventory_sha256": _digest(value)}


def _journal_state(state_dir, tree, stack):
    path = _path(state_dir)
    _require(path != tree.authority and tree.authority not in path.parents, "journal_inside_authority")
    chain = _Chain(path, stack)
    try:
        os.stat(JOURNAL, dir_fd=chain.fd, follow_symlinks=False)
    except FileNotFoundError:
        pass
    else:
        raise LabelError("existing_journal")
    chain.check()
    return chain


class _Journal:
    def __init__(self, chain, stack):
        self.chain = chain
        self.fd = _retain(stack, os.open(JOURNAL, os.O_WRONLY | os.O_APPEND | os.O_CREAT
                                        | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self.chain.fd))
        self.original = os.fstat(self.fd)
        self.size = 0
        self.check()

    def check(self):
        self.chain.check()
        info = os.fstat(self.fd)
        attached = os.stat(JOURNAL, dir_fd=self.chain.fd, follow_symlinks=False)
        _private(info)
        _require(_id(info) == _id(self.original) == _id(attached)
                 and info.st_gid == self.original.st_gid and info.st_size == self.size,
                 "journal_custody_changed")

    def append(self, event, **values):
        self.check()
        data = _json({"schema": SCHEMA, "event": event, **values}) + b"\n"
        _require(self.size + len(data) <= 128 * 1024, "journal_limit")
        count = os.write(self.fd, data)
        _require(count == len(data), "journal_short_write")
        self.size += count
        os.fsync(self.fd)
        self.check()
        if event == "pending":
            os.fsync(self.chain.fd)
            self.check()


@contextmanager
def _inspection(env_file):
    try:
        with ExitStack() as stack:
            yield _Tree(env_file, stack), stack
    except LabelError:
        raise
    except (OSError, ValueError, TypeError, UnicodeError) as error:
        code = getattr(error, "errno", None)
        suffix = str(code) if type(code) is int and 0 <= code <= 4096 else "unknown"
        raise LabelError("metadata_unavailable:errno=" + suffix) from None


def inventory(env_file):
    if not _applicable():
        return {"schema": SCHEMA, "status": "not_applicable"}
    with _inspection(env_file) as (tree, _stack):
        return tree.preview()


def check(env_file):
    if not _applicable():
        return {"schema": SCHEMA, "status": "not_applicable"}
    with _inspection(env_file) as (tree, _stack):
        _require(all(_shared(item["context"]) for item in tree.objects.values()), "shared_label_required")
        return {"schema": SCHEMA, "status": "valid", "inventory_sha256": tree.preview()["inventory_sha256"]}


def apply(env_file, *, state_dir, expected_inventory_sha256):
    if not _applicable():
        return {"schema": SCHEMA, "status": "not_applicable"}
    _require(type(expected_inventory_sha256) is str
             and re.fullmatch("[0-9a-f]{64}", expected_inventory_sha256), "invalid_inventory_hash")
    with _inspection(env_file) as (tree, stack):
        preview = tree.preview()
        _require(preview["inventory_sha256"] == expected_inventory_sha256, "inventory_approval_changed")
        todo = [key for key, value in tree.objects.items() if not _shared(value["context"])]
        todo.sort(key=lambda key: (2 if key == "." else 1 if key == CACHE else 0, key))
        state = _journal_state(state_dir, tree, stack)
        if not todo:
            tree.validate()
            return {"schema": SCHEMA, "status": "unchanged", "changed": []}
        changed = []
        try:
            journal = _Journal(state, stack)
            journal.append("pending", inventory=preview)
            for name in todo:
                tree.validate()
                journal.append("intent", object=name, before=tree.objects[name]["context"], target=TARGET.decode())
                tree.validate()
                journal.check()
                fd = tree.fds[name]
                os.setxattr(fd, "security.selinux", TARGET, flags=os.XATTR_REPLACE)
                changed.append(name)
                after = _metadata(fd)
                expected = tree.objects[name]
                _require({key: value for key, value in after.items() if key != "ctime_ns"}
                         == {key: value for key, value in expected.items() if key not in ("ctime_ns", "context")},
                         "mutation_metadata_changed")
                _require(_context(fd) == TARGET.decode(), "label_readback_changed")
                tree.objects[name] = {**after, "context": TARGET.decode()}
                tree.validate()
                journal.append("result", object=name, ctime_ns=after["ctime_ns"], context=TARGET.decode())
            tree.validate()
            journal.append("complete", changed=changed)
            tree.validate()
            journal.check()
            return {"schema": SCHEMA, "status": "complete", "changed": changed,
                    "inventory_sha256": preview["inventory_sha256"]}
        except (OSError, LabelError, ValueError, TypeError) as error:
            # A journal error may mean a durable record is detached or a write
            # completed without its result receipt. Never append through doubt.
            code = getattr(error, "errno", None)
            suffix = str(code) if type(code) is int and 0 <= code <= 4096 else "unknown"
            raise LabelError(("partial" if changed else "journal_unavailable") + ":errno=" + suffix) from None
