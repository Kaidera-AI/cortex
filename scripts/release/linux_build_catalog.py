"""Pure native build receipt binding; no engine, database or signing effects."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))
from cortex_v2.native_prerequisite import payload_inventory
from cortex_v2.clients.native_prerequisite import physical_path, strict_json

MESSAGE = 'native build catalog missing or mismatched'


def _source_stamp(info):
    return (info.st_dev, info.st_ino, info.st_uid, info.st_mode, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _source_inputs(root: Path) -> dict:
    physical_path(root)
    paths = {root, root / 'deploy', root / 'deploy/release'}
    for directory in (root / 'src', root / 'migrations'):
        if not directory.is_dir() or directory.is_symlink():
            raise ValueError
        paths.add(directory)
        for path in directory.rglob('*'):
            if 'secrets' in path.relative_to(directory).parts:
                raise ValueError
            paths.add(path)
            if len(paths) > 4096:
                raise ValueError
    paths.add(root / 'deploy/release/10-create-roles.sh')
    for name in ('pyproject.toml', 'uv.lock', 'requirements-build.txt', '.dockerignore',
                 'Dockerfile', 'deploy/release/Dockerfile.linux-amd64',
                 'deploy/release/Containerfile.db', 'deploy/release/Containerfile.db.linux-amd64'):
        if os.path.lexists(root / name):
            paths.add(root / name)
    result = {}
    for path in paths:
        physical_path(path)
        info = path.lstat()
        if (info.st_uid != os.getuid() or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode))
                or (stat.S_ISREG(info.st_mode) and (info.st_nlink != 1 or info.st_size > 8 * 1024**2))):
            raise ValueError
        result[path] = _source_stamp(info)
    return result


def _source_snapshot(root: Path) -> tuple[dict, dict]:
    """Own-source payload plus private inode observations, never image digests."""
    try:
        before = _source_inputs(root)
        app = payload_inventory(root / 'src', root / 'migrations')
        if (not any(n.startswith('src/') for n in app['files'])
                or not any(n.startswith('migrations/') for n in app['files'])):
            raise ValueError
        name = 'deploy/release/10-create-roles.sh'
        fd = os.open(root / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            if _source_stamp(os.fstat(stream.fileno())) != before[root / name]:
                raise ValueError
            raw = stream.read(8 * 1024**2 + 1)
            if _source_stamp(os.fstat(stream.fileno())) != before[root / name]:
                raise ValueError
        if not raw or len(raw) > 8 * 1024**2:
            raise ValueError
        files = {name: hashlib.sha256(raw).hexdigest()}
        db = {'files': files, 'sha256': hashlib.sha256(
            json.dumps(files, sort_keys=True, separators=(',', ':')).encode()).hexdigest()}
        if _source_inputs(root) != before:
            raise ValueError
        roles = {role: {'files': dict(app['files']), 'sha256': app['sha256']}
                 for role in ('api', 'doc', 'embed', 'graph')}
        roles['db'] = db
        return roles, before
    except Exception:
        raise RuntimeError('native source payload missing or mismatched') from None


def source_payloads(root: Path) -> dict:
    return _source_snapshot(root)[0]


def read_json(path: Path) -> dict:
    """Read one actual bounded, physical public receipt without duplicate keys."""
    try:
        physical_path(path)
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode) or before.st_size > 1048576:
            raise ValueError
        identity = lambda value: (value.st_dev, value.st_ino, value.st_mode, value.st_size,
                                  value.st_mtime_ns, value.st_ctime_ns)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            if identity(os.fstat(stream.fileno())) != identity(before):
                raise ValueError
            raw = stream.read(1048577)
            if identity(os.fstat(stream.fileno())) != identity(before):
                raise ValueError
        physical_path(path)
        if identity(path.lstat()) != identity(before):
            raise ValueError
        return strict_json(raw, limit=1048576)
    except Exception:
        raise RuntimeError(MESSAGE) from None


def catalog_bytes(migration_receipt: dict, *, source_sha: str,
                  source_root: Path, migration_root: Path) -> bytes:
    """Bind the real fixture ledger and catalog to unchanged source payload bytes."""
    try:
        if not isinstance(source_sha, str) or re.fullmatch(r'[0-9a-f]{40}', source_sha) is None:
            raise ValueError
        receipt = strict_json(json.dumps(migration_receipt, allow_nan=False).encode(), limit=1048576)
        if (set(receipt) != {'instance', 'fixture', 'status', 'migrations', 'build_catalog'}
                or receipt['instance'] != 'cortex_v2_package_test'
                or receipt['fixture'] != 'verified' or receipt['status'] not in ('applied', 'verified')):
            raise ValueError
        payload = payload_inventory(source_root, migration_root)
        expected = {name[len('migrations/'):]: digest for name, digest in payload['files'].items()
                    if name.startswith('migrations/')}
        if (not any(name.startswith('src/') for name in payload['files'])
                or not 1 <= len(expected) <= 1024
                or any(re.fullmatch(r'[0-9]{4}_[a-z0-9_]+\.sql', name) is None for name in expected)):
            raise ValueError
        ledger = receipt['migrations']
        if not isinstance(ledger, list) or len(ledger) != len(expected):
            raise ValueError
        actual = {}
        for row in ledger:
            if (not isinstance(row, dict) or set(row) != {'migration', 'checksum'}
                    or not isinstance(row['migration'], str) or row['migration'] in actual
                    or row['migration'] not in expected or row['checksum'] != expected[row['migration']]):
                raise ValueError
            actual[row['migration']] = row['checksum']
        value = receipt['build_catalog']
        if (not isinstance(value, dict)
                or set(value) != {'schema', 'source_revision', 'api_source_payload_sha256', 'migrations', 'relations'}
                or value['schema'] != 'cortex.rls-inventory.v1'
                or value['source_revision'] != source_sha
                or value['api_source_payload_sha256'] != payload['sha256']
                or value['migrations'] != expected or actual != expected
                or not isinstance(value['relations'], list) or not 1 <= len(value['relations']) <= 1024):
            raise ValueError
        names = []
        for row in value['relations']:
            if (not isinstance(row, dict) or set(row) != {'name', 'rls', 'forced', 'app_direct_grant'}
                    or not isinstance(row['name'], str)
                    or re.fullmatch(r'cortex_(?:auth|core)\.[a-z_][a-z0-9_]{0,62}', row['name']) is None
                    or any(type(row[key]) is not bool for key in ('rls', 'forced', 'app_direct_grant'))
                    or row['rls'] and not row['forced'] or not row['rls'] and row['app_direct_grant']):
                raise ValueError
            names.append(row['name'])
        if names != sorted(set(names)):
            raise ValueError
        raw = json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode() + b'\n'
        if len(raw) > 65536 or payload_inventory(source_root, migration_root) != payload:
            raise ValueError
        return raw
    except Exception:
        raise RuntimeError(MESSAGE) from None


def rehearsal_catalog_bytes(rehearsal: dict, entries: dict, *, source_sha: str,
                            version: str, source_root: Path, migration_root: Path) -> bytes:
    """Bind initial/replay observations, smoke and owned teardown before assembly."""
    try:
        from install_candidate import ROLES, names, namespace
        from podman_policy import validate_local_version
        from cortex_v2.clients.native_prerequisite import canonical_uuid
        value = strict_json(json.dumps(rehearsal, allow_nan=False).encode(), limit=1048576)
        if (set(value) != {'scope', 'target', 'source_sha', 'version', 'podman', 'image_ids',
                'migration', 'migration_replay', 'build_catalog', 'build_catalog_sha256',
                'internal_network_loopback_publish', 'smoke', 'restart_smoke', 'status', 'epoch', 'cleanup'}
                or value['scope'] != 'native Linux amd64 CI; not installed product qualification'
                or value['target'] != 'linux-x86_64' or value['source_sha'] != source_sha
                or not isinstance(version, str) or re.fullmatch(r'0\.2\.001-test\.[0-9]{8}\.[1-9][0-9]*', version) is None
                or value['version'] != version or value['status'] != 'PASS'
                or type(value['epoch']) is not int or value['epoch'] <= 0
                or value['internal_network_loopback_publish'] != 'PASS' or set(entries) != set(ROLES)):
            raise ValueError
        ids = {role: entries[role]['config_id'] for role in ROLES}
        if (any(not isinstance(item, str) or re.fullmatch(r'sha256:[0-9a-f]{64}', item) is None for item in ids.values())
                or len(set(ids.values())) != 5 or value['image_ids'] != ids):
            raise ValueError
        validate_local_version(value['podman'])
        initial, replay = value['migration'], value['migration_replay']
        if initial['status'] != 'applied' or replay['status'] != 'verified' or len(initial['migrations']) != 15:
            raise ValueError
        kwargs = {'source_sha': source_sha, 'source_root': source_root, 'migration_root': migration_root}
        raw = catalog_bytes(initial, **kwargs)
        if (catalog_bytes(replay, **kwargs) != raw or initial['migrations'] != replay['migrations']
                or json.dumps(value['build_catalog'], sort_keys=True, separators=(',', ':'), allow_nan=False).encode() + b'\n' != raw
                or value['build_catalog_sha256'] != hashlib.sha256(raw).hexdigest()):
            raise ValueError
        for key in ('smoke', 'restart_smoke'):
            smoke = value[key]
            if (set(smoke) != {'status', 'scope', 'source_sha', 'version', 'checks', 'record_id', 'epoch'}
                    or smoke['status'] != 'PASS' or smoke['scope'] != 'TEST package smoke only'
                    or smoke['source_sha'] != source_sha or smoke['version'] != version
                    or smoke['checks'] != ['readiness', 'unauthenticated refusal', 'synthetic write/read']
                    or type(smoke['epoch']) is not int or not 0 < smoke['epoch'] <= value['epoch']):
                raise ValueError
            canonical_uuid(smoke['record_id'])
        cleanup = value['cleanup']
        if (set(cleanup) != {'status', 'installation', 'namespace', 'source_sha', 'version', 'removed', 'retained_shared_images', 'root'}
                or cleanup['status'] != 'erased' or cleanup['source_sha'] != source_sha
                or cleanup['version'] != version or cleanup['retained_shared_images'] != []):
            raise ValueError
        installation = canonical_uuid(cleanup['installation'])
        record = {'installation': installation}
        root = Path(cleanup['root'])
        if (cleanup['namespace'] != namespace(record) or not root.is_absolute() or '..' in root.parts
                or any(ord(c) < 32 for c in str(root)) or root.name != 'ci-rehearsal-' + installation
                or not isinstance(cleanup['removed'], list) or len(cleanup['removed']) != len(names(record))):
            raise ValueError
        actual = []
        for item in cleanup['removed']:
            if not isinstance(item, dict) or set(item) != {'kind', 'name'}:
                raise ValueError
            actual.append((item['kind'], item['name']))
        if len(set(actual)) != len(actual) or set(actual) != set(names(record)):
            raise ValueError
        return raw
    except Exception:
        raise RuntimeError(MESSAGE) from None
