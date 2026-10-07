"""Read-only signed Linux bundle preflight; never extract or execute payloads."""
from __future__ import annotations

import gzip
import hashlib
import math
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tarfile
import time

from . import native_prerequisite as custody

MAX_ARCHIVE_BYTES = 8 * 1024**3
MAX_PAYLOAD_BYTES = 16 * 1024**3
MAX_METADATA_BYTES = 1024**2
MAX_HEADERS = 8192
MAX_FILES = 4096


def _name(value):
    if (not isinstance(value, str) or not value or len(value.encode('utf-8')) > 4096
            or PurePosixPath(value).is_absolute() or PurePosixPath(value).as_posix() != value
            or any(part in ('', '.', '..') for part in value.split('/'))
            or '\\' in value or ':' in value or any(ord(c) < 32 for c in value)):
        raise ValueError
    return value


def _payload(stream, release, check):
    headers = 0

    class BoundedInfo(tarfile.TarInfo):
        # Bound pinned CPython's header seam before PAX/longname allocation.
        def _proc_member(self, archive):
            nonlocal headers
            headers += 1
            check()
            if headers > MAX_HEADERS or type(self.size) is not int or not 0 <= self.size <= MAX_PAYLOAD_BYTES:
                raise ValueError
            if self.type in (tarfile.XHDTYPE, tarfile.XGLTYPE, tarfile.SOLARIS_XHDTYPE,
                              tarfile.GNUTYPE_LONGNAME, tarfile.GNUTYPE_LONGLINK) and self.size > MAX_METADATA_BYTES:
                raise ValueError
            return super()._proc_member(archive)

        def _refuse_sparse(self, *args):
            raise ValueError
        _proc_sparse = _refuse_sparse
        _proc_gnusparse_00 = _refuse_sparse
        _proc_gnusparse_01 = _refuse_sparse
        _proc_gnusparse_10 = _refuse_sparse

    class BoundedGzip:
        total = 0
        def read(self, count):
            check()
            if not 0 <= count <= 65536:
                raise ValueError
            value = stream.read(count)
            self.total += len(value)
            if self.total > MAX_PAYLOAD_BYTES:
                raise ValueError
            check()
            return value

    files, directories, modes, metadata = {}, set(), {}, {}
    root = None
    with tarfile.open(fileobj=BoundedGzip(), mode='r|', tarinfo=BoundedInfo,
                      encoding='utf-8', errors='strict') as archive:
        for entry in archive:
            check()
            full = _name(entry.name)
            prefix, _, name = full.partition('/')
            if root is None: root = prefix
            if prefix != root or entry.mode & ~0o777 or entry.mode & 0o022 or entry.sparse is not None:
                raise ValueError
            if full in directories or name in files:
                raise ValueError
            if entry.type == tarfile.DIRTYPE:
                if entry.size != 0: raise ValueError
                directories.add(full)
                continue
            if entry.type not in (tarfile.REGTYPE, tarfile.AREGTYPE) or not name:
                raise ValueError
            if len(files) >= MAX_FILES+2 or not 0 <= entry.size <= MAX_PAYLOAD_BYTES:
                raise ValueError
            if name in ('release.json','SHA256SUMS') and entry.size > MAX_METADATA_BYTES:
                raise ValueError
            digest = hashlib.sha256()
            saved = bytearray() if name in ('release.json','SHA256SUMS') else None
            actual = 0
            with archive.extractfile(entry) as member:
                while True:
                    check(); chunk = member.read(65536); check()
                    if not chunk: break
                    actual += len(chunk); digest.update(chunk)
                    if saved is not None: saved.extend(chunk)
            if actual != entry.size: raise ValueError
            files[name] = digest.hexdigest(); modes[name] = entry.mode
            if saved is not None: metadata[name] = bytes(saved)
        # Finish the decompressed stream, including gzip CRC/EOF. A second
        # hidden tar or nonzero trailing payload is not permitted.
        padding = 0
        while True:
            check(); tail = archive.fileobj.read(65536); check()
            if not tail: break
            padding += len(tail)
            if padding > MAX_METADATA_BYTES or tail.strip(b'\0'): raise ValueError

    raw = metadata['release.json']
    if hashlib.sha256(raw).hexdigest() != release['payload_manifest_sha256']:
        raise custody.PrerequisiteRefusal('cortex_release_signature_invalid')
    inner = custody.strict_json(raw, limit=MAX_METADATA_BYTES)
    if (inner.get('schema') != 'cortex.test-package.v1' or inner.get('target') != release['target']
            or inner.get('deployment_class') != release['deployment_class']
            or inner.get('source_sha') != release['source_revision']
            or not isinstance(inner.get('files'), dict) or not 1 <= len(inner['files']) <= MAX_FILES
            or not isinstance(inner.get('images'), dict) or set(inner['images']) != set(custody.ROLES)):
        raise ValueError
    payload = inner['files']
    for name,digest in payload.items():
        _name(name)
        if name in ('release.json','SHA256SUMS') or not isinstance(digest,str) or not custody.HEX.fullmatch(digest):
            raise ValueError
    optional = {'SHA256SUMS'} if 'SHA256SUMS' in files else set()
    if set(files) != set(payload) | {'release.json'} | optional:
        raise ValueError
    expected_dirs = {root}
    expected_dirs.update(root+'/'+str(parent) for name in files for parent in PurePosixPath(name).parents if str(parent) != '.')
    if directories != expected_dirs or any(files[n] != digest for n,digest in payload.items()):
        raise ValueError
    if any(payload.get(n) != digest for n,digest in release['files'].items()):
        raise ValueError
    for name in ('bin/cortex','bin/cortex-agent') + (('bin/cortex-boot',) if 'bin/cortex-boot' in payload else ()):
        if name not in payload or not modes[name] & 0o100: raise ValueError
    for role in custody.ROLES:
        image = inner['images'][role]
        if (not isinstance(image,dict) or any(image.get(k) != v for k,v in release['images'][role].items())
                or payload.get(image['archive']) != image['archive_sha256']):
            raise ValueError
    if optional:
        sums = dict(payload, **{'release.json': release['payload_manifest_sha256']})
        expected = ''.join(f'{digest}  {name}\n' for name,digest in sorted(sums.items())).encode()
        if metadata['SHA256SUMS'] != expected: raise ValueError
    check()
    return inner, root


def inspect_linux_bundle(bundle_root: Path, *, deadline: float | None = None) -> dict:
    """Authenticate and inspect actual held archive bytes without installation.

    The result describes this observation only. A later installer must bind its
    staging reads and reverify the complete staged runtime before any activation.
    """
    descriptors, parents, changing_parents, inputs = [], {}, {}, {}
    now = time.monotonic()
    if deadline is None: deadline = now+30
    if type(deadline) not in (int,float) or not math.isfinite(deadline):
        raise custody.PrerequisiteRefusal('cortex_health_unavailable')
    deadline = min(deadline,now+30)

    def identity(info): return custody._file_identity(info),info.st_ctime_ns
    def check():
        if time.monotonic() >= deadline:
            raise custody.PrerequisiteRefusal('cortex_health_unavailable')
        custody._recheck_parents(parents)
        for path,expected in changing_parents.items():
            info=path.lstat()
            if (custody._directory_identity(info),info.st_ctime_ns) != expected: raise ValueError
        for path,(fd,expected) in inputs.items():
            if identity(path.lstat()) != expected or identity(os.fstat(fd)) != expected: raise ValueError
        if time.monotonic() >= deadline:
            raise custody.PrerequisiteRefusal('cortex_health_unavailable')

    def hold(path,limit):
        parents.update(custody._parents(path))
        for parent in path.parents:
            info=parent.lstat()
            if info.st_uid==os.getuid():
                expected=(custody._directory_identity(info),info.st_ctime_ns)
                if parent in changing_parents and changing_parents[parent]!=expected:raise ValueError
                changing_parents[parent]=expected
        before=path.lstat();custody._private_file(before)
        if not 0 < before.st_size <= limit: raise ValueError
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK);descriptors.append(fd)
        opened=os.fstat(fd);custody._private_file(opened)
        if identity(opened)!=identity(before):raise ValueError
        inputs[path]=(fd,identity(opened));check()
        return fd

    try:
        check();custody.physical_path(bundle_root)
        manifest=bundle_root/'release.json';signature=bundle_root/'release.json.minisig'
        fd=hold(manifest,MAX_METADATA_BYTES)
        raw=os.read(fd,MAX_METADATA_BYTES+1);check()
        candidate=custody.strict_json(raw,limit=MAX_METADATA_BYTES)
        name=candidate['archive']['name']
        if not isinstance(name,str) or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}',name) is None:
            raise ValueError
        if name in ('release.json','release.json.minisig'):raise ValueError
        hold(signature,4096);archive_fd=hold(bundle_root/name,MAX_ARCHIVE_BYTES)
        if {p.name for p in bundle_root.iterdir()} != {'release.json','release.json.minisig',name}:raise ValueError
        check();custody.verify_signature(manifest,signature,timeout=min(5,deadline-time.monotonic()));check()
        release=custody.validate_release_manifest(candidate,target='linux-x86_64')
        if os.fstat(archive_fd).st_size != release['archive']['size_bytes']:raise ValueError
        digest=hashlib.sha256()
        while True:
            check();chunk=os.read(archive_fd,65536);check()
            if not chunk:break
            digest.update(chunk)
        if digest.hexdigest()!=release['archive']['sha256']:
            raise custody.PrerequisiteRefusal('cortex_release_signature_invalid')
        os.lseek(archive_fd,0,os.SEEK_SET)
        with os.fdopen(os.dup(archive_fd),'rb') as held, gzip.GzipFile(fileobj=held,mode='rb') as compressed:
            inner,archive_root=_payload(compressed,release,check)
        check()
        return {'release':release,'payload_manifest':inner,'archive_root':archive_root,
                'release_manifest_sha256':hashlib.sha256(raw).hexdigest()}
    except custody.PrerequisiteRefusal:
        raise
    except (OSError,ValueError,TypeError,KeyError,AttributeError,OverflowError,EOFError,tarfile.TarError,RecursionError):
        raise custody.PrerequisiteRefusal('cortex_descriptor_invalid') from None
    finally:
        failed=False
        for fd in descriptors:
            try:os.close(fd)
            except OSError:failed=True
        if failed:raise custody.PrerequisiteRefusal('cortex_descriptor_invalid') from None
