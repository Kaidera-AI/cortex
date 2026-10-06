"""Inspect literal native ELF and PyInstaller payload bytes before executing help."""
from pathlib import Path, PurePosixPath
import hashlib
import io
import re
import stat
import struct
import tempfile
import zipfile
import zlib

MAX_BINARY = 512 * 1024 * 1024
MAX_COOKIE_WINDOW = 1024 * 1024
MAX_MEMBER = 256 * 1024 * 1024
MAX_EXPANDED = 1024 * 1024 * 1024
MAX_MEMBERS = 100_000
COOKIE = struct.Struct('!8sIIII64s')
ENTRY = struct.Struct('!IIIIBc')
MAGIC = b'MEI\014\013\012\013\016'


def elf_requirements(data: bytes, version_report: str) -> dict:
    if (len(data) < 64 or data[:7] != b'\x7fELF\x02\x01\x01'
            or struct.unpack_from('<H', data, 18)[0] != 62
            or struct.unpack_from('<I', data, 20)[0] != 1):
        raise RuntimeError('ELF must be a complete little-endian x86_64 header')
    needed, in_needs, recognized = [], False, False
    for line in version_report.splitlines():
        if line.startswith('Version '):
            in_needs = line.startswith('Version needs section ')
            recognized |= in_needs
        if in_needs:
            for name in re.findall(r'\bName:\s+(GLIBC_\S+)', line):
                value = name.removeprefix('GLIBC_')
                if not re.fullmatch(r'[0-9]+\.[0-9]+(?:\.[0-9]+)?', value):
                    raise RuntimeError('GLIBC requirement is private or unrecognized: ' + name)
                number = tuple(int(v) for v in value.split('.'))
                if number + (0,) * (3 - len(number)) > (2, 35, 0):
                    raise RuntimeError('GLIBC requirement exceeds 2.35: ' + name)
                needed.append((number + (0,) * (3 - len(number)), value))
    if not recognized and 'No version information found in this file.' not in version_report:
        raise RuntimeError('ELF version requirements could not be established')
    return {'architecture': 'x86_64', 'maximum_required_glibc': max(needed)[1] if needed else None}


def safe_name(name: str) -> None:
    path = PurePosixPath(name)
    if (not name or '\\' in name or '\x00' in name or ':' in name or path.is_absolute()
            or '..' in path.parts or name in ('.', '/')):
        raise RuntimeError('unsafe embedded archive member name')


def embedded_payloads(binary: Path):
    info = binary.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BINARY:
        raise RuntimeError('invalid or oversized executable')
    raw = binary.read_bytes()
    if len(raw) < COOKIE.size:
        raise RuntimeError('PyInstaller archive footer missing')
    # ELF section names/headers can follow the PKG cookie. Search only a bounded
    # tail, deriving the archive end from each candidate's actual position.
    candidates = []
    lower, cursor = max(0, len(raw) - MAX_COOKIE_WINDOW), len(raw)
    while cursor > lower:
        position = raw.rfind(MAGIC, lower, cursor)
        if position < 0:
            break
        cursor = position
        end = position + COOKIE.size
        if end > len(raw):
            continue
        magic, package_size, toc_offset, toc_size, pyver, library = COOKIE.unpack_from(raw, position)
        name, separator, padding = library.partition(b'\0')
        if (pyver == 312 and separator and not padding.strip(b'\0')
                and name in (b'libpython3.12.so', b'libpython3.12.so.1.0')
                and COOKIE.size <= package_size <= end
                and toc_offset + toc_size == package_size - COOKIE.size):
            candidates.append((end - package_size, toc_offset, toc_size))
    if len(candidates) > 1:
        raise RuntimeError('ambiguous PyInstaller archive positions')
    if not candidates:
        raise RuntimeError('PyInstaller archive footer or native Python binding invalid')
    start, toc_offset, toc_size = candidates[0]
    toc = raw[start + toc_offset:start + toc_offset + toc_size]
    position, expanded, count, names = 0, 0, 0, set()
    while position < len(toc):
        if len(toc) - position < ENTRY.size:
            raise RuntimeError('truncated PyInstaller archive table')
        size, offset, compressed_size, unpacked_size, compressed, kind = ENTRY.unpack_from(toc, position)
        if (size <= ENTRY.size or size % 16 or size > len(toc) - position
                or offset + compressed_size > toc_offset or unpacked_size > MAX_MEMBER
                or compressed not in (0, 1)):
            raise RuntimeError('invalid PyInstaller archive entry bounds')
        name_bytes = toc[position + ENTRY.size:position + size]
        name, separator, padding = name_bytes.partition(b'\0')
        if not separator or padding.strip(b'\0'):
            raise RuntimeError('invalid PyInstaller archive member encoding')
        try:
            name = name.decode('utf-8')
        except UnicodeDecodeError:
            raise RuntimeError('invalid PyInstaller archive member encoding') from None
        position += size
        count += 1
        if count > MAX_MEMBERS:
            raise RuntimeError('too many embedded archive members')
        # Upstream runtime options are metadata, may repeat, and contain no file.
        if kind == b'o':
            if compressed_size or unpacked_size:
                raise RuntimeError('PyInstaller runtime option contains a payload')
            continue
        safe_name(name)
        if name in names:
            raise RuntimeError('duplicate embedded archive member')
        names.add(name)
        expanded += unpacked_size
        if expanded > MAX_EXPANDED:
            raise RuntimeError('embedded archive expansion limit exceeded')
        payload = raw[start + offset:start + offset + compressed_size]
        if compressed:
            try:
                decoder = zlib.decompressobj()
                payload = decoder.decompress(payload, unpacked_size + 1)
                if not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
                    raise RuntimeError('invalid compressed archive member')
            except zlib.error:
                raise RuntimeError('invalid compressed archive member') from None
        if len(payload) != unpacked_size:
            raise RuntimeError('embedded archive member size differs')
        yield name, payload


def verify_program(binary: Path, *, run) -> dict:
    """Check outer ELF and every CArchive/ZIP ELF, then execute this exact help."""
    original = hashlib.sha256(binary.read_bytes()).hexdigest()
    report = run(['readelf', '--version-info', str(binary)], read=True)
    outer = elf_requirements(binary.read_bytes(), report)
    libraries, expanded, members = [], 0, 0
    with tempfile.TemporaryDirectory(prefix='cortex-elf-') as temporary:
        def visit(name, payload, depth=0):
            nonlocal expanded, members
            members += 1
            expanded += len(payload)
            if members > MAX_MEMBERS or expanded > MAX_EXPANDED or len(payload) > MAX_MEMBER:
                raise RuntimeError('embedded archive expansion limit exceeded')
            if payload.startswith(b'\x7fELF'):
                # Never extract to an archive-controlled pathname.
                path = Path(temporary) / str(len(libraries))
                path.write_bytes(payload)
                requirements = elf_requirements(payload, run(['readelf', '--version-info', str(path)], read=True))
                libraries.append(dict(requirements, name=name, sha256=hashlib.sha256(payload).hexdigest()))
            elif payload.startswith(b'PK'):
                if depth >= 4:
                    raise RuntimeError('embedded ZIP nesting limit exceeded')
                try:
                    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                        seen = set()
                        for entry in archive.infolist():
                            safe_name(entry.filename)
                            if entry.filename in seen or entry.flag_bits & 1:
                                raise RuntimeError('duplicate or encrypted embedded ZIP member')
                            seen.add(entry.filename)
                            if entry.is_dir():
                                continue
                            if entry.file_size > MAX_MEMBER or expanded + entry.file_size > MAX_EXPANDED:
                                raise RuntimeError('embedded ZIP expansion limit exceeded')
                            visit(name + '!' + entry.filename, archive.read(entry), depth + 1)
                except (zipfile.BadZipFile, OSError, ValueError, NotImplementedError, zlib.error):
                    raise RuntimeError('invalid embedded ZIP archive') from None
        for name, payload in embedded_payloads(binary):
            visit(name, payload)
    if not libraries:
        raise RuntimeError('ELF inventory requires embedded native libraries')
    run([str(binary), '--help'])
    if hashlib.sha256(binary.read_bytes()).hexdigest() != original:
        raise RuntimeError('executable bytes changed during qualification')
    return dict(outer, sha256=original, dependencies=re.findall(r'\bFile:\s+(\S+)', report),
                embedded_elf_count=len(libraries), embedded_elfs=libraries, help='PASS', maximum_glibc='2.35')
