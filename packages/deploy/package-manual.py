#!/usr/bin/env python3
"""Regenerate truthful public-source provenance in an isolated install payload."""
import argparse
import hashlib
import importlib.util
import io
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import tarfile

SPEC=importlib.util.spec_from_file_location('manual_release_packager',Path(__file__).with_name('package-release.py'))
packager=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(packager)
PROVENANCE='public git archive at source_revision with derived release identity'


def payload_identity(files, sha):
    if re.fullmatch('[0-9a-f]{40}',sha) is None:
        raise ValueError('exact public source revision required')
    prefix='packages/schema/migrations/'
    entries=sorted((name[len(prefix):],hashlib.sha256(body).hexdigest())
                   for name,(body,_) in files.items() if name.startswith(prefix)
                   and '/' not in name[len(prefix):] and name.endswith('.sql'))
    if not entries:raise ValueError('migration inventory is missing')
    schema=hashlib.sha256(''.join(name+'\t'+digest+'\n' for name,digest in entries).encode()).hexdigest()
    return {'schema':'cortex.payload.v1','version':'0.1.003-manual.1',
            'source_revision':sha,'schema_revision':schema}


def projection(repo, sha, output):
    if subprocess.check_output(['git','-C',str(repo),'rev-parse','HEAD'],text=True).strip()!=sha:
        raise ValueError('source checkout must equal the exact requested revision')
    if subprocess.check_output(['git','-C',str(repo),'status','--porcelain','--untracked-files=all'],text=True).strip():
        raise ValueError('source checkout must be clean')
    raw=subprocess.check_output(['git','-C',str(repo),'archive','--format=tar',sha,'packages'])
    if len(raw)>packager.MAX_BYTES:raise ValueError('source package bound exceeded')
    files={};directories=set()
    with tarfile.open(fileobj=io.BytesIO(raw),mode='r:') as archive:
        for member in archive:
            name=member.name.rstrip('/')
            parts=PurePosixPath(name).parts
            if (name.startswith('/') or '..' in parts or not name.startswith('packages')
                    or name in files or name in directories or not (member.isfile() or member.isdir())):
                raise ValueError('unsafe public source archive')
            if member.isdir():directories.add(name)
            else:
                with archive.extractfile(member) as stream:body=stream.read()
                files[name]=(body,0o755 if member.mode&0o111 else 0o644)
    identity=payload_identity(files,sha)
    files['packages/deploy/release.json']=(packager.images.encode(identity),0o644)
    components={}
    for name,prefix in packager.COMPONENTS.items():
        members={path:pair for path,pair in files.items() if path.startswith(prefix+'/')}
        digest=hashlib.sha256()
        for path in sorted(members,key=lambda x:(str(PurePosixPath(x).parent),PurePosixPath(x).name)):
            digest.update(path[len(prefix)+1:].encode());digest.update(members[path][0])
        components[name]={'path':prefix,'files':len(members),'tree_sha256':digest.hexdigest()}
    entry_points={name:hashlib.sha256(files[path][0]).hexdigest() for name,path in packager.ENTRY_POINTS.items()}
    manifest={'schema':'cortex.projection_manifest.v1','source_repo':'Kaidera-AI/cortex',
              'source_provenance':PROVENANCE,'source_revision':sha,'components':components,
              'entry_points':entry_points,'derived_files':{'packages/deploy/release.json':hashlib.sha256(files['packages/deploy/release.json'][0]).hexdigest()}}
    files['PROJECTION_MANIFEST.json']=(packager.images.encode(manifest),0o644)
    if output.exists():raise ValueError('projection output must be new')
    output.mkdir(parents=True,mode=0o700)
    for directory in sorted(directories): (output/directory).mkdir(parents=True,exist_ok=True)
    for name,(body,mode) in files.items():
        path=output/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(body);path.chmod(mode)
    # Full existing snapshot custody/checksum/migration verification, no bypass.
    verified,_,_=packager.snapshot(output)
    if verified!=identity:raise ValueError('derived payload identity mismatch')
    return identity


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,required=True)
    parser.add_argument('--source-sha',required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--images',type=Path)
    parser.add_argument('--dry-run',action='store_true')
    args=parser.parse_args();root=args.output.absolute().resolve()
    if root.exists():raise ValueError('output must be new')
    root.mkdir(mode=0o700,parents=True)
    identity=projection(args.source,args.source_sha,root/'projection')
    if args.dry_run:
        print(json.dumps({'identity':identity,'dry_run':True,'qualification':False}));return
    if args.images is None:raise ValueError('real qualified image inventory required')
    result=packager.build_release(root/'projection',root/'release',inventory=json.loads(args.images.read_bytes()),platform='linux/amd64')
    print(json.dumps(result))

if __name__=='__main__':main()
