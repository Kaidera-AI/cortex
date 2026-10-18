"""S5 install record: the ONE writer/reader of `<prefix>/install-record.json` (the locator/ownership record of the objects the installer made; NOT custody, NOT a secret store, NOT permission).
Extracted unchanged in schema from install.sh's former embedded writer; install.sh `record_object` and Bob's later lifecycle consumer both go through this module, so no second writer or parser exists.
Standard library only (explicit import closure). Schema: {"release_id": str, "objects": [{"kind","name","id"}, ...]}, objects sorted by name, written as json.dumps(sort_keys=True, indent=1) + "\\n".
  record_object(prefix, release_id, kind, name, ident, *, owner_uid=None) -> None
  forget_object(prefix, name, *, owner_uid=None) -> bool   (True = removed, False = not in the record, nothing written)
  read_record(prefix, *, owner_uid=None) -> dict | None     (None = no record yet)
  CLI (used by install.sh): record --prefix P --release-id R --kind K --name N --id I | forget --prefix P --name N | read --prefix P
Ownership rules: the prefix is walked from / without following any symlink; prefix and record belong to the effective uid (or the injected expected uid), carry no group/other bits, the record has one
link and at most 1 MiB; an existing record of ANOTHER release_id is never overwritten; a malformed record is refused before any effect; publication is a same-directory 0600 temp file, fsync and
rename under one flock on the prefix, and any failure leaves the previous record and no temp file. Refusals are typed (RecordRefusal.reason): record_input, record_path_unsafe, record_owner,
record_private, record_invalid, record_unreadable, record_foreign_release, record_write_failed."""
import fcntl
import json
import os
import stat
import sys
import uuid
from pathlib import PurePosixPath

FILE = 'install-record.json'
MAX_BYTES = 1 << 20
MAX_OBJECTS = 4096
MAX_FIELD = 512
_FLAGS = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY


class RecordRefusal(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def _field(value, reason='record_input'):
    if type(value) is not str or not 1 <= len(value) <= MAX_FIELD or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise RecordRefusal(reason)
    return value


def _owner(owner_uid):
    if owner_uid is None:
        return os.geteuid()
    if type(owner_uid) is not int or owner_uid < 0:
        raise RecordRefusal('record_input')
    return owner_uid


def _open_prefix(prefix, owner, *, must_exist):
    """No-follow descent from / to the prefix directory; -> fd, or None when it does not exist and may be absent."""
    if (type(prefix) is not str or not prefix.startswith('/') or prefix.startswith('//') or prefix == '/' or str(PurePosixPath(prefix)) != prefix
            or {'.', '..'} & set(PurePosixPath(prefix).parts)):
        raise RecordRefusal('record_input')
    fd = os.open('/', _FLAGS)
    try:
        for part in PurePosixPath(prefix).parts[1:]:
            try:
                nxt = os.open(part, _FLAGS, dir_fd=fd)
            except FileNotFoundError:
                if must_exist:
                    raise RecordRefusal('record_input') from None
                os.close(fd)
                return None
            except OSError:
                raise RecordRefusal('record_path_unsafe') from None
            os.close(fd)
            fd = nxt
        st = os.fstat(fd)
        if st.st_uid != owner:
            raise RecordRefusal('record_owner')
        if st.st_mode & 0o077:
            raise RecordRefusal('record_private')
        return fd
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


def _no_duplicates(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError('duplicate key')
        out[key] = value
    return out


def _refuse_constant(_name):
    raise ValueError('non-finite number')


def _validate(doc):
    if type(doc) is not dict or set(doc) != {'release_id', 'objects'}:
        raise RecordRefusal('record_invalid')
    _field(doc['release_id'], 'record_invalid')
    objects = doc['objects']
    if type(objects) is not list or len(objects) > MAX_OBJECTS:
        raise RecordRefusal('record_invalid')
    names = set()
    for entry in objects:
        if type(entry) is not dict or set(entry) != {'kind', 'name', 'id'}:
            raise RecordRefusal('record_invalid')
        for key in ('kind', 'name', 'id'):
            _field(entry[key], 'record_invalid')
        if entry['name'] in names:
            raise RecordRefusal('record_invalid')
        names.add(entry['name'])
    return doc


def _read(dirfd, owner):
    """-> (raw bytes, validated doc) or (None, None) when there is no record."""
    try:
        fd = os.open(FILE, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dirfd)
    except FileNotFoundError:
        return None, None
    except OSError:
        raise RecordRefusal('record_path_unsafe') from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise RecordRefusal('record_path_unsafe')
        if st.st_uid != owner:
            raise RecordRefusal('record_owner')
        if st.st_mode & 0o077 or st.st_nlink != 1:
            raise RecordRefusal('record_private')
        if st.st_size > MAX_BYTES:
            raise RecordRefusal('record_invalid')
        raw = bytearray()
        while len(raw) <= MAX_BYTES:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            raw.extend(chunk)
    except OSError:
        raise RecordRefusal('record_unreadable') from None
    finally:
        os.close(fd)
    if len(raw) > MAX_BYTES:
        raise RecordRefusal('record_invalid')
    try:
        doc = json.loads(bytes(raw).decode('utf-8'), object_pairs_hook=_no_duplicates, parse_constant=_refuse_constant)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise RecordRefusal('record_invalid') from None
    return bytes(raw), _validate(doc)


def _encode(doc):
    doc = dict(doc, objects=sorted(doc['objects'], key=lambda o: o['name']))
    return (json.dumps(doc, sort_keys=True, indent=1) + '\n').encode()


def _publish(dirfd, raw):
    tmp = f'.{FILE}.{uuid.uuid4().hex}'
    created = False
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=dirfd)
        created = True
        try:
            os.fchmod(fd, 0o600)
            view = memoryview(raw)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.rename(tmp, FILE, src_dir_fd=dirfd, dst_dir_fd=dirfd)
        created = False
        os.fsync(dirfd)
    except OSError:
        raise RecordRefusal('record_write_failed') from None
    finally:
        if created:
            try:
                os.unlink(tmp, dir_fd=dirfd)
            except OSError:
                pass


def record_object(prefix, release_id, kind, name, ident, *, owner_uid=None):
    for value in (release_id, kind, name, ident):
        _field(value)
    owner = _owner(owner_uid)
    dirfd = _open_prefix(prefix, owner, must_exist=True)
    try:
        fcntl.flock(dirfd, fcntl.LOCK_EX)
        raw, doc = _read(dirfd, owner)
        if doc is not None and doc['release_id'] != release_id:
            raise RecordRefusal('record_foreign_release')   # never overwrite another installation's ownership
        doc = doc or {'release_id': release_id, 'objects': []}
        doc = dict(doc, objects=[o for o in doc['objects'] if o['name'] != name] + [{'kind': kind, 'name': name, 'id': ident}])
        new = _encode(doc)
        if new != raw:
            _publish(dirfd, new)
    finally:
        os.close(dirfd)


def forget_object(prefix, name, *, owner_uid=None):
    _field(name)
    owner = _owner(owner_uid)
    dirfd = _open_prefix(prefix, owner, must_exist=False)
    if dirfd is None:
        return False
    try:
        fcntl.flock(dirfd, fcntl.LOCK_EX)
        raw, doc = _read(dirfd, owner)
        if doc is None or all(o['name'] != name for o in doc['objects']):
            return False
        _publish(dirfd, _encode(dict(doc, objects=[o for o in doc['objects'] if o['name'] != name])))
        return True
    finally:
        os.close(dirfd)


def read_record(prefix, *, owner_uid=None):
    owner = _owner(owner_uid)
    dirfd = _open_prefix(prefix, owner, must_exist=False)
    if dirfd is None:
        return None
    try:
        return _read(dirfd, owner)[1]
    finally:
        os.close(dirfd)


def main(argv):
    flags = {'record': ('--prefix', '--release-id', '--kind', '--name', '--id'), 'forget': ('--prefix', '--name'), 'read': ('--prefix',)}
    try:
        if not argv or argv[0] not in flags:
            raise RecordRefusal('record_input')
        verb, values, pending = argv[0], {}, list(argv[1:])
        while pending:
            name = pending.pop(0)
            if name not in flags[verb] or name in values or not pending:
                raise RecordRefusal('record_input')
            values[name] = pending.pop(0)
        if set(values) != set(flags[verb]):
            raise RecordRefusal('record_input')
        if verb == 'record':
            record_object(values['--prefix'], values['--release-id'], values['--kind'], values['--name'], values['--id'])
        elif verb == 'forget':
            print(json.dumps({'forgotten': forget_object(values['--prefix'], values['--name'])}))
        else:
            print(json.dumps(read_record(values['--prefix']), sort_keys=True))
    except RecordRefusal as refusal:
        print(json.dumps({'reason': refusal.reason, 'unit': 'record'}), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
