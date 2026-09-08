"""Strict, stdlib-only prebuilt image and installation contract.

Locks name platform manifests, never config IDs or multi-platform indexes.
Maintainers resolve indexes before packaging; runtime verifies the selected
Linux platform and manifest identity. This is integrity, not release approval.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import stat

IMAGE_SOURCE = 'https://github.com/Kaidera-AI/cortex'
PLATFORMS = {'linux/arm64', 'linux/amd64'}
SERVICE_ROLES = {
    'cortex-tls-init': 'tls', 'cortex-pg': 'db', 'cortex-migrate': 'api',
    'cortex-provider': 'provider', 'cortex-api': 'api',
    'cortex-embed-worker': 'embed-worker', 'cortex-graph-worker': 'graph-worker',
    'cortex-pdf-worker': 'pdf-worker', 'cortex-backup': 'db', 'cortex-pki-restore': 'db',
}
INSTALL_FILE = 'packages/deploy/install.json'
COMPOSE_FILE = 'packages/deploy/install-compose.json'
LOCK_FILE = 'packages/deploy/image-lock.json'
HEX = re.compile(r'[a-f0-9]{64}')


def encode(value):
    return (json.dumps(value, indent=2, sort_keys=True) + '\n').encode()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def validate_inventory(value, identity):
    if (not isinstance(value, dict) or set(value) != {'schema', 'version', 'source_revision', 'platforms'}
            or value['schema'] != 'cortex.images.v1'
            or value['version'] != identity.get('version')
            or value['source_revision'] != identity.get('source_revision')
            or not re.fullmatch(r'[a-f0-9]{40}', str(value['source_revision']))
            or not re.fullmatch(r'\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?', str(value['version']))):
        raise ValueError('image inventory identity is invalid')
    platforms = value['platforms']
    if not isinstance(platforms, dict) or not platforms or not set(platforms) <= PLATFORMS:
        raise ValueError('image inventory requires supported Linux platform manifests')
    for roles in platforms.values():
        if not isinstance(roles, dict) or set(roles) != set(SERVICE_ROLES.values()):
            raise ValueError('image inventory must cover exactly all seven roles')
        for lock in roles.values():
            if (not isinstance(lock, dict) or set(lock) != {'repository', 'tag', 'manifest_digest'}
                    or not re.fullmatch(r'[a-z0-9]+(?:[.-][a-z0-9]+)*(?::[0-9]{1,5})?/[a-z0-9]+(?:[._/-][a-z0-9]+)*', str(lock.get('repository', '')))
                    or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}', str(lock.get('tag', '')))
                    or not re.fullmatch(r'sha256:[a-f0-9]{64}', str(lock.get('manifest_digest', '')))):
                raise ValueError('image lock requires a registry repository, tag and platform manifest digest')
            registry = lock['repository'].split('/', 1)[0]
            if registry != 'localhost' and '.' not in registry and ':' not in registry:
                raise ValueError('image repository must use a fully qualified registry host')
    return value


def image_ref(lock):
    return lock['repository'] + ':' + lock['tag'] + '@' + lock['manifest_digest']


def validate_compose(value, inventory, platform):
    if platform not in inventory['platforms']:
        raise ValueError('installation platform has no complete image locks')
    if not isinstance(value, dict) or not isinstance(value.get('services'), dict) or set(value['services']) != set(SERVICE_ROLES):
        raise ValueError('installation Compose must cover exactly the runtime services')
    # Generated Compose has no build instructions or source extensions at any depth.
    def no_build(node):
        if isinstance(node, dict):
            if any(key in {'build', 'include', 'extends'} or str(key).startswith('x-') for key in node):
                raise ValueError('installation Compose forbids build and external source extensions')
            for item in node.values(): no_build(item)
        elif isinstance(node, list):
            for item in node: no_build(item)
    no_build(value)
    for service, role in SERVICE_ROLES.items():
        config = value['services'][service]
        if (not isinstance(config, dict) or config.get('image') != image_ref(inventory['platforms'][platform][role])
                or config.get('platform') != platform or config.get('pull_policy') != 'never'):
            raise ValueError('installation Compose image differs from its locked platform role: ' + service)
    return value


def verify_image(info, lock, platform, identity):
    labels = info.get('Labels') or info.get('Config', {}).get('Labels', {})
    if (info.get('Digest') != lock['manifest_digest']
            or lock['repository'] + '@' + lock['manifest_digest'] not in info.get('RepoDigests', [])
            or str(info.get('Os')) + '/' + str(info.get('Architecture')) != platform
            or info.get('ManifestType') not in {'application/vnd.oci.image.manifest.v1+json', 'application/vnd.docker.distribution.manifest.v2+json'}
            or labels.get('org.opencontainers.image.source') != IMAGE_SOURCE
            or labels.get('org.opencontainers.image.version') != identity['version']
            or labels.get('org.opencontainers.image.revision') != identity['source_revision']):
        raise ValueError('image platform manifest or source identity does not match its lock')


def read_member(root, member):
    parts = PurePosixPath(member).parts
    if not parts or member != PurePosixPath(member).as_posix() or member.startswith('/') or '..' in parts or '\\' in member:
        raise ValueError('invalid installation file path')
    path = root
    for part in parts:
        path /= part
        if path.is_symlink(): raise ValueError('installation files must not be symlinks')
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 256 * 1024 * 1024:
        raise ValueError('invalid installation file type or size')
    return path.read_bytes()


def load_installation(root, identity):
    root = Path(root)
    install = json.loads(read_member(root, INSTALL_FILE))
    if (not isinstance(install, dict) or install.get('schema') != 'cortex.install.v1'
            or install.get('delivery_kind') != 'prebuilt'
            or any(install.get(key) != identity.get(key) for key in ('version', 'source_revision', 'schema_revision'))
            or install.get('platform') not in PLATFORMS):
        raise ValueError('prebuilt installation identity is invalid')
    files = install.get('files')
    required = {'PROJECTION_MANIFEST.json', 'packages/deploy/release.json',
                'packages/deploy/cortex-runtime', 'packages/deploy/image_manifest.py', COMPOSE_FILE, LOCK_FILE}
    if not isinstance(files, dict) or not required <= set(files) or INSTALL_FILE in files:
        raise ValueError('prebuilt installation file inventory is incomplete')
    for member, expected in files.items():
        if not isinstance(expected, str) or not HEX.fullmatch(expected) or sha(read_member(root, member)) != expected:
            raise ValueError('prebuilt installation file checksum mismatch: ' + member)
    inventory = validate_inventory(json.loads(read_member(root, LOCK_FILE)), identity)
    validate_compose(json.loads(read_member(root, COMPOSE_FILE)), inventory, install['platform'])
    return install, inventory
