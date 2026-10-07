#!/usr/bin/env python3
"""Build a pinned native Python/wheel host closure; no target acquisition."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import ssl
import subprocess
import tarfile
from urllib import request
from urllib.parse import urlsplit

REQUIRED = {'podman-compose','python-dotenv','PyYAML','cryptography','cffi','pycparser'}


def validate(value):
    if value.get('schema') != 'cortex.manual.host-inputs.v1' or value.get('platform') != 'linux/amd64':
        raise ValueError('native host input identity required')
    wheels = value.get('wheels', [])
    if len(wheels) != 6 or {x.get('name') for x in wheels} != REQUIRED:
        raise ValueError('complete six-wheel closure required')
    if value['python'].get('version') != '3.12.15':
        raise ValueError('pinned CPython 3.12 required')
    for entry in [value['python'], *wheels]:
        parsed = urlsplit(entry.get('url', ''))
        if (parsed.scheme != 'https' or parsed.username or parsed.password
                or parsed.hostname not in {'github.com','files.pythonhosted.org'}
                or re.fullmatch('[0-9a-f]{64}', str(entry.get('sha256'))) is None):
            raise ValueError('safe HTTPS hash-pinned input required')
    for wheel in wheels:
        name = wheel.get('filename', '')
        if (not name.endswith('.whl') or '/' in name or '\\' in name
                or not ('none-any.whl' in name or ('manylinux' in name and 'x86_64' in name
                      and ('cp312' in name or 'abi3' in name)))):
            raise ValueError('native binary or universal wheel required')
    if next(x for x in wheels if x['name']=='cryptography')['version'] != '45.0.7':
        raise ValueError('descriptor cryptography version must remain pinned')


class HTTPSOnly(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, url):
        if urlsplit(url).scheme != 'https':
            raise ValueError('insecure input redirect refused')
        return super().redirect_request(req, fp, code, msg, headers, url)


def fetch(entry, destination):
    context=ssl.create_default_context()
    opener=request.build_opener(request.HTTPSHandler(context=context), HTTPSOnly)
    digest=hashlib.sha256(); count=0
    with opener.open(entry['url'],timeout=40) as response, destination.open('xb') as stream:
        while chunk:=response.read(1024*1024):
            count+=len(chunk)
            if count>512*1024**2: raise ValueError('input exceeds download bound')
            digest.update(chunk);stream.write(chunk)
    if digest.hexdigest()!=entry['sha256'] or ('size' in entry and count!=entry['size']):
        raise ValueError('input digest or size mismatch')


def unpack(path, destination):
    with tarfile.open(path,'r:*') as archive:
        members=archive.getmembers(); seen=set(); total=0
        if len(members)>20000: raise ValueError('runtime member bound exceeded')
        for item in members:
            name=PurePosixPath(item.name)
            if name.is_absolute() or '..' in name.parts or item.name in seen:
                raise ValueError('unsafe runtime member')
            seen.add(item.name);total+=item.size
            if total>1024**3:raise ValueError('runtime expanded bound exceeded')
            if item.issym():
                link=PurePosixPath(item.linkname)
                if link.is_absolute() or '..' in link.parts: raise ValueError('unsafe runtime link')
            elif not (item.isfile() or item.isdir()):raise ValueError('runtime special member refused')
        archive.extractall(destination,filter='data')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inputs',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--dry-run',action='store_true')
    args=parser.parse_args();raw=args.inputs.read_bytes();inputs=json.loads(raw);validate(inputs)
    if args.dry_run:
        print(json.dumps({'input_sha256':hashlib.sha256(raw).hexdigest(),'wheels':6,'native_execution':False}));return
    if platform.system()!='Linux' or platform.machine()!='x86_64' or os.getuid()==0:
        raise ValueError('native non-root Linux x86_64 host builder required')
    out=args.output.absolute()
    if out.exists():raise ValueError('host output must be a new isolated directory')
    out.mkdir(parents=True,mode=0o700)
    downloads=out/'inputs';downloads.mkdir(mode=0o700)
    python_archive=downloads/'python.tar.gz';fetch(inputs['python'],python_archive)
    stage=out/'host';stage.mkdir(mode=0o700);unpack(python_archive,stage)
    wheels=downloads/'wheels';wheels.mkdir(mode=0o700)
    for entry in inputs['wheels']:fetch(entry,wheels/entry['filename'])
    python=stage/'python/bin/python3'
    if not python.is_file():raise ValueError('CPython archive layout mismatch')
    target=stage/'python/lib/python3.12/site-packages'
    env={'PATH':'/usr/bin:/bin','HOME':str(out),'TMPDIR':str(out),'LANG':'C.UTF-8','PIP_CONFIG_FILE':'/dev/null'}
    with (out/'pip-build.log').open('w') as log:
        subprocess.run([str(python),'-I','-m','pip','install','--no-index','--no-deps',
                        '--no-cache-dir','--only-binary=:all:','--target',str(target),
                        *[str(wheels/x['filename']) for x in inputs['wheels']]],env=env,stdout=log,stderr=subprocess.STDOUT,check=True)
    check="import cryptography,yaml,dotenv,podman_compose,cffi,pycparser; assert cryptography.__version__=='45.0.7'; from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey; k=Ed25519PrivateKey.generate(); k.public_key().verify(k.sign(b'host-closure'),b'host-closure'); print('six native imports and synthetic Ed25519 PASS')"
    result=subprocess.run([str(python),'-I','-c',check],env=env,capture_output=True,text=True,check=True)
    (out/'native-import-proof.log').write_text(result.stdout+result.stderr)
    files={}
    for path in sorted(stage.rglob('*')):
        member=path.relative_to(stage).as_posix()
        if path.is_symlink():files[member]={'symlink':os.readlink(path)}
        elif path.is_file():files[member]={'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'size':path.stat().st_size,'mode':path.stat().st_mode&0o777}
    receipt={'schema':'cortex.manual.host-closure.v1','input_sha256':hashlib.sha256(raw).hexdigest(),
             'platform':'linux/amd64','files':files,'native_imports':True,
             'qualification':False,'complete_release':False}
    (out/'host-files.json').write_text(json.dumps(receipt,indent=2)+'\n')
    print(json.dumps({'host':str(stage),'receipt':str(out/'host-files.json'),'qualification':False}))

if __name__=='__main__':main()
