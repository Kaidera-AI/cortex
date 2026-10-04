"""Deterministic stdlib-only reader archive for consumers to vendor at build."""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODULES = ('__init__.py', 'key_lifetime.py', 'clients/__init__.py',
           'clients/key_store.py', 'clients/mac_keychain.py', 'clients/member_reader.py')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    sha = subprocess.check_output(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], text=True).strip()
    if subprocess.check_output(['git', '-C', str(ROOT), 'status', '--porcelain'], text=True).strip():
        raise RuntimeError('freeze a clean source before labelling a reader archive')
    args.output.mkdir(parents=True, exist_ok=False)
    archive = args.output / ('cortex-key-reader-' + sha + '.zip')
    files = {}
    with zipfile.ZipFile(archive, 'x', compression=zipfile.ZIP_DEFLATED) as stream:
        for module in MODULES:
            data = (ROOT / 'src/cortex_v2' / module).read_bytes()
            name = 'cortex_v2/' + module
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            stream.writestr(info, data)
            files[name] = hashlib.sha256(data).hexdigest()
    (args.output / 'reader.json').write_text(json.dumps({
        'schema': 'cortex.member-reader.v1', 'source_sha': sha,
        'module': 'cortex_v2.clients.member_reader', 'api': 'MemberKeyReader.headers',
        'archive': archive.name, 'sha256': hashlib.sha256(archive.read_bytes()).hexdigest(),
        'python': '3.12', 'dependencies': 'standard library only; native Security.framework on macOS',
        'files': files,
    }, indent=2) + '\n')
    print('Reader archive and public digest inventory created')


if __name__ == '__main__':
    main()
