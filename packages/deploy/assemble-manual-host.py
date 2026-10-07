#!/usr/bin/env python3
"""Finish an isolated host closure, with measured native libraries and licenses."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def native_header(header):
    if len(header)<20 or header[:6]!=b'\x7fELF\x02\x01' or header[18:20]!=b'\x3e\x00':
        raise ValueError('native Linux x86_64 ELF required')


def library_paths(output):
    if 'not found' in output:
        raise ValueError('unresolved native dependency')
    return set(re.findall(r'(?:=>\s+|^\s*)(/[^\s]+)\s+\(',output,re.MULTILINE))


def checked_tree(root, expected):
    actual={}
    for path in sorted(root.rglob('*')):
        name=path.relative_to(root).as_posix()
        if path.is_symlink():
            if not path.resolve().is_relative_to(root.resolve()):
                raise ValueError('host link escapes closure')
            actual[name]={'symlink':os.readlink(path)}
        elif path.is_file():
            info=path.stat()
            if info.st_nlink!=1 or info.st_mode&0o7000:
                raise ValueError('unsafe host member')
            actual[name]={'sha256':digest(path),'size':info.st_size,'mode':info.st_mode&0o777}
    if actual!=expected:
        raise ValueError('host input differs from measured receipt')


def prune_optional_crypt(stage):
    # crypt is deprecated, unused by our host interfaces, and requires the
    # retired libcrypt.so.1 ABI that Rocky10 deliberately does not ship.
    lib=stage/'python/lib/python3.12'
    paths=[lib/'crypt.py',*lib.glob('lib-dynload/_crypt.cpython-312-x86_64-linux-gnu.so')]
    removed=[]
    for path in paths:
        if path.is_file() and not path.is_symlink():
            removed.append(path.relative_to(stage).as_posix());path.unlink()
    return removed


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host',type=Path,required=True)
    parser.add_argument('--receipt',type=Path,required=True)
    parser.add_argument('--inputs',type=Path,required=True)
    parser.add_argument('--minisign-license',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if platform.system()!='Linux' or platform.machine()!='x86_64' or os.getuid()==0:
        raise ValueError('native nonroot Linux x86_64 required')
    source=args.host.absolute();out=args.output.absolute()
    checked_tree(source,json.loads(args.receipt.read_bytes())['files'])
    if out.exists():raise ValueError('output must be new')
    out.mkdir(parents=True,mode=0o700)
    stage=out/'host';shutil.copytree(source,stage,symlinks=True)
    (out/'optional-module-pruning.json').write_text(json.dumps(prune_optional_crypt(stage))+'\n')
    env={'PATH':'/usr/bin:/bin','HOME':str(out),'LANG':'C.UTF-8'}
    required=set();ldd_receipts={}
    for path in sorted(stage.rglob('*')):
        if path.is_symlink() or not path.is_file():continue
        with path.open('rb') as stream:header=stream.read(20)
        if header[:4]!=b'\x7fELF':continue
        native_header(header)
        result=subprocess.run(['/usr/bin/ldd',str(path)],env=env,capture_output=True,text=True)
        text=result.stdout+result.stderr
        if result.returncode and not ('not a dynamic executable' in text or 'statically linked' in text):
            raise ValueError('ldd refused native member: '+str(path))
        ldd_receipts[path.relative_to(stage).as_posix()]=text
        try:required.update(library_paths(text))
        except ValueError as error:raise ValueError(str(path)+': '+str(error)) from None
    libraries=stage/'lib';libraries.mkdir(mode=0o755)
    library_receipt={}
    for name in sorted(required):
        path=Path(name)
        if not path.is_absolute() or not path.is_file():raise ValueError('invalid native library')
        body=path.read_bytes();native_header(body[:20]);target=libraries/path.name
        if target.exists() and target.read_bytes()!=body:raise ValueError('native library collision')
        target.write_bytes(body);target.chmod(0o755)
        library_receipt[path.name]={'source':name,'sha256':hashlib.sha256(body).hexdigest()}
    loader=libraries/'ld-linux-x86-64.so.2'
    if not loader.is_file():raise ValueError('native loader missing')
    wrapper='#!/bin/sh\nset -eu\nclosure_root=$(CDPATH= cd "$(dirname "$0")/.." && pwd -P)\nexec "$closure_root/lib/ld-linux-x86-64.so.2" --library-path "$closure_root/lib:$closure_root/python/lib" "$closure_root/python/bin/python3.12" "$@"\n'
    binary=stage/'bin';binary.mkdir(exist_ok=True)
    (binary/'python3').write_text(wrapper);(binary/'python3').chmod(0o755)
    compose='#!/bin/sh\nset -eu\nclosure_bin=$(CDPATH= cd "$(dirname "$0")" && pwd -P)\nexec "$closure_bin/python3" -I -m podman_compose "$@"\n'
    (binary/'podman-compose').write_text(compose);(binary/'podman-compose').chmod(0o755)
    # Confirm relocated interpreter, cryptographic dependency and no acquisition.
    check="import cryptography,yaml,dotenv,podman_compose,cffi,pycparser,importlib.util; assert cryptography.__version__=='45.0.7'; assert importlib.util.find_spec('pip') is None; assert importlib.util.find_spec('ensurepip') is None; print('relocatable host closure PASS')"
    result=subprocess.run([str(binary/'python3'),'-I','-B','-c',check],env=env,capture_output=True,text=True,check=True)
    (out/'relocation-proof.log').write_text(result.stdout+result.stderr)
    licenses=stage/'licenses';licenses.mkdir(mode=0o755)
    licenses_map={}
    paths=[stage/'python/lib/python3.12/LICENSE.txt']
    paths.extend(stage.glob('python/lib/python3.12/site-packages/*.dist-info/licenses/*'))
    paths.append(args.minisign_license)
    # Include the native system library licensors alongside the bundled bytes.
    for package in ('glibc','libgcc','libxcrypt'):
        result=subprocess.run(['/usr/bin/rpm','-ql',package],capture_output=True,text=True)
        if result.returncode==0:
            paths.extend(Path(x) for x in result.stdout.splitlines() if x.startswith('/usr/share/licenses/') and Path(x).is_file())
    for index,path in enumerate(paths):
        if not path.is_file():raise ValueError('missing host license')
        body=path.read_bytes();name=f'{index:03d}-{path.name}';(licenses/name).write_bytes(body)
        licenses_map[name]={'source':str(path),'sha256':hashlib.sha256(body).hexdigest()}
    (out/'native-libraries.json').write_text(json.dumps({'libraries':library_receipt,'ldd':ldd_receipts},indent=2)+'\n')
    (out/'licenses.json').write_text(json.dumps(licenses_map,indent=2)+'\n')
    inputs=json.loads(args.inputs.read_bytes())
    sbom={'bomFormat':'CycloneDX','specVersion':'1.6','version':1,'components':[]}
    for entry in [inputs['python'],inputs['minisign'],*inputs['wheels']]:
        sbom['components'].append({'type':'library','name':entry.get('name','minisign' if entry.get('version')=='0.12' else 'CPython'),
            'version':entry['version'],'hashes':[{'alg':'SHA-256','content':entry['sha256']}],
            'externalReferences':[{'type':'distribution','url':entry['url']}]})
    for name,value in library_receipt.items():
        sbom['components'].append({'type':'library','name':name,'hashes':[{'alg':'SHA-256','content':value['sha256']}]})
    (out/'host-sbom.cdx.json').write_text(json.dumps(sbom,indent=2)+'\n')
    print(json.dumps({'host':str(stage),'libraries':len(library_receipt),'licenses':len(licenses_map),'qualification':False}))


if __name__=='__main__':main()
