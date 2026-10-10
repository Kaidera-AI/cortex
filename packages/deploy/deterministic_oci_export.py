"""Create a new deterministic USTAR archive directly from a physical OCI directory."""
import hashlib
import json
from pathlib import Path
import stat
import tarfile
import re

EPOCH = 1791586380


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def inventory(root):
    if root.is_symlink() or not root.is_dir():
        raise ValueError('physical OCI directory required')
    rows = []
    for path in sorted(root.rglob('*'), key=lambda p: p.relative_to(root).as_posix()):
        st = path.lstat()
        if not (stat.S_ISREG(st.st_mode) or stat.S_ISDIR(st.st_mode)):
            raise ValueError('OCI export symlink/special entry refused')
        name = path.relative_to(root).as_posix()
        row = {'name': name, 'type': 'directory' if path.is_dir() else 'file',
               'mode': stat.S_IMODE(st.st_mode), 'size': st.st_size if path.is_file() else 0,
               'sha256': digest(path) if path.is_file() else None}
        if path.is_file() and name.startswith('blobs/sha256/'):
            if row['sha256'] != path.name:
                raise ValueError('OCI blob digest differs')
        rows.append(row)
    if not (root/'oci-layout').is_file() or not (root/'index.json').is_file():
        raise ValueError('OCI layout/index required')
    if json.loads((root/'oci-layout').read_bytes()).get('imageLayoutVersion') != '1.0.0':
        raise ValueError('OCI layout version differs')
    index = json.loads((root/'index.json').read_bytes())
    if index.get('schemaVersion') != 2 or not isinstance(index.get('manifests'), list):
        raise ValueError('OCI index differs')
    def descriptor(row):
        value = row.get('digest', '')
        if re.fullmatch(r'sha256:[0-9a-f]{64}', value) is None:
            raise ValueError('OCI descriptor digest required')
        path = root/'blobs/sha256'/value.split(':')[1]
        if not path.is_file() or path.stat().st_size != row.get('size') or digest(path) != value.split(':')[1]:
            raise ValueError('OCI descriptor bytes/size differ')
        return path
    for row in index['manifests']:
        manifest = json.loads(descriptor(row).read_bytes())
        descriptor(manifest['config'])
        for layer in manifest['layers']:
            descriptor(layer)
    return rows


def verify_archive(root, archive, expected=None):
    rows = inventory(root)
    if expected is not None and rows != expected:
        raise ValueError('OCI directory changed during export')
    actual = []
    with tarfile.open(archive, 'r:') as tar:
        for member in tar:
            if (member.mtime != EPOCH or member.uid != 0 or member.gid != 0 or
                    member.uname != '' or member.gname != '' or member.pax_headers or
                    member.linkname or not (member.isfile() or member.isdir())):
                raise ValueError('non-deterministic OCI outer header')
            raw_hash = None
            if member.isfile():
                with tar.extractfile(member) as stream:
                    raw_hash = hashlib.file_digest(stream, 'sha256').hexdigest()
            actual.append({'name': member.name.rstrip('/'),
                           'type': 'directory' if member.isdir() else 'file',
                           'mode': member.mode, 'size': member.size, 'sha256': raw_hash})
    if actual != rows:
        raise ValueError('OCI archive order/members/modes/sizes/content differ from directory')
    return {'result': 'PASS', 'format': 'USTAR', 'epoch': EPOCH, 'members': rows,
            'archive_sha256': digest(archive), 'archive_size': archive.stat().st_size}


def write_archive(root, archive):
    root, archive = Path(root), Path(archive)
    rows = inventory(root)
    if root.resolve() == archive.resolve() or root.resolve() in archive.resolve().parents:
        raise ValueError('archive must be outside OCI directory')
    with archive.open('xb') as output:
        with tarfile.open(fileobj=output, mode='w', format=tarfile.USTAR_FORMAT) as tar:
            for row in rows:
                member = tarfile.TarInfo(row['name'])
                member.type = tarfile.DIRTYPE if row['type'] == 'directory' else tarfile.REGTYPE
                member.size, member.mode = row['size'], row['mode']
                member.mtime = EPOCH
                member.uid = member.gid = 0
                member.uname = member.gname = ''
                if row['type'] == 'file':
                    with (root/row['name']).open('rb') as stream:
                        tar.addfile(member, stream)
                else:
                    tar.addfile(member)
    return verify_archive(root, archive, rows)
