"""Pure native build receipt binding; no engine, database or signing effects."""
from __future__ import annotations

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
