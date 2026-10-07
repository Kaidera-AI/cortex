#!/usr/bin/env python3
"""r412 isolated seven-image producer. No signing, install or publication.

Dry-run accepts the preparation checkpoint. Execution requires a separate clean
source checkout and an explicit source/pipeline admission receipt. Artifacts are
build outputs, never qualification. Gate A TEST trust and Gate B production trust
are deliberately outside this image producer.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import oci_archive

VERSION = '0.1.003-manual.1'
RELEASE = 'v' + VERSION
SOURCE = 'https://github.com/Kaidera-AI/cortex'
ROLES = {
    'tls': ('packages/deploy/tls', 'Containerfile'),
    'db': ('packages', 'deploy/Containerfile.db'),
    'api': ('packages', 'api/Dockerfile'),
    'provider': ('packages/deploy', 'Containerfile.provider'),
    'embed-worker': ('packages/containers/embed-worker', 'Dockerfile'),
    'graph-worker': ('packages/containers/graph-worker', 'Dockerfile'),
    'pdf-worker': ('packages/containers/pdf-worker', 'Dockerfile'),
}
ORDER = ['A_TEST_rehearsal', 'freeze', 'Vera_review', 'CTO_sign',
         'verify_public_signature', 'B_signed_native_proof']


def native_check(system, machine, uid):
    if system != 'Linux' or machine != 'x86_64' or type(uid) is not int or uid <= 0:
        raise ValueError('native non-root Linux x86_64 builder required')


def manifest_digest(raw):
    data = json.loads(raw)
    if (data.get('mediaType') not in {
            'application/vnd.oci.image.manifest.v1+json',
            'application/vnd.docker.distribution.manifest.v2+json'}
            or not isinstance(data.get('config'), dict)
            or not isinstance(data.get('layers'), list)):
        raise ValueError('a platform manifest is required; index/config IDs refuse')
    return 'sha256:' + hashlib.sha256(raw).hexdigest()


def make_plan(root, sha):
    if re.fullmatch('[0-9a-f]{40}', sha) is None or not root.is_absolute():
        raise ValueError('exact source SHA and absolute source root required')
    args = {'KOS_VERSION': VERSION, 'KOS_SOURCE_REVISION': sha,
            'KOS_IMAGE_SOURCE': SOURCE, 'KOS_EDITION': 'open-source',
            'CORTEX_RELEASE_ID': RELEASE, 'CORTEX_RELEASE_LINEAGE': 'cortex-v1-manual',
            'CORTEX_RELEASE_SEQUENCE': '1', 'CORTEX_API_CONTRACT': 'cortex-kos-v02009.v1'}
    images = []
    for role, (context, recipe) in ROLES.items():
        tag = 'ghcr.io/kaidera-ai/cortex-' + role + ':' + RELEASE
        argv = ['build', '--platform', 'linux/amd64', '--format', 'oci',
                '--tag', tag, '--file', str(root/context/recipe)]
        for name, value in args.items():
            argv += ['--build-arg', name + '=' + value]
        argv.append(str(root/context))
        images.append({'role': role, 'tag': tag, 'argv': argv})
    return {'schema': 'cortex.manual.build-plan.v1', 'source_revision': sha,
            'platform': 'linux/amd64', 'version': VERSION, 'images': images,
            'gate_order': ORDER, 'qualification': False, 'published': False}


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], text=True).strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--source-sha', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--admission', type=Path)
    parser.add_argument('--podman', type=Path)
    options = parser.parse_args()
    root = options.source.absolute()
    plan = make_plan(root, options.source_sha)
    if git(root, 'rev-parse', 'HEAD') != options.source_sha:
        raise ValueError('source checkout differs from exact input SHA')
    plan['recipes'] = {}
    for role, (context, recipe) in ROLES.items():
        path = context + '/' + recipe
        raw = subprocess.check_output(['git', '-C', str(root), 'show', options.source_sha + ':' + path])
        if (root/path).read_bytes() != raw:
            raise ValueError('recipe differs from committed source')
        plan['recipes'][role] = hashlib.sha256(raw).hexdigest()
    if options.execute:
        native_check(platform.system(), platform.machine(), os.getuid())
        for tool in (options.podman,):
            if tool is None or not tool.is_absolute() or not tool.is_file() or not os.access(tool, os.X_OK):
                raise ValueError('execution requires explicit existing native tool paths')
        if git(root, 'status', '--porcelain', '--untracked-files=all'):
            raise ValueError('execution source must be completely clean')
        if options.admission is None:
            raise ValueError('source and pipeline admission receipt required')
        admission = json.loads(options.admission.read_bytes())
        if (admission.get('source_revision') != options.source_sha
                or admission.get('source_accepted') is not True
                or admission.get('pipeline_accepted') is not True
                or not admission.get('decision_id')):
            raise ValueError('admission receipt does not authorize this source/pipeline')
    out = options.output.absolute()
    if out.exists():
        raise ValueError('output must be a new isolated directory')
    out.mkdir(mode=0o700, parents=True)
    (out/'build-plan.json').write_text(json.dumps(plan, indent=2)+'\n')
    if not options.execute:
        print(json.dumps({'dry_run': True, 'plan': str(out/'build-plan.json'), 'qualification': False}))
        return
    # Dedicated engine storage and auth file; never use an existing user store.
    (out/'auth.json').write_text('{}\n'); (out/'auth.json').chmod(0o600)
    engine = [str(options.podman), '--root', str(out/'store'), '--runroot', str(out/'runroot')]
    env = {key: os.environ[key] for key in ('PATH', 'HOME', 'XDG_RUNTIME_DIR') if key in os.environ}
    env.update(REGISTRY_AUTH_FILE=str(out/'auth.json'), LANG='C.UTF-8')
    (out/'native-tools.json').write_text(json.dumps({str(tool): hashlib.sha256(tool.read_bytes()).hexdigest() for tool in (options.podman,)}, indent=2)+'\n')
    inventory = {'schema': 'cortex.images.v1', 'version': VERSION,
                 'source_revision': options.source_sha, 'platforms': {'linux/amd64': {}}}
    for image in plan['images']:
        role = image['role']
        with (out/(role+'.build.log')).open('w') as log:
            subprocess.run(engine+image['argv'], env=env, stdout=log, stderr=subprocess.STDOUT, check=True)
        archive = out/('cortex-'+role+'-'+VERSION+'-linux-amd64.oci.tar')
        subprocess.run(engine+['save', '--format', 'oci-archive', '--output', str(archive), image['tag']], env=env, check=True)
        inspection = oci_archive.verify(archive, options.source_sha)
        digest = inspection['manifest_digest']
        (out/(role+'.archive-verification.json')).write_text(json.dumps(inspection, indent=2)+'\n')
        inventory['platforms']['linux/amd64'][role] = {
            'repository': 'ghcr.io/kaidera-ai/cortex-'+role, 'tag': RELEASE,
            'manifest_digest': digest}
    (out/'image-inventory.json').write_text(json.dumps(inventory, indent=2)+'\n')
    print(json.dumps({'images_exported': 7, 'qualification': False, 'published': False}))

if __name__ == '__main__':
    main()
