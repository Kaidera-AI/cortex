"""Assemble public CM-2 producer artifacts; never sign, execute or install."""
from __future__ import annotations

import gzip
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tarfile
import time
import zipfile

from linux_build_catalog import (_source_inputs, _source_snapshot, _source_stamp,
                                 physical_path, read_json, rehearsal_catalog_bytes)
from podman_policy import POLICY
from cortex_v2.clients import linux_bundle, native_prerequisite as custody

MESSAGE = 'CM-2 assembly inputs missing or mismatched'


def _json(value):
    raw = json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode() + b'\n'
    if len(raw) > linux_bundle.MAX_METADATA_BYTES:
        raise ValueError
    return raw


class _Inputs:
    def __init__(self, roots):
        self.roots, self.fds = roots, {}
        self.deadline = time.monotonic() + 300
        try:
            self.before = self.scan()
            for path, expected in self.before.items():
                if stat.S_ISREG(expected[3]):
                    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                    self.fds[path] = fd
                    if _source_stamp(os.fstat(fd)) != expected:
                        raise ValueError
            self.check()
        except BaseException:
            self.close()
            raise

    def tick(self):
        if time.monotonic() >= self.deadline:
            raise ValueError

    def scan(self):
        result, pending, total = {}, list(self.roots), 0
        while pending:
            self.tick()
            path = pending.pop()
            physical_path(path)
            info = path.lstat()
            if (info.st_uid != os.getuid() or info.st_mode & 0o7022
                    or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode))):
                raise ValueError
            if stat.S_ISREG(info.st_mode):
                if info.st_nlink != 1 or info.st_size > linux_bundle.MAX_ARCHIVE_BYTES:
                    raise ValueError
                total += info.st_size
            else:
                pending.extend(path.iterdir())
            result[path] = _source_stamp(info)
            if len(result) > linux_bundle.MAX_HEADERS or total > linux_bundle.MAX_PAYLOAD_BYTES:
                raise ValueError
        return result

    def check(self):
        self.tick()
        if self.scan() != self.before:
            raise ValueError
        if any(_source_stamp(os.fstat(fd)) != self.before[path] for path, fd in self.fds.items()):
            raise ValueError

    def chunks(self, path):
        self.check()
        fd = self.fds[path]
        os.lseek(fd, 0, os.SEEK_SET)
        total = 0
        while True:
            self.tick()
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            total += len(chunk)
            if total > self.before[path][5]:
                raise ValueError
            yield chunk
        if total != self.before[path][5]:
            raise ValueError
        self.check()

    @contextmanager
    def stream(self, path):
        self.check()
        os.lseek(self.fds[path], 0, os.SEEK_SET)
        with os.fdopen(os.dup(self.fds[path]), 'rb') as stream:
            yield stream
        self.check()

    def namespace(self, directory, names):
        expected = {directory}
        for name in names:
            expected.update(p for p in (directory / name).parents if p.is_relative_to(directory))
        actual = {p for p, info in self.before.items()
                  if p.is_relative_to(directory) and stat.S_ISDIR(info[3])}
        if actual != expected:
            raise ValueError

    def digest(self, path):
        value = hashlib.sha256()
        for chunk in self.chunks(path):
            value.update(chunk)
        return value.hexdigest()

    def close(self):
        for fd in self.fds.values():
            os.close(fd)
        self.fds.clear()


def _write(path, raw, mode=0o600):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(descriptor, 'wb') as stream:
        os.fchmod(stream.fileno(), mode)
        if stream.write(raw) != len(raw):
            raise ValueError


def _assemble(out, images_dir, host_dir, reader_dir, *, root, source_sha, version,
              release_sequence, expected_bootstrap, oci_identity):
    if (type(release_sequence) is not int or release_sequence < 1
            or not isinstance(source_sha, str) or re.fullmatch(r'[0-9a-f]{40}', source_sha) is None
            or not isinstance(version, str) or re.fullmatch(r'0\.2\.001-test\.[0-9]{8}\.[1-9][0-9]*', version) is None
            or os.path.lexists(out) or any(out.is_relative_to(p) for p in (root, images_dir, host_dir, reader_dir))):
        raise ValueError
    physical_path(out.parent)
    parent = out.parent.lstat()
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid() or parent.st_mode & 0o7022:
        raise ValueError
    payloads, source_observation = _source_snapshot(root)
    lock = root / 'scripts/release/requirements-linux-host-build.txt'
    inputs = _Inputs((images_dir, host_dir, reader_dir, root / 'LICENSE', lock))
    try:
        inventory = read_json(images_dir / 'image-inventory.json')
        native = read_json(host_dir / 'host-inventory.json')
        sources = read_json(images_dir / 'source-payload-inventory.json')
        rehearsal = read_json(images_dir / 'rehearsal-receipt.json')
        reader = read_json(reader_dir / 'reader.json')
        for receipt in (inventory, native):
            if (receipt.get('source_sha') != source_sha or receipt.get('version') != version
                    or receipt.get('target') != 'linux-x86_64'):
                raise ValueError
        if sources != {'schema': 'cortex.image-source-payloads.v1', 'source_revision': source_sha, 'roles': payloads}:
            raise ValueError
        entries = inventory['images']
        if set(entries) != set(custody.ROLES):
            raise ValueError
        image_files = {'image-inventory.json', 'source-payload-inventory.json', 'rehearsal-receipt.json'}
        mapping = {root / 'LICENSE': 'LICENSE', lock: 'sbom/host-build-lock.txt'}
        for role, entry in entries.items():
            name = 'images/' + role + '.oci.tar'
            if (entry.get('archive') != name or entry.get('source_payload_sha256') != payloads[role]['sha256']
                    or entry.get('archive_sha256') != inputs.digest(images_dir / name)):
                raise ValueError
            with inputs.stream(images_dir / name) as stream:
                actual = oci_identity(images_dir / name, architecture='amd64', source_sha=source_sha,
                                      version=version, fileobj=stream)
            if any(entry.get(k) != v for k, v in actual.items()):
                raise ValueError
            notice = 'sbom/' + role + '.spdx.json'
            if read_json(images_dir / notice).get('spdxVersion') != 'SPDX-2.3':
                raise ValueError
            mapping[images_dir / name] = name
            mapping[images_dir / notice] = notice
            image_files.update((name, notice))
        if {p.relative_to(images_dir).as_posix() for p in inputs.fds if p.is_relative_to(images_dir)} != image_files:
            raise ValueError
        inputs.namespace(images_dir, image_files)
        catalog = rehearsal_catalog_bytes(rehearsal, entries, source_sha=source_sha, version=version,
                                           source_root=root / 'src', migration_root=root / 'migrations')
        programs = native['programs']
        bootstrap = native['native_runtime_bootstrap']
        if (native.get('host_contract') != 'linux-cm2' or set(programs) != {'cortex', 'cortex-agent'}
                or native.get('maximum_glibc') != '2.35' or native.get('python') != '3.12.14'
                or native.get('build_lock_sha256') != inputs.digest(lock)
                or bootstrap.get('schema') != 'cortex.native-ci-runtime.v1'
                or bootstrap.get('inputs') != expected_bootstrap or bootstrap.get('runtime', {}).get('python') != '3.12.14'):
            raise ValueError
        host_files = {'host-inventory.json', 'host.spdx.json'}
        for name, receipt in programs.items():
            listing = 'cm2-host-archive-inventory.txt' if name == 'cortex' else 'agent-archive-inventory.txt'
            if (receipt.get('sha256') != inputs.digest(host_dir / 'bin' / name)
                    or receipt.get('architecture') != 'x86_64' or receipt.get('help') != 'PASS'
                    or receipt.get('maximum_glibc') != '2.35'
                    or type(receipt.get('embedded_elf_count')) is not int or receipt['embedded_elf_count'] < 1
                    or receipt.get('archive_inventory') != listing
                    or not (host_dir / 'bin' / name).lstat().st_mode & 0o100
                    or not 0 < (host_dir / listing).lstat().st_size <= 16 * 1024**2):
                raise ValueError
            mapping[host_dir / 'bin' / name] = 'bin/' + name
            mapping[host_dir / listing] = 'sbom/' + listing
            host_files.update(('bin/' + name, listing))
        if read_json(host_dir / 'host.spdx.json').get('spdxVersion') != 'SPDX-2.3':
            raise ValueError
        licenses = list((host_dir / 'licenses').iterdir())
        if not licenses:
            raise ValueError
        for path in licenses:
            custody_name = linux_bundle._name('licenses/' + path.name)
            if path not in inputs.fds or not 0 < path.lstat().st_size <= 8 * 1024**2:
                raise ValueError
            mapping[path] = custody_name
            host_files.add(custody_name)
        for name, digest in bootstrap['licenses'].items():
            if name != Path(name).name or inputs.digest(host_dir / 'licenses' / name) != digest:
                raise ValueError
        if {p.relative_to(host_dir).as_posix() for p in inputs.fds if p.is_relative_to(host_dir)} != host_files:
            raise ValueError
        inputs.namespace(host_dir, host_files)
        for directory, names in ((images_dir, image_files), (host_dir, host_files)):
            for name in names:
                mapping.setdefault(directory / name, 'sbom/' + name if '/' not in name else name)
        archive_name = 'cortex-key-reader-' + source_sha + '.zip'
        reader_archive = reader_dir / archive_name
        expected_reader_files = {name.removeprefix('src/'): value for name, value in payloads['api']['files'].items()
                                 if name.removeprefix('src/') in reader.get('files', {})}
        if (reader.get('schema') != 'cortex.member-reader.v1' or reader.get('source_sha') != source_sha
                or reader.get('archive') != archive_name or reader.get('python') != '3.12'
                or reader.get('sha256') != custody.READER_SHA256 or inputs.digest(reader_archive) != custody.READER_SHA256
                or len(expected_reader_files) != 6 or reader['files'] != expected_reader_files
                or {p.name for p in reader_dir.iterdir()} != {'reader.json', archive_name}):
            raise ValueError
        inputs.namespace(reader_dir, {'reader.json', archive_name})
        with inputs.stream(reader_archive) as stream, zipfile.ZipFile(stream) as archive:
            members = archive.infolist()
            if (len(members) != 6 or {p.filename for p in members} != set(reader['files'])
                    or sum(p.file_size for p in members) > linux_bundle.MAX_METADATA_BYTES
                    or any(hashlib.sha256(archive.read(p)).hexdigest() != reader['files'][p.filename] for p in members)):
                raise ValueError
        mapping[reader_archive] = 'native/member-reader.zip'
        mapping[reader_dir / 'reader.json'] = 'sbom/member-reader.json'
        inputs.check()
        if _source_snapshot(root) != (payloads, source_observation):
            raise ValueError
        out.mkdir(mode=0o700)
        unsigned = out / 'unsigned'; unsigned.mkdir(mode=0o700)
        package = out / ('cortex-' + version + '-linux-x86_64'); package.mkdir(mode=0o700)
        files = {}
        for source, name in sorted(mapping.items(), key=lambda row: row[1]):
            destination = package / name
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            mode = 0o755 if name in ('bin/cortex', 'bin/cortex-agent') else 0o600
            fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
            digest = hashlib.sha256()
            with os.fdopen(fd, 'wb') as stream:
                os.fchmod(stream.fileno(), mode)
                for chunk in inputs.chunks(source):
                    digest.update(chunk)
                    if stream.write(chunk) != len(chunk):
                        raise ValueError
            files[name] = digest.hexdigest()
        _write(package / 'native/rls-inventory.json', catalog)
        files['native/rls-inventory.json'] = hashlib.sha256(catalog).hexdigest()
        inner = {'schema': 'cortex.test-package.v1', 'deployment_class': 'TEST', 'target': 'linux-x86_64',
                 'source_sha': source_sha, 'version': version, 'files': files, 'images': entries,
                 'builder': inventory['builder'], 'signature': 'unsigned TEST; not a signed customer release'}
        inner_raw = _json(inner); inner_hash = hashlib.sha256(inner_raw).hexdigest()
        _write(package / 'release.json', inner_raw)
        sums = dict(files, **{'release.json': inner_hash})
        _write(package / 'SHA256SUMS', ''.join(f'{digest}  {name}\n' for name, digest in sorted(sums.items())).encode())
        archive = unsigned / (package.name + '.tar.gz')
        fd = os.open(archive, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), 0o600)
            staged = _Inputs((package,))
            try:
                with gzip.GzipFile(filename='', fileobj=stream, mode='wb', mtime=0) as zipped:
                    with tarfile.open(fileobj=zipped, mode='w', format=tarfile.PAX_FORMAT) as tar:
                        for path in [package] + sorted(p for p in staged.before if p != package):
                            staged.check()
                            observed = staged.before[path]
                            member = tarfile.TarInfo(package.name if path == package else package.name + '/' + path.relative_to(package).as_posix())
                            member.mode = stat.S_IMODE(observed[3])
                            if stat.S_ISDIR(observed[3]):
                                member.type = tarfile.DIRTYPE; tar.addfile(member)
                            else:
                                member.size = observed[5]
                                with staged.stream(path) as source:
                                    tar.addfile(member, source)
                            staged.check()
                staged.check()
            finally:
                staged.close()
        inputs.check()
        if _source_snapshot(root) != (payloads, source_observation):
            raise ValueError
        retained = _Inputs((archive,))
        try:
            archive_hash = retained.digest(archive)
            value = {'schema': 'cortex.release.v2', 'release_id': version, 'release_lineage': 'cortex-v2-native',
                 'release_sequence': release_sequence, 'api_contract': 'cortex-kos-v02009.v2',
                 'source_revision': source_sha, 'deployment_class': 'TEST', 'target': 'linux-x86_64',
                 'archive': {'name': archive.name, 'sha256': archive_hash, 'size_bytes': retained.before[archive][5]},
                 'payload_manifest_sha256': inner_hash, 'files': files, 'images': entries,
                 'migrations': [{'name': n, 'sha256': d} for n, d in sorted(json.loads(catalog)['migrations'].items())],
                 'rls_inventory': {'sha256': files['native/rls-inventory.json']},
                 'podman': dict(POLICY, provider='cortex-native-lifecycle'), 'member_reader_archive_sha256': custody.READER_SHA256}
            custody.validate_release_manifest(value, target='linux-x86_64')
            with retained.stream(archive) as stream, gzip.GzipFile(fileobj=stream, mode='rb') as zipped:
                measured, archive_root = linux_bundle._payload(zipped, value, inputs.tick)
            if measured != inner or archive_root != package.name:
                raise ValueError
            inputs.check(); retained.check()
            if _source_inputs(root) != source_observation:
                raise ValueError
            _write(unsigned / 'release.json', _json(value))
            return value
        finally:
            retained.close()
    finally:
        inputs.close()


def assemble(*args, **kwargs):
    try:
        return _assemble(*args, **kwargs)
    except Exception:
        raise RuntimeError(MESSAGE) from None
