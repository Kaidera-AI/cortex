"""Builder-only proof that the exact bundled OpenSSL supplies and loads its ABI."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import zipfile

from linux_binaries import (MAX_EXPANDED, MAX_MEMBER, MAX_MEMBERS, embedded_payloads,
                            elf_requirements, safe_name)

PROBE = '''
import ctypes,json,sys
crypto=ctypes.CDLL(sys.argv[2])
ssl=ctypes.CDLL(sys.argv[1])
crypto.OpenSSL_version.argtypes=[ctypes.c_int]
crypto.OpenSSL_version.restype=ctypes.c_char_p
loaded=set()
for line in open('/proc/self/maps'):
    fields=line.rstrip().split(None,5)
    if len(fields)==6 and fields[5].rsplit('/',1)[-1] in ('libssl.so.3','libcrypto.so.3'):
        loaded.add(fields[5])
print(json.dumps({'openssl':crypto.OpenSSL_version(0).decode('ascii'),'loaded':sorted(loaded)}))
'''


def _run(argv, *, read=False):
    result = subprocess.run(argv, check=True, text=True, timeout=5,
                            stdout=subprocess.PIPE if read else None)
    if read and len(result.stdout.encode()) > 1024 * 1024:
        raise RuntimeError('bounded native tool output required')
    return result.stdout.strip() if read else ''


def _versions(report: str) -> tuple[set[str], dict[str, set[str]]]:
    definitions, needed = set(), {}
    section = None; provider = None; recognized = False
    for line in report.splitlines():
        if line.startswith('Version '):
            section = ('definitions' if line.startswith('Version definition section ')
                       else 'needs' if line.startswith('Version needs section ') else None)
            recognized |= section is not None
            provider = None
        file = re.search(r'\bFile:\s+(\S+)', line)
        if section == 'needs' and file: provider = file.group(1)
        for name in re.findall(r'\bName:\s+(OPENSSL_\S+)', line):
            if not re.fullmatch(r'OPENSSL_[0-9]+\.[0-9]+\.[0-9]+', name):
                raise RuntimeError('unrecognized OpenSSL version node')
            if section == 'definitions': definitions.add(name)
            elif section == 'needs' and provider: needed.setdefault(provider, set()).add(name)
            else: raise RuntimeError('unbound OpenSSL version node')
    if not recognized and 'No version information found in this file.' not in report:
        raise RuntimeError('unrecognized ELF version report')
    return definitions, needed


def qualify(binary: Path, runtime: Path, *, run=_run) -> dict:
    """Measure compiled/bundled bytes, every requested node, then loaded paths."""
    original = hashlib.sha256(binary.read_bytes()).hexdigest()
    try:
        inventory = json.loads((runtime / 'runtime-inventory.json').read_text())
        identity = inventory['runtime']
        if (inventory['schema'] != 'cortex.native-ci-runtime.v1' or identity['python'] != '3.12.14'
                or identity['architecture'] != 'x86_64'
                or not identity['openssl'].startswith('OpenSSL 3.5.8 ')):
            raise ValueError
        compiled = {}
        for name in ('libssl.so.3', 'libcrypto.so.3'):
            path = runtime / 'openssl/lib' / name
            if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_MEMBER:
                raise ValueError
            compiled[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    except (OSError, ValueError, KeyError, TypeError):
        raise RuntimeError('independent pinned compiled OpenSSL required') from None
    providers, consumers = {}, []; members = 0; expanded = 0
    with tempfile.TemporaryDirectory(prefix='cortex-openssl-') as temporary:
        root = Path(temporary)
        def visit(name, payload, depth=0):
            nonlocal members, expanded
            members += 1; expanded += len(payload)
            if members > MAX_MEMBERS or expanded > MAX_EXPANDED or len(payload) > MAX_MEMBER:
                raise RuntimeError('OpenSSL closure expansion limit exceeded')
            if payload.startswith(b'\x7fELF'):
                path = root / ('elf-' + str(members)); path.write_bytes(payload)
                report = run(['readelf', '--version-info', str(path)], read=True)
                elf_requirements(payload, report)
                definitions, needed = _versions(report)
                dynamic = run(['readelf', '--dynamic', str(path)], read=True)
                dependencies = re.findall(r'\(NEEDED\).*?\[([^\]]+)\]', dynamic)
                sonames = re.findall(r'\(SONAME\).*?\[([^\]]+)\]', dynamic)
                for library in needed:
                    if library not in dependencies or library not in compiled:
                        raise RuntimeError('OpenSSL version provider is not a bundled needed library')
                consumers.append(needed)
                basename = name.rsplit('/', 1)[-1]
                if basename in compiled:
                    digest = hashlib.sha256(payload).hexdigest()
                    if basename in providers or sonames != [basename] or digest != compiled[basename] or not definitions:
                        raise RuntimeError('bundled OpenSSL differs from compiled library')
                    destination = root / basename; destination.write_bytes(payload)
                    providers[basename] = {'sha256': digest, 'definitions': sorted(definitions),
                                           'needed': dependencies, 'path': str(destination)}
            elif payload.startswith(b'PK'):
                if depth >= 4: raise RuntimeError('OpenSSL closure nesting limit exceeded')
                try:
                    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                        seen = set()
                        for item in archive.infolist():
                            safe_name(item.filename)
                            if item.filename in seen or item.flag_bits & 1:
                                raise RuntimeError('invalid OpenSSL closure ZIP member')
                            seen.add(item.filename)
                            if item.is_dir(): continue
                            if item.file_size > MAX_MEMBER or expanded + item.file_size > MAX_EXPANDED:
                                raise RuntimeError('OpenSSL closure expansion limit exceeded')
                            visit(name + '!' + item.filename, archive.read(item), depth + 1)
                except (zipfile.BadZipFile, OSError, ValueError, NotImplementedError):
                    raise RuntimeError('invalid OpenSSL closure ZIP') from None
        for name, payload in embedded_payloads(binary): visit(name, payload)
        if set(providers) != set(compiled) or 'libcrypto.so.3' not in providers['libssl.so.3']['needed']:
            raise RuntimeError('complete bundled OpenSSL library pair required')
        for needed in consumers:
            for library, nodes in needed.items():
                if not nodes.issubset(providers[library]['definitions']):
                    raise RuntimeError('bundled OpenSSL does not supply a requested version node')
        ssl, crypto = (providers[name]['path'] for name in ('libssl.so.3', 'libcrypto.so.3'))
        value = run(['env', '-i', 'PATH=/usr/bin:/bin', 'LD_LIBRARY_PATH=' + str(root),
                     sys.executable, '-I', '-c', PROBE, ssl, crypto], read=True)
        try:
            actual = json.loads(value)
            if (set(actual) != {'openssl', 'loaded'} or not actual['openssl'].startswith('OpenSSL 3.5.8 ')
                    or sorted(actual['loaded']) != sorted([ssl, crypto])):
                raise ValueError
        except (ValueError, TypeError, KeyError, AttributeError):
            raise RuntimeError('bundled OpenSSL version and loaded paths must match') from None
        for name, item in providers.items():
            if (hashlib.sha256(Path(item['path']).read_bytes()).hexdigest() != item['sha256']
                    or hashlib.sha256((runtime / 'openssl/lib' / name).read_bytes()).hexdigest() != compiled[name]):
                raise RuntimeError('OpenSSL library changed during qualification')
        if hashlib.sha256(binary.read_bytes()).hexdigest() != original:
            raise RuntimeError('binary changed during OpenSSL qualification')
        return {'binary_sha256': original, 'openssl': actual['openssl'], 'libraries': providers,
                'host_fallback': False, 'loaded': actual['loaded']}


def trace_product(binary: Path, libraries: dict, *, run=subprocess.run) -> dict:
    """Observe the actual product loader; no inherited library/environment fallback."""
    original = hashlib.sha256(binary.read_bytes()).hexdigest()
    environment = {'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'LC_ALL': 'C',
                   'LD_DEBUG': 'libs,versions', 'LD_BIND_NOW': '1'}
    result = run([str(binary), '--help'], env=environment, text=True,
                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
    if (result.returncode != 0 or not isinstance(result.stdout, str) or not isinstance(result.stderr, str)
            or len(result.stdout.encode()) > 2 * 1024 * 1024
            or len(result.stderr.encode()) > 2 * 1024 * 1024):
        raise RuntimeError('bounded successful product loader observation required')
    names = {'libssl.so.3', 'libcrypto.so.3'}
    loaded, checked = {}, {name: set() for name in names}
    checked_paths = {name: set() for name in names}
    for line in result.stderr.splitlines():
        match = re.search(r'calling init:\s+(\S+)', line)
        if match:
            path = Path(match.group(1)); name = path.name
            if name in names:
                if (not path.is_absolute() or '..' in path.parts or not path.parent.name.startswith('_MEI')
                        or name in loaded and loaded[name] != str(path)):
                    raise RuntimeError('product loaded a foreign OpenSSL provider')
                loaded[name] = str(path)
        match = re.search(r"checking for version `([^']+)' in file (\S+)", line)
        if match and match.group(1).startswith('OPENSSL_'):
            node, value = match.groups(); path = Path(value); name = path.name
            if (name not in names or not path.is_absolute() or '..' in path.parts
                    or not path.parent.name.startswith('_MEI')
                    or node not in libraries[name]['definitions']):
                raise RuntimeError('product requested an unbound OpenSSL version node')
            checked[name].add(node)
            checked_paths[name].add(str(path))
    if (set(loaded) != names or any(not nodes for nodes in checked.values())
            or len({str(Path(value).parent) for value in loaded.values()}) != 1
            or any(checked_paths[name] != {loaded[name]} for name in names)):
        raise RuntimeError('complete actual product OpenSSL loader proof required')
    if hashlib.sha256(binary.read_bytes()).hexdigest() != original:
        raise RuntimeError('product binary changed during loader observation')
    return {'help': 'PASS', 'host_fallback': False, 'loaded': loaded,
            'checked_nodes': {name: sorted(nodes) for name, nodes in checked.items()},
            'binary_sha256': original, 'loader_trace': result.stderr,
            'loader_trace_sha256': hashlib.sha256(result.stderr.encode()).hexdigest()}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--binary', type=Path, action='append', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    receipts = {}
    for binary in args.binary:
        receipt = qualify(binary, args.runtime)
        receipt['product_loader'] = trace_product(binary, receipt['libraries'])
        receipts[str(binary)] = receipt
    with args.output.open('x') as stream: json.dump(receipts, stream, indent=2); stream.write('\n')
    print(json.dumps(receipts, sort_keys=True))


if __name__ == '__main__':
    main()
