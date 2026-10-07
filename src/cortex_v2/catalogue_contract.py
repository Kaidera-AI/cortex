"""Exact product catalogue namespaces and source-bound migration identity."""
from __future__ import annotations

import ast
from pathlib import Path
import re

CATALOGUE_SCHEMAS = (
    'cortex_auth', 'cortex_core', 'cortex_coord', 'cortex_processing',
    'cortex_retrieval', 'cortex_context', 'cortex_feed', 'cortex_verification',
)
CATALOGUE_SQL_SCHEMAS = ','.join("'" + name + "'" for name in CATALOGUE_SCHEMAS)
RELATION_PATTERN = '(?:' + '|'.join(CATALOGUE_SCHEMAS) + r')\.[a-z_][a-z0-9_]{0,62}'


def relation_name(value) -> bool:
    return isinstance(value, str) and re.fullmatch(RELATION_PATTERN, value) is not None


def migration_identity(source_root: Path, migration_root: Path) -> dict[str, str]:
    """Bind exact file IDs/checksums; actual source profiles must agree.

    Small library fixtures can carry only payload files. Native product source
    carries cortex_v2/config.py: its literal full profile is checked as data,
    without importing or executing candidate source during qualification.
    """
    from .native_prerequisite import payload_inventory
    source, migrations = Path(source_root), Path(migration_root)
    payload = payload_inventory(source, migrations)
    expected = {name[len('migrations/'):]: digest for name, digest in payload['files'].items()
                if name.startswith('migrations/')}
    if (not 1 <= len(expected) <= 1024
            or any(re.fullmatch(r'[0-9]{4}_[a-z0-9_]+\.sql', n) is None for n in expected)):
        raise ValueError('migration identity refused')
    config = source / 'cortex_v2/config.py'
    if config.exists():
        names = []
        for node in ast.parse(config.read_text()).body:
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == 'FULL_V2_MIGRATIONS':
                names.append(ast.literal_eval(node.value))
            elif isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'FULL_V2_MIGRATIONS' for t in node.targets):
                names.append(ast.literal_eval(node.value))
        if (len(names) != 1 or not isinstance(names[0], tuple)
                or any(not isinstance(n, str) for n in names[0])
                or len(names[0]) != len(set(names[0])) or set(names[0]) != set(expected)):
            raise ValueError('source profile differs from migration payload')
    if payload_inventory(source, migrations) != payload:
        raise ValueError('migration payload changed')
    return expected


def validate_migration_receipt(receipt: dict, source_root: Path, migration_root: Path) -> dict[str, str]:
    expected = migration_identity(source_root, migration_root)
    rows = receipt.get('migrations') if isinstance(receipt, dict) else None
    if not isinstance(rows, list) or len(rows) != len(expected):
        raise ValueError('migration ledger identity refused')
    actual = {}
    for row in rows:
        if (not isinstance(row, dict) or set(row) != {'migration', 'checksum'}
                or not isinstance(row['migration'], str) or row['migration'] in actual
                or row['migration'] not in expected or row['checksum'] != expected[row['migration']]):
            raise ValueError('migration ledger identity refused')
        actual[row['migration']] = row['checksum']
    if actual != expected:
        raise ValueError('migration ledger identity refused')
    return expected
